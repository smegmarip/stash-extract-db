"""Unit tests for /api/admin/* — display projections over the cache.

Covers (per CLAUDE.md §17.3):
  - GET /api/admin/jobs returns scene-shaped jobs with record counts
  - GET /api/admin/records: search across id/title/url/performer; sort by all
    four keys; pagination math; job filter narrows correctly; tiebreak is
    deterministic (CLAUDE.md §10).
  - GET /api/admin/records/{job_id}/{idx}: full record with asset URLs
    rewritten to bridge proxy paths.
  - GET /api/admin/performers: aggregation counts records and jobs.
  - GET /api/admin/performers/{name}: groups records by job; case-insensitive.

Endpoints are called as plain async coroutines with explicit args to avoid
spinning up the full FastAPI app.
"""
from datetime import datetime, timedelta

import pytest

from bridge.app.api import admin


# --- shared fixtures ----------------------------------------------------

async def _seed(bridge_db, jobs):
    """Seed extractor_jobs + extractor_results from a list of (job_id,
    job_name, completed_at, [(idx, page_url, data), ...]) tuples."""
    for job_id, job_name, completed_at, results in jobs:
        await bridge_db.upsert_job_and_results(
            job_id=job_id, job_name=job_name, schema_id="s1",
            completed_at=completed_at, fetched_at=completed_at,
            results=[
                {"page_url": pu, "data": d} for (_idx, pu, d) in results
            ],
        )


# --- /jobs --------------------------------------------------------------

class TestJobsEndpoint:
    async def test_returns_jobs_with_record_counts(self, bridge_db):
        t0 = datetime.utcnow().isoformat()
        t1 = (datetime.utcnow() + timedelta(seconds=10)).isoformat()
        await _seed(bridge_db, [
            ("j1", "Studio A", t0, [
                (0, "u1", {"id": "r0", "title": "T0"}),
                (1, "u2", {"id": "r1", "title": "T1"}),
            ]),
            ("j2", "Studio B", t1, [
                (0, "u3", {"id": "r0", "title": "T2"}),
            ]),
        ])
        out = await admin.list_jobs()
        # Most-recent completed_at first
        assert [j["job_id"] for j in out] == ["j2", "j1"]
        assert out[0]["record_count"] == 1
        assert out[1]["record_count"] == 2
        assert out[0]["job_name"] == "Studio B"

    async def test_empty_when_no_jobs(self, bridge_db):
        assert await admin.list_jobs() == []

    async def test_includes_full_feature_state(self, bridge_db):
        """The Status page renders progress, started_at, error from this
        endpoint — verify they're populated."""
        t0 = datetime.utcnow().isoformat()
        await _seed(bridge_db, [
            ("j1", "Studio A", t0, [(0, "u", {"id": "r0"})]),
            ("j2", "Studio B", t0, [(0, "u", {"id": "r0"})]),
        ])
        await bridge_db.upsert_feature_state("j1", "ready", 1.0)
        await bridge_db.mark_feature_ready("j1")
        await bridge_db.upsert_feature_state("j2", "featurizing", 0.42)

        by_id = {j["job_id"]: j for j in await admin.list_jobs()}
        assert by_id["j1"]["feature_state"] == "ready"
        assert by_id["j1"]["feature_progress"] == 1.0
        assert by_id["j1"]["feature_finished_at"] is not None
        assert by_id["j1"]["feature_error"] is None

        assert by_id["j2"]["feature_state"] == "featurizing"
        assert by_id["j2"]["feature_progress"] == 0.42
        assert by_id["j2"]["feature_finished_at"] is None

    async def test_no_feature_state_row_yields_nulls(self, bridge_db):
        t0 = datetime.utcnow().isoformat()
        await _seed(bridge_db, [
            ("j1", "Studio A", t0, [(0, "u", {"id": "r0"})]),
        ])
        # no upsert_feature_state — LEFT JOIN should produce nulls
        out = await admin.list_jobs()
        assert out[0]["feature_state"] is None
        assert out[0]["feature_progress"] is None
        assert out[0]["feature_started_at"] is None


# --- /records ------------------------------------------------------------

