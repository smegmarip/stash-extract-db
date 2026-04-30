"""Unit tests for channels B (color histogram) and C (low-res tone).

Pure compute against synthetic PIL images; no DB or HTTP required.
Ports the inline heredoc tests run during Phase 5.1 implementation.
See bridge/app/matching/imgmatch/channels.py and §3.1 / §3.6 / §3.7.
"""
import io

import numpy as np
import pytest
from PIL import Image

from bridge.app.matching.imgmatch.channels import (
    COLOR_HIST_BINS,
    TONE_SIZE,
    aggregate_color_hist,
    color_hist_from_bytes,
    color_hist_similarity,
    compute_color_hist,
    compute_tone,
    tone_from_bytes,
    tone_similarity,
    _color_hist_quality,
)


@pytest.fixture
def synth_pil(synth_image):
    """Return a callable: seed -> PIL.Image (RGB)."""
    def _make(seed: int, size: int = 256) -> Image.Image:
        return Image.open(io.BytesIO(synth_image(seed, size)))
    return _make


# --- Channel B: color histogram ------------------------------------------

class TestColorHistogram:
    def test_blob_shape_and_dtype(self, synth_pil):
        blob, _q = compute_color_hist(synth_pil(1))
        n_bins = COLOR_HIST_BINS[0] * COLOR_HIST_BINS[1] * COLOR_HIST_BINS[2]
        assert blob.shape == (n_bins,)
        assert blob.dtype == np.uint8

    def test_normalized_sum_is_255(self, synth_pil):
        """Normalization rescales bin counts so max bin = 255 (the
        encoding for uint8). Sum is bounded by n_bins * 255 = 16320."""
        blob, _q = compute_color_hist(synth_pil(1))
        assert 0 < blob.sum() <= 255 * blob.size

    def test_quality_in_range(self, synth_pil):
        _b, q = compute_color_hist(synth_pil(1))
        assert 0.0 <= q <= 1.0

    def test_monochromatic_image_low_quality(self):
        """Black image → all mass in one bin → high gini → q ≈ 0."""
        black = Image.new("RGB", (256, 256), (0, 0, 0))
        _b, q = compute_color_hist(black)
        assert q < 0.1

    def test_self_similarity_equals_one(self, synth_pil):
        blob, _ = compute_color_hist(synth_pil(1))
        assert color_hist_similarity(blob, blob) == pytest.approx(1.0)

    def test_different_images_lower_similarity(self, synth_pil):
        a, _ = compute_color_hist(synth_pil(1))
        b, _ = compute_color_hist(synth_pil(2))
        s_self = color_hist_similarity(a, a)
        s_diff = color_hist_similarity(a, b)
        assert s_diff < s_self

    def test_similarity_zero_for_disjoint(self):
        """Two histograms with no bin overlap → intersection 0."""
        a = np.zeros(64, dtype=np.uint8); a[0] = 255
        b = np.zeros(64, dtype=np.uint8); b[63] = 255
        assert color_hist_similarity(a, b) == 0.0

    def test_similarity_handles_empty(self):
        empty = np.zeros(64, dtype=np.uint8)
        nonempty = np.zeros(64, dtype=np.uint8); nonempty[5] = 100
        assert color_hist_similarity(empty, nonempty) == 0.0
        assert color_hist_similarity(nonempty, empty) == 0.0
        assert color_hist_similarity(None, nonempty) == 0.0

    def test_aggregate_per_bin_median(self, synth_pil):
        """Per-bin median across 5 frames; result is uint8 length 64."""
        hists = [compute_color_hist(synth_pil(i))[0] for i in range(5)]
        agg = aggregate_color_hist(hists)
        assert agg is not None
        assert agg.shape == (64,)
        assert agg.dtype == np.uint8

    def test_aggregate_empty_returns_none(self):
        assert aggregate_color_hist([]) is None

    def test_aggregate_robust_to_outlier_frames(self):
        """One spike-bin outlier among 4 normal frames; per-bin median
        ignores the outlier."""
        normal = np.full(64, 4, dtype=np.uint8)        # uniform-ish
        outlier = np.zeros(64, dtype=np.uint8); outlier[0] = 255
        agg = aggregate_color_hist([normal, normal, normal, normal, outlier])
        # The outlier's spike bin should not dominate; agg should still be
        # close to the normal-frame shape.
        assert agg[0] < 200  # not pulled to the outlier extreme

    def test_quality_uniform_distribution_high(self):
        """A perfectly uniform histogram has gini=0 → q=1."""
        uniform = np.full(64, 1 / 64.0)
        assert _color_hist_quality(uniform) == pytest.approx(1.0, abs=0.01)

    def test_quality_single_spike_low(self):
        """All mass in one bin → gini ≈ 1 → q ≈ 0."""
        spike = np.zeros(64); spike[0] = 1.0
        assert _color_hist_quality(spike) < 0.05

    def test_color_hist_from_bytes_round_trip(self, synth_image):
        result = color_hist_from_bytes(synth_image(1))
        assert result is not None
        blob, q = result
        assert blob.shape == (64,)
        assert 0.0 <= q <= 1.0

    def test_color_hist_from_bytes_invalid(self):
        assert color_hist_from_bytes(b"not an image") is None


# --- Channel C: low-res tone ---------------------------------------------

class TestTone:
    def test_blob_shape_and_dtype(self, synth_pil):
        blob, _q = compute_tone(synth_pil(1))
        assert blob.shape == (TONE_SIZE[0] * TONE_SIZE[1],)
        assert blob.dtype == np.uint8

    def test_quality_in_range(self, synth_pil):
        _b, q = compute_tone(synth_pil(1))
        assert 0.0 <= q <= 1.0

    def test_self_similarity_equals_one(self, synth_pil):
        blob, _ = compute_tone(synth_pil(1))
        assert tone_similarity(blob, blob) == 1.0

    def test_different_images_lower_similarity(self, synth_pil):
        a, _ = compute_tone(synth_pil(1))
        b, _ = compute_tone(synth_pil(2))
        s_self = tone_similarity(a, a)
        s_diff = tone_similarity(a, b)
        assert s_diff < s_self

    def test_similarity_max_difference(self):
        """All-zero vs all-255 → mean L1 = 1 → similarity = 0."""
        a = np.zeros(64, dtype=np.uint8)
        b = np.full(64, 255, dtype=np.uint8)
        assert tone_similarity(a, b) == 0.0

    def test_similarity_handles_shape_mismatch(self):
        a = np.zeros(64, dtype=np.uint8)
        b = np.zeros(32, dtype=np.uint8)
        assert tone_similarity(a, b) == 0.0

    def test_similarity_handles_none(self):
        a = np.zeros(64, dtype=np.uint8)
        assert tone_similarity(None, a) == 0.0
        assert tone_similarity(a, None) == 0.0

    def test_tone_from_bytes_round_trip(self, synth_image):
        result = tone_from_bytes(synth_image(1))
        assert result is not None
        blob, q = result
        assert blob.shape == (64,)

    def test_tone_from_bytes_invalid(self):
        assert tone_from_bytes(b"not an image") is None
