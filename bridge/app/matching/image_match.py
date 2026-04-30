"""Image similarity computation between Stash scenes and extractor records.

Cover mode: Stash screenshot vs extractor record images[] (1:N → max).
Sprite mode: Stash sprite frames vs extractor record images[] (M:N → max).
Both: union of both, take max.

Hashes are cached via the SQLite image_hashes table (per CLAUDE.md §7),
keyed by content fingerprint:
  - Stash cover: ?t=<epoch> from screenshot URL.
  - Stash sprite: oshash from files[].fingerprints (one fingerprint per scene).
  - Extractor image: asset URL string (extractor results are versioned by
    completed_at — when that advances we drop the row block, so URL is stable
    within a snapshot).
"""
import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from ..stash import client as stash_client
from ..extractor import client as ex_client
from ..cache import db as cdb
from ..settings import settings
from .imgmatch.image_comparison import (
    hash_image_bytes, hex_to_hash, hash_distance_to_similarity,
)
from .imgmatch.sprite_processor import hash_sprite_frames

logger = logging.getLogger(__name__)


def _screenshot_fingerprint(screenshot_url: str) -> str:
    if not screenshot_url:
        return ""
    qs = parse_qs(urlparse(screenshot_url).query)
    return qs.get("t", [""])[0]


def _scene_oshash(scene: dict[str, Any]) -> str:
    files = scene.get("files") or []
    if not files:
        return ""
    for fp in files[0].get("fingerprints") or []:
        if fp.get("type") == "oshash":
            return fp.get("value") or ""
    return ""


def _phash_algo_key(algorithm: str, hash_size: int) -> str:
    """Combined algorithm string for image_features (e.g. 'phash:8'). The
    legacy image_hashes table keeps algorithm and hash_size as separate
    columns — see CLAUDE.md §15 for the rationale."""
    return f"{algorithm}:{hash_size}"


async def _hash_or_compute(
    source: str, ref_id: str, fingerprint: str,
    algorithm: str, hash_size: int,
    fetcher,
) -> Optional[Any]:
    """fetcher() -> bytes (awaitable). Returns imagehash or None.

    Dual-write Phase 2: reads from image_features first (channel='phash'),
    falls back to legacy image_hashes on miss. On compute, writes to both
    tables. Quality (q_i) is computed and stored on image_features now;
    not yet consulted by scoring (Phase 4).

    None means "no usable hash" for any of the upstream reasons:
      - empty fingerprint (no cache key)
      - fetch failed (404, network error, missing asset)
      - image too low-variance (near-uniform; hash_image_bytes returns None)
      - hashing raised
    Callers must skip None-hash images from the per-image sims list rather
    than treating them as 0.0 comparisons (CLAUDE.md §13).
    """
    if not fingerprint:
        return None
    new_algo = _phash_algo_key(algorithm, hash_size)
    use_legacy = settings.bridge_legacy_dual_write_enabled

    # Read path: image_features first; legacy image_hashes fallback only
    # when dual-write is still active.
    cached_feat = await cdb.get_image_feature(source, ref_id, fingerprint, "phash", new_algo)
    if cached_feat is not None:
        blob, _quality = cached_feat
        return hex_to_hash(blob.hex())
    if use_legacy:
        cached_hex = await cdb.get_image_hash(source, ref_id, fingerprint, algorithm, hash_size)
        if cached_hex:
            return hex_to_hash(cached_hex)

    data = await fetcher()
    if not data:
        return None
    try:
        # Offload PIL/imagehash/numpy compute to a thread so the event
        # loop stays responsive while the bridge is featurizing many
        # images at once.
        result = await asyncio.to_thread(hash_image_bytes, data, algorithm, hash_size)
    except Exception as e:
        logger.warning("hash failed source=%s ref=%s :: %s", source, ref_id, e)
        return None
    if result is None:
        logger.debug("skipping low-variance image source=%s ref=%s", source, ref_id)
        return None
    h, q = result
    h_hex = str(h)
    if use_legacy:
        await cdb.set_image_hash(source, ref_id, fingerprint, algorithm, hash_size, h_hex)
    await cdb.set_image_feature(
        source, ref_id, fingerprint, "phash", new_algo, bytes.fromhex(h_hex), q,
    )
    return h


async def stash_cover_hash(scene: dict[str, Any], algorithm: str, hash_size: int):
    paths = scene.get("paths") or {}
    url = paths.get("screenshot") or ""
    if not url:
        return None
    fingerprint = _screenshot_fingerprint(url) or url  # fallback to whole URL
    return await _hash_or_compute(
        "stash_cover", scene["id"], fingerprint, algorithm, hash_size,
        lambda: stash_client.fetch_image_bytes(url),
    )


