"""Unit tests for `score_image_channel_e` and channel-E composite wiring.

The kornia model path is exercised in test_local_features.py; here we
focus on the scoring logic shape — what S_E looks like at boundary inlier
counts, and how the composite math integrates E alongside D.
"""
from unittest.mock import patch, AsyncMock

import numpy as np
import pytest

from bridge.app.matching import image_match as im
from bridge.app.matching import local_features as lf
from bridge.app.matching.scoring import ChannelScore, compose


def _stub_lf_blob(n: int = 32) -> bytes:
    """Build a deserializable LF blob with N keypoints."""
    rng = np.random.default_rng(0)
    fake = {
        "keypoints": rng.uniform(0, 480, (n, 2)).astype(np.float16),
        "scores": rng.uniform(0, 1, n).astype(np.float16),
        "descriptors": rng.uniform(-1, 1, (n, 256)).astype(np.float16),
        "image_hw": (480, 640),
    }
    return lf.lf_to_blob(fake)


# --- score_image_channel_e: shape + boundary cases -----------------------

class TestScoreImageChannelE:
    async def test_no_extractor_keypoints_returns_zero(self):
        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg", "b.jpg"], "cover_image": None}

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg", "b.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=None)), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[])):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="cover", sprite_sample_size=8,
            )
        assert r["S"] == 0.0
        assert r["n_extractor_images"] == 0
        assert r["scoring"] == "local_features"

    async def test_no_stash_keypoints_returns_zero(self):
        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg"]}

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=None)), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[])):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="cover", sprite_sample_size=8,
            )
        assert r["S"] == 0.0
        assert r["n_stash_hashes"] == 0

    async def test_below_min_inliers_does_not_fire(self, clean_settings):
        clean_settings.bridge_local_feature_min_inliers = 15
        clean_settings.bridge_local_feature_target_inliers = 50

        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg"]}

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[])), \
             patch.object(lf, "match_pair_async", AsyncMock(return_value=10)):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="cover", sprite_sample_size=8,
            )
        assert r["S"] == 0.0
        assert r["max_inliers"] == 10

    async def test_above_min_inliers_fires_proportionally(self, clean_settings):
        clean_settings.bridge_local_feature_min_inliers = 15
        clean_settings.bridge_local_feature_target_inliers = 50

        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg"]}

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[])), \
             patch.object(lf, "match_pair_async", AsyncMock(return_value=25)):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="cover", sprite_sample_size=8,
            )
        # 25 / 50 = 0.5
        assert r["S"] == pytest.approx(0.5)
        assert r["max_inliers"] == 25

    async def test_inliers_at_or_above_target_clamps_to_one(self, clean_settings):
        clean_settings.bridge_local_feature_min_inliers = 15
        clean_settings.bridge_local_feature_target_inliers = 50

        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg"]}

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[])), \
             patch.object(lf, "match_pair_async", AsyncMock(return_value=200)):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="cover", sprite_sample_size=8,
            )
        assert r["S"] == 1.0

    async def test_max_taken_across_pairs(self, clean_settings):
        clean_settings.bridge_local_feature_min_inliers = 15
        clean_settings.bridge_local_feature_target_inliers = 50

        scene = {"id": "s1", "paths": {"screenshot": "http://x/cover.jpg"}}
        record = {"images": ["a.jpg", "b.jpg"]}

        # match_pair_async returns different values for each call — the
        # channel should use the max across all pairs.
        match_results = [10, 30, 5, 18]   # 4 pairs (2 stash × 2 ext)
        match_iter = iter(match_results)

        async def _mp(*args, **kwargs):
            return next(match_iter)

        with patch.object(im, "_expanded_extractor_refs", AsyncMock(return_value=["a.jpg", "b.jpg"])), \
             patch.object(im, "extractor_image_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_cover_keypoints", AsyncMock(return_value=_stub_lf_blob())), \
             patch.object(im, "stash_sprite_keypoints", AsyncMock(return_value=[_stub_lf_blob()])), \
             patch.object(lf, "match_pair_async", side_effect=_mp):
            r = await im.score_image_channel_e(
                scene, "j1", record, image_mode="both", sprite_sample_size=1,
            )
        assert r["max_inliers"] == 30
        assert r["S"] == pytest.approx(30 / 50)


# --- compose() math: E joins as just another fired channel ---------------

class TestComposeWithE:
    def test_e_alone_above_min_contribution_fires(self):
        cs = {
            "local_features": ChannelScore(
                S=0.6, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[],
            ),
        }
        composite, fired = compose(cs, min_contribution=0.05, bonus_per_extra=0.1)
        assert fired == ["local_features"]
        assert composite == pytest.approx(0.6)

    def test_d_plus_e_both_fire_get_bonus(self):
        cs = {
            "embedding": ChannelScore(S=0.65, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
            "local_features": ChannelScore(S=0.50, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
        }
        composite, fired = compose(cs, min_contribution=0.05, bonus_per_extra=0.1)
        assert set(fired) == {"embedding", "local_features"}
        # max(0.65, 0.50) + 0.1 * (2 - 1) = 0.75
        assert composite == pytest.approx(0.75)

    def test_e_below_min_contribution_does_not_fire(self):
        cs = {
            "embedding": ChannelScore(S=0.7, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
            "local_features": ChannelScore(S=0.0, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
        }
        composite, fired = compose(cs, min_contribution=0.05, bonus_per_extra=0.1)
        assert fired == ["embedding"]
        assert composite == pytest.approx(0.7)

    def test_e_lifts_borderline_d_above_threshold(self):
        # The motivating case: D at 0.55 (sub-threshold), E fires modestly,
        # composite crosses 0.7. This is what Channel E exists to do.
        cs = {
            "embedding": ChannelScore(S=0.55, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
            "local_features": ChannelScore(S=0.40, E=0.0, count_conf=1.0, dist_q=1.0, m_primes=[]),
        }
        composite, fired = compose(cs, min_contribution=0.05, bonus_per_extra=0.15)
        # max(0.55, 0.40) + 0.15 * 1 = 0.70
        assert composite == pytest.approx(0.70)
        assert composite >= 0.7   # the threshold gate
