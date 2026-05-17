"""Per-job featurization.

Computes per-(record image, channel) features, per-job channel baselines,
and per-(record image, channel) uniqueness. See CLAUDE.md §14 for the
task lifecycle and §13 for the formulas.

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
from ..extractor import client as ex_client
from ..settings import settings
from .image_match import (
    extractor_image_hash, extractor_image_bc_features,
    CHANNEL_B_ALGO, CHANNEL_C_ALGO,
)
from .imgmatch import frames as _frames
from .imgmatch.image_comparison import hash_distance_to_similarity, hex_to_hash
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

    # First pass: collect the original (un-expanded) ref → record-set
    # mapping. Animated assets get expanded into synthetic frame refs
    # below, after a fetch+scene-detect pass.
    original_ref_to_records: dict[str, set[int]] = defaultdict(set)
    original_record_refs: dict[int, list[str]] = {}
    for rec in records:
        idx = rec["result_index"]
        data = rec["data"] or {}
        cover = data.get("cover_image")
        images = data.get("images") or []
        rec_refs: list[str] = []
        for r in [cover, *images]:
            if r and isinstance(r, str):
                original_ref_to_records[r].add(idx)
                if r not in rec_refs:
                    rec_refs.append(r)
        original_record_refs[idx] = rec_refs

    if not original_ref_to_records:
        logger.info("featurize: no image refs for job=%s — marking ready empty", job_id)
        return

    algorithm = settings.bridge_hash_algorithm
    hash_size = settings.bridge_hash_size
    new_algo_a = _algo_key(algorithm, hash_size)

    # Per-job pre-pass: build the animated-frame expansion map and bulk-load
    # per-channel cache presence for every (synth) ref we'd otherwise fetch.
    # A ref is "already featurized" iff every required (channel, algorithm)
    # has a fingerprint-matched row for every synth_ref. The fast path in
    # featurize_one below skips fetch + animatedness detection + compute
    # when that holds, populating ref_hash_a / _b / _c directly from the
    # bulk-fetched blobs so phase 2 (baseline / uniqueness) still has the
    # data it needs. Negative-cache sentinel rows count as "tried" so we
    # don't refetch refs that were known unfetchable.
    frame_map = await cdb.bulk_list_frame_refs_for_job(job_id)

    parent_to_synth_refs: dict[str, list[str]] = {}
    ref_id_to_fp: dict[str, str] = {}
    for parent_ref in original_ref_to_records:
        synth_list = frame_map.get(parent_ref) or [parent_ref]
        parent_fp = (
            parent_ref if parent_ref.startswith("http")
            else ex_client.resolve_asset_url(job_id, parent_ref)
        )
        parent_to_synth_refs[parent_ref] = synth_list
        for s in synth_list:
            ref_id_to_fp[f"{job_id}:{s}"] = parent_fp

    required_channels: list[tuple[str, str]] = [
        ("phash", new_algo_a),
        ("color_hist", CHANNEL_B_ALGO),
        ("tone", CHANNEL_C_ALGO),
    ]
    if settings.bridge_embedding_enabled:
        from . import embedding as emb
        required_channels.append(("embedding", emb.algorithm_key()))

    cached_per_channel: dict[str, dict[str, tuple[bytes, float]]] = {}
    for channel, alg in required_channels:
        cached_per_channel[channel] = await cdb.bulk_get_image_features(
            "extractor_image", channel, alg, ref_id_to_fp,
        )

    def _fully_cached(parent_ref: str) -> bool:
        synth_list = parent_to_synth_refs.get(parent_ref)
        if not synth_list:
            return False
        for s in synth_list:
            rid = f"{job_id}:{s}"
            for channel, _ in required_channels:
                if rid not in cached_per_channel[channel]:
                    return False
        return True

    original_refs = list(original_ref_to_records.keys())
    total = len(original_refs)
    completed = [0]
    n_skipped = [0]  # telemetry: refs that took the fast path
    sem = asyncio.Semaphore(settings.bridge_featurize_per_job_concurrency)
    ref_hash_a: dict[str, object] = {}     # imagehash objects
    ref_blob_b: dict[str, np.ndarray] = {}  # numpy uint8 hist
    ref_blob_c: dict[str, np.ndarray] = {}  # numpy uint8 tone
    # Animated refs that expanded — original_ref → ordered list of synthetic refs.
    expanded: dict[str, list[str]] = {}

    async def featurize_one(original_ref: str) -> None:
        """Featurize one parent asset.

        Fast path: when every required channel is already cached for every
        synth_ref this asset would produce, skip the HTTP fetch +
        is_animated_bytes() detection + per-channel compute. Just load the
        cached blobs into the in-memory dicts so phase 2 (aggregate,
        baseline, uniqueness) has the data. Animatedness is recovered from
        the cache itself (presence of `#frame_N` siblings — CLAUDE.md
        §13.8.1's implicit marker).

        Slow path: at least one channel is missing for some synth_ref →
        fetch + detect + compute as before.
        """
        async with sem:
            if _fully_cached(original_ref):
                synth_list = parent_to_synth_refs[original_ref]
                if frame_map.get(original_ref):
                    # Record the animated expansion so phase 2 sees the
                    # post-expansion ref shape, matching the slow path's
                    # `expanded[original_ref] = ...` write below.
                    expanded[original_ref] = synth_list
                for s in synth_list:
                    rid = f"{job_id}:{s}"
                    p_row = cached_per_channel["phash"].get(rid)
                    if p_row and p_row[1] != cdb.NEGATIVE_CACHE_QUALITY:
                        ref_hash_a[s] = hex_to_hash(p_row[0].hex())
                    b_row = cached_per_channel["color_hist"].get(rid)
                    if b_row and b_row[1] != cdb.NEGATIVE_CACHE_QUALITY:
                        ref_blob_b[s] = np.frombuffer(b_row[0], dtype=np.uint8)
                    c_row = cached_per_channel["tone"].get(rid)
                    if c_row and c_row[1] != cdb.NEGATIVE_CACHE_QUALITY:
                        ref_blob_c[s] = np.frombuffer(c_row[0], dtype=np.uint8)
                n_skipped[0] += 1
                completed[0] += 1
                await cdb.set_feature_progress(job_id, 0.80 * (completed[0] / total))
                return

            try:
                data = await ex_client.fetch_asset(job_id, original_ref)
            except Exception as e:
                logger.warning("featurize: fetch failed job=%s ref=%s :: %s",
                               job_id, original_ref, e)
                data = None

            if data and await asyncio.to_thread(_frames.is_animated_bytes, data):
                frame_bytes_list = await asyncio.to_thread(
                    _frames.uniform_sample_frames, data,
                    max_frames=settings.bridge_animated_frames_max,
                )
                if not frame_bytes_list:
                    # Animated but decode produced no frames → treat as
                    # decode failure per §13.8 rule 1: skip.
                    completed[0] += 1
                    await cdb.set_feature_progress(job_id, 0.80 * (completed[0] / total))
                    return
                synth_pairs = [
                    (f"{original_ref}#frame_{i}", fb)
                    for i, fb in enumerate(frame_bytes_list)
                ]
                expanded[original_ref] = [s for s, _ in synth_pairs]
            else:
                # Static asset (data may be None — let downstream fetch
                # try again and cache the failure sentinel).
                synth_pairs = [(original_ref, data)]

            for synth_ref, frame_bytes in synth_pairs:
                try:
                    h = await extractor_image_hash(
                        job_id, synth_ref, algorithm, hash_size,
                        prefetched_bytes=frame_bytes,
                    )
                except Exception as e:
                    logger.warning("featurize: hash A failed job=%s ref=%s :: %s",
                                   job_id, synth_ref, e)
                    h = None
                try:
                    bc = await extractor_image_bc_features(
                        job_id, synth_ref, prefetched_bytes=frame_bytes,
                    )
                except Exception as e:
                    logger.warning("featurize: B/C compute failed job=%s ref=%s :: %s",
                                   job_id, synth_ref, e)
                    bc = {"color_hist": None, "tone": None}
                if settings.bridge_embedding_enabled:
                    try:
                        from .image_match import extractor_image_embedding
                        await extractor_image_embedding(
                            job_id, synth_ref, prefetched_bytes=frame_bytes,
                        )
                    except Exception as e:
                        logger.warning("featurize: embedding compute failed job=%s ref=%s :: %s",
                                       job_id, synth_ref, e)
                if h is not None:
                    ref_hash_a[synth_ref] = h
                if bc.get("color_hist") is not None:
                    ref_blob_b[synth_ref] = np.frombuffer(bc["color_hist"][0], dtype=np.uint8)
                if bc.get("tone") is not None:
                    ref_blob_c[synth_ref] = np.frombuffer(bc["tone"][0], dtype=np.uint8)

        completed[0] += 1
        await cdb.set_feature_progress(job_id, 0.80 * (completed[0] / total))

    await asyncio.gather(*(featurize_one(ref) for ref in original_refs))

    if n_skipped[0]:
        logger.info(
            "featurize: job=%s skipped %d/%d ref(s) via cached-feature fast path",
            job_id, n_skipped[0], total,
        )

    # Now build the post-expansion ref↔record mappings used by aggregate /
    # baseline / uniqueness compute. Each animated original ref contributes
    # its synthetic frame refs in the same record-index slot.
    ref_to_records: dict[str, set[int]] = defaultdict(set)
    record_refs: dict[int, list[str]] = {idx: [] for idx in original_record_refs}
    for original_ref, rec_set in original_ref_to_records.items():
        synth_list = expanded.get(original_ref, [original_ref])
        for synth in synth_list:
            for idx in rec_set:
                ref_to_records[synth].add(idx)
                if synth not in record_refs[idx]:
                    record_refs[idx].append(synth)

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