async def stash_sprite_hashes(scene: dict[str, Any], algorithm: str, hash_size: int, sample_size: int) -> list:
    """Returns list of imagehash objects for sampled sprite frames.

    Dual-write Phase 2: reads from image_features first (channel='phash'),
    falls back to legacy image_hashes on miss. On compute, writes each frame
    to both tables.
    """
    paths = scene.get("paths") or {}
    sprite_url = paths.get("sprite") or ""
    vtt_url = paths.get("vtt") or ""
    oshash = _scene_oshash(scene)
    if not sprite_url or not vtt_url or not oshash:
        return []

    new_algo = _phash_algo_key(algorithm, hash_size)
    use_legacy = settings.bridge_legacy_dual_write_enabled

    # Try cache per-frame: image_features first; image_hashes fallback only
    # when dual-write is still active.
    out: list = []
    cached_count = 0
    for idx in range(sample_size):
        ref_id = f"{scene['id']}:{idx}"
        feat = await cdb.get_image_feature("stash_sprite", ref_id, oshash, "phash", new_algo)
        if feat is not None:
            blob, _q = feat
            out.append(hex_to_hash(blob.hex()))
            cached_count += 1
            continue
        if use_legacy:
            c = await cdb.get_image_hash("stash_sprite", ref_id, oshash, algorithm, hash_size)
            if c:
                out.append(hex_to_hash(c))
                cached_count += 1
                continue
        out = []  # any miss → recompute the whole thing
        break

    if out and cached_count == sample_size:
        return out

    sprite_bytes = await stash_client.fetch_image_bytes(sprite_url)
    vtt_text = await stash_client.fetch_text(vtt_url)
    if not sprite_bytes or not vtt_text:
        return []

    try:
        # Sprite parsing + per-frame hashing are CPU-bound; run in a
        # thread so the event loop stays responsive.
        results = await asyncio.to_thread(
            hash_sprite_frames, sprite_bytes, vtt_text, sample_size, algorithm, hash_size,
        )
    except Exception as e:
        logger.warning("sprite hash failed for scene %s :: %s", scene.get("id"), e)
        return []

    hashes: list = []
    for idx, (h, q) in enumerate(results):
        ref_id = f"{scene['id']}:{idx}"
        h_hex = str(h)
        if use_legacy:
            await cdb.set_image_hash("stash_sprite", ref_id, oshash, algorithm, hash_size, h_hex)
        await cdb.set_image_feature(
            "stash_sprite", ref_id, oshash, "phash", new_algo, bytes.fromhex(h_hex), q,
        )
        hashes.append(h)
    return hashes


async def extractor_image_hash(job_id: str, ref: str, algorithm: str, hash_size: int):
    """ref is the record's image string (e.g. ../assets/abc.jpg or full URL)."""
    if not ref:
        return None
    full_url = ex_client.resolve_asset_url(job_id, ref) if not ref.startswith("http") else ref
    return await _hash_or_compute(
        "extractor_image", f"{job_id}:{ref}", full_url, algorithm, hash_size,
        lambda: ex_client.fetch_asset(job_id, ref),
    )


# --- Multi-channel per-image features (Phase 5) -----------------------------

# Channel B and C don't have a legacy image_hashes fallback (they're new
# in Phase 5). They write only to image_features.

CHANNEL_B_ALGO = "color_hist:hsv:4x4x4"
CHANNEL_C_ALGO = "tone:gray:8x8"


async def _features_or_compute_bc(
    source: str, ref_id: str, fingerprint: str, fetcher,
) -> dict[str, Optional[tuple[bytes, float]]]:
    """Read or compute channel B and C features for one image. Single fetch
    serves both. Returns {channel: (blob, quality) or None}.

    None values mean "no usable feature" — empty fingerprint, fetch failed,
    or the image couldn't be opened.
    """
    from .imgmatch.channels import color_hist_from_bytes, tone_from_bytes

    out: dict[str, Optional[tuple[bytes, float]]] = {"color_hist": None, "tone": None}
    if not fingerprint:
        return out

    cached_b = await cdb.get_image_feature(source, ref_id, fingerprint, "color_hist", CHANNEL_B_ALGO)
    cached_c = await cdb.get_image_feature(source, ref_id, fingerprint, "tone", CHANNEL_C_ALGO)
    if cached_b is not None:
        out["color_hist"] = (bytes(cached_b[0]), cached_b[1])
    if cached_c is not None:
        out["tone"] = (bytes(cached_c[0]), cached_c[1])
    if out["color_hist"] is not None and out["tone"] is not None:
        return out

    data = await fetcher()
    if not data:
        return out

    if out["color_hist"] is None:
        # PIL decode + HSV histogram is CPU-bound — offload to thread.
        bres = await asyncio.to_thread(color_hist_from_bytes, data)
        if bres is not None:
            blob, q = bres
            blob_bytes = bytes(blob)
            await cdb.set_image_feature(
                source, ref_id, fingerprint, "color_hist", CHANNEL_B_ALGO, blob_bytes, q,
            )
            out["color_hist"] = (blob_bytes, q)
    if out["tone"] is None:
        # Same: PIL decode + LANCZOS resize + entropy/variance is CPU-bound.
        cres = await asyncio.to_thread(tone_from_bytes, data)
        if cres is not None:
            blob, q = cres
            blob_bytes = bytes(blob)
            await cdb.set_image_feature(
                source, ref_id, fingerprint, "tone", CHANNEL_C_ALGO, blob_bytes, q,
            )
            out["tone"] = (blob_bytes, q)
    return out


