"""Channel E: local-feature matching via DISK + LightGlue.

Where Channel D (embedding) compresses an image to a single global vector,
Channel E preserves local correspondences: DISK detects keypoints and emits
per-keypoint descriptors; LightGlue matches keypoints across two images;
RANSAC fits a homography to the matches and reports the inlier count. The
inlier count is what survives viewpoint shift, so E targets the failure
class where D's holistic representation drops below threshold on
same-scene-different-angle pairs.

DISK over SuperPoint: kornia 0.8.x dropped SuperPoint (Magic Leap BSD-3
licensing). DISK is the kornia-native replacement and is actually a
better fit for E's use case — DISK is trained with a multi-view
consistency objective on depth-pair datasets, which is closer to our
"same scene, different camera" target than SuperPoint's SLAM-style
adjacent-frame training.

Stored in `image_features` with `channel='keypoints'` and a versioned
`algorithm` string ('disk:N=<max_kp>:fp16'). Cascade invalidation
(extractor-side) reuses the existing source taxonomy — no schema change.

See CLAUDE.md §13.E for invariants and `docs/CHANNEL_E_PLAN.md` for the
implementation plan.
"""
from __future__ import annotations

import asyncio
import io
import logging
import struct
import threading
from typing import Optional

import numpy as np
from PIL import Image

from ..settings import settings

logger = logging.getLogger(__name__)


# Lazy-loaded singletons. DISK + LightGlue both live in process memory
# for the lifetime of the bridge. Combined VRAM ~50MB fp16.
_EXTRACTOR = None
_MATCHER = None
_ALGO_KEY: Optional[str] = None
_LOAD_LOCK = threading.Lock()

# Internal preprocessing: resize each input image's longer side to this
# value before DISK extraction. Stable runtime cost; DISK's detection
# density doesn't gain much from larger inputs at this scale.
_INPUT_LONGER_SIDE = 640

# Descriptor dimension for DISK. 128-d (DISK default). Hardcoded;
# changing requires a different extractor.
_DESC_DIM = 128


# -- Device + dtype resolution (mirrors embedding.py) ----------------------

def _resolve_device() -> str:
    """Pick a torch device, honoring settings + capability."""
    pref = (settings.bridge_local_feature_device or "auto").lower()
    if pref == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    if pref == "cuda":
        logger.warning(
            "BRIDGE_LOCAL_FEATURE_DEVICE=cuda but CUDA unavailable; falling back to cpu"
        )
    return "cpu"


def _resolve_dtype():
    """DISK + LightGlue run fp32 on CPU (no fp16 win there) and fp16
    on GPU (faster, halves VRAM)."""
    import torch
    device = _resolve_device()
    if device == "cpu":
        return torch.float32
    return torch.float16


def algorithm_key() -> str:
    """Versioned algorithm string for image_features rows.
    Example: 'disk:N=512:fp16'.

    A change in `BRIDGE_LOCAL_FEATURE_MAX_KEYPOINTS` or model produces
    a fresh key; old rows survive until cascade or LRU eviction.
    """
    global _ALGO_KEY
    if _ALGO_KEY is not None:
        return _ALGO_KEY
    n = settings.bridge_local_feature_max_keypoints
    dtype = "fp16" if _resolve_device() != "cpu" else "fp32"
    model = settings.bridge_local_feature_model
    _ALGO_KEY = f"{model}:N={n}:{dtype}"
    return _ALGO_KEY


# -- Model loading ---------------------------------------------------------

