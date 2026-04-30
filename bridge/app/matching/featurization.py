"""Per-job featurization.

Computes per-(record image, channel) features, per-job channel baselines,
and per-(record image, channel) uniqueness. See MULTI_CHANNEL_SCORING.md
§4.4 for the task body and §3 for the formulas.

Phase 5 ships all three channels: A (pHash), B (color histogram, per-record
aggregate), C (low-res tone). Channel B's per-record aggregate is stored
as an `extractor_aggregate` row keyed by `<job_id>:<record_idx>`.
"""
import asyncio
import logging
import random
from collections import defaultdict
from typing import Optional

import numpy as np

from ..cache import db as cdb
from ..settings import settings
from .image_match import (
    extractor_image_hash, extractor_image_bc_features,
    CHANNEL_B_ALGO, CHANNEL_C_ALGO,
)
from .imgmatch.image_comparison import hash_distance_to_similarity
from .imgmatch.channels import (
    aggregate_color_hist, color_hist_similarity, tone_similarity,
    _color_hist_quality,
)

logger = logging.getLogger(__name__)


def _algo_key(algorithm: str, hash_size: int) -> str:
    return f"{algorithm}:{hash_size}"


async def featurize_job(job_id: str) -> None:
    """Featurize one extractor job. Idempotent — re-running on a partial
    failure reuses already-cached image_features rows; only missing refs
    re-fetch.

    State transitions: caller has already inserted state='featurizing',
    progress=0. This function updates progress along the way and finishes
    in either 'ready' or 'failed'.
    """
    try:
        await _featurize_inner(job_id)
        await cdb.mark_feature_ready(job_id)
        logger.info("featurization complete: job=%s", job_id)
    except Exception as e:
        logger.exception("featurization failed: job=%s :: %s", job_id, e)
        await cdb.mark_feature_failed(job_id, str(e))