@pytest.fixture
async def seeded_records(bridge_db):
    """Three jobs, varied performers + dates for filter/sort coverage."""
    t0 = datetime.utcnow().isoformat()
    t1 = (datetime.utcnow() + timedelta(seconds=1)).isoformat()
    await _seed(bridge_db, [
        ("j1", "Studio A", t0, [
            (0, "https://a.com/scene-1", {
                "id": "1001", "title": "Alpha Scene", "date": "2024-01-15",
                "performers": ["Alice"], "details": "first",
                "cover_image": "../assets/c1.jpg",
                "images": ["../assets/i1.jpg", "../assets/i2.jpg"],
            }),
            (1, "https://a.com/scene-2", {
                "id": "1002", "title": "Beta Scene", "date": "2024-03-20",
                "performers": ["Bob", "Carol"], "details": "second",
            }),
        ]),
        ("j2", "Studio B", t1, [
            (0, "https://b.com/show", {
                "id": "9001", "title": "Charlie Show", "date": "2023-12-01",
                "performers": ["Alice", "David"], "details": "third",
            }),
        ]),
    ])
    return bridge_db


class TestRecordsList:
    async def test_no_filter_returns_all(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="id", dir="asc", page=1, limit=24,
        )
        assert out["pagination"]["total"] == 3
        assert len(out["data"]) == 3

    async def test_job_filter(self, seeded_records):
        out = await admin.list_records(
            job_id="j1", q=None, sort="id", dir="asc", page=1, limit=24,
        )
        assert out["pagination"]["total"] == 2
        assert {r["job_id"] for r in out["data"]} == {"j1"}

    async def test_search_by_id_exact(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q="9001", sort="id", dir="asc", page=1, limit=24,
        )
        assert out["pagination"]["total"] == 1
        assert out["data"][0]["id"] == "9001"

    async def test_search_by_title_substring(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q="charlie", sort="id", dir="asc", page=1, limit=24,
        )
        assert out["pagination"]["total"] == 1
        assert out["data"][0]["title"] == "Charlie Show"

    async def test_search_by_url(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q="b.com", sort="id", dir="asc", page=1, limit=24,
        )
        assert out["pagination"]["total"] == 1
        assert out["data"][0]["job_id"] == "j2"

    async def test_search_by_performer(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q="alice", sort="id", dir="asc", page=1, limit=24,
        )
        # Alice appears in j1/r0 and j2/r0
        ids = {(r["job_id"], r["result_index"]) for r in out["data"]}
        assert ids == {("j1", 0), ("j2", 0)}

    async def test_search_with_wildcard_in_input_is_escaped(self, seeded_records):
        # User-supplied % must NOT act as a wildcard
        out = await admin.list_records(
            job_id=None, q="%charlie%", sort="id", dir="asc", page=1, limit=24,
        )
        # No record literally contains "%charlie%"
        assert out["pagination"]["total"] == 0

    async def test_sort_by_title_asc(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="title", dir="asc", page=1, limit=24,
        )
        titles = [r["title"] for r in out["data"]]
        assert titles == ["Alpha Scene", "Beta Scene", "Charlie Show"]

    async def test_sort_by_title_desc(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="title", dir="desc", page=1, limit=24,
        )
        titles = [r["title"] for r in out["data"]]
        assert titles == ["Charlie Show", "Beta Scene", "Alpha Scene"]

    async def test_sort_by_date(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="date", dir="asc", page=1, limit=24,
        )
        dates = [r["date"] for r in out["data"]]
        assert dates == ["2023-12-01", "2024-01-15", "2024-03-20"]

    async def test_sort_by_performer(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="performer", dir="asc", page=1, limit=24,
        )
        # Min performer per record (lowercased): j1/r0=alice, j1/r1=bob, j2/r0=alice
        # Tiebreak is (job_id, result_index) ascending → j1/r0 then j2/r0 then j1/r1
        keys = [(r["job_id"], r["result_index"]) for r in out["data"]]
        assert keys == [("j1", 0), ("j2", 0), ("j1", 1)]

    async def test_pagination(self, seeded_records):
        out = await admin.list_records(
            job_id=None, q=None, sort="id", dir="asc", page=1, limit=2,
        )
        assert out["pagination"]["total"] == 3
        assert out["pagination"]["totalPages"] == 2
        assert out["pagination"]["page"] == 1
        assert len(out["data"]) == 2

        out2 = await admin.list_records(
            job_id=None, q=None, sort="id", dir="asc", page=2, limit=2,
        )
        assert len(out2["data"]) == 1


# --- /records/{job_id}/{idx} --------------------------------------------