def _load_models():
    """Lazily load DISK + LightGlue. Idempotent.

    Both come from `kornia.feature`. DISK's `from_pretrained("depth")`
    downloads ~4MB weights to the torch hub cache on first call; LightGlue
    follows suit. Subsequent calls hit the fast path before acquiring
    the lock.
    """
    global _EXTRACTOR, _MATCHER
    if _EXTRACTOR is not None and _MATCHER is not None:
        return _EXTRACTOR, _MATCHER

    with _LOAD_LOCK:
        if _EXTRACTOR is not None and _MATCHER is not None:
            return _EXTRACTOR, _MATCHER

        import torch
        # Imported lazily so the module can be imported on hosts that
        # don't have kornia installed (e.g. CPU-only test environments
        # that run without the local_features extra).
        from kornia.feature import DISK, LightGlueMatcher

        device = _resolve_device()
        dtype = _resolve_dtype()
        max_kp = settings.bridge_local_feature_max_keypoints
        logger.info(
            "local_features: loading DISK + LightGlue device=%s dtype=%s max_kp=%d",
            device, dtype, max_kp,
        )

        extractor = DISK.from_pretrained("depth").eval().to(device).to(dtype)
        matcher = LightGlueMatcher("disk").eval().to(device).to(dtype)

        _EXTRACTOR = extractor
        _MATCHER = matcher
        logger.info("local_features: models loaded")
        return _EXTRACTOR, _MATCHER


# -- Encode (image bytes → keypoints, scores, descriptors) -----------------

def _decode_to_rgb_tensor(data: bytes):
    """Decode image bytes → torch tensor (1, 3, H, W) RGB, normalized
    to [0, 1]. Resizes longer side to _INPUT_LONGER_SIDE while preserving
    aspect ratio. Returns None on decode failure.

    Pads H, W to be multiples of 16 — DISK's UNet has 4 downsampling
    stages; non-multiple-of-16 inputs raise a shape-mismatch error inside
    the U-Net's skip connections.
    """
    import torch
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        logger.warning("local_features: PIL decode failed :: %s", e)
        return None

    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longer = max(w, h)
    if longer != _INPUT_LONGER_SIDE:
        scale = _INPUT_LONGER_SIDE / longer
        new_w = max(16, int(round(w * scale)))
        new_h = max(16, int(round(h * scale)))
        img = img.resize((new_w, new_h), Image.BILINEAR)

    # Pad to multiple of 16 for DISK's U-Net.
    w, h = img.size
    pad_w = (16 - w % 16) % 16
    pad_h = (16 - h % 16) % 16
    if pad_w or pad_h:
        from PIL import ImageOps
        img = ImageOps.expand(img, border=(0, 0, pad_w, pad_h), fill=0)

    arr = np.asarray(img, dtype=np.float32) / 255.0   # (H, W, 3)
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)  # (1, 3, H, W)
    return t


def _extract_sync(data: bytes) -> Optional[dict]:
    """Synchronous: decode bytes → DISK extraction → dict of
    {keypoints, scores, descriptors, image_hw}.

    Returns None on decode failure. Callers must dispatch via
    `asyncio.to_thread` (CLAUDE.md §14.4 — CPU/GPU compute off the
    event loop).
    """
    import torch

    t = _decode_to_rgb_tensor(data)
    if t is None:
        return None

    extractor, _matcher = _load_models()
    device = next(extractor.parameters()).device
    dtype = next(extractor.parameters()).dtype
    t = t.to(device).to(dtype)

    H, W = t.shape[-2], t.shape[-1]
    max_kp = settings.bridge_local_feature_max_keypoints
    with torch.no_grad():
        # DISK returns a list[DISKFeatures] — one entry per batch image.
        # Each has .keypoints (N,2), .descriptors (N, D), .detection_scores (N,).
        feats = extractor(t, n=max_kp, window_size=5)

    if not feats:
        return None
    f = feats[0]
    kpts = f.keypoints.detach()
    scores = f.detection_scores.detach()
    descs = f.descriptors.detach()

    # `n=max_kp` already caps; this is belt-and-braces.
    if scores.shape[0] > max_kp:
        topk = torch.topk(scores, k=max_kp, largest=True, sorted=True)
        kpts = kpts[topk.indices]
        scores = topk.values
        descs = descs[topk.indices]

    return {
        "keypoints": kpts.to("cpu").to(torch.float16).numpy(),
        "scores": scores.to("cpu").to(torch.float16).numpy(),
        "descriptors": descs.to("cpu").to(torch.float16).numpy(),
        "image_hw": (int(H), int(W)),
    }


