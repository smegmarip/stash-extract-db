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
        assert "idx_extractor_results_uuid" in idx

    async def test_unwrapped_dml_does_not_block_explicit_begin(self, bridge_db):
        """Regression: in autocommit mode (isolation_level=None), a DML
        without an explicit commit() does not leave an implicit transaction
        open. A subsequent BEGIN must succeed.

        Pre-fix: with the default isolation_level="", the INSERT below
        opened an implicit transaction, and `BEGIN` raised
        `sqlite3.OperationalError: cannot start a transaction within a
        transaction`. That dropped the third job during startup_discover
        when one task's mid-DML overlapped another task's
        upsert_job_and_results.
        """
        conn = bridge_db.db()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "INSERT INTO extractor_jobs(job_id, job_name, schema_id, completed_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("j_autocommit", "n", "s", now, now),
        )
        # Deliberately no explicit commit() here — autocommit handles it.
        # If implicit-txn behavior had survived, the next BEGIN would error.
        await conn.execute("BEGIN")
        await conn.execute("ROLLBACK")


# --- Record UUID --------------------------------------------------------

class TestRecordUuid:
    def test_compute_is_deterministic(self, bridge_db):
        a = bridge_db.compute_record_uuid("jobA", "https://x.test/p", 0, {"title": "t"})
        b = bridge_db.compute_record_uuid("jobA", "https://x.test/p", 0, {"title": "t"})
        assert a == b

    def test_compute_changes_on_data_change(self, bridge_db):
        a = bridge_db.compute_record_uuid("jobA", "u", 0, {"title": "t"})
        b = bridge_db.compute_record_uuid("jobA", "u", 0, {"title": "T"})
        assert a != b

    def test_compute_changes_on_job_change(self, bridge_db):
        """Two jobs with identical record content must not collide on the
        global UNIQUE index."""
        a = bridge_db.compute_record_uuid("jobA", "u", 0, {"title": "t"})
        b = bridge_db.compute_record_uuid("jobB", "u", 0, {"title": "t"})
        assert a != b

    def test_compute_independent_of_dict_order(self, bridge_db):
        """Canonical JSON sort means insertion order doesn't change the uuid."""
        a = bridge_db.compute_record_uuid("j", "u", 0, {"a": 1, "b": 2})
        b = bridge_db.compute_record_uuid("j", "u", 0, {"b": 2, "a": 1})
        assert a == b

    def test_compute_returns_16_hex_chars(self, bridge_db):
        u = bridge_db.compute_record_uuid("j", "u", 0, {"x": 1})
        assert len(u) == 16
        assert all(c in "0123456789abcdef" for c in u)

    async def test_upsert_populates_uuid(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jU", job_name="U", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[
                {"page_url": "https://a", "data": {"title": "a", "date": "2024-01-01"}},
                {"page_url": "https://b", "data": {"title": "b", "date": "2024-01-02"}},
            ],
        )
        async with bridge_db.db().execute(
            "SELECT result_index, record_uuid FROM extractor_results "
            "WHERE job_id='jU' ORDER BY result_index ASC"
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 2
        assert all(r[1] != "" for r in rows)
        # Distinct uuids per record
        assert rows[0][1] != rows[1][1]

    async def test_list_results_returns_uuid(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jL", job_name="L", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[{"page_url": "https://x", "data": {"title": "x", "date": "2024-01-01"}}],
        )
        results = await bridge_db.list_results("jL")
        assert len(results) == 1
        assert results[0]["record_uuid"] == bridge_db.compute_record_uuid(
            "jL", "https://x", 0, {"title": "x", "date": "2024-01-01"}
        )

    async def test_migration_backfills_legacy_rows(self, bridge_db):
        """Rows written without a uuid (simulating pre-migration data) get
        populated by _migrate_record_uuid on init_db."""
        now = datetime.utcnow().isoformat()
        await bridge_db.db().execute(
            "INSERT INTO extractor_jobs VALUES (?, ?, ?, ?, ?)",
            ("jOld", "Old", "s1", now, now),
        )
        # Insert with empty record_uuid (the column default)
        await bridge_db.db().execute(
            "INSERT INTO extractor_results(job_id, result_index, page_url, data_json) "
            "VALUES (?, ?, ?, ?)",
            ("jOld", 0, "https://o", '{"title":"o"}'),
        )
        await bridge_db.db().commit()
        # Re-run the migration explicitly (init_db's tail step)
        await bridge_db._migrate_record_uuid(bridge_db.db())
        async with bridge_db.db().execute(
            "SELECT record_uuid FROM extractor_results WHERE job_id='jOld'"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == bridge_db.compute_record_uuid("jOld", "https://o", 0, {"title": "o"})

    async def test_get_by_uuid_resolves_picked_record(self, bridge_db):
        """The sceneByQueryFragment round-trip lookup: stamp uuids on
        every result, look one up, get exactly that record (not the
        first hit). Two records sharing a page_url must still resolve
        to themselves — that's the whole point versus a URL-based
        round-trip."""
        now = datetime.utcnow().isoformat()
        shared_url = "https://shared.test/page"
        await bridge_db.upsert_job_and_results(
            job_id="jQ", job_name="Q", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[
                {"page_url": shared_url, "data": {"title": "first", "id": "a"}},
                {"page_url": shared_url, "data": {"title": "second", "id": "b"}},
            ],
        )
        results = await bridge_db.list_results("jQ")
        first, second = results[0], results[1]
        assert first["record_uuid"] != second["record_uuid"]

        hit = await bridge_db.get_result_by_uuid(second["record_uuid"])
        assert hit is not None
        assert hit["job_id"] == "jQ"
        assert hit["result_index"] == 1
        assert hit["data"]["title"] == "second"

        hit = await bridge_db.get_result_by_uuid(first["record_uuid"])
        assert hit is not None
        assert hit["result_index"] == 0
        assert hit["data"]["title"] == "first"

    async def test_get_by_uuid_unknown_returns_none(self, bridge_db):
        assert await bridge_db.get_result_by_uuid("0" * 16) is None
        assert await bridge_db.get_result_by_uuid("") is None

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


# --- Soft-404 filtering (CLAUDE.md §18) ---------------------------------

class TestSubstantivePredicate:
    """The predicate decides whether a record is real-enough to cache.

    Soft-404s reliably yield only URL + page-chrome title. The ≥2 fields rule
    rejects those without burning legitimately-sparse records (title+date,
    id+performers, etc.).
    """

    def test_empty_data_fails(self, bridge_db):
        assert not bridge_db._is_substantive_record({})

    def test_none_data_fails(self, bridge_db):
        assert not bridge_db._is_substantive_record(None)

    def test_title_only_fails(self, bridge_db):
        # The canonical soft-404 chrome: <title>Wayback Machine</title>
        assert not bridge_db._is_substantive_record({"title": "Wayback Machine"})

    def test_details_only_fails(self, bridge_db):
        assert not bridge_db._is_substantive_record({"details": "Page not found."})

    def test_whitespace_title_fails(self, bridge_db):
        # Strip-then-truthy guards against regex captures of empty <title> tags.
        assert not bridge_db._is_substantive_record({"title": "   \n  "})

    def test_title_plus_date_passes(self, bridge_db):
        # Legitimately-sparse real record.
        assert bridge_db._is_substantive_record(
            {"title": "Real Scene", "date": "2024-01-15"}
        )

    def test_id_plus_performers_passes(self, bridge_db):
        assert bridge_db._is_substantive_record(
            {"id": "abc123", "performers": ["Real Name"]}
        )

    def test_cover_image_plus_title_passes(self, bridge_db):
        assert bridge_db._is_substantive_record(
            {"title": "Scene", "cover_image": "https://x/c.jpg"}
        )

    def test_empty_list_does_not_count(self, bridge_db):
        # `performers: []` is "no performers", not a substantive signal.
        assert not bridge_db._is_substantive_record(
            {"title": "Scene", "performers": [], "images": []}
        )

    def test_list_of_empty_strings_does_not_count(self, bridge_db):
        # Defensive: `["", ""]` is effectively empty.
        assert not bridge_db._is_substantive_record(
            {"title": "Scene", "performers": ["", ""]}
        )

    def test_url_only_record_fails(self, bridge_db):
        # The page_url is on the wrapper, not in `data`; `data` for a true
        # URL-only soft-404 is `{}`.
        assert not bridge_db._is_substantive_record({})


class TestSubstantiveFilterAtUpsert:
    """Drops soft-404 rows before they ever enter `extractor_results`."""

    async def test_filter_drops_non_substantive(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jF", job_name="F", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[
                # Substantive — kept
                {"page_url": "https://a", "data": {"title": "Real", "date": "2024-01-01"}},
                # Soft-404 — dropped
                {"page_url": "https://b", "data": {"title": "Wayback Machine"}},
                # Substantive — kept
                {"page_url": "https://c", "data": {"id": "x", "performers": ["P"]}},
                # URL-only — dropped
                {"page_url": "https://d", "data": {}},
            ],
        )
        results = await bridge_db.list_results("jF")
        assert len(results) == 2
        # Survivors get gap-free result_index starting at 0.
        assert [r["result_index"] for r in results] == [0, 1]
        urls = {r["page_url"] for r in results}
        assert urls == {"https://a", "https://c"}

    async def test_filter_idempotent_uuid(self, bridge_db):
        """record_uuid uses the post-filter result_index. Two upserts of the
        same content produce the same uuids."""
        now = datetime.utcnow().isoformat()
        records = [
            {"page_url": "https://a", "data": {"title": "Real", "date": "2024-01-01"}},
            {"page_url": "https://b", "data": {"title": "Wayback Machine"}},
            {"page_url": "https://c", "data": {"id": "x", "performers": ["P"]}},
        ]
        await bridge_db.upsert_job_and_results(
            job_id="jI", job_name="I", schema_id="s",
            completed_at=now, fetched_at=now, results=records,
        )
        uuids_first = [r["record_uuid"] for r in await bridge_db.list_results("jI")]
        await bridge_db.upsert_job_and_results(
            job_id="jI", job_name="I", schema_id="s",
            completed_at=now, fetched_at=now, results=records,
        )
        uuids_second = [r["record_uuid"] for r in await bridge_db.list_results("jI")]
        assert uuids_first == uuids_second

    async def test_all_records_dropped_leaves_job_empty(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jE", job_name="E", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[
                {"page_url": "https://a", "data": {}},
                {"page_url": "https://b", "data": {"title": "Wayback Machine"}},
            ],
        )
        # Job row exists, results empty.
        cached = await bridge_db.get_cached_job("jE")
        assert cached is not None
        assert await bridge_db.list_results("jE") == []


