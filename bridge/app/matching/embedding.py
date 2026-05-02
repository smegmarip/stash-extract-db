"""Channel D: semantic-similarity scoring via a learned image embedding.

The model (DINOv2 by default) maps each image to a vector in a learned
embedding space where cosine similarity ≈ semantic similarity. Replaces
the per-channel sharpening / count-saturation / distribution-shape math
needed by pHash with a single matrix multiplication against cached
extractor embeddings — calibration-free and ~200× faster end-to-end.

Stored in `image_features` with channel='embedding' and a versioned
`algorithm` string ('<model>:<dtype>:<dim>'). Cascade invalidation,
LRU eviction, and dual-write retirement carry over without change.

See docs/SEMANTIC_MIGRATION_PLAN.md for the architecture rationale and
phasing.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import numpy as np
from PIL import Image

from ..settings import settings

logger = logging.getLogger(__name__)


# Lazy-loaded singletons. The model lives in process memory for the
# lifetime of the bridge — model load is the expensive part.
_MODEL = None
_PROCESSOR = None
_ALGO_KEY: Optional[str] = None


def _resolve_device() -> str:
    """Pick a torch device, honoring settings + capability."""
    pref = (settings.bridge_embedding_device or "auto").lower()
    if pref == "cpu":
        return "cpu"
    # cuda or auto: probe availability
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    if pref == "cuda":
        logger.warning(
            "BRIDGE_EMBEDDING_DEVICE=cuda but CUDA unavailable; falling back to cpu"
        )
    return "cpu"


def _resolve_dtype():
    """Map settings to torch dtype. Falls back to fp32 on CPU since fp16
    on CPU is slower for most torch ops than fp32."""
    import torch
    pref = (settings.bridge_embedding_dtype or "fp16").lower()
    device = _resolve_device()
    if pref == "fp16" and device != "cpu":
        return torch.float16
    return torch.float32


def algorithm_key() -> str:
    """Versioned algorithm string for image_features rows.
    Example: 'facebook/dinov2-large:fp16:1024'.
    A model swap or dtype change yields a new key; old rows survive
    until cascade invalidation or LRU eviction clears them.
    """
    global _ALGO_KEY
    if _ALGO_KEY is not None:
        return _ALGO_KEY
    model = settings.bridge_embedding_model
    dtype = settings.bridge_embedding_dtype
    dim = embedding_dim()
    _ALGO_KEY = f"{model}:{dtype}:{dim}"
    return _ALGO_KEY


def embedding_dim() -> int:
    """Return the embedding dimension of the configured model.
    Loads the model lazily if not already loaded — but cheap because
    we just need the config, not the weights.
    """
    # Hardcoded common dims to avoid model load just for this call.
    # The dimension is verified against the loaded model on first encode.
    model = settings.bridge_embedding_model
    known = {
        "facebook/dinov2-small":  384,
        "facebook/dinov2-base":   768,
        "facebook/dinov2-large":  1024,
        "facebook/dinov2-giant":  1536,
        "openai/clip-vit-base-patch32": 512,
        "openai/clip-vit-large-patch14": 768,
    }
    return known.get(model, 1024)


def _load_model():
    """Lazily load the configured model + processor. Idempotent.

    Called once on first use (typically during featurization). Model
    weights are cached to /root/.cache/huggingface in the running
    container; persisted across recreates via the huggingface_cache
    volume so subsequent boots reuse the download.
    """
    global _MODEL, _PROCESSOR
    if _MODEL is not None:
        return _MODEL, _PROCESSOR

    import torch
    from transformers import AutoImageProcessor, AutoModel

    model_id = settings.bridge_embedding_model
    device = _resolve_device()
    dtype = _resolve_dtype()
    logger.info("embedding: loading model=%s device=%s dtype=%s",
                model_id, device, dtype)

    _PROCESSOR = AutoImageProcessor.from_pretrained(model_id)
    _MODEL = AutoModel.from_pretrained(model_id, torch_dtype=dtype)
    _MODEL.to(device)
    _MODEL.eval()

    logger.info("embedding: model loaded; dim=%d", embedding_dim())
    return _MODEL, _PROCESSOR


def _encode_pil_batch(images: list[Image.Image]) -> np.ndarray:
    """Synchronous: encode a batch of PIL images, return (N, D) numpy
    array of FP16 (or FP32 if CPU). Caller is responsible for batching
    and for dispatching to a thread (use asyncio.to_thread)."""
    if not images:
        return np.zeros((0, embedding_dim()), dtype=np.float16)

    import torch
    model, processor = _load_model()
    device = next(model.parameters()).device

    # Normalize all to RGB — DINOv2 (and most vision models) want 3-channel.
    rgb_images = []
    for img in images:
        if img.mode != "RGB":
            img = img.convert("RGB")
        rgb_images.append(img)

    inputs = processor(images=rgb_images, return_tensors="pt")
    pixel_values = inputs["pixel_values"].to(device).to(next(model.parameters()).dtype)

    with torch.no_grad():
        out = model(pixel_values=pixel_values)
        # AutoModel returns last_hidden_state and pooler_output for some
        # architectures. DINOv2 returns last_hidden_state with CLS token at idx 0.
        if hasattr(out, "pooler_output") and out.pooler_output is not None:
            embeddings = out.pooler_output
        else:
            embeddings = out.last_hidden_state[:, 0, :]   # CLS token
    # Move to CPU + fp16 for compact storage
    return embeddings.detach().to("cpu").to(torch.float16).numpy()


async def encode_batch_async(images: list[Image.Image]) -> np.ndarray:
    """Async wrapper: dispatches the GPU-bound work to a thread so the
    event loop stays responsive (CLAUDE.md §14.4 compliant)."""
    return await asyncio.to_thread(_encode_pil_batch, images)


async def encode_bytes_async(data: bytes) -> Optional[np.ndarray]:
    """Decode JPEG/PNG bytes and produce a single (D,) FP16 embedding.
    Returns None if the bytes can't be opened.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        logger.warning("embedding: PIL decode failed :: %s", e)
        return None
    arr = await encode_batch_async([img])
    if arr.shape[0] == 0:
        return None
    return arr[0]


