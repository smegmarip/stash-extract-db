"""Unit tests for cache.db.

Covers:
  - Phase 1: schema migration creates all tables + indexes
  - Phase 1: cascade deletion via FK + manual extractor_image purge
  - Phase 2: image_features CRUD parity with image_hashes
  - Phase 3: job_feature_state lifecycle helpers
  - Phase 6: LRU touch on read + eviction routine
  - Phase 7: dual-write flag controls legacy writes (verified indirectly)

Synthetic data only — no HTTP, no real videos.
"""
from datetime import datetime, timedelta

import pytest


# Tables expected after init_db. Order doesn't matter; the test just
# checks set equality.
EXPECTED_TABLES = {
    "extractor_jobs", "extractor_results", "image_hashes",
    "match_results", "image_features", "corpus_stats",
    "image_uniqueness", "job_feature_state",
}


# --- Schema -------------------------------------------------------------

class TestSchema:
    async def test_init_creates_all_tables(self, bridge_db):
        cur = await bridge_db.db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = {r[0] for r in await cur.fetchall()}
        assert EXPECTED_TABLES.issubset(rows)

    async def test_init_creates_indexes(self, bridge_db):
        cur = await bridge_db.db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        idx = {r[0] for r in await cur.fetchall()}
        assert "idx_jobs_name_lower" in idx
        assert "idx_features_ref" in idx
        assert "idx_features_lru" in idx

    async def test_init_idempotent(self, bridge_db, tmp_path):
        """Running init_db on an already-initialized DB doesn't fail or
        duplicate tables."""
        # Re-call init by closing + reinitializing
        await bridge_db.close_db()
        await bridge_db.init_db()
        cur = await bridge_db.db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = {r[0] for r in await cur.fetchall()}
        assert EXPECTED_TABLES.issubset(rows)


# --- Cascade ------------------------------------------------------------

class TestCascade:
    async def test_cascade_clears_extractor_results_and_features(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jobX", job_name="X", schema_id="s1",
            completed_at=now, fetched_at=now,
            results=[{"page_url": "https://e/0", "data": {"id": "0"}}],
        )
        # Add an extractor_image feature row (manual; no FK to extractor_jobs)
        await bridge_db.set_image_feature(
            "extractor_image", "jobX:img0", "fp1", "phash", "phash:8",
            b"\x00" * 8, 0.5,
        )
        # And a Stash-side row (must survive)
        await bridge_db.set_image_feature(
            "stash_cover", "scene1", "fp_s", "phash", "phash:8",
            b"\x00" * 8, 0.5,
        )
        await bridge_db.upsert_feature_state("jobX", "ready", 1.0)
        await bridge_db.set_corpus_stat("jobX", "phash", "phash:8", 0.5)
        await bridge_db.set_image_uniqueness("jobX", "img0", "phash", 1.0)

        # Now cascade: completed_at advance triggers purge
        new_now = (datetime.utcnow() + timedelta(seconds=1)).isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jobX", job_name="X", schema_id="s1",
            completed_at=new_now, fetched_at=new_now,
            results=[{"page_url": "https://e/0", "data": {"id": "0"}}],
        )

        # Job-bound rows are gone (FK cascade + manual purge)
        for tbl in ("corpus_stats", "image_uniqueness", "job_feature_state"):
            cur = await bridge_db.db().execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE job_id='jobX'"
            )
            assert (await cur.fetchone())[0] == 0, f"{tbl} not purged"

        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM image_features "
            "WHERE source='extractor_image' AND ref_id LIKE 'jobX:%'"
        )
        assert (await cur.fetchone())[0] == 0, "extractor_image not purged"

        # Stash-side row survives (keyed by content fingerprint, not job)
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM image_features WHERE source='stash_cover'"
        )
        assert (await cur.fetchone())[0] == 1


# --- image_features CRUD ------------------------------------------------

