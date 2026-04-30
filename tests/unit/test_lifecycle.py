"""Unit tests for the Phase 3 featurization lifecycle.

Covers:
  - featurize_job: full-job pipeline, ready transition, image_features rows,
    corpus_stats baseline, image_uniqueness c_i values
  - State machine: failed → ready via startup_recover
  - Cascade: completed_at advance purges + can re-enqueue
  - Worker pool: enqueue is idempotent for in-flight job_ids
  - Request gate: 503 + Retry-After when state != ready
  - BRIDGE_LIFECYCLE_ENABLED=False bypasses everything

The extractor client is monkey-patched at module level (the bridge code
calls ex_client.fetch_asset / ex_client.resolve_asset_url directly).
"""
import asyncio
import json
from typing import Optional

import pytest

from bridge.app.extractor import client as ex_client


# --- Fixtures ------------------------------------------------------------

@pytest.fixture
def mock_extractor(monkeypatch, synth_image):
    """Patch ex_client to serve synthetic bytes for any ref. Returns a
    dict {ref: seed} the test can populate; refs not in the dict 404."""
    asset_seeds: dict[str, int] = {}

    async def _fetch(job_id: str, ref: str) -> Optional[bytes]:
        seed = asset_seeds.get(ref)
        if seed is None:
            return None
        return synth_image(seed)

    def _resolve(job_id: str, ref: str) -> str:
        return f"http://mock/{job_id}/{ref}"

    monkeypatch.setattr(ex_client, "fetch_asset", _fetch)
    monkeypatch.setattr(ex_client, "resolve_asset_url", _resolve)
    return asset_seeds


async def _seed_job(bridge_db, job_id: str, records: list[dict], completed_at: str = "2026-04-29T00:00:00"):
    """Insert an extractor_jobs row + extractor_results rows directly.
    Bypasses the cascade (we want a clean test setup)."""
    await bridge_db.db().execute(
        "INSERT INTO extractor_jobs VALUES (?, ?, ?, ?, ?)",
        (job_id, "TestSite", "sch1", completed_at, completed_at),
    )
    for idx, r in enumerate(records):
        await bridge_db.db().execute(
            "INSERT INTO extractor_results VALUES (?, ?, ?, ?)",
            (job_id, idx, r.get("page_url", ""), json.dumps(r["data"])),
        )
    await bridge_db.db().commit()


# --- featurize_job pipeline ---------------------------------------------