class TestPurgeSoft404Records:
    """One-shot startup cleanup of legacy soft-404 rows."""

    async def _seed_raw(self, bridge_db, job_id: str, records: list[dict]):
        """Insert rows directly, bypassing the upsert filter — simulates
        legacy data cached before §18 existed."""
        import json as _json
        now = datetime.utcnow().isoformat()
        await bridge_db.db().execute(
            "INSERT INTO extractor_jobs(job_id, job_name, schema_id, completed_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, job_id, "s", now, now),
        )
        for idx, r in enumerate(records):
            data = r["data"]
            uuid = bridge_db.compute_record_uuid(job_id, r["page_url"], idx, data)
            await bridge_db.db().execute(
                "INSERT INTO extractor_results(job_id, result_index, page_url, data_json, record_uuid) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, idx, r["page_url"], _json.dumps(data), uuid),
            )
        await bridge_db.db().commit()

    async def test_purge_drops_offending_rows(self, bridge_db):
        await self._seed_raw(bridge_db, "jP", [
            {"page_url": "https://a", "data": {"title": "Real", "date": "2024-01-01"}},
            {"page_url": "https://b", "data": {"title": "Wayback Machine"}},  # soft-404
            {"page_url": "https://c", "data": {"id": "x", "performers": ["P"]}},
        ])
        dropped = await bridge_db.purge_soft_404_records()
        assert dropped == 1
        results = await bridge_db.list_results("jP")
        assert len(results) == 2
        # Survivors are re-indexed gap-free.
        assert [r["result_index"] for r in results] == [0, 1]

    async def test_purge_is_idempotent(self, bridge_db):
        await self._seed_raw(bridge_db, "jI2", [
            {"page_url": "https://a", "data": {"title": "Real", "date": "2024-01-01"}},
            {"page_url": "https://b", "data": {}},
        ])
        first = await bridge_db.purge_soft_404_records()
        assert first == 1
        second = await bridge_db.purge_soft_404_records()
        assert second == 0  # no offenders left

    async def test_purge_noop_on_clean_cache(self, bridge_db):
        # Going through the upsert filter means the cache is clean by
        # construction; purge should report zero.
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jC", job_name="C", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[{"page_url": "https://a", "data": {"title": "T", "date": "2024-01-01"}}],
        )
        assert await bridge_db.purge_soft_404_records() == 0

    async def test_purge_clears_feature_state_for_affected_job(self, bridge_db):
        """The cascade in upsert_job_and_results fires when the purge rebuilds
        the job, so job_feature_state for affected jobs is cleared. The
        lifecycle worker's startup_recover will re-discover and re-featurize."""
        await self._seed_raw(bridge_db, "jR", [
            {"page_url": "https://a", "data": {"title": "Real", "date": "2024-01-01"}},
            {"page_url": "https://b", "data": {}},
        ])
        await bridge_db.upsert_feature_state("jR", "ready", 1.0)
        await bridge_db.set_corpus_stat("jR", "phash", "phash:8", 0.5)

        await bridge_db.purge_soft_404_records()

        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM job_feature_state WHERE job_id='jR'"
        )
        assert (await cur.fetchone())[0] == 0
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM corpus_stats WHERE job_id='jR'"
        )
        assert (await cur.fetchone())[0] == 0

    async def test_purge_leaves_unaffected_jobs_untouched(self, bridge_db):
        """A job whose records are all substantive must not be rebuilt — its
        feature_state row should survive the purge."""
        # Job A: clean
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="jA_clean", job_name="A", schema_id="s",
            completed_at=now, fetched_at=now,
            results=[{"page_url": "https://a", "data": {"title": "T", "date": "2024-01-01"}}],
        )
        await bridge_db.upsert_feature_state("jA_clean", "ready", 1.0)

        # Job B: dirty
        await self._seed_raw(bridge_db, "jB_dirty", [
            {"page_url": "https://b", "data": {"title": "Real", "date": "2024-01-01"}},
            {"page_url": "https://b2", "data": {}},
        ])

        await bridge_db.purge_soft_404_records()

        # A's feature_state survives
        cur = await bridge_db.db().execute(
            "SELECT state FROM job_feature_state WHERE job_id='jA_clean'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "ready"

        # B's feature_state is gone (cleared by the rebuild cascade)
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM job_feature_state WHERE job_id='jB_dirty'"
        )
        assert (await cur.fetchone())[0] == 0
