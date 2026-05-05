"""Animation-aware image decode (CLAUDE.md §13.7/§13.8).

Both sides of the bridge can receive animated assets:
  - Stash `paths.screenshot` may be an animated GIF / WebP / APNG.
  - Extractor `cover_image` / `images[]` frequently include animated previews.

Today, `Image.open(...).convert('RGB')` silently degrades animated assets to
frame 0. This module replaces that with explicit detection + handling:

  - Stash-cover side: collapse to a single representative frame (temporal
    middle by default; per-pixel median as an alternative). Keeps the
    "one cover comparison = one cover feature" invariant.
  - Extractor side: uniform temporal sampling — N frames at evenly-spaced
    indices across the GIF/WebP. Each frame becomes its own synthetic ref
    `<ref>#frame_N`. Scene-detect was tried and failed: animated assets in
    real corpora are typically action loops with no scene cuts, so
    scene-change-threshold gating collapsed nearly every asset to a single
    frame. Uniform sampling is the right algorithm for non-scenic content.
    Image-level dedup is already handled corpus-wide by the A/B/C tier's
    `c_i` factor.

Format allowlist is explicit — only PIL formats {GIF, WEBP, PNG, JPEG} are
accepted. Anything else (HEIF/AVIF sequences, anything PIL can't decode)
returns None and is treated as §13.8 rule 1 fetch failure (drop the ref).

All functions in this module are sync compute. Callers MUST wrap them in
`asyncio.to_thread` per CLAUDE.md §14.4.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# Pillow `format` strings we accept. HEIF / AVIF deliberately excluded
# (CLAUDE.md §13.7/§13.8 — hard skip). The check is positive-by-allowlist
# rather than negative-by-error so behavior is deterministic regardless
# of which Pillow plugins are loaded in the running container.
SUPPORTED_FORMATS = frozenset({"GIF", "WEBP", "PNG", "JPEG"})


def _is_animated(img: Image.Image) -> bool:
    """True iff Pillow reports more than one frame. Robust across formats:
    GIF / animated WebP / APNG all expose `is_animated` + `n_frames`;
    static formats either lack the attribute or report n_frames == 1."""
    if not getattr(img, "is_animated", False):
        return False
    return getattr(img, "n_frames", 1) > 1


def _open_supported(data: bytes) -> Optional[Image.Image]:
    """Open bytes through PIL and enforce the format allowlist.

    Returns the (un-converted) PIL Image on success, or None when:
      - PIL can't decode the bytes (UnidentifiedImageError or other).
      - PIL decoded the image but its format is outside the allowlist
        (e.g. HEIF or AVIF picked up via an installed plugin).
    """
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as e:
        logger.debug("frames: PIL open failed :: %s", e)
        return None
    fmt = getattr(img, "format", None)
    if fmt not in SUPPORTED_FORMATS:
        logger.debug("frames: skipping unsupported format=%s", fmt)
        return None
    return img


def load_image_frames(data: bytes, *, max_frames: int = 0) -> Optional[list[Image.Image]]:
    """Return RGB frames for `data`. Length 1 for static; up to `max_frames`
    for animated (0 means "no cap — return every frame").

    Returns None on unsupported format or decode failure — callers treat
    this identically to a §13.8 rule 1 fetch failure.

    The single source of truth for "is this animated and how many frames"
    on the Stash-cover side.
    """
    img = _open_supported(data)
    if img is None:
        return None

    if not _is_animated(img):
        try:
            return [img.convert("RGB")]
        except Exception as e:
            logger.debug("frames: static convert failed :: %s", e)
            return None

    n_total = getattr(img, "n_frames", 1)
    cap = n_total if max_frames <= 0 else min(max_frames, n_total)
    out: list[Image.Image] = []
    for i in range(cap):
        try:
            img.seek(i)
            out.append(img.convert("RGB"))
        except Exception as e:
            logger.debug("frames: seek/convert frame %d failed :: %s", i, e)
            continue
    if not out:
        return None
    return out


def collapse_stash_cover(
    data: bytes, *, mode: str = "middle"
) -> Optional[bytes]:
    """Collapse an animated Stash cover into a single static JPEG.

    Mode `middle` picks the temporal middle frame (one real frame from
    the loop). Mode `median` computes a per-pixel median across all
    usable frames — robust to outlier frames but ghosts on motion.

    Returns the JPEG bytes ready to be fed back into the existing
    decode/feature path, OR the original bytes if the input is already
    static (no copy avoided — caller decides whether to re-encode), OR
    None if decode fails.
    """
    img = _open_supported(data)
    if img is None:
        return None

    if not _is_animated(img):
        # Static — caller can use the original bytes as-is.
        return data

    frames = load_image_frames(data, max_frames=0)
    if not frames:
        return None

    if mode == "median":
        chosen = _per_pixel_median(frames)
    else:
        chosen = frames[len(frames) // 2]

    buf = io.BytesIO()
    chosen.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _per_pixel_median(frames: list[Image.Image]) -> Image.Image:
    """Per-pixel median across same-size RGB frames. Cheap; no ffmpeg.

    All frames are resized to the first frame's dimensions before stacking
    — animated formats are usually fixed-size loops so this is a no-op,
    but the resize keeps the function total over weird inputs.
    """
    base_size = frames[0].size
    arrs = []
    for f in frames:
        if f.size != base_size:
            f = f.resize(base_size, Image.LANCZOS)
        arrs.append(np.asarray(f, dtype=np.uint8))
    stacked = np.stack(arrs, axis=0)
    median = np.median(stacked, axis=0).astype(np.uint8)
    return Image.fromarray(median, mode="RGB")


def is_animated_bytes(data: bytes) -> bool:
    """Cheap predicate: are these bytes a supported animated image?
    Returns False for static, unsupported formats, or decode failures."""
    img = _open_supported(data)
    if img is None:
        return False
    return _is_animated(img)


def uniform_sample_frames(
    data: bytes, *, max_frames: int
) -> Optional[list[bytes]]:
    """Return up to `max_frames` JPEG frame bytes sampled at uniform temporal
    indices across an animated asset.

    For an animated input with N frames and `max_frames=K`:
      - If N <= K, return every frame (length N).
      - If N > K, return frames at indices `round(linspace(0, N-1, K))`.

    Static inputs return a single frame (the static content). The caller
    should still check `is_animated_bytes` first to avoid spawning the
    decode path on static images, but this function is total — it works
    for both kinds of input.

    Returns None on unsupported format or decode failure.

    Why uniform and not scene-detect: animated assets in real corpora are
    typically action loops with no scene cuts. Scene-change-threshold
    gating collapsed nearly every asset to a single frame in production
    measurements (109 animated assets → 117 frames total, ~1.07/asset).
    Uniform sampling guarantees N representative frames spanning the asset's
    temporal extent, so the embedding channel sees real diversity.
    """
    img = _open_supported(data)
    if img is None:
        return None

    n_total = getattr(img, "n_frames", 1) if _is_animated(img) else 1
    if n_total <= 1:
        try:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            return [buf.getvalue()]
        except Exception as e:
            logger.debug("uniform_sample_frames: static encode failed :: %s", e)
            return None

    k = max(1, max_frames)
    if n_total <= k:
        indices = list(range(n_total))
    else:
        # linspace endpoints inclusive: 0 .. n_total-1, k samples
        step = (n_total - 1) / (k - 1) if k > 1 else 0
        indices = [round(i * step) for i in range(k)]
        # Dedup in case rounding collides on very short clips
        seen: set[int] = set()
        deduped: list[int] = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                deduped.append(idx)
        indices = deduped

    out: list[bytes] = []
    for idx in indices:
        try:
            img.seek(idx)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            out.append(buf.getvalue())
        except Exception as e:
            logger.debug("uniform_sample_frames: seek/encode frame %d failed :: %s", idx, e)
            continue
    if not out:
        return None
    return out


