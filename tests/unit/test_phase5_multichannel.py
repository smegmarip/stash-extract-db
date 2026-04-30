"""Unit tests for the Phase 5 multi-channel composition.

Covers:
  - score_image_channel_b uses the per-record aggregate; sim is
    histogram-intersection between scene + record aggregates
  - score_image_channel_c is frame-level (same shape as A)
  - score_image_composite combines all three with max + bonus
  - image_channels=["phash"] alone falls through to the Phase 4 path

Per CLAUDE.md §1, scoring config is bridge-owned and missing fields
fall back to settings defaults — the previous "400 on missing field"
test was removed in the config-ownership inversion.
"""
import json
from typing import Optional

import pytest

from bridge.app.extractor import client as ex_client
from bridge.app.stash import client as stash_client


@pytest.fixture
def mock_clients(monkeypatch, synth_image):
    asset_seeds: dict[str, int] = {}
    scene_state = {"bytes": None}

    async def _ex_fetch(job_id: str, ref: str) -> Optional[bytes]:
        seed = asset_seeds.get(ref)
        return synth_image(seed) if seed is not None else None

    def _ex_resolve(job_id: str, ref: str) -> str:
        return f"http://mock-extractor/{job_id}/{ref}"

    async def _stash_fetch(url: str) -> Optional[bytes]:
        return scene_state["bytes"]

    monkeypatch.setattr(ex_client, "fetch_asset", _ex_fetch)
    monkeypatch.setattr(ex_client, "resolve_asset_url", _ex_resolve)
    monkeypatch.setattr(stash_client, "fetch_image_bytes", _stash_fetch)

    def set_scene_bytes(b: bytes) -> None:
        scene_state["bytes"] = b

    return asset_seeds, set_scene_bytes


async def _seed_and_featurize(bridge_db, reset_worker, clean_settings, mock_clients,
                              job_id: str, records: list[dict], asset_seed_map: dict):
    clean_settings.bridge_lifecycle_enabled = True
    asset_seeds, _ = mock_clients
    asset_seeds.update(asset_seed_map)

    await bridge_db.db().execute(
        "INSERT INTO extractor_jobs VALUES (?, ?, ?, ?, ?)",
        (job_id, "TestSite", "sch1", "2026-04-29T00:00:00", "2026-04-29T00:00:00"),
    )
    for idx, r in enumerate(records):
        await bridge_db.db().execute(
            "INSERT INTO extractor_results VALUES (?, ?, ?, ?)",
            (job_id, idx, "", json.dumps(r["data"])),
        )
    await bridge_db.db().commit()

    await reset_worker.enqueue(job_id)
    if job_id in reset_worker._inflight:
        await reset_worker._inflight[job_id]


def _scene(scene_id: str = "sceneX") -> dict:
    return {
        "id": scene_id,
        "paths": {"screenshot": "http://stash/scene.jpg?t=12345"},
        "files": [{"basename": "test.mp4",
                   "fingerprints": [{"type": "oshash", "value": "abc"}]}],
    }


def _candidates(records: list[dict], job_id: str) -> list[dict]:
    return [
        {"job_id": job_id, "result_index": idx, "page_url": "", "data": r["data"]}
        for idx, r in enumerate(records)
    ]


# --- Channel B (color histogram aggregate) -------------------------------