class TestImageFeatures:
    async def test_set_and_get(self, bridge_db):
        await bridge_db.set_image_feature(
            "extractor_image", "j1:r1", "fp1", "phash", "phash:8",
            b"\x12\x34", 0.7,
        )
        row = await bridge_db.get_image_feature(
            "extractor_image", "j1:r1", "fp1", "phash", "phash:8",
        )
        assert row is not None
        blob, q = row
        assert bytes(blob) == b"\x12\x34"
        assert q == 0.7

    async def test_get_returns_none_on_missing(self, bridge_db):
        assert await bridge_db.get_image_feature(
            "extractor_image", "nope", "fp", "phash", "phash:8",
        ) is None

    async def test_fingerprint_mismatch_is_miss(self, bridge_db):
        await bridge_db.set_image_feature(
            "stash_cover", "scene1", "fp_old", "phash", "phash:8",
            b"\x00", 0.5,
        )
        # Same source/ref but different fingerprint → must miss
        assert await bridge_db.get_image_feature(
            "stash_cover", "scene1", "fp_new", "phash", "phash:8",
        ) is None

    async def test_replaces_on_same_pk(self, bridge_db):
        """INSERT OR REPLACE — re-write at same PK updates the row."""
        await bridge_db.set_image_feature(
            "stash_cover", "scene1", "fp1", "phash", "phash:8", b"\x00", 0.5,
        )
        await bridge_db.set_image_feature(
            "stash_cover", "scene1", "fp1", "phash", "phash:8", b"\xff", 0.9,
        )
        row = await bridge_db.get_image_feature(
            "stash_cover", "scene1", "fp1", "phash", "phash:8",
        )
        assert row[1] == 0.9


# --- job_feature_state ---------------------------------------------------

