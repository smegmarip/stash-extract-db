"""Unit tests for performer_index — display-only aggregation index for the
viewer service.

Covers (per CLAUDE.md §17.2):
  - Schema is created by init_db
  - upsert_job_and_results populates rows from data.performers
  - Cascade clears rows when a job is replaced (FK chain through
    extractor_results -> extractor_jobs)
  - Within-record dedup by lowercased name (case-insensitive)
  - Null/empty/non-string entries are skipped
  - Backfill is idempotent and only runs on un-indexed jobs
"""
from datetime import datetime, timedelta


# --- Schema -------------------------------------------------------------

class TestSchema:
    async def test_init_creates_performer_index_table(self, bridge_db):
        cur = await bridge_db.db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='performer_index'"
        )
        assert (await cur.fetchone()) is not None

    async def test_init_creates_performer_index_indexes(self, bridge_db):
        cur = await bridge_db.db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_performer_index%'"
        )
        names = {r[0] for r in await cur.fetchall()}
        assert "idx_performer_index_name" in names
        assert "idx_performer_index_job" in names


# --- Populate path ------------------------------------------------------

class TestUpsertPopulates:
    async def test_inserts_one_row_per_performer(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            job_id="j1", job_name="Studio A", schema_id="s1",
            completed_at=now, fetched_at=now,
            results=[
                {"page_url": "u1", "data": {"id": "r0", "performers": ["Alice", "Bob"]}},
                {"page_url": "u2", "data": {"id": "r1", "performers": ["Carol"]}},
            ],
        )
        cur = await bridge_db.db().execute(
            "SELECT job_id, result_index, name_lower, name_original "
            "FROM performer_index ORDER BY result_index, name_lower"
        )
        rows = await cur.fetchall()
        assert rows == [
            ("j1", 0, "alice", "Alice"),
            ("j1", 0, "bob", "Bob"),
            ("j1", 1, "carol", "Carol"),
        ]

    async def test_no_performers_yields_no_rows(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u", "data": {"id": "r0"}}],
        )
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 0

    async def test_dedup_within_record_case_insensitive(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[
                {"page_url": "u",
                 "data": {"id": "r0", "performers": ["Alice", "alice", "ALICE", "Bob"]}}
            ],
        )
        cur = await bridge_db.db().execute(
            "SELECT name_lower, name_original FROM performer_index "
            "WHERE job_id='j1' AND result_index=0 ORDER BY name_lower"
        )
        rows = await cur.fetchall()
        # First-occurrence wins for display casing
        assert rows == [("alice", "Alice"), ("bob", "Bob")]

    async def test_skips_null_empty_and_non_string(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[
                {"page_url": "u",
                 "data": {"id": "r0",
                          "performers": ["Alice", None, "", "  ", 42, {"name": "x"}, "Bob"]}}
            ],
        )
        cur = await bridge_db.db().execute(
            "SELECT name_lower FROM performer_index WHERE job_id='j1' ORDER BY name_lower"
        )
        rows = [r[0] for r in await cur.fetchall()]
        assert rows == ["alice", "bob"]

    async def test_strips_whitespace(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[
                {"page_url": "u",
                 "data": {"id": "r0", "performers": ["  Alice  ", "Bob\t"]}},
            ],
        )
        cur = await bridge_db.db().execute(
            "SELECT name_original FROM performer_index WHERE job_id='j1' ORDER BY name_lower"
        )
        rows = [r[0] for r in await cur.fetchall()]
        assert rows == ["Alice", "Bob"]


# --- Cascade ------------------------------------------------------------