async def extractor_image_bc_features(
    job_id: str, ref: str,
) -> dict[str, Optional[tuple[bytes, float]]]:
    """Channel B + C features for one extractor image. Caches in
    image_features. Channel A is handled separately by extractor_image_hash
    (which has the legacy image_hashes fallback)."""
    if not ref:
        return {"color_hist": None, "tone": None}
    full_url = ex_client.resolve_asset_url(job_id, ref) if not ref.startswith("http") else ref
    return await _features_or_compute_bc(
        "extractor_image", f"{job_id}:{ref}", full_url,
        lambda: ex_client.fetch_asset(job_id, ref),
    )


async def stash_cover_bc_features(
    scene: dict[str, Any],
) -> dict[str, Optional[tuple[bytes, float]]]:
    paths = scene.get("paths") or {}
    url = paths.get("screenshot") or ""
    if not url:
        return {"color_hist": None, "tone": None}
    fingerprint = _screenshot_fingerprint(url) or url
    return await _features_or_compute_bc(
        "stash_cover", scene["id"], fingerprint,
        lambda: stash_client.fetch_image_bytes(url),
    )


async def stash_sprite_bc_features(
    scene: dict[str, Any], sample_size: int,
) -> list[dict[str, Optional[tuple[bytes, float]]]]:
    """Channel B + C features per sprite frame. Returns list aligned with
    frame indices. Frames whose features couldn't be computed appear with
    `{color_hist: None, tone: None}`.

    Cache per-frame (`scene_id:idx`), keyed by oshash (same fingerprint as
    channel A's sprite cache).
    """
    paths = scene.get("paths") or {}
    sprite_url = paths.get("sprite") or ""
    vtt_url = paths.get("vtt") or ""
    oshash = _scene_oshash(scene)
    if not sprite_url or not vtt_url or not oshash:
        return []

    # Try cache first per-frame for both channels.
    out: list[dict[str, Optional[tuple[bytes, float]]]] = []
    cached_count = 0
    for idx in range(sample_size):
        ref_id = f"{scene['id']}:{idx}"
        b = await cdb.get_image_feature("stash_sprite", ref_id, oshash, "color_hist", CHANNEL_B_ALGO)
        c = await cdb.get_image_feature("stash_sprite", ref_id, oshash, "tone", CHANNEL_C_ALGO)
        if b is None or c is None:
            out = []
            break
        out.append({
            "color_hist": (bytes(b[0]), b[1]),
            "tone": (bytes(c[0]), c[1]),
        })
        cached_count += 1
    if out and cached_count == sample_size:
        return out

    # Cache miss — fetch + decode the sprite once, compute per-frame.
    from .imgmatch.sprite_processor import (
        parse_vtt, decode_vtt_text, extract_sprite_frames, sample_frames,
    )
    from .imgmatch.channels import compute_color_hist, compute_tone

    sprite_bytes = await stash_client.fetch_image_bytes(sprite_url)
    vtt_text = await stash_client.fetch_text(vtt_url)
    if not sprite_bytes or not vtt_text:
        return []

    def _decode_and_sample():
        from PIL import Image as PILImage
        import io as _io
        sprite_img = PILImage.open(_io.BytesIO(sprite_bytes))
        vtt_frames = parse_vtt(decode_vtt_text(vtt_text))
        if not vtt_frames:
            return None
        extracted = extract_sprite_frames(sprite_img, vtt_frames)
        return sample_frames(extracted, sample_size)

    try:
        # Sprite decode + frame extraction is CPU/IO bound; off-loop.
        sampled = await asyncio.to_thread(_decode_and_sample)
        if sampled is None:
            return []
    except Exception as e:
        logger.warning("sprite B/C decode failed for scene %s :: %s", scene.get("id"), e)
        return []

    def _compute_frame(frame_image):
        b_blob, b_q = compute_color_hist(frame_image)
        c_blob, c_q = compute_tone(frame_image)
        return b_blob, b_q, c_blob, c_q

    out = []
    for idx, frame in enumerate(sampled):
        ref_id = f"{scene['id']}:{idx}"
        try:
            b_blob, b_q, c_blob, c_q = await asyncio.to_thread(_compute_frame, frame["image"])
        except Exception as e:
            logger.warning("sprite B/C compute failed scene=%s idx=%d :: %s", scene.get("id"), idx, e)
            out.append({"color_hist": None, "tone": None})
            continue
        b_bytes = bytes(b_blob)
        c_bytes = bytes(c_blob)
        await cdb.set_image_feature(
            "stash_sprite", ref_id, oshash, "color_hist", CHANNEL_B_ALGO, b_bytes, b_q,
        )
        await cdb.set_image_feature(
            "stash_sprite", ref_id, oshash, "tone", CHANNEL_C_ALGO, c_bytes, c_q,
        )
        out.append({"color_hist": (b_bytes, b_q), "tone": (c_bytes, c_q)})
    return out


