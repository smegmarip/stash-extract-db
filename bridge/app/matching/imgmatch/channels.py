"""Channel B (color histogram) and channel C (low-res tone) features.

Per CLAUDE.md §13.1, §13.4, §13.5. All functions are pure
compute on PIL Images / numpy arrays; the bridge fetches bytes via the
stash/extractor clients and passes them in.

Compact storage formats:
- color_hist:hsv:4x4x4 → 64 uint8 bin counts (normalized so max bin = 255)
- tone:gray:8x8       → 64 uint8 luminance values

Aggregate row (per scene / per record):
- Stored as the same blob format as a single-image row.
- Computed by per-bin median across all usable per-image rows.
"""
from __future__ import annotations

import io
from typing import Optional

import numpy as np
from PIL import Image

from .image_comparison import normalize_image


# --- Channel B: HSV color histogram ----------------------------------------

COLOR_HIST_BINS = (4, 4, 4)  # 4×4×4 = 64 bins; matches algorithm string color_hist:hsv:4x4x4


def compute_color_hist(img: Image.Image) -> tuple[np.ndarray, float]:
    """Compute a 4×4×4 HSV histogram of the (full-size) image, normalized
    so the bin counts fit a uint8 (0..255). Returns (hist_64, quality).

    Quality = 1 - gini(bins): a varied palette has near-uniform bin
    counts → low gini → high quality (good for matching). A monochromatic
    image concentrates mass in few bins → high gini → low quality.
    """
    hsv = img.convert("HSV")
    arr = np.asarray(hsv, dtype=np.uint8).reshape(-1, 3)
    bins_h, bins_s, bins_v = COLOR_HIST_BINS
    h_idx = (arr[:, 0].astype(np.uint16) * bins_h // 256).astype(np.uint8)
    s_idx = (arr[:, 1].astype(np.uint16) * bins_s // 256).astype(np.uint8)
    v_idx = (arr[:, 2].astype(np.uint16) * bins_v // 256).astype(np.uint8)
    flat = (h_idx * bins_s * bins_v + s_idx * bins_v + v_idx).astype(np.int64)
    counts = np.bincount(flat, minlength=bins_h * bins_s * bins_v).astype(np.float64)

    # Normalize counts → uint8 [0..255], preserving relative shape.
    total = counts.sum()
    if total <= 0:
        return np.zeros(bins_h * bins_s * bins_v, dtype=np.uint8), 0.0
    norm = counts / total                      # sums to 1.0
    quantized = np.clip(np.round(norm * 255.0), 0, 255).astype(np.uint8)
    quality = _color_hist_quality(norm)
    return quantized, quality


def _color_hist_quality(normalized_hist: np.ndarray) -> float:
    """1 - gini(bins). `normalized_hist` should sum to 1.0."""
    n = normalized_hist.size
    if n == 0:
        return 0.0
    sorted_h = np.sort(normalized_hist)
    cum = np.cumsum(sorted_h)
    if cum[-1] <= 0:
        return 0.0
    # Gini for non-negative values: 1 - 2 * sum((n - i) * x_i) / (n * sum(x))
    # where x is sorted ascending and i is 1-indexed.
    indices = np.arange(1, n + 1, dtype=np.float64)
    gini = 1.0 - 2.0 * np.sum((n + 1 - indices) * sorted_h) / (n * cum[-1])
    return float(max(0.0, min(1.0, 1.0 - gini)))


def color_hist_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Histogram intersection on uint8 blobs.

    For two uint8 blobs encoding the same normalization scheme, the
    intersection sum is bounded by the per-blob total. We normalize each
    blob to sum=1 and compute Σ min(a_i, b_i) ∈ [0, 1]. 1.0 iff identical
    distributions; 0 iff disjoint.
    """
    if a is None or b is None:
        return 0.0
    af = a.astype(np.float64)
    bf = b.astype(np.float64)
    a_sum = af.sum()
    b_sum = bf.sum()
    if a_sum <= 0 or b_sum <= 0:
        return 0.0
    return float(np.minimum(af / a_sum, bf / b_sum).sum())


def aggregate_color_hist(hists: list[np.ndarray]) -> Optional[np.ndarray]:
    """Per-bin median across `hists`, re-quantized to uint8. Returns None
    if input is empty.

    Per-bin (not whole-frame) median is the §3.1 contract — robust to
    outlier frames (black frames, fade transitions) without depending on
    the §13 binary filters being perfect.
    """
    if not hists:
        return None
    # Stack as (n_frames, n_bins) float64. Re-normalize each row to [0, 1]
    # before taking median so the per-bin median is comparing proportions
    # consistently across frames.
    rows = np.stack(hists, axis=0).astype(np.float64)
    sums = rows.sum(axis=1, keepdims=True)
    sums[sums <= 0] = 1.0
    rows = rows / sums                          # each row sums to 1.0
    med = np.median(rows, axis=0)               # length 64
    total = med.sum()
    if total <= 0:
        return np.zeros(rows.shape[1], dtype=np.uint8)
    norm = med / total
    return np.clip(np.round(norm * 255.0), 0, 255).astype(np.uint8)


# --- Channel C: low-res tone -----------------------------------------------

TONE_SIZE = (8, 8)


def compute_tone(img: Image.Image) -> tuple[np.ndarray, float]:
    """Compute an 8×8 grayscale luminance signature. Returns (tone_64, quality).

    The tone blob is the 64 luminance values flattened, uint8.
    Quality reuses the grayscale entropy*variance formula from channel A
    (§3.6) — applied to the *normalized* image (the same one channel A
    hashes), not the 8×8 reduction, since q_i is an intrinsic property of
    the image content, not of any specific channel's compression.
    """
    from .image_comparison import compute_quality

    normalized = normalize_image(img)           # 256×256 grayscale
    quality = compute_quality(normalized)
    tone = normalized.resize(TONE_SIZE, Image.LANCZOS)
    blob = np.asarray(tone, dtype=np.uint8).reshape(-1)
    return blob, quality


def tone_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """1 - mean(|a[i] - b[i]| / 255). Bounded [0, 1]."""
    if a is None or b is None:
        return 0.0
    if a.shape != b.shape or a.size == 0:
        return 0.0
    diff = np.abs(a.astype(np.int32) - b.astype(np.int32))
    return float(1.0 - diff.mean() / 255.0)


# --- Bytes-in entry points (used by image_match.extractor_image_features) ---

def color_hist_from_bytes(data: bytes) -> Optional[tuple[np.ndarray, float]]:
    """Returns (blob, quality) or None if the image can't be opened."""
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return None
    return compute_color_hist(img)


def tone_from_bytes(data: bytes) -> Optional[tuple[np.ndarray, float]]:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return None
    return compute_tone(img)