class TestFeatureState:
    async def test_upsert_and_get(self, bridge_db):
        # FK to extractor_jobs requires the parent row to exist
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "name", "s1", now, now, results=[],
        )
        await bridge_db.upsert_feature_state("j1", "featurizing", 0.5)
        s = await bridge_db.get_feature_state("j1")
        assert s["state"] == "featurizing"
        assert s["progress"] == 0.5

    async def test_mark_ready(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results("j1", "n", "s1", now, now, results=[])
        await bridge_db.upsert_feature_state("j1", "featurizing", 0.5)
        await bridge_db.mark_feature_ready("j1")
        s = await bridge_db.get_feature_state("j1")
        assert s["state"] == "ready"
        assert s["progress"] == 1.0
        assert s["finished_at"] is not None

    async def test_mark_failed(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results("j1", "n", "s1", now, now, results=[])
        await bridge_db.upsert_feature_state("j1", "featurizing", 0.3)
        await bridge_db.mark_feature_failed("j1", "boom")
        s = await bridge_db.get_feature_state("j1")
        assert s["state"] == "failed"
        assert s["error"] == "boom"

    async def test_list_jobs_needing_featurization(self, bridge_db):
        now = datetime.utcnow().isoformat()
        for jid, state in [("a", "ready"), ("b", "featurizing"), ("c", "failed")]:
            await bridge_db.upsert_job_and_results(jid, jid, "s", now, now, [])
            await bridge_db.upsert_feature_state(jid, state, 0.5 if state != "ready" else 1.0)
        # 'd' has no feature_state row at all → also needs featurization
        await bridge_db.upsert_job_and_results("d", "d", "s", now, now, [])

        out = set(await bridge_db.list_jobs_needing_featurization(
            datetime.utcnow().isoformat()
        ))
        assert out == {"b", "c", "d"}

    async def test_reset_stale_featurizing(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results("j1", "n", "s", now, now, [])
        # Stale: started 2h ago
        old = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        await bridge_db.upsert_feature_state(
            "j1", "featurizing", 0.4, started_at=old,
        )
        cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
        n = await bridge_db.reset_stale_featurizing(cutoff)
        assert n == 1
        s = await bridge_db.get_feature_state("j1")
        assert s["progress"] == 0.0  # reset

    async def test_feature_state_counts(self, bridge_db):
        now = datetime.utcnow().isoformat()
        for jid, state, prog in [
            ("a", "ready", 1.0),
            ("b", "ready", 1.0),
            ("c", "featurizing", 0.0),  # queued
            ("d", "featurizing", 0.5),  # in-progress
            ("e", "failed", 0.3),
        ]:
            await bridge_db.upsert_job_and_results(jid, jid, "s", now, now, [])
            await bridge_db.upsert_feature_state(jid, state, prog)

        counts = await bridge_db.feature_state_counts()
        assert counts["ready"] == 2
        assert counts["queued"] == 1
        assert counts["in_progress"] == 1
        assert counts["failed"] == 1


# --- LRU eviction (Phase 6) ---------------------------------------------

class TestLRU:
    async def test_get_touches_last_accessed_for_stash(self, bridge_db):
        await bridge_db.set_image_feature(
            "stash_cover", "scene1", "fp1", "phash", "phash:8", b"\x00", 0.5,
        )
        # Override last_accessed_at to a known-old value
        old = (datetime.utcnow() - timedelta(hours=10)).isoformat()
        await bridge_db.db().execute(
            "UPDATE image_features SET last_accessed_at=? WHERE ref_id='scene1'",
            (old,),
        )
        await bridge_db.db().commit()
        # Read it
        await bridge_db.get_image_feature(
            "stash_cover", "scene1", "fp1", "phash", "phash:8",
        )
        # Verify last_accessed_at was bumped
        cur = await bridge_db.db().execute(
            "SELECT last_accessed_at FROM image_features WHERE ref_id='scene1'"
        )
        new = (await cur.fetchone())[0]
        assert new > old

    async def test_get_does_not_touch_extractor_rows(self, bridge_db):
        """Extractor-side rows are job-cascade-bound; LRU touch wastes cache pressure."""
        await bridge_db.set_image_feature(
            "extractor_image", "j1:r0", "fp", "phash", "phash:8", b"\x00", 0.5,
        )
        # set_image_feature seeds last_accessed_at=NULL for non-Stash sources
        cur = await bridge_db.db().execute(
            "SELECT last_accessed_at FROM image_features WHERE source='extractor_image'"
        )
        before = (await cur.fetchone())[0]
        assert before is None

        # Read should NOT update it
        await bridge_db.get_image_feature(
            "extractor_image", "j1:r0", "fp", "phash", "phash:8",
        )
        cur = await bridge_db.db().execute(
            "SELECT last_accessed_at FROM image_features WHERE source='extractor_image'"
        )
        after = (await cur.fetchone())[0]
        assert after is None

    async def test_storage_bytes_excludes_extractor(self, bridge_db):
        await bridge_db.set_image_feature(
            "stash_cover", "s1", "fp", "phash", "phash:8", b"x" * 100, 0.5,
        )
        await bridge_db.set_image_feature(
            "extractor_image", "j:r", "fp", "phash", "phash:8", b"y" * 200, 0.5,
        )
        n = await bridge_db.stash_feature_storage_bytes()
        assert n == 100  # only the stash row

    async def test_eviction_keeps_recently_touched(self, bridge_db):
        """Insert 20 rows with timestamps in the past; touch one; evict
        to half budget; touched row survives, oldest is gone."""
        base = datetime.utcnow()
        for i in range(20):
            source = ["stash_cover", "stash_sprite", "stash_aggregate"][i % 3]
            await bridge_db.set_image_feature(
                source, f"s{i}", f"fp{i}", "phash", "phash:8", b"x" * 100, 0.5,
            )
            ts = (base - timedelta(hours=20 - i)).isoformat()
            await bridge_db.db().execute(
                "UPDATE image_features SET last_accessed_at=? WHERE source=? AND ref_id=?",
                (ts, source, f"s{i}"),
            )
        await bridge_db.db().commit()

        # Touch the oldest row (s0) → bumps it newest
        await bridge_db.get_image_feature(
            "stash_cover", "s0", "fp0", "phash", "phash:8",
        )

        # Evict to half budget
        evicted, freed = await bridge_db.evict_lru_stash_features(target_bytes=1000)
        assert evicted > 0
        assert freed > 0
        assert await bridge_db.stash_feature_storage_bytes() <= 1000

        # Touched row survives
        assert await bridge_db.get_image_feature(
            "stash_cover", "s0", "fp0", "phash", "phash:8",
        ) is not None

        # Oldest non-touched row should be gone
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM image_features WHERE ref_id='s1'"
        )
        assert (await cur.fetchone())[0] == 0

    async def test_eviction_no_op_under_budget(self, bridge_db):
        await bridge_db.set_image_feature(
            "stash_cover", "s", "fp", "phash", "phash:8", b"x" * 100, 0.5,
        )
        assert await bridge_db.evict_lru_stash_features(target_bytes=10_000) == (0, 0)


# --- Per-channel uniqueness settings (architectural Run 7) -------------

class TestPerChannelUniqueness:
    """Architectural: per-channel threshold/alpha overrides fall back to
    the global value when None.
    """

    async def test_phash_inherits_global_when_none(self, bridge_db, clean_settings):
        clean_settings.bridge_featurize_uniqueness_threshold = 0.85
        clean_settings.bridge_featurize_uniqueness_threshold_phash = None
        assert clean_settings.channel_uniqueness_threshold("phash") == 0.85

    async def test_tone_override(self, bridge_db, clean_settings):
        # Per-channel override takes precedence; default inherits global.
        clean_settings.bridge_featurize_uniqueness_threshold = 0.85
        clean_settings.bridge_featurize_uniqueness_threshold_tone = 0.95
        assert clean_settings.channel_uniqueness_threshold("tone") == 0.95
        clean_settings.bridge_featurize_uniqueness_threshold_tone = None
        assert clean_settings.channel_uniqueness_threshold("tone") == 0.85

    async def test_per_channel_override_takes_precedence(self, bridge_db, clean_settings):
        clean_settings.bridge_featurize_uniqueness_threshold = 0.85
        clean_settings.bridge_featurize_uniqueness_threshold_phash = 0.92
        assert clean_settings.channel_uniqueness_threshold("phash") == 0.92

    async def test_alpha_resolution_mirrors_threshold(self, bridge_db, clean_settings):
        clean_settings.bridge_featurize_uniqueness_alpha = 1.0
        clean_settings.bridge_featurize_uniqueness_alpha_phash = None
        clean_settings.bridge_featurize_uniqueness_alpha_tone = 2.0
        assert clean_settings.channel_uniqueness_alpha("phash") == 1.0
        assert clean_settings.channel_uniqueness_alpha("tone") == 2.0

    async def test_unknown_channel_falls_back(self, bridge_db, clean_settings):
        clean_settings.bridge_featurize_uniqueness_threshold = 0.85
        # color_hist isn't in the per-channel dict; falls back to global
        assert clean_settings.channel_uniqueness_threshold("color_hist") == 0.85


class TestNegativeFeatureCache:
    """Sentinel rows in image_features for "tried, no usable result" —
    suppresses repeat fetch+compute attempts when an asset is unfetchable
    (404) or its image is low-variance / undecodeable."""

    async def test_get_returns_none_for_sentinel(self, bridge_db):
        await bridge_db.set_feature_attempt_failed(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        )
        # get_image_feature returns None on sentinel (same as miss for callers
        # that just want the feature blob)
        assert await bridge_db.get_image_feature(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        ) is None

    async def test_is_feature_attempt_cached_distinguishes_miss(self, bridge_db):
        await bridge_db.set_feature_attempt_failed(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        )
        # Sentinel exists for this exact key
        assert await bridge_db.is_feature_attempt_cached(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        ) is True
        # No sentinel for a different ref → caller will fetch
        assert await bridge_db.is_feature_attempt_cached(
            "extractor_image", "j1:other_ref", "fp", "phash", "phash:16",
        ) is False

    async def test_sentinel_does_not_collide_with_success(self, bridge_db):
        # A successful row exists for one channel
        await bridge_db.set_image_feature(
            "extractor_image", "j1:ref", "fp", "color_hist", "color_hist:hsv:4x4x4",
            b"\x01\x02", 0.7,
        )
        # Sentinel for a different channel on the same ref
        await bridge_db.set_feature_attempt_failed(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        )
        # Success channel still readable normally
        success = await bridge_db.get_image_feature(
            "extractor_image", "j1:ref", "fp", "color_hist", "color_hist:hsv:4x4x4",
        )
        assert success == (b"\x01\x02", 0.7)
        # Failed channel returns None
        failed = await bridge_db.get_image_feature(
            "extractor_image", "j1:ref", "fp", "phash", "phash:16",
        )
        assert failed is None