def _is_degenerate_hash(phash) -> bool:
    """Bit-density check on a pHash hex string. A real image's pHash bits
    cluster around 50% population (because pHash thresholds against the DCT
    median). All-black/all-white/near-uniform images produce hashes with
    bit density near 0% or 100% — those collisions are spurious.

    The cutoff is generous (10%/90%) so the variance check at hash time
    stays the primary defense; this is belt-and-braces for any degenerate
    hash that snuck through (e.g. cached from before the variance filter
    was added)."""
    if phash is None:
        return True
    s = str(phash)
    if not s:
        return True
    try:
        ones = bin(int(s, 16))[2:].count("1")
    except ValueError:
        return True
    total = len(s) * 4
    if total == 0:
        return True
    frac = ones / total
    return frac < 0.10 or frac > 0.90


def _sim(h_a, h_b, hash_size: int) -> float:
    if h_a is None or h_b is None:
        return 0.0
    if _is_degenerate_hash(h_a) or _is_degenerate_hash(h_b):
        return 0.0
    try:
        d = h_a - h_b
        return hash_distance_to_similarity(d, hash_size)
    except Exception:
        return 0.0


async def all_pair_sims(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
) -> tuple[list[float], int]:
    """Compute every pair similarity between the configured Stash-side hash
    set and the record's extractor images. Returns (flat_sims, n_images).

    Stash-side hash set per `image_mode`:
      - cover  → {screenshot}                  (1)
      - sprite → {sprite frame 1..M}           (M)
      - both   → {screenshot, frame 1..M}      (M+1)

    Extractor side: cover_image + images[] (deduped). Both sides are
    filtered for degenerate / 404 / low-variance hashes (CLAUDE.md §13).

    `flat_sims` length == |Stash side| × |extractor side| pairs (not
    collapsed per-image). The full pair distribution is the input to the
    distribution-sensitive aggregation — `_top_k_mean` with K = n_images.

    `n_images` is returned alongside because aggregation needs it as K.
    """
    refs: list[str] = list(record.get("images") or [])
    cover_ref = record.get("cover_image")
    if cover_ref and cover_ref not in refs:
        refs = [cover_ref] + refs
    if not refs:
        return [], 0

    extractor_hashes: list = []
    for ref in refs:
        eh = await extractor_image_hash(job_id, ref, algorithm, hash_size)
        if eh is None or _is_degenerate_hash(eh):
            # 404, low-variance, or degenerate — drop from comparison set.
            continue
        extractor_hashes.append(eh)
    if not extractor_hashes:
        return [], 0

    stash_hashes: list = []
    if image_mode in ("cover", "both"):
        c = await stash_cover_hash(scene, algorithm, hash_size)
        if c is not None and not _is_degenerate_hash(c):
            stash_hashes.append(c)
    if image_mode in ("sprite", "both"):
        for sh in await stash_sprite_hashes(scene, algorithm, hash_size, sprite_sample_size):
            if sh is not None and not _is_degenerate_hash(sh):
                stash_hashes.append(sh)
    if not stash_hashes:
        return [], len(extractor_hashes)

    sims: list[float] = []
    for sh in stash_hashes:
        for eh in extractor_hashes:
            sims.append(_sim(sh, eh, hash_size))
    return sims, len(extractor_hashes)