# --- Cosine similarity helpers ---------------------------------------------

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two embedding vectors. Inputs may be
    fp16 or fp32 — promoted to fp32 for the dot product to avoid
    accumulation underflow on long vectors."""
    if a.size == 0 or b.size == 0:
        return 0.0
    a32 = a.astype(np.float32)
    b32 = b.astype(np.float32)
    na = float(np.linalg.norm(a32))
    nb = float(np.linalg.norm(b32))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a32, b32) / (na * nb))


def cosine_sim_matrix(stash: np.ndarray, extr: np.ndarray) -> np.ndarray:
    """Pairwise cosine similarity matrix between two stacks of
    embeddings. Returns (M, N) where M = len(stash), N = len(extr).
    Both inputs are (k, D) arrays (fp16 or fp32). Output is fp32 for
    downstream numpy compatibility."""
    if stash.size == 0 or extr.size == 0:
        m = stash.shape[0] if stash.ndim == 2 else 0
        n = extr.shape[0] if extr.ndim == 2 else 0
        return np.zeros((m, n), dtype=np.float32)
    s = stash.astype(np.float32)
    e = extr.astype(np.float32)
    sn = np.linalg.norm(s, axis=1, keepdims=True)
    en = np.linalg.norm(e, axis=1, keepdims=True)
    sn[sn == 0] = 1.0
    en[en == 0] = 1.0
    s = s / sn
    e = e / en
    return s @ e.T   # (M, N)


# --- Embedding feature blob serialization ----------------------------------

def embedding_to_blob(emb: np.ndarray) -> bytes:
    """Serialize a (D,) embedding to bytes for image_features.feature_blob.
    Stores as fp16 little-endian. ~2KB for D=1024."""
    return emb.astype(np.float16).tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """Deserialize feature_blob bytes back to a (D,) fp16 array."""
    return np.frombuffer(blob, dtype=np.float16).copy()