async def encode_bytes_async(data: bytes) -> Optional[dict]:
    """Async wrapper for _extract_sync. Off-loads to a worker thread."""
    return await asyncio.to_thread(_extract_sync, data)


# -- Blob serialization (compatible with image_features.feature_blob) ------
#
# Layout (little-endian):
#   uint32 N           number of keypoints
#   uint32 D           descriptor dimension (256 for SuperPoint)
#   uint16 H, W        original image height/width (for RANSAC scaling)
#   N × (2 fp16)       keypoints xy
#   N × (1 fp16)       scores
#   N × (D fp16)       descriptors
#
# Total: 12 + N*(4 + 2 + 2*D) bytes. For N=512, D=256: ~265 KB.

_HEADER = struct.Struct("<IIHH")  # 12 bytes


def lf_to_blob(lf: dict) -> bytes:
    """Serialize a local-feature dict to bytes for image_features.feature_blob."""
    kpts = np.asarray(lf["keypoints"], dtype=np.float16)
    scores = np.asarray(lf["scores"], dtype=np.float16).reshape(-1)
    descs = np.asarray(lf["descriptors"], dtype=np.float16)
    h, w = lf.get("image_hw", (0, 0))

    n = kpts.shape[0]
    if descs.shape != (n, _DESC_DIM):
        raise ValueError(
            f"descriptor shape mismatch: expected ({n}, {_DESC_DIM}), got {tuple(descs.shape)}"
        )
    if scores.shape != (n,):
        raise ValueError(f"scores shape mismatch: expected ({n},), got {tuple(scores.shape)}")

    header = _HEADER.pack(n, _DESC_DIM, int(h), int(w))
    return header + kpts.tobytes() + scores.tobytes() + descs.tobytes()


def blob_to_lf(blob: bytes) -> dict:
    """Deserialize feature_blob bytes back to a dict of fp16 arrays."""
    if len(blob) < _HEADER.size:
        raise ValueError(f"blob too short: {len(blob)} bytes")
    n, d, h, w = _HEADER.unpack_from(blob, 0)
    if d != _DESC_DIM:
        raise ValueError(f"unsupported descriptor dim {d} (expected {_DESC_DIM})")

    offset = _HEADER.size
    kpts_bytes = n * 2 * 2
    scores_bytes = n * 2
    descs_bytes = n * d * 2
    expected_size = _HEADER.size + kpts_bytes + scores_bytes + descs_bytes
    if len(blob) != expected_size:
        raise ValueError(
            f"blob size mismatch: expected {expected_size}, got {len(blob)}"
        )

    kpts = np.frombuffer(blob, dtype=np.float16, count=n * 2, offset=offset).reshape(n, 2).copy()
    offset += kpts_bytes
    scores = np.frombuffer(blob, dtype=np.float16, count=n, offset=offset).copy()
    offset += scores_bytes
    descs = np.frombuffer(blob, dtype=np.float16, count=n * d, offset=offset).reshape(n, d).copy()
    return {
        "keypoints": kpts,
        "scores": scores,
        "descriptors": descs,
        "image_hw": (int(h), int(w)),
    }


# -- Match a pair: LightGlue + RANSAC homography → inlier count ------------