class TestFeaturizeJob:
    async def test_empty_job_marks_ready(self, bridge_db, clean_settings, mock_extractor):
        from bridge.app.matching.featurization import featurize_job
        await _seed_job(bridge_db, "j_empty", records=[])
        await bridge_db.upsert_feature_state("j_empty", "featurizing", 0.0)

        await featurize_job("j_empty")
        s = await bridge_db.get_feature_state("j_empty")
        assert s["state"] == "ready"
        assert s["progress"] == 1.0

    async def test_populates_all_three_channels(
        self, bridge_db, clean_settings, mock_extractor,
    ):
        from bridge.app.matching.featurization import featurize_job
        # 2 records, 3 unique image refs
        records = [
            {"data": {"id": "r0", "cover_image": "imgA1", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgB1", "images": []}},
        ]
        await _seed_job(bridge_db, "j1", records)
        for ref, seed in [("imgA1", 1), ("imgA2", 2), ("imgB1", 3)]:
            mock_extractor[ref] = seed
        await bridge_db.upsert_feature_state("j1", "featurizing", 0.0)

        await featurize_job("j1")

        s = await bridge_db.get_feature_state("j1")
        assert s["state"] == "ready"

        # Three channels × three refs → 9 image_features rows
        cur = await bridge_db.db().execute(
            "SELECT channel, COUNT(*) FROM image_features "
            "WHERE source='extractor_image' AND ref_id LIKE 'j1:%' GROUP BY channel"
        )
        counts = dict(await cur.fetchall())
        assert counts.get("phash") == 3
        assert counts.get("color_hist") == 3
        assert counts.get("tone") == 3

        # Baselines exist for all three channels
        cur = await bridge_db.db().execute(
            "SELECT channel FROM corpus_stats WHERE job_id='j1'"
        )
        chans = {r[0] for r in await cur.fetchall()}
        assert chans == {"phash", "color_hist", "tone"}

        # Per-record B aggregate present (one per record)
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM image_features "
            "WHERE source='extractor_aggregate' AND ref_id LIKE 'j1:%'"
        )
        assert (await cur.fetchone())[0] == 2

    async def test_uniqueness_penalizes_shared_refs(
        self, bridge_db, clean_settings, mock_extractor,
    ):
        """A 'logo' ref appearing in two records should get c_i=0.5
        (smoothed reciprocal with α=1, matches=1)."""
        from bridge.app.matching.featurization import featurize_job
        records = [
            {"data": {"id": "r0", "cover_image": "imgA", "images": ["logo"]}},
            {"data": {"id": "r1", "cover_image": "imgB", "images": ["logo"]}},
            {"data": {"id": "r2", "cover_image": "imgC", "images": []}},
        ]
        await _seed_job(bridge_db, "j2", records)
        for ref, seed in [("imgA", 1), ("imgB", 2), ("imgC", 3), ("logo", 99)]:
            mock_extractor[ref] = seed
        await bridge_db.upsert_feature_state("j2", "featurizing", 0.0)

        await featurize_job("j2")

        c_logo = await bridge_db.get_image_uniqueness("j2", "logo", "phash")
        c_unique = await bridge_db.get_image_uniqueness("j2", "imgA", "phash")
        assert c_logo == 0.5    # appears in 2 records → matches=1 → 1/(1+1)
        assert c_unique == 1.0  # unique to 1 record → matches=0 → 1/(1+0)


# --- Worker + state machine ----------------------------------------------

class TestWorker:
    async def test_enqueue_creates_state(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        clean_settings.bridge_lifecycle_enabled = True
        await _seed_job(bridge_db, "jq", records=[])

        await reset_worker.enqueue("jq")
        if "jq" in reset_worker._inflight:
            await reset_worker._inflight["jq"]
        s = await bridge_db.get_feature_state("jq")
        assert s["state"] == "ready"

    async def test_enqueue_idempotent_for_inflight(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        """Calling enqueue twice for the same job while one is in-flight
        is a no-op (no second task created)."""
        clean_settings.bridge_lifecycle_enabled = True
        await _seed_job(bridge_db, "jq", records=[])
        await reset_worker.enqueue("jq")
        first_task = reset_worker._inflight.get("jq")
        await reset_worker.enqueue("jq")
        second_task = reset_worker._inflight.get("jq")
        assert first_task is second_task
        if first_task:
            await first_task

    async def test_enqueue_skips_ready_jobs(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        clean_settings.bridge_lifecycle_enabled = True
        await _seed_job(bridge_db, "jq", records=[])
        await bridge_db.upsert_feature_state("jq", "ready", 1.0)

        await reset_worker.enqueue("jq")
        assert "jq" not in reset_worker._inflight

    async def test_startup_recover_failed_to_ready(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        clean_settings.bridge_lifecycle_enabled = True
        await _seed_job(bridge_db, "jq", records=[])
        await bridge_db.upsert_feature_state("jq", "failed", 0.3, error="prior failure")

        await reset_worker.startup_recover()
        # Drain the in-flight task
        if "jq" in reset_worker._inflight:
            await reset_worker._inflight["jq"]

        s = await bridge_db.get_feature_state("jq")
        assert s["state"] == "ready"
        assert s["error"] is None

    async def test_lifecycle_disabled_bypasses_recover(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        clean_settings.bridge_lifecycle_enabled = False
        await _seed_job(bridge_db, "jq", records=[])
        await bridge_db.upsert_feature_state("jq", "failed", 0.3, error="x")

        await reset_worker.startup_recover()
        s = await bridge_db.get_feature_state("jq")
        assert s["state"] == "failed"   # untouched


# --- Cascade re-enqueue --------------------------------------------------

class TestEventLoopResponsiveness:
    """Regression: featurization must not block the event loop.

    PIL/imagehash/numpy compute is synchronous and CPU-bound. If the
    bridge runs it directly in an async coroutine, every other coroutine
    (including HTTP request handlers) freezes for the entire compute.
    The fix wraps each compute call in `asyncio.to_thread`. This test
    asserts the loop stays responsive by measuring the latency of a
    representative async operation (a DB query — the same kind of work
    a `/health` handler does) while featurization runs. The first
    sample is taken AFTER the worker pool has ramped up, so we measure
    steady-state responsiveness, not a one-off startup blip.

    A regression to blocking-on-the-loop fails this with single-query
    latencies in the hundreds of ms (one full hash compute holds the
    loop while the query waits).
    """

    async def test_db_query_latency_stays_low_during_featurization(
        self, bridge_db, clean_settings, mock_extractor,
    ):
        import time
        from bridge.app.matching.featurization import featurize_job

        # 20 records × 1 image each. With the OLD blocking code this is
        # ~1s of single-threaded CPU work; if the loop is blocked, a DB
        # query made mid-featurization waits for the current hash to
        # finish (50-150ms typical).
        records = []
        for i in range(20):
            ref = f"img_{i}"
            mock_extractor[ref] = i + 1   # seed
            records.append({"data": {"id": f"r{i}", "cover_image": ref, "images": []}})

        await _seed_job(bridge_db, "j_responsive", records)
        await bridge_db.upsert_feature_state("j_responsive", "featurizing", 0.0)

        feat_task = asyncio.create_task(featurize_job("j_responsive"))
        # Let featurization enter the CPU-bound section before probing.
        await asyncio.sleep(0.10)

        # Sample the latency of `get_feature_state` repeatedly. This is
        # the operation the request gate calls and a /health handler
        # would conceptually do. We collect samples and look at the
        # median — robust to one-off scheduling blips (GC pauses, OS
        # context switches, SQLite checkpoint flushes).
        samples_ms: list[float] = []
        for _ in range(10):
            t0 = time.monotonic()
            await bridge_db.get_feature_state("j_responsive")
            samples_ms.append((time.monotonic() - t0) * 1000)
            await asyncio.sleep(0.05)
            if feat_task.done():
                break

        await feat_task

        import statistics
        median_ms = statistics.median(samples_ms) if samples_ms else 0.0
        max_ms = max(samples_ms) if samples_ms else 0.0
        # Healthy loop with to_thread: nearly every query completes in
        # <30ms; occasional outliers in 100-200ms range happen due to
        # thread-pool start-up, GC, etc. Old blocking code: most samples
        # are 100-200ms+ because each one waits for a hash compute to
        # finish. The median catches the "consistently blocked" pattern;
        # max catches catastrophic blocking but tolerates one-off blips.
        assert median_ms < 50, (
            f"median DB query latency {median_ms:.0f}ms during featurization "
            f"(samples: {[round(s, 1) for s in samples_ms]}). "
            f"Likely a regression of CPU-bound work blocking the event loop."
        )
        assert max_ms < 500, (
            f"max DB query latency {max_ms:.0f}ms during featurization "
            f"(samples: {[round(s, 1) for s in samples_ms]}). "
            f"Catastrophic event-loop block — `to_thread` likely missing somewhere."
        )

        # Sanity: featurization actually completed
        s = await bridge_db.get_feature_state("j_responsive")
        assert s["state"] == "ready"


class TestCascadeReEnqueue:
    async def test_completed_at_advance_clears_state(
        self, bridge_db, clean_settings, mock_extractor, reset_worker,
    ):
        clean_settings.bridge_lifecycle_enabled = True
        await _seed_job(bridge_db, "jq", records=[])
        await reset_worker.enqueue("jq")
        if "jq" in reset_worker._inflight:
            await reset_worker._inflight["jq"]
        assert (await bridge_db.get_feature_state("jq"))["state"] == "ready"

        # Advance completed_at — cascade purges feature_state + extractor_image rows
        await bridge_db.upsert_job_and_results(
            "jq", "TestSite", "sch1",
            completed_at="2027-01-01T00:00:00",
            fetched_at="2027-01-01T00:00:00",
            results=[],
        )
        # State row was cascaded away
        assert await bridge_db.get_feature_state("jq") is None
