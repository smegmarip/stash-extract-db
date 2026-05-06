"""Unit tests for channel E (local-feature matching).

Pure-numpy bits — algorithm-key formatting, blob serialization round-trip —
run anywhere. Encode + match tests auto-skip if kornia isn't installed
(local dev machines may not have the local_features extra). On the GPU
host the full path runs.
"""
import io

import numpy as np
import pytest
from PIL import Image

from bridge.app.matching import local_features as lf


# --- Algorithm key versioning -------------------------------------------

class TestAlgorithmKey:
    def test_format_includes_model_max_kp_and_dtype(self, clean_settings):
        lf._ALGO_KEY = None
        clean_settings.bridge_local_feature_max_keypoints = 512
        clean_settings.bridge_local_feature_model = "disk"
        key = lf.algorithm_key()
        assert "disk" in key
        assert "N=512" in key
        # dtype suffix is fp16 on cuda, fp32 on cpu — assert one of them
        assert "fp16" in key or "fp32" in key

    def test_max_kp_change_yields_new_key(self, clean_settings):
        lf._ALGO_KEY = None
        clean_settings.bridge_local_feature_max_keypoints = 256
        k1 = lf.algorithm_key()

        lf._ALGO_KEY = None
        clean_settings.bridge_local_feature_max_keypoints = 512
        k2 = lf.algorithm_key()

        assert k1 != k2
        assert "N=256" in k1
        assert "N=512" in k2


# --- Blob serialization round-trip ---------------------------------------

class TestBlobRoundTrip:
    def _make_synthetic_lf(self, n: int, d: int = 128, seed: int = 0) -> dict:
        rng = np.random.default_rng(seed)
        return {
            "keypoints": rng.uniform(0, 480, (n, 2)).astype(np.float16),
            "scores": rng.uniform(0, 1, n).astype(np.float16),
            "descriptors": rng.uniform(-1, 1, (n, d)).astype(np.float16),
            "image_hw": (480, 640),
        }

    def test_round_trip_preserves_arrays(self):
        original = self._make_synthetic_lf(n=10)
        blob = lf.lf_to_blob(original)
        back = lf.blob_to_lf(blob)
        np.testing.assert_array_equal(original["keypoints"], back["keypoints"])
        np.testing.assert_array_equal(original["scores"], back["scores"])
        np.testing.assert_array_equal(original["descriptors"], back["descriptors"])
        assert back["image_hw"] == original["image_hw"]

    def test_blob_size_matches_formula(self):
        # Header (12) + N*(2 fp16 kpts + 1 fp16 score + D*2 fp16 desc bytes)
        # For N=512, D=128: 12 + 512 * (4 + 2 + 256) = 12 + 512*262 = 134156
        original = self._make_synthetic_lf(n=512)
        blob = lf.lf_to_blob(original)
        expected = 12 + 512 * (4 + 2 + 128 * 2)
        assert len(blob) == expected

    def test_empty_keypoints_round_trip(self):
        empty = {
            "keypoints": np.zeros((0, 2), dtype=np.float16),
            "scores": np.zeros(0, dtype=np.float16),
            "descriptors": np.zeros((0, 128), dtype=np.float16),
            "image_hw": (100, 100),
        }
        blob = lf.lf_to_blob(empty)
        back = lf.blob_to_lf(blob)
        assert back["keypoints"].shape == (0, 2)
        assert back["descriptors"].shape == (0, 128)
        assert back["scores"].shape == (0,)
        assert back["image_hw"] == (100, 100)

    def test_blob_corruption_raises(self):
        original = self._make_synthetic_lf(n=10)
        blob = lf.lf_to_blob(original)
        # Truncate by 100 bytes — header still parses, but body length wrong.
        with pytest.raises(ValueError):
            lf.blob_to_lf(blob[:-100])

    def test_short_blob_raises(self):
        with pytest.raises(ValueError):
            lf.blob_to_lf(b"\x00" * 5)   # smaller than the 12-byte header

    def test_descriptor_shape_mismatch_raises(self):
        bad = {
            "keypoints": np.zeros((10, 2), dtype=np.float16),
            "scores": np.zeros(10, dtype=np.float16),
            "descriptors": np.zeros((10, 256), dtype=np.float16),  # wrong dim (DISK is 128)
            "image_hw": (100, 100),
        }
        with pytest.raises(ValueError):
            lf.lf_to_blob(bad)