def _top_k_mean(sims: list[float], k: int) -> float:
    """Mean of the top-K values from `sims`, sorted descending. K is
    clamped to [1, len(sims)]. Bounded [0, 1].

    Distribution-sensitive: uses the upper tail rather than the full
    distribution — single-outlier values can't dominate, but multiple
    high values get full credit. A record with one spurious 1.0 sim
    surrounded by weak ones scores low because the K-1 trailing terms
    drag the mean down; a record with K consistently strong sims scores
    high because every term is large.

    See CLAUDE.md §13 for the dispatch rule on K. Short version:
    K = number of distinct extractor images participating, because we
    want one strong match per extractor image as the "all images
    accounted for" benchmark.
    """
    if not sims:
        return 0.0
    k = max(1, min(k, len(sims)))
    return sum(sorted(sims, reverse=True)[:k]) / k


def aggregate_search(sims: list[float], n_images: int) -> float:
    """Search-mode: top-K mean over the full M×N pair set (no threshold gate)."""
    return _top_k_mean(sims, n_images)


def aggregate_scrape(sims: list[float], n_images: int, threshold: float) -> float:
    """Scrape-mode: top-K mean over the full M×N pair set; fires only when
    the aggregate clears the threshold. The threshold now gates the
    *aggregate*, not individual pair sims — this is what kills the
    one-outlier false positive: a single 1.0 in a sea of low sims won't
    push the aggregate over the threshold."""
    score = _top_k_mean(sims, n_images)
    return score if score >= threshold else 0.0


# --- Phase 4 new scoring path -------------------------------------------------

async def _gather_pair_data(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
) -> dict[str, Any]:
    """Rich version of all_pair_sims used by the Phase 4 scorer.

    Returns a dict with:
      - `sims`: flat list of M*N pair similarities (row-major, stash-outer)
      - `n_extractor_images`: N (after filtering)
      - `n_stash_hashes`: M (after filtering)
      - `extractor_refs`: list[str] of length N — each ref participating
    Plus the same `[]/0/0/[]` empty-result shape when there's nothing usable.
    """
    refs: list[str] = list(record.get("images") or [])
    cover_ref = record.get("cover_image")
    if cover_ref and cover_ref not in refs:
        refs = [cover_ref] + refs

    extractor_hashes: list = []
    extractor_refs: list[str] = []
    for ref in refs:
        eh = await extractor_image_hash(job_id, ref, algorithm, hash_size)
        if eh is None or _is_degenerate_hash(eh):
            continue
        extractor_hashes.append(eh)
        extractor_refs.append(ref)
    if not extractor_hashes:
        return {"sims": [], "n_extractor_images": 0, "n_stash_hashes": 0, "extractor_refs": []}

    stash_hashes: list = []
    if image_mode in ("cover", "both"):
        c = await stash_cover_hash(scene, algorithm, hash_size)
        if c is not None and not _is_degenerate_hash(c):
            stash_hashes.append(c)
    if image_mode in ("sprite", "both"):
        for sh in await stash_sprite_hashes(scene, algorithm, hash_size, sprite_sample_size):
            if sh is not None and not _is_degenerate_hash(sh):
                stash_hashes.append(sh)
    if not stash_hashes:
        return {"sims": [], "n_extractor_images": len(extractor_hashes),
                "n_stash_hashes": 0, "extractor_refs": extractor_refs}

    sims: list[float] = []
    for sh in stash_hashes:
        for eh in extractor_hashes:
            sims.append(_sim(sh, eh, hash_size))
    return {
        "sims": sims,
        "n_extractor_images": len(extractor_hashes),
        "n_stash_hashes": len(stash_hashes),
        "extractor_refs": extractor_refs,
    }


def _per_image_max(sims: list[float], M: int, N: int) -> list[float]:
    """Reshape flat M*N sims (row-major, stash-outer) and take the max per
    extractor image. Returns a length-N vector. M=0 or N=0 → empty list.
    """
    if M == 0 or N == 0 or len(sims) != M * N:
        return [0.0] * N
    out = [0.0] * N
    for n in range(N):
        best = 0.0
        for m in range(M):
            v = sims[m * N + n]
            if v > best:
                best = v
        out[n] = best
    return out


