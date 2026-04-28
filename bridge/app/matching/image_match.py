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
import logging
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from ..stash import client as stash_client
from ..extractor import client as ex_client
from ..cache import db as cdb
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


async def _hash_or_compute(
    source: str, ref_id: str, fingerprint: str,
    algorithm: str, hash_size: int,
    fetcher,
) -> Optional[Any]:
    """fetcher() -> bytes (awaitable). Returns imagehash or None.

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
    cached = await cdb.get_image_hash(source, ref_id, fingerprint, algorithm, hash_size)
    if cached:
        return hex_to_hash(cached)
    data = await fetcher()
    if not data:
        return None
    try:
        h = hash_image_bytes(data, algorithm, hash_size)
    except Exception as e:
        logger.warning("hash failed source=%s ref=%s :: %s", source, ref_id, e)
        return None
    if h is None:
        # Low-variance (near-uniform) image — can't produce a reliable hash.
        # Don't cache; the next request will retry but the same source bytes
        # will still fail, so we burn one fetch per low-variance image per
        # cache lifetime. Acceptable — these are rare.
        logger.debug("skipping low-variance image source=%s ref=%s", source, ref_id)
        return None
    await cdb.set_image_hash(source, ref_id, fingerprint, algorithm, hash_size, str(h))
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
    """Returns list of imagehash objects for sampled sprite frames."""
    paths = scene.get("paths") or {}
    sprite_url = paths.get("sprite") or ""
    vtt_url = paths.get("vtt") or ""
    oshash = _scene_oshash(scene)
    if not sprite_url or not vtt_url or not oshash:
        return []

    # Try cache: ref_id keyed per-frame (idx)
    out: list = []
    cached_count = 0
    for idx in range(sample_size):
        c = await cdb.get_image_hash("stash_sprite", f"{scene['id']}:{idx}", oshash, algorithm, hash_size)
        if c:
            out.append(hex_to_hash(c))
            cached_count += 1
        else:
            out = []  # any miss → recompute the whole thing
            break

    if out and cached_count == sample_size:
        return out

    sprite_bytes = await stash_client.fetch_image_bytes(sprite_url)
    vtt_text = await stash_client.fetch_text(vtt_url)
    if not sprite_bytes or not vtt_text:
        return []

    try:
        hashes = hash_sprite_frames(sprite_bytes, vtt_text, sample_size, algorithm, hash_size)
    except Exception as e:
        logger.warning("sprite hash failed for scene %s :: %s", scene.get("id"), e)
        return []

    for idx, h in enumerate(hashes):
        await cdb.set_image_hash("stash_sprite", f"{scene['id']}:{idx}", oshash, algorithm, hash_size, str(h))
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


async def per_extractor_image_sims(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
) -> list[float]:
    """For each extractor image (cover_image + images[], deduped), return
    the best similarity against the configured Stash-side image set:

      - cover  → Stash-side set is {screenshot}                  (1)
      - sprite → Stash-side set is {sprite frame 1..M}           (M)
      - both   → Stash-side set is {screenshot, frame 1..M}      (M+1)

    Result length == number of *usable* extractor image refs — we drop
    refs whose hash is None (404, fetch error, low-variance image — see
    `_hash_or_compute`) and refs whose hash is degenerate (all-black /
    all-white / near-uniform — see `_is_degenerate_hash`). Stash-side
    degenerate frames are filtered the same way. The goal is "signal
    only": every entry in the returned list is a real comparison between
    two images that carry visual information.

    Each entry is the max similarity against the Stash-side set for that
    extractor image. This per-image array is the input to the
    distribution-sensitive aggregation (see aggregate_search /
    aggregate_scrape).

    Per CLAUDE.md §13: rewards records with multiple strong matches over
    records with many mediocre matches; soft-OR aggregation lifts a single
    high-similarity hit to dominance regardless of weak siblings — which
    is exactly why we have to reject degenerate sources here. Two
    all-black images would otherwise match at sim=1.0 and saturate
    soft-OR, producing an agg=1.0 false-positive scrape result.
    """
    refs: list[str] = list(record.get("images") or [])
    cover_ref = record.get("cover_image")
    if cover_ref and cover_ref not in refs:
        refs = [cover_ref] + refs
    if not refs:
        return []

    # Build Stash-side hash set once per scene+mode. Drop None and
    # degenerate hashes — these contribute no usable signal and can
    # collide spuriously.
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
        return []

    sims: list[float] = []
    for ref in refs:
        eh = await extractor_image_hash(job_id, ref, algorithm, hash_size)
        if eh is None or _is_degenerate_hash(eh):
            # 404, low-variance, or degenerate — drop from sims entirely.
            # Soft-OR is unchanged by zeros, but listing them in debug is
            # noise; an absent entry says "no comparison happened here".
            continue
        best = 0.0
        for sh in stash_hashes:
            s = _sim(sh, eh, hash_size)
            if s > best:
                best = s
        sims.append(best)
    return sims


def soft_or(sims: list[float]) -> float:
    """Probabilistic OR — `1 - prod(1 - s)`.

    Properties (per CLAUDE.md §13):
      - Bounded [0, 1]; saturates at 1.0 when any sim hits 1.0.
      - Distribution-sensitive: multiple weak matches accumulate, but a
        single strong match dominates.
      - Monotonic in every component sim.

    Example: sims=[0.1,0.1,0.1,1.0] → 1.0;  sims=[0.13,0.13,0.13,0.13] → 0.427
    """
    if not sims:
        return 0.0
    p = 1.0
    for s in sims:
        if s < 0.0:
            s = 0.0
        elif s > 1.0:
            s = 1.0
        p *= (1.0 - s)
    return 1.0 - p


def aggregate_search(sims: list[float]) -> float:
    """Search-mode aggregation — every per-image sim contributes (no threshold)."""
    return soft_or(sims)


def aggregate_scrape(sims: list[float], threshold: float) -> float:
    """Scrape-mode aggregation — only above-threshold sims contribute. Returns
    0.0 when no extractor image clears the threshold (the candidate doesn't
    fire the image tier)."""
    above = [s for s in sims if s >= threshold]
    return soft_or(above)


# Back-compat thin wrapper — returns the simple max similarity. Kept only for
# any caller that still wants the old single-number signal; new code should
# use per_extractor_image_sims + aggregate_*.
async def best_image_similarity(
    scene: dict[str, Any],
    job_id: str,
    record: dict[str, Any],
    image_mode: str,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
) -> float:
    sims = await per_extractor_image_sims(
        scene, job_id, record, image_mode, algorithm, hash_size, sprite_sample_size,
    )
    return max(sims) if sims else 0.0