async def _featurize_inner(job_id: str) -> None:
    records = await cdb.list_results(job_id)
    if not records:
        logger.info("featurize: no records for job=%s — marking ready empty", job_id)
        return

    # Map: ref string → set of result_index values it appears in. Plus
    # per-record refs list for B aggregate compute.
    ref_to_records: dict[str, set[int]] = defaultdict(set)
    record_refs: dict[int, list[str]] = {}
    for rec in records:
        idx = rec["result_index"]
        data = rec["data"] or {}
        cover = data.get("cover_image")
        images = data.get("images") or []
        rec_refs: list[str] = []
        for r in [cover, *images]:
            if r and isinstance(r, str):
                ref_to_records[r].add(idx)
                if r not in rec_refs:
                    rec_refs.append(r)
        record_refs[idx] = rec_refs

    if not ref_to_records:
        logger.info("featurize: no image refs for job=%s — marking ready empty", job_id)
        return

    algorithm = settings.bridge_featurize_algorithm
    hash_size = settings.bridge_featurize_hash_size
    new_algo_a = _algo_key(algorithm, hash_size)

    refs = list(ref_to_records.keys())
    total = len(refs)
    completed = [0]
    sem = asyncio.Semaphore(settings.bridge_featurize_per_job_concurrency)
    ref_hash_a: dict[str, object] = {}     # imagehash objects
    ref_blob_b: dict[str, np.ndarray] = {}  # numpy uint8 hist
    ref_blob_c: dict[str, np.ndarray] = {}  # numpy uint8 tone

    async def featurize_one(ref: str) -> None:
        async with sem:
            try:
                h = await extractor_image_hash(job_id, ref, algorithm, hash_size)
            except Exception as e:
                logger.warning("featurize: hash A failed job=%s ref=%s :: %s", job_id, ref, e)
                h = None
            try:
                bc = await extractor_image_bc_features(job_id, ref)
            except Exception as e:
                logger.warning("featurize: B/C compute failed job=%s ref=%s :: %s", job_id, ref, e)
                bc = {"color_hist": None, "tone": None}
        completed[0] += 1
        await cdb.set_feature_progress(job_id, 0.80 * (completed[0] / total))
        if h is not None:
            ref_hash_a[ref] = h
        if bc.get("color_hist") is not None:
            ref_blob_b[ref] = np.frombuffer(bc["color_hist"][0], dtype=np.uint8)
        if bc.get("tone") is not None:
            ref_blob_c[ref] = np.frombuffer(bc["tone"][0], dtype=np.uint8)

    await asyncio.gather(*(featurize_one(ref) for ref in refs))

    # Per-record B aggregate. Keyed by (job_id, record_idx) using the
    # extractor_aggregate source/ref convention from §2.1.
    await cdb.set_feature_progress(job_id, 0.82)

    def _aggregate_per_record() -> dict[int, np.ndarray]:
        """CPU-bound per-bin median + quality compute for every record."""
        out: dict[int, np.ndarray] = {}
        for rec_idx, rec_refs in record_refs.items():
            hists = [ref_blob_b[r] for r in rec_refs if r in ref_blob_b]
            agg = aggregate_color_hist(hists)
            if agg is None:
                continue
            out[rec_idx] = agg
        return out

    record_b_agg = await asyncio.to_thread(_aggregate_per_record)

    for rec_idx, agg in record_b_agg.items():
        rec_refs = record_refs[rec_idx]
        agg_quality = _color_hist_quality(agg.astype(np.float64) / max(1.0, agg.sum()))
        fingerprint = "|".join(sorted(rec_refs))
        await cdb.set_image_feature(
            "extractor_aggregate", f"{job_id}:{rec_idx}", fingerprint,
            "color_hist", CHANNEL_B_ALGO, bytes(agg), agg_quality,
        )

    # Phase 2: empirical baselines per channel. Each compute is a tight
    # numeric loop over up to 1000 sampled pairs — off-loop so the bridge
    # stays responsive while featurization runs.
    await cdb.set_feature_progress(job_id, 0.85)
    baseline_a = await asyncio.to_thread(
        _compute_baseline_phash,
        list(ref_hash_a.keys()), ref_to_records, ref_hash_a, hash_size,
    )
    await cdb.set_corpus_stat(job_id, "phash", new_algo_a, baseline_a)

    baseline_b = await asyncio.to_thread(_compute_baseline_color_hist, record_b_agg)
    await cdb.set_corpus_stat(job_id, "color_hist", CHANNEL_B_ALGO, baseline_b)

    baseline_c = await asyncio.to_thread(
        _compute_baseline_tone,
        list(ref_blob_c.keys()), ref_to_records, ref_blob_c,
    )
    await cdb.set_corpus_stat(job_id, "tone", CHANNEL_C_ALGO, baseline_c)

    # Phase 3: uniqueness for A and C (B is aggregate-only and the formula
    # for aggregate channels — `S_B = m_B' * q_B` — does not consume c_i).
    # Per-channel threshold/alpha resolution lets tone use a stricter
    # near-duplicate threshold than pHash (architectural fix Run 7).
    await cdb.set_feature_progress(job_id, 0.92)
    phash_threshold = settings.channel_uniqueness_threshold("phash")
    phash_alpha = settings.channel_uniqueness_alpha("phash")
    tone_threshold = settings.channel_uniqueness_threshold("tone")
    tone_alpha = settings.channel_uniqueness_alpha("tone")

    # Compute all phash uniqueness values in one thread call (N×N inner
    # loop), then await sequential DB writes back on the loop.
    def _all_uniqueness_phash() -> dict[str, float]:
        out: dict[str, float] = {}
        keys = list(ref_hash_a.keys())
        for ref in keys:
            out[ref] = _compute_uniqueness_phash(
                ref, keys, ref_to_records, ref_hash_a, hash_size,
                phash_threshold, phash_alpha,
            )
        return out

    phash_uniq = await asyncio.to_thread(_all_uniqueness_phash)
    for ref, c in phash_uniq.items():
        await cdb.set_image_uniqueness(job_id, ref, "phash", c)

    await cdb.set_feature_progress(job_id, 0.97)

    def _all_uniqueness_tone() -> dict[str, float]:
        out: dict[str, float] = {}
        keys = list(ref_blob_c.keys())
        for ref in keys:
            out[ref] = _compute_uniqueness_tone(
                ref, keys, ref_to_records, ref_blob_c,
                tone_threshold, tone_alpha,
            )
        return out

    tone_uniq = await asyncio.to_thread(_all_uniqueness_tone)
    for ref, c in tone_uniq.items():
        await cdb.set_image_uniqueness(job_id, ref, "tone", c)