async def score_image_channel_a(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
    gamma: float,
    count_k: float,
) -> dict[str, Any]:
    """Compute the channel-A (pHash) score using the new within-channel
    formula (§3.2). Returns a dict:
      {
        "S": float,                # channel score [0, 1]
        "E": float, "count_conf": float, "dist_q": float,
        "m_primes": list[float],   # sharpened per-image sims
        "per_image_max": list[float],
        "qualities": list[float],
        "uniquenesses": list[float],
        "baseline": float,
        "n_extractor_images": int,
        "n_stash_hashes": int,
        "extractor_refs": list[str],
      }

    Returns S=0 when there are no usable images on either side.

    NOTE (Phase 4 limitation): `c_i` values are read from
    `image_uniqueness` as cached at featurization time. The request's
    `image_uniqueness_alpha` therefore must match the bridge's
    `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA` for correct scoring; mismatches
    yield a c_i computed against a different alpha. Phase 5 stores the
    match count and recomputes c_i at scoring time.
    """
    from .scoring import score_frame_channel

    pair = await _gather_pair_data(
        scene, job_id, record, image_mode, algorithm, hash_size, sprite_sample_size,
    )
    N = pair["n_extractor_images"]
    M = pair["n_stash_hashes"]
    refs = pair["extractor_refs"]
    sims = pair["sims"]

    if N == 0 or M == 0:
        return {
            "S": 0.0, "E": 0.0, "count_conf": 0.0, "dist_q": 0.5,
            "m_primes": [], "per_image_max": [], "qualities": [], "uniquenesses": [],
            "baseline": 0.5, "n_extractor_images": N, "n_stash_hashes": M, "extractor_refs": refs,
        }

    per_image_max = _per_image_max(sims, M, N)

    # Lookup baseline + per-ref q_i / c_i. Missing values default to
    # neutral (q=1, c=1, baseline=0.5) — this is the "first request hits
    # before featurization populated everything" path. Phase 3's request
    # gate ensures the bridge is `ready` before serving requests, so in
    # practice these defaults trigger only when BRIDGE_LIFECYCLE_ENABLED
    # is false.
    new_algo = _phash_algo_key(algorithm, hash_size)
    baseline = await cdb.get_corpus_stat(job_id, "phash", new_algo)
    if baseline is None:
        baseline = 0.5

    qualities: list[float] = []
    uniquenesses: list[float] = []
    for ref in refs:
        feat = await cdb.get_image_feature(
            "extractor_image", f"{job_id}:{ref}", _resolve_fingerprint_for(job_id, ref),
            "phash", new_algo,
        )
        q = feat[1] if feat is not None else 1.0
        c = await cdb.get_image_uniqueness(job_id, ref, "phash")
        if c is None:
            c = 1.0
        qualities.append(q)
        uniquenesses.append(c)

    cs = score_frame_channel(per_image_max, qualities, uniquenesses, baseline, gamma, count_k)
    return {
        "S": cs.S, "E": cs.E, "count_conf": cs.count_conf, "dist_q": cs.dist_q,
        "m_primes": cs.m_primes, "per_image_max": per_image_max,
        "qualities": qualities, "uniquenesses": uniquenesses,
        "baseline": baseline, "n_extractor_images": N, "n_stash_hashes": M,
        "extractor_refs": refs,
    }


def _resolve_fingerprint_for(job_id: str, ref: str) -> str:
    """Mirror of the fingerprint convention used by extractor_image_hash:
    for extractor images, the fingerprint is the resolved asset URL.
    Used to look up cached image_features rows.
    """
    if ref.startswith("http"):
        return ref
    return ex_client.resolve_asset_url(job_id, ref)


# --- Phase 5: channels B and C scoring entry points ------------------------

async def _stash_color_hist_aggregate(
    scene: dict[str, Any], sprite_sample_size: int,
) -> Optional[tuple[bytes, float]]:
    """Compute the scene-level B aggregate (per-bin median over sprite
    frames + cover). Cached as a `stash_aggregate` row keyed by scene id;
    fingerprint is the composite of oshash + screenshot t-param so either
    side changing triggers re-compute.
    """
    import numpy as _np
    from .imgmatch.channels import aggregate_color_hist, _color_hist_quality

    # Composite fingerprint
    paths = scene.get("paths") or {}
    screenshot_t = _screenshot_fingerprint(paths.get("screenshot") or "")
    oshash = _scene_oshash(scene)
    fingerprint = f"{oshash}|{screenshot_t}"
    if not oshash and not screenshot_t:
        return None

    # Cache hit?
    cached = await cdb.get_image_feature(
        "stash_aggregate", scene["id"], fingerprint, "color_hist", CHANNEL_B_ALGO,
    )
    if cached is not None:
        return (bytes(cached[0]), cached[1])

    # Build the aggregate from per-frame blobs.
    hists: list[_np.ndarray] = []
    cover = await stash_cover_bc_features(scene)
    if cover.get("color_hist") is not None:
        hists.append(_np.frombuffer(cover["color_hist"][0], dtype=_np.uint8))
    sprite = await stash_sprite_bc_features(scene, sprite_sample_size)
    for frame in sprite:
        if frame.get("color_hist") is not None:
            hists.append(_np.frombuffer(frame["color_hist"][0], dtype=_np.uint8))
    if not hists:
        return None
    agg = aggregate_color_hist(hists)
    if agg is None:
        return None
    quality = _color_hist_quality(agg.astype(float) / max(1.0, agg.sum()))
    blob = bytes(agg)
    await cdb.set_image_feature(
        "stash_aggregate", scene["id"], fingerprint, "color_hist", CHANNEL_B_ALGO,
        blob, quality,
    )
    return (blob, quality)