class TestChannelB:
    async def test_aggregate_intersection_for_matching_record(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        from bridge.app.matching.image_match import score_image_channel_b

        records = [
            {"data": {"id": "r0", "cover_image": "imgScene", "images": []}},
            {"data": {"id": "r1", "cover_image": "imgB", "images": []}},
        ]
        asset_map = {"imgScene": 42, "imgB": 99}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_b", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(42))

        b0 = await score_image_channel_b(
            _scene(), "j_b", records[0]["data"], record_idx=0,
            sprite_sample_size=8, gamma=2.0,
        )
        b1 = await score_image_channel_b(
            _scene(), "j_b", records[1]["data"], record_idx=1,
            sprite_sample_size=8, gamma=2.0,
        )
        # Matching record should have higher intersection sim
        assert b0["sim"] >= b1["sim"]
        # Both have aggregates
        assert b0["have_stash"] and b0["have_extractor"]


# --- Channel C (low-res tone, frame-level) -------------------------------

class TestChannelC:
    async def test_frame_level_score_for_matching(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        from bridge.app.matching.image_match import score_image_channel_c

        records = [
            {"data": {"id": "r0", "cover_image": "imgA", "images": []}},
            {"data": {"id": "r1", "cover_image": "imgScene", "images": ["imgB2"]}},
            {"data": {"id": "r2", "cover_image": "imgC", "images": []}},
        ]
        asset_map = {"imgA": 1, "imgScene": 42, "imgB2": 4, "imgC": 5}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_c", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(42))

        results = []
        for idx, r in enumerate(records):
            c = await score_image_channel_c(
                _scene(), "j_c", r["data"], image_mode="cover",
                sprite_sample_size=8, gamma=2.0, count_k=2.0,
            )
            results.append(c)

        # Record 1 has the matching cover — its top per-image-max should
        # be ~1.0 (perfect tone match).
        assert max(results[1]["per_image_max"]) > max(results[0]["per_image_max"])
        assert results[1]["n_extractor_images"] == 2
        assert results[0]["n_extractor_images"] == 1


# --- Composite scoring ---------------------------------------------------

class TestComposite:
    async def test_composite_picks_matching_record(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        from bridge.app.matching.image_match import score_image_composite

        records = [
            {"data": {"id": "r0", "cover_image": "imgA1", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgScene", "images": ["imgB2"]}},
            {"data": {"id": "r2", "cover_image": "imgC1", "images": ["imgC2"]}},
        ]
        asset_map = {
            "imgA1": 1, "imgA2": 2,
            "imgScene": 42, "imgB2": 4,
            "imgC1": 5, "imgC2": 6,
        }
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_comp", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(42))

        composites = []
        for idx, r in enumerate(records):
            c = await score_image_composite(
                _scene(), "j_comp", r["data"], record_idx=idx,
                image_mode="cover", algorithm="phash", hash_size=8,
                sprite_sample_size=8,
                gamma=2.0, count_k=2.0,
                channels=["phash", "color_hist", "tone"],
                min_contribution=0.05, bonus_per_extra=0.1,
            )
            composites.append(c["S"])

        # Record 1 wins
        assert composites[1] > composites[0]
        assert composites[1] > composites[2]


# --- Multi-channel single-channel pass-through ---------------------------

class TestMultiChannelValidation:
    async def test_single_channel_phash_only_skips_validation(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        """image_channels=['phash'] is the Phase 4 path; doesn't require
        min_contribution / bonus_per_extra."""
        from bridge.app.matching.scrape import scrape as do_scrape

        clean_settings.bridge_new_scoring_enabled = True
        # N=2 per record so count_conf is high enough for the threshold to
        # admit the matching record. (Single-image records produce S near
        # threshold; this test asserts the validation skip, not a tight
        # threshold.)
        records = [
            {"data": {"id": "r0", "cover_image": "imgA1", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgScene", "images": ["imgB2"]}},
        ]
        asset_map = {"imgA1": 1, "imgA2": 2, "imgScene": 42, "imgB2": 4}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_one", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(42))

        # No min_contribution / bonus → must NOT raise
        winner = await do_scrape(
            _scene(), _candidates(records, "j_one"),
            False, "cover", 0.05, "phash", 8, 8,
            image_gamma=2.0, image_count_k=2.0,
            image_channels=["phash"],
            image_min_contribution=None,
            image_bonus_per_extra=None,
        )
        assert winner is not None
        assert winner["result_index"] == 1


# --- Search confidence floor (architectural addition) -------------------

class TestSearchConfidenceFloor:
    """Phase 6: search-mode floor drops weak image-only candidates.

    Definitive signals (Studio+Code, Exact Title) bypass the floor.
    """

    async def test_floor_drops_weak_candidates(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        """A scene with no real match should produce only weak composites
        for all candidates. With a floor at 0.15, the search returns []."""
        from bridge.app.matching.search import search as do_search
        from bridge.app.stash.alias_index import AliasResolver

        clean_settings.bridge_new_scoring_enabled = True

        # Two records whose cover images are random-noise different from
        # the scene; both should produce weak composites.
        records = [
            {"data": {"id": "r0", "cover_image": "imgA", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgB", "images": ["imgB2"]}},
        ]
        asset_map = {"imgA": 1, "imgA2": 2, "imgB": 3, "imgB2": 4}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_floor", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(99))  # scene with no match in the records

        ranked = await do_search(
            _scene(), _candidates(records, "j_floor"),
            False, "cover", 0.0, "phash", 8, 8,
            limit=5, alias_resolver=AliasResolver(), debug=False,
            image_gamma=2.0, image_count_k=2.0,
            image_channels=["phash"],
            image_min_contribution=None, image_bonus_per_extra=None,
            image_search_floor=0.15,
        )
        # All candidates should be below the floor since nothing matches
        assert ranked == [], f"expected empty ranked list, got {len(ranked)} items"

    async def test_floor_keeps_strong_matches(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        """Strong matches above the floor are preserved."""
        from bridge.app.matching.search import search as do_search
        from bridge.app.stash.alias_index import AliasResolver

        clean_settings.bridge_new_scoring_enabled = True

        records = [
            {"data": {"id": "r0", "cover_image": "imgA1", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgScene", "images": ["imgB2"]}},
        ]
        asset_map = {"imgA1": 1, "imgA2": 2, "imgScene": 42, "imgB2": 4}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_floor2", records, asset_map,
        )
        _, set_scene = mock_clients
        set_scene(synth_image(42))  # matches r1's cover exactly

        ranked = await do_search(
            _scene(), _candidates(records, "j_floor2"),
            False, "cover", 0.0, "phash", 8, 8,
            limit=5, alias_resolver=AliasResolver(), debug=False,
            image_gamma=2.0, image_count_k=2.0,
            image_channels=["phash"],
            image_min_contribution=None, image_bonus_per_extra=None,
            image_search_floor=0.05,  # low enough that the real match passes
        )
        # The strong-match record should survive
        codes = [r[0]["data"]["id"] for r in ranked]
        assert "r1" in codes, f"strong match dropped by floor; got {codes}"

    async def test_floor_none_means_no_filter(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        """floor=None preserves legacy behavior — every candidate kept."""
        from bridge.app.matching.search import search as do_search
        from bridge.app.stash.alias_index import AliasResolver

        clean_settings.bridge_new_scoring_enabled = True

        records = [
            {"data": {"id": "r0", "cover_image": "imgA", "images": ["imgA2"]}},
            {"data": {"id": "r1", "cover_image": "imgB", "images": ["imgB2"]}},
        ]
        asset_map = {"imgA": 1, "imgA2": 2, "imgB": 3, "imgB2": 4}
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_floor3", records, asset_map,
        )
        _, set_scene = mock_clients
        set_scene(synth_image(99))

        ranked = await do_search(
            _scene(), _candidates(records, "j_floor3"),
            False, "cover", 0.0, "phash", 8, 8,
            limit=5, alias_resolver=AliasResolver(), debug=False,
            image_gamma=2.0, image_count_k=2.0,
            image_channels=["phash"],
            image_min_contribution=None, image_bonus_per_extra=None,
            image_search_floor=None,
        )
        # No floor → all 2 candidates returned (with weak scores)
        assert len(ranked) == 2