# --- Encode + match (skipped if kornia / cuda unavailable) ----------------

def _has_kornia() -> bool:
    try:
        import kornia  # noqa: F401
        import torch
        return torch.cuda.is_available() or True   # CPU is acceptable here
    except ImportError:
        return False


def _has_cuda_and_kornia() -> bool:
    try:
        import kornia  # noqa: F401
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.fixture
def reset_lf_module():
    """Module-level _EXTRACTOR/_MATCHER are singletons; clear between tests
    that load the model."""
    yield
    lf._EXTRACTOR = None
    lf._MATCHER = None
    lf._ALGO_KEY = None


@pytest.mark.skipif(not _has_cuda_and_kornia(), reason="GPU + kornia required")
class TestEncodeOnGPU:
    """Smoke tests for SuperPoint extraction. GPU-only — CPU path works
    too but is slow enough to want skipping in the unit suite."""

    async def test_encode_returns_keypoints_and_descriptors(
        self, clean_settings, synth_image, reset_lf_module,
    ):
        clean_settings.bridge_local_features_enabled = True
        clean_settings.bridge_local_feature_max_keypoints = 256
        clean_settings.bridge_local_feature_device = "cuda"

        png = synth_image(seed=1, size=480)
        feat = await lf.encode_bytes_async(png)
        assert feat is not None
        assert feat["keypoints"].ndim == 2 and feat["keypoints"].shape[1] == 2
        assert feat["descriptors"].ndim == 2 and feat["descriptors"].shape[1] == 256
        assert feat["scores"].ndim == 1
        # SuperPoint may emit fewer than max_kp on smooth images; assert > 0.
        assert feat["keypoints"].shape[0] > 0
        assert feat["keypoints"].shape[0] <= 256

    async def test_encode_bytes_returns_none_on_garbage(
        self, clean_settings, reset_lf_module,
    ):
        clean_settings.bridge_local_features_enabled = True
        clean_settings.bridge_local_feature_device = "cuda"
        feat = await lf.encode_bytes_async(b"definitely not an image")
        assert feat is None

    async def test_match_identical_image_high_inliers(
        self, clean_settings, synth_image, reset_lf_module,
    ):
        clean_settings.bridge_local_features_enabled = True
        clean_settings.bridge_local_feature_device = "cuda"
        clean_settings.bridge_local_feature_max_keypoints = 256

        png = synth_image(seed=7, size=480)
        feat = await lf.encode_bytes_async(png)
        assert feat is not None
        # Match the image against itself — should produce a healthy
        # number of inliers.
        n = await lf.match_pair_async(feat, feat)
        assert n >= 30   # generous; SuperPoint+LightGlue+RANSAC self-match >> 30

    async def test_match_unrelated_images_low_inliers(
        self, clean_settings, synth_image, reset_lf_module,
    ):
        clean_settings.bridge_local_features_enabled = True
        clean_settings.bridge_local_feature_device = "cuda"
        clean_settings.bridge_local_feature_max_keypoints = 256

        a = await lf.encode_bytes_async(synth_image(seed=11, size=480))
        b = await lf.encode_bytes_async(synth_image(seed=37, size=480))
        assert a is not None and b is not None
        n = await lf.match_pair_async(a, b)
        # Two unrelated synthetic gradients should produce few stable
        # matches under RANSAC. 15 is the documented "fires" gate; this
        # asserts they fall below it.
        assert n < 15