async def score_image_channel_b(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    record_idx: int,
    sprite_sample_size: int,
    gamma: float,
) -> dict[str, Any]:
    """Channel B (color histogram, scene-aggregate). Aggregate-only, no
    distribution term: `S_B = m_B' * q_B`."""
    import numpy as _np
    from .scoring import score_aggregate_channel
    from .imgmatch.channels import color_hist_similarity, aggregate_color_hist, _color_hist_quality

    stash_agg = await _stash_color_hist_aggregate(scene, sprite_sample_size)

    # Look up the precomputed extractor record aggregate. Fingerprint
    # convention matches what featurization writes: sorted("|".join(refs)).
    refs: list[str] = list(record.get("images") or [])
    cover = record.get("cover_image")
    if cover and cover not in refs:
        refs = [cover] + refs
    fingerprint = "|".join(sorted(refs))
    rec_agg_row = await cdb.get_image_feature(
        "extractor_aggregate", f"{job_id}:{record_idx}", fingerprint,
        "color_hist", CHANNEL_B_ALGO,
    )

    # Fallback: compute on-the-fly if not yet featurized (e.g., lifecycle
    # disabled or job is mid-featurization). Caches for next time.
    if rec_agg_row is None:
        hists: list[_np.ndarray] = []
        for ref in refs:
            bc = await extractor_image_bc_features(job_id, ref)
            if bc.get("color_hist") is not None:
                hists.append(_np.frombuffer(bc["color_hist"][0], dtype=_np.uint8))
        if hists:
            agg = aggregate_color_hist(hists)
            if agg is not None:
                quality = _color_hist_quality(agg.astype(float) / max(1.0, agg.sum()))
                blob = bytes(agg)
                await cdb.set_image_feature(
                    "extractor_aggregate", f"{job_id}:{record_idx}", fingerprint,
                    "color_hist", CHANNEL_B_ALGO, blob, quality,
                )
                rec_agg_row = (blob, quality)

    if stash_agg is None or rec_agg_row is None:
        return {"S": 0.0, "sim": 0.0, "m_prime": 0.0, "quality": 0.0,
                "baseline": 0.5, "have_stash": stash_agg is not None,
                "have_extractor": rec_agg_row is not None}

    stash_arr = _np.frombuffer(stash_agg[0], dtype=_np.uint8)
    rec_arr = _np.frombuffer(rec_agg_row[0], dtype=_np.uint8)
    sim = color_hist_similarity(stash_arr, rec_arr)

    baseline = await cdb.get_corpus_stat(job_id, "color_hist", CHANNEL_B_ALGO)
    if baseline is None:
        baseline = 0.5

    cs = score_aggregate_channel(sim, rec_agg_row[1], baseline, gamma)
    return {
        "S": cs.S, "sim": sim, "m_prime": cs.m_primes[0] if cs.m_primes else 0.0,
        "quality": rec_agg_row[1], "baseline": baseline,
        "have_stash": True, "have_extractor": True,
    }