class TestCascade:
    async def test_cascade_clears_on_completed_at_advance(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Alice", "Bob"]}}],
        )
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 2

        # Advance — old rows must be gone, new rows reflect the new payload
        new_now = (datetime.utcnow() + timedelta(seconds=1)).isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", new_now, new_now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Carol"]}}],
        )
        cur = await bridge_db.db().execute(
            "SELECT name_lower FROM performer_index WHERE job_id='j1'"
        )
        rows = [r[0] for r in await cur.fetchall()]
        assert rows == ["carol"]

    async def test_cascade_clears_when_job_deleted(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Alice"]}}],
        )
        # Direct DELETE on the parent — cascade should clear performer_index
        await bridge_db.db().execute("DELETE FROM extractor_jobs WHERE job_id='j1'")
        await bridge_db.db().commit()
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 0

    async def test_cascade_isolation_across_jobs(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Alice"]}}],
        )
        await bridge_db.upsert_job_and_results(
            "j2", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Bob"]}}],
        )
        # Cascade j1 only
        new_now = (datetime.utcnow() + timedelta(seconds=1)).isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", new_now, new_now, results=[],
        )
        cur = await bridge_db.db().execute(
            "SELECT job_id, name_lower FROM performer_index ORDER BY job_id, name_lower"
        )
        rows = await cur.fetchall()
        assert rows == [("j2", "bob")]


# --- Backfill -----------------------------------------------------------

class TestBackfill:
    async def test_backfill_populates_existing_results(self, bridge_db):
        """Simulate an existing deployment: extractor_results rows present
        but performer_index empty (e.g., because the table was just added)."""
        now = datetime.utcnow().isoformat()
        # Seed via upsert (which now also populates performer_index), then
        # wipe performer_index to simulate the pre-migration state.
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[
                {"page_url": "u1", "data": {"id": "r0", "performers": ["Alice", "Bob"]}},
                {"page_url": "u2", "data": {"id": "r1", "performers": ["Carol"]}},
            ],
        )
        await bridge_db.db().execute("DELETE FROM performer_index")
        await bridge_db.db().commit()

        n = await bridge_db.backfill_performer_index()
        assert n == 3
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 3

    async def test_backfill_idempotent(self, bridge_db):
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Alice"]}}],
        )
        # Already populated by upsert. A backfill run should find nothing
        # to do (no candidate jobs).
        assert await bridge_db.backfill_performer_index() == 0
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 1

    async def test_backfill_skips_jobs_with_no_performers(self, bridge_db):
        """A job with results but zero performers across all of them is a
        candidate (no performer_index rows) — backfill scans, finds nothing
        to insert, and is harmless. Subsequent runs are also no-ops because
        the candidate query only looks for missing rows.
        """
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u", "data": {"id": "r0"}}],
        )
        # Backfill finds j1 as a candidate but inserts nothing.
        assert await bridge_db.backfill_performer_index() == 0
        cur = await bridge_db.db().execute(
            "SELECT COUNT(*) FROM performer_index WHERE job_id='j1'"
        )
        assert (await cur.fetchone())[0] == 0

    async def test_backfill_only_touches_unindexed_jobs(self, bridge_db):
        """If j1 is already indexed and j2 is not, backfill processes j2 only."""
        now = datetime.utcnow().isoformat()
        await bridge_db.upsert_job_and_results(
            "j1", "n", "s", now, now,
            results=[{"page_url": "u",
                      "data": {"id": "r0", "performers": ["Alice"]}}],
        )
        # Insert j2 directly via raw SQL to bypass the populate hook,
        # simulating a pre-migration row.
        await bridge_db.db().execute(
            "INSERT INTO extractor_jobs(job_id, job_name, schema_id, completed_at, fetched_at) "
            "VALUES ('j2', 'n', 's', ?, ?)",
            (now, now),
        )
        await bridge_db.db().execute(
            "INSERT INTO extractor_results(job_id, result_index, page_url, data_json) "
            "VALUES ('j2', 0, 'u', ?)",
            ('{"id":"r0","performers":["Bob"]}',),
        )
        await bridge_db.db().commit()

        n = await bridge_db.backfill_performer_index()
        assert n == 1  # only Bob (j1 was already indexed)
        cur = await bridge_db.db().execute(
            "SELECT job_id, name_lower FROM performer_index ORDER BY job_id"
        )
        rows = await cur.fetchall()
        assert rows == [("j1", "alice"), ("j2", "bob")]