# --- Channel A (pHash) baselines + uniqueness -------------------------------

def _compute_baseline_phash(
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    ref_hash: dict[str, object],
    hash_size: int,
) -> float:
    return _baseline_via_sim(
        valid_refs, ref_to_records,
        sim_fn=lambda a, b: hash_distance_to_similarity(ref_hash[a] - ref_hash[b], hash_size),
    )


def _compute_uniqueness_phash(
    ref: str,
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    ref_hash: dict[str, object],
    hash_size: int,
    threshold: float,
    alpha: float,
) -> float:
    return _uniqueness_via_sim(
        ref, valid_refs, ref_to_records,
        sim_fn=lambda a, b: hash_distance_to_similarity(ref_hash[a] - ref_hash[b], hash_size),
        threshold=threshold, alpha=alpha,
    )


# --- Channel B (color histogram) — baseline only; no uniqueness -------------

def _compute_baseline_color_hist(record_b_agg: dict[int, np.ndarray]) -> float:
    """Baseline for channel B uses the per-record aggregates (since B's
    scoring operates on aggregates, not per-image hists). Sample non-equal
    record-pair similarities and take the mean.
    """
    rec_ids = list(record_b_agg.keys())
    n = len(rec_ids)
    if n < 2:
        return 0.5
    rng = random.Random(0)
    target = min(1000, n * (n - 1) // 2)
    sims: list[float] = []
    attempts = 0
    max_attempts = target * 5
    while len(sims) < target and attempts < max_attempts:
        a, b = rng.sample(rec_ids, 2)
        attempts += 1
        sims.append(color_hist_similarity(record_b_agg[a], record_b_agg[b]))
    if not sims:
        return 0.5
    return sum(sims) / len(sims)


# --- Channel C (low-res tone) baselines + uniqueness ------------------------

def _compute_baseline_tone(
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    ref_blob_c: dict[str, np.ndarray],
) -> float:
    return _baseline_via_sim(
        valid_refs, ref_to_records,
        sim_fn=lambda a, b: tone_similarity(ref_blob_c[a], ref_blob_c[b]),
    )


def _compute_uniqueness_tone(
    ref: str,
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    ref_blob_c: dict[str, np.ndarray],
    threshold: float,
    alpha: float,
) -> float:
    return _uniqueness_via_sim(
        ref, valid_refs, ref_to_records,
        sim_fn=lambda a, b: tone_similarity(ref_blob_c[a], ref_blob_c[b]),
        threshold=threshold, alpha=alpha,
    )


# --- Channel-agnostic helpers -----------------------------------------------

def _baseline_via_sim(
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    sim_fn,
) -> float:
    n = len(valid_refs)
    if n < 2:
        return 0.5
    rng = random.Random(0)
    target = min(1000, n * (n - 1) // 2)
    sims: list[float] = []
    attempts = 0
    max_attempts = target * 5
    while len(sims) < target and attempts < max_attempts:
        a, b = rng.sample(valid_refs, 2)
        attempts += 1
        if ref_to_records[a] & ref_to_records[b]:
            continue
        sims.append(sim_fn(a, b))
    if not sims:
        return 0.5
    return sum(sims) / len(sims)


def _uniqueness_via_sim(
    ref: str,
    valid_refs: list[str],
    ref_to_records: dict[str, set[int]],
    sim_fn,
    threshold: float,
    alpha: float,
) -> float:
    """Per §4.6: count records (not refs) containing this content beyond
    the canonical occurrence. Smoothed reciprocal `1/(1 + α·matches)`."""
    own_records = set(ref_to_records[ref])
    near_dup_records: set[int] = set()
    for other in valid_refs:
        if other == ref:
            continue
        sim = sim_fn(ref, other)
        if sim >= threshold:
            near_dup_records.update(ref_to_records[other])
    total = own_records | near_dup_records
    matches = max(0, len(total) - 1)
    return 1.0 / (1.0 + alpha * matches)