class TestRecordDetail:
    async def test_returns_full_record(self, seeded_records):
        rec = await admin.get_record("j1", 0)
        assert rec["job_id"] == "j1"
        assert rec["result_index"] == 0
        assert rec["title"] == "Alpha Scene"
        assert rec["performers"] == ["Alice"]
        assert rec["performer_count"] == 1

    async def test_asset_urls_rewritten_to_bridge_proxy(self, seeded_records):
        rec = await admin.get_record("j1", 0)
        # Relative refs should point to bridge proxy path
        assert rec["cover_image"] == "/api/asset/j1/assets/c1.jpg"
        assert rec["images"] == [
            "/api/asset/j1/assets/i1.jpg",
            "/api/asset/j1/assets/i2.jpg",
        ]

    async def test_image_count_includes_cover(self, seeded_records):
        rec = await admin.get_record("j1", 0)
        # 1 cover + 2 images = 3
        assert rec["image_count"] == 3

    async def test_404_on_missing(self, seeded_records):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await admin.get_record("j1", 99)
        assert exc.value.status_code == 404


class TestAssetUrlRewrite:
    """Direct unit tests for the helper. CLAUDE.md §13.6 guarantees relative
    refs use `../assets/<file>` form; absolutes are http(s)://.
    """
    def test_relative_assets_path(self):
        assert admin._bridge_asset_url("j1", "../assets/c1.jpg") == "/api/asset/j1/assets/c1.jpg"

    def test_bare_filename_routed_to_assets(self):
        assert admin._bridge_asset_url("j1", "c1.jpg") == "/api/asset/j1/assets/c1.jpg"

    def test_absolute_url_passthrough(self):
        url = "https://cdn.example.com/img.jpg"
        assert admin._bridge_asset_url("j1", url) == url

    def test_none_returns_none(self):
        assert admin._bridge_asset_url("j1", None) is None
        assert admin._bridge_asset_url("j1", "") is None


# --- /performers --------------------------------------------------------

class TestPerformersList:
    async def test_aggregation_counts(self, seeded_records):
        out = await admin.list_performers(
            job_id=None, q=None, page=1, limit=24,
        )
        names = {p["name_lower"]: p for p in out["data"]}
        # Alice: 2 records across 2 jobs; Bob/Carol: 1 record / 1 job each
        assert names["alice"]["record_count"] == 2
        assert names["alice"]["job_count"] == 2
        assert names["bob"]["record_count"] == 1
        assert names["bob"]["job_count"] == 1

    async def test_display_casing_from_min_original(self, seeded_records):
        out = await admin.list_performers(
            job_id=None, q="alice", page=1, limit=24,
        )
        assert out["data"][0]["name_display"] == "Alice"

    async def test_job_filter_narrows(self, seeded_records):
        out = await admin.list_performers(
            job_id="j2", q=None, page=1, limit=24,
        )
        names = {p["name_lower"] for p in out["data"]}
        assert names == {"alice", "david"}

    async def test_name_search(self, seeded_records):
        out = await admin.list_performers(
            job_id=None, q="ali", page=1, limit=24,
        )
        assert {p["name_lower"] for p in out["data"]} == {"alice"}

    async def test_pagination_math(self, seeded_records):
        # Total distinct performers = {alice, bob, carol, david} = 4
        out = await admin.list_performers(
            job_id=None, q=None, page=1, limit=2,
        )
        assert out["pagination"]["total"] == 4
        assert out["pagination"]["totalPages"] == 2

    async def test_alphabetical_order(self, seeded_records):
        out = await admin.list_performers(
            job_id=None, q=None, page=1, limit=24,
        )
        names = [p["name_lower"] for p in out["data"]]
        assert names == sorted(names)


# --- /performers/{name} -------------------------------------------------

class TestPerformerDetail:
    async def test_groups_by_job(self, seeded_records):
        out = await admin.get_performer("Alice")
        assert out["name_display"] == "Alice"
        assert out["record_count"] == 2
        assert out["job_count"] == 2
        # Jobs ordered by completed_at DESC: j2 (newer) first
        assert [j["job_id"] for j in out["jobs"]] == ["j2", "j1"]
        # Each job has its records list populated
        assert len(out["jobs"][0]["records"]) == 1
        assert len(out["jobs"][1]["records"]) == 1

    async def test_case_insensitive_lookup(self, seeded_records):
        a = await admin.get_performer("alice")
        b = await admin.get_performer("ALICE")
        c = await admin.get_performer("Alice")
        assert a["record_count"] == b["record_count"] == c["record_count"]

    async def test_404_on_unknown(self, seeded_records):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await admin.get_performer("nonexistent")
        assert exc.value.status_code == 404

    async def test_records_have_asset_urls_rewritten(self, seeded_records):
        out = await admin.get_performer("Alice")
        # Find j1's record (which had cover/images set in the fixture)
        j1 = next(j for j in out["jobs"] if j["job_id"] == "j1")
        rec = j1["records"][0]
        assert rec["cover_image"] == "/api/asset/j1/assets/c1.jpg"
