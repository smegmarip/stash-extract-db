"""Unit tests for channel D (semantic embedding).

The pure-numpy bits (cosine math, blob serialization, algorithm-key
formatting) run anywhere. The model-load + encode tests auto-skip
if torch / transformers aren't installed — local dev machines without
a CUDA GPU may not have torch installed; CI on the GPU host will.
"""
import io
import pytest
import numpy as np
from PIL import Image

from bridge.app.matching import embedding as emb


# --- Pure numpy: cosine math --------------------------------------------

class TestCosine:
    def test_orthogonal_vectors_zero(self):
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        assert emb.cosine_sim(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_identical_vectors_one(self):
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert emb.cosine_sim(a, a) == pytest.approx(1.0, abs=1e-6)

    def test_antiparallel_negative_one(self):
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        b = -a
        assert emb.cosine_sim(a, b) == pytest.approx(-1.0, abs=1e-6)

    def test_zero_vector_returns_zero(self):
        a = np.zeros(3, dtype=np.float32)
        b = np.array([1.0, 1.0, 1.0], dtype=np.float32)
        assert emb.cosine_sim(a, b) == 0.0
        assert emb.cosine_sim(b, a) == 0.0

    def test_fp16_input_promotes_to_fp32(self):
        # fp16 with extreme values would underflow if we kept fp16 in the
        # accumulation; the helper promotes to fp32 internally.
        a = np.full(1024, 0.1, dtype=np.float16)
        b = np.full(1024, 0.1, dtype=np.float16)
        assert emb.cosine_sim(a, b) == pytest.approx(1.0, abs=1e-3)


class TestCosineMatrix:
    def test_shape(self):
        S = np.random.randn(3, 8).astype(np.float32)
        E = np.random.randn(5, 8).astype(np.float32)
        sims = emb.cosine_sim_matrix(S, E)
        assert sims.shape == (3, 5)
        # All entries in [-1, 1]
        assert sims.min() >= -1.0 - 1e-6
        assert sims.max() <= 1.0 + 1e-6

    def test_self_similarity_diag_one(self):
        # Make S = E so the diagonal of the result should be ~1.0.
        rng = np.random.default_rng(0)
        E = rng.standard_normal((4, 8)).astype(np.float32)
        sims = emb.cosine_sim_matrix(E, E)
        diag = np.diag(sims)
        np.testing.assert_allclose(diag, 1.0, atol=1e-5)

    def test_empty_inputs_return_zero_shaped_matrix(self):
        S = np.zeros((0, 8), dtype=np.float32)
        E = np.random.randn(3, 8).astype(np.float32)
        sims = emb.cosine_sim_matrix(S, E)
        assert sims.shape == (0, 3)


# --- Blob serialization round-trip ---------------------------------------

class TestBlobRoundTrip:
    def test_fp32_input_serialized_as_fp16(self):
        v = np.array([0.1, 0.2, 0.3, -0.5], dtype=np.float32)
        blob = emb.embedding_to_blob(v)
        assert len(blob) == 4 * 2  # 4 floats × 2 bytes (fp16)
        back = emb.blob_to_embedding(blob)
        assert back.dtype == np.float16
        # fp16 has limited precision; 1e-3 is plenty
        np.testing.assert_allclose(back.astype(np.float32), v, atol=1e-3)

    def test_fp16_input_serialized_as_fp16(self):
        v = np.array([0.1, 0.2, 0.3], dtype=np.float16)
        blob = emb.embedding_to_blob(v)
        back = emb.blob_to_embedding(blob)
        np.testing.assert_array_equal(back, v)


# --- Algorithm key versioning -------------------------------------------

class TestAlgorithmKey:
    def test_format_includes_model_dtype_dim(self, clean_settings):
        emb._ALGO_KEY = None  # reset module cache
        clean_settings.bridge_embedding_model = "facebook/dinov2-large"
        clean_settings.bridge_embedding_dtype = "fp16"
        key = emb.algorithm_key()
        assert "facebook/dinov2-large" in key
        assert "fp16" in key
        assert "1024" in key   # known dim for dinov2-large

    def test_unknown_model_falls_back_to_1024(self, clean_settings):
        emb._ALGO_KEY = None
        clean_settings.bridge_embedding_model = "experimental/some-model"
        key = emb.algorithm_key()
        assert "1024" in key   # default fallback


# --- Model load + encode (skipped if torch unavailable) -----------------

@pytest.fixture
def reset_emb_module():
    """Module-level _MODEL/_PROCESSOR are singletons; clear between tests
    that rely on a specific model being loaded."""
    yield
    emb._MODEL = None
    emb._PROCESSOR = None
    emb._ALGO_KEY = None


def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(
    not _has_cuda(),
    reason="GPU-only test — torch not installed or no CUDA in this env",
)
class TestEncodeOnGPU:
    """Smoke tests that exercise the actual model path. Pinned to small
    model to keep run time bounded. Skipped on hosts without GPU."""

    async def test_encode_batch_returns_correct_shape(
        self, clean_settings, reset_emb_module,
    ):
        clean_settings.bridge_embedding_model = "facebook/dinov2-small"
        clean_settings.bridge_embedding_device = "cuda"
        clean_settings.bridge_embedding_dtype = "fp16"

        # Two synthetic 256x256 RGB images.
        images = [
            Image.new("RGB", (256, 256), color=(255, 0, 0)),
            Image.new("RGB", (256, 256), color=(0, 0, 255)),
        ]
        arr = await emb.encode_batch_async(images)
        assert arr.shape[0] == 2
        assert arr.shape[1] == 384   # dinov2-small dim
        assert arr.dtype == np.float16

    async def test_encode_bytes_decodes_jpeg(
        self, clean_settings, synth_image, reset_emb_module,
    ):
        clean_settings.bridge_embedding_model = "facebook/dinov2-small"
        clean_settings.bridge_embedding_device = "cuda"

        png_bytes = synth_image(seed=42)
        vec = await emb.encode_bytes_async(png_bytes)
        assert vec is not None
        assert vec.shape == (384,)
        assert vec.dtype == np.float16

    async def test_encode_bytes_returns_none_on_garbage(
        self, clean_settings, reset_emb_module,
    ):
        clean_settings.bridge_embedding_model = "facebook/dinov2-small"
        clean_settings.bridge_embedding_device = "cuda"

        vec = await emb.encode_bytes_async(b"this is not an image")
        assert vec is None