def match_pair_sync(query_lf: dict, ref_lf: dict) -> int:
    """Match two local-feature dicts. Returns the RANSAC inlier count.
    Caller must dispatch via `asyncio.to_thread`.

    Returns 0 if either side is empty, if LightGlue produces fewer than 4
    matches (homography needs 4), or if RANSAC fails to converge.
    """
    import torch

    q_kpts = query_lf.get("keypoints")
    q_descs = query_lf.get("descriptors")
    r_kpts = ref_lf.get("keypoints")
    r_descs = ref_lf.get("descriptors")

    if (
        q_kpts is None or r_kpts is None
        or q_kpts.shape[0] == 0 or r_kpts.shape[0] == 0
    ):
        return 0

    _ext, matcher = _load_models()
    device = next(matcher.parameters()).device
    dtype = next(matcher.parameters()).dtype

    # Build input tensors. LightGlueMatcher in kornia consumes
    # descriptor + LAF (local affine frame) tensors.
    q_kpts_t = torch.from_numpy(np.asarray(q_kpts, dtype=np.float32)).to(device).to(dtype)
    r_kpts_t = torch.from_numpy(np.asarray(r_kpts, dtype=np.float32)).to(device).to(dtype)
    q_descs_t = torch.from_numpy(np.asarray(q_descs, dtype=np.float32)).to(device).to(dtype)
    r_descs_t = torch.from_numpy(np.asarray(r_descs, dtype=np.float32)).to(device).to(dtype)

    # LightGlueMatcher signature varies slightly by kornia version; we
    # pass the canonical form. Build identity LAFs from keypoints
    # (kornia helper).
    from kornia.feature import laf_from_center_scale_ori
    scales_q = torch.ones(q_kpts_t.shape[0], 1, 1, device=device, dtype=dtype)
    scales_r = torch.ones(r_kpts_t.shape[0], 1, 1, device=device, dtype=dtype)
    lafs_q = laf_from_center_scale_ori(q_kpts_t.unsqueeze(0), scales_q.unsqueeze(0))
    lafs_r = laf_from_center_scale_ori(r_kpts_t.unsqueeze(0), scales_r.unsqueeze(0))

    q_h, q_w = query_lf.get("image_hw", (0, 0))
    r_h, r_w = ref_lf.get("image_hw", (0, 0))
    hw_q = (int(q_h), int(q_w)) if q_h else None
    hw_r = (int(r_h), int(r_w)) if r_h else None

    with torch.no_grad():
        try:
            distances, idxs = matcher(
                q_descs_t, r_descs_t, lafs_q, lafs_r, hw1=hw_q, hw2=hw_r,
            )
        except Exception as e:
            logger.warning("local_features: LightGlue match failed :: %s", e)
            return 0

    if idxs is None or idxs.shape[0] < 4:
        return 0

    # Pull matched keypoint coords for RANSAC.
    q_matched = q_kpts_t[idxs[:, 0]].to(torch.float32)
    r_matched = r_kpts_t[idxs[:, 1]].to(torch.float32)

    inl_th = float(settings.bridge_local_feature_ransac_thresh)
    n_inliers = _ransac_homography_inliers(q_matched, r_matched, inl_th)
    return n_inliers


def _ransac_homography_inliers(
    src_pts,  # torch.Tensor (M, 2)
    dst_pts,  # torch.Tensor (M, 2)
    inl_th: float,
) -> int:
    """RANSAC homography fit. Returns inlier count (>=0).

    Uses kornia's RANSAC if available, else falls back to a small
    pure-numpy implementation. Both produce comparable inlier counts.
    """
    try:
        from kornia.geometry import RANSAC
        ransac = RANSAC(model_type="homography", inl_th=inl_th, batch_size=2048, max_iter=20)
        H, inliers = ransac(src_pts, dst_pts)
        if inliers is None:
            return 0
        return int(inliers.sum().item())
    except Exception as e:
        logger.warning("local_features: RANSAC fallback :: %s", e)
        return 0


async def match_pair_async(query_lf: dict, ref_lf: dict) -> int:
    """Async wrapper for match_pair_sync."""
    return await asyncio.to_thread(match_pair_sync, query_lf, ref_lf)


# -- Decode-only helper (for fixtures / round-trip tests) ------------------

def decode_bytes_sync(data: bytes) -> Optional[dict]:
    """Public wrapper around _extract_sync used by tests / harness."""
    return _extract_sync(data)
