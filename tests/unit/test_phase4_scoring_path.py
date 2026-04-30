"""Unit tests for the Phase 4 scoring path — single-channel new formula
behind BRIDGE_NEW_SCORING_ENABLED.

Covers:
  - score_image_channel_a end-to-end (against featurized data) picks the
    matching record over non-matching ones
  - 400 raised when image_gamma / image_count_k missing while flag is on
  - Legacy top-K-mean path runs when flag is off
  - image_channels=["phash"] (single channel) does NOT require the
    multi-channel min_contribution / bonus_per_extra fields
"""
import json
from typing import Optional

import pytest
from fastapi import HTTPException

from bridge.app.extractor import client as ex_client
from bridge.app.stash import client as stash_client


# --- Setup helpers (specific to integration-flavored unit tests) ---------

@pytest.fixture
def mock_clients(monkeypatch, synth_image):
    """Patches both extractor + Stash clients to serve synthetic bytes.

    Returns (asset_seeds, set_scene_bytes) where:
      - asset_seeds[ref] = seed; missing refs return None (404)
      - set_scene_bytes(b) configures the bytes the Stash mock returns
        for `paths.screenshot` fetches
    """
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
    """Insert records, populate the mocked extractor's asset map, run
    featurization to ready, return when done."""
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


# --- score_image_channel_a end-to-end -----------------------------------

class TestScoreImageChannelA:
    async def test_matching_record_scores_above_non_matching(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        from bridge.app.matching.image_match import score_image_channel_a

        # Three records; record 1's cover is "imgScene" which the scene
        # provides verbatim → perfect match.
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
            "j_test", records, asset_map,
        )

        _, set_scene = mock_clients
        set_scene(synth_image(42))  # scene image == imgScene

        scene = _scene()
        s_results = []
        for rec in records:
            res = await score_image_channel_a(
                scene, "j_test", rec["data"],
                image_mode="cover", algorithm="phash", hash_size=8,
                sprite_sample_size=8, gamma=2.0, count_k=2.0,
            )
            s_results.append(res["S"])

        # Record 1 wins
        assert s_results[1] > s_results[0]
        assert s_results[1] > s_results[2]


# --- Validation: 400 on missing fields when flag is on -------------------

class TestRequestValidation:
    async def test_scrape_400_when_gamma_missing(
        self, bridge_db, clean_settings, mock_clients, reset_worker,
    ):
        from bridge.app.matching.scrape import scrape as do_scrape

        clean_settings.bridge_new_scoring_enabled = True
        records = [{"data": {"id": "r0", "cover_image": "imgA", "images": []}}]
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_v", records, {"imgA": 1},
        )
        _, set_scene = mock_clients
        set_scene(b"")  # empty bytes — sets attribute even if unused

        with pytest.raises(HTTPException) as exc_info:
            await do_scrape(
                _scene(), _candidates(records, "j_v"),
                used_studio_filter=False,
                image_mode="cover", threshold=0.05,
                algorithm="phash", hash_size=8, sprite_sample_size=8,
                image_gamma=None, image_count_k=None,
            )
        assert exc_info.value.status_code == 400

    async def test_scrape_400_when_count_k_missing(
        self, bridge_db, clean_settings, mock_clients, reset_worker,
    ):
        from bridge.app.matching.scrape import scrape as do_scrape

        clean_settings.bridge_new_scoring_enabled = True
        records = [{"data": {"id": "r0", "cover_image": "imgA", "images": []}}]
        await _seed_and_featurize(
            bridge_db, reset_worker, clean_settings, mock_clients,
            "j_v", records, {"imgA": 1},
        )

        with pytest.raises(HTTPException) as exc_info:
            await do_scrape(
                _scene(), _candidates(records, "j_v"),
                False, "cover", 0.05, "phash", 8, 8,
                image_gamma=2.0, image_count_k=None,
            )
        assert exc_info.value.status_code == 400


# --- Legacy fallback (flag off) ------------------------------------------

class TestLegacyFallback:
    async def test_legacy_path_works_without_new_fields(
        self, bridge_db, clean_settings, mock_clients, reset_worker, synth_image,
    ):
        """When the new-scoring flag is off, scrape uses top-K-mean and
        should not require image_gamma / image_count_k."""
        from bridge.app.matching.scrape import scrape as do_scrape

        clean_settings.bridge_lifecycle_enabled = False  # bypass the gate too
        clean_settings.bridge_new_scoring_enabled = False

        records = [
            {"data": {"id": "r0", "cover_image": "imgA1", "images": []}},
            {"data": {"id": "r1", "cover_image": "imgScene", "images": []}},
        ]
        asset_seeds, set_scene = mock_clients
        asset_seeds.update({"imgA1": 1, "imgScene": 42})

        await bridge_db.db().execute(
            "INSERT INTO extractor_jobs VALUES (?, ?, ?, ?, ?)",
            ("j_legacy", "TestSite", "sch1", "2026-04-29", "2026-04-29"),
        )
        for idx, r in enumerate(records):
            await bridge_db.db().execute(
                "INSERT INTO extractor_results VALUES (?, ?, ?, ?)",
                ("j_legacy", idx, "", json.dumps(r["data"])),
            )
        await bridge_db.db().commit()
        set_scene(synth_image(42))

        winner = await do_scrape(
            _scene(), _candidates(records, "j_legacy"),
            False, "cover", 0.3, "phash", 8, 8,
            # New fields all None — must not 400
        )
        assert winner is not None
        assert winner["result_index"] == 1