async def score_image_channel_c(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    sprite_sample_size: int,
    gamma: float,
    count_k: float,
) -> dict[str, Any]:
    """Channel C (low-res tone). Frame-level, same shape as A."""
    import numpy as _np
    from .scoring import score_frame_channel
    from .imgmatch.channels import tone_similarity

    refs: list[str] = list(record.get("images") or [])
    cover = record.get("cover_image")
    if cover and cover not in refs:
        refs = [cover] + refs

    extractor_blobs: list[_np.ndarray] = []
    extractor_refs: list[str] = []
    for ref in refs:
        bc = await extractor_image_bc_features(job_id, ref)
        if bc.get("tone") is None:
            continue
        extractor_blobs.append(_np.frombuffer(bc["tone"][0], dtype=_np.uint8))
        extractor_refs.append(ref)
    N = len(extractor_blobs)
    if N == 0:
        return {"S": 0.0, "E": 0.0, "count_conf": 0.0, "dist_q": 0.5,
                "m_primes": [], "per_image_max": [], "qualities": [], "uniquenesses": [],
                "baseline": 0.5, "n_extractor_images": 0, "n_stash_hashes": 0,
                "extractor_refs": []}

    stash_blobs: list[_np.ndarray] = []
    if image_mode in ("cover", "both"):
        cover_bc = await stash_cover_bc_features(scene)
        if cover_bc.get("tone") is not None:
            stash_blobs.append(_np.frombuffer(cover_bc["tone"][0], dtype=_np.uint8))
    if image_mode in ("sprite", "both"):
        for frame in await stash_sprite_bc_features(scene, sprite_sample_size):
            if frame.get("tone") is not None:
                stash_blobs.append(_np.frombuffer(frame["tone"][0], dtype=_np.uint8))
    M = len(stash_blobs)
    if M == 0:
        return {"S": 0.0, "E": 0.0, "count_conf": 0.0, "dist_q": 0.5,
                "m_primes": [], "per_image_max": [0.0] * N, "qualities": [], "uniquenesses": [],
                "baseline": 0.5, "n_extractor_images": N, "n_stash_hashes": 0,
                "extractor_refs": extractor_refs}

    # Per-image max sim across the M Stash blobs.
    per_image_max = [0.0] * N
    for n in range(N):
        best = 0.0
        for sb in stash_blobs:
            v = tone_similarity(sb, extractor_blobs[n])
            if v > best:
                best = v
        per_image_max[n] = best

    # Per-ref quality and uniqueness.
    qualities: list[float] = []
    uniquenesses: list[float] = []
    for ref in extractor_refs:
        cached = await cdb.get_image_feature(
            "extractor_image", f"{job_id}:{ref}",
            _resolve_fingerprint_for(job_id, ref),
            "tone", CHANNEL_C_ALGO,
        )
        q = cached[1] if cached is not None else 1.0
        qualities.append(q)
        c = await cdb.get_image_uniqueness(job_id, ref, "tone")
        uniquenesses.append(c if c is not None else 1.0)

    baseline = await cdb.get_corpus_stat(job_id, "tone", CHANNEL_C_ALGO)
    if baseline is None:
        baseline = 0.5

    cs = score_frame_channel(per_image_max, qualities, uniquenesses, baseline, gamma, count_k)
    return {
        "S": cs.S, "E": cs.E, "count_conf": cs.count_conf, "dist_q": cs.dist_q,
        "m_primes": cs.m_primes, "per_image_max": per_image_max,
        "qualities": qualities, "uniquenesses": uniquenesses,
        "baseline": baseline, "n_extractor_images": N, "n_stash_hashes": M,
        "extractor_refs": extractor_refs,
    }


async def score_image_composite(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    record_idx: int,
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
    gamma: float,
    count_k: float,
    channels: list[str],
    min_contribution: float,
    bonus_per_extra: float,
) -> dict[str, Any]:
    """Run all requested channels and compose. `channels` is the ordered
    list from the scraper config — only those listed are evaluated.

    Returns a dict ready for both scoring use (`S` = composite) and debug
    output (per-channel breakdowns under `channels`).
    """
    from .scoring import ChannelScore, compose

    per_channel_debug: dict[str, dict] = {}
    channel_scores: dict[str, ChannelScore] = {}

    if "phash" in channels:
        a = await score_image_channel_a(
            scene, job_id, record, image_mode, algorithm, hash_size,
            sprite_sample_size, gamma, count_k,
        )
        per_channel_debug["phash"] = a
        channel_scores["phash"] = ChannelScore(
            S=a["S"], E=a["E"], count_conf=a["count_conf"],
            dist_q=a["dist_q"], m_primes=a["m_primes"],
        )

    if "color_hist" in channels:
        b = await score_image_channel_b(
            scene, job_id, record, record_idx, sprite_sample_size, gamma,
        )
        per_channel_debug["color_hist"] = b
        channel_scores["color_hist"] = ChannelScore(
            S=b["S"], E=0.0, count_conf=1.0, dist_q=1.0,
            m_primes=[b.get("m_prime", 0.0)],
        )

    if "tone" in channels:
        c = await score_image_channel_c(
            scene, job_id, record, image_mode, sprite_sample_size, gamma, count_k,
        )
        per_channel_debug["tone"] = c
        channel_scores["tone"] = ChannelScore(
            S=c["S"], E=c["E"], count_conf=c["count_conf"],
            dist_q=c["dist_q"], m_primes=c["m_primes"],
        )

    composite, fired = compose(channel_scores, min_contribution, bonus_per_extra)
    return {
        "S": composite, "fired": fired, "channels": per_channel_debug,
    }


