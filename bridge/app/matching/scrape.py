"""Scrape-mode binary cascade. Per CLAUDE.md §2 + requirements §6.1.

Cascade order: Studio+Code → Exact Title → Image (cheap-first; equivalent outcome).
Returns a single record (dict with job_id, result_index, data) or None.
"""
import logging
from typing import Any, Optional

from fastapi import HTTPException

from ..settings import settings
from .text import studio_and_code_fires, exact_title_fires
from .image_match import (
    all_pair_sims, aggregate_scrape, score_image_channel_a,
    score_image_channel_d_batch, score_image_composite,
)

logger = logging.getLogger(__name__)


async def scrape(
    scene: dict[str, Any],
    candidates: list[dict[str, Any]],   # each: {job_id, result_index, data}
    used_studio_filter: bool,
    image_mode: str,
    threshold: float,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
    image_gamma: Optional[float] = None,
    image_count_k: Optional[float] = None,
    image_channels: Optional[list[str]] = None,
    image_min_contribution: Optional[float] = None,
    image_bonus_per_extra: Optional[float] = None,
) -> Optional[dict[str, Any]]:

    n_cand = len(candidates)

    # Tier 1: Studio + Code
    if used_studio_filter:
        hits = [c for c in candidates if studio_and_code_fires(scene, c["data"], True)]
        if hits:
            best = min(hits, key=lambda c: (c["job_id"], c["result_index"]))
            logger.info(
                "scrape: tier=studio_code fired=true job=%s idx=%d (n_hits=%d)",
                best["job_id"], best["result_index"], len(hits),
            )
            return best
        logger.info("scrape: tier=studio_code fired=false (no candidates matched scene.code over n=%d)", n_cand)
    else:
        logger.info("scrape: tier=studio_code skipped (no studio filter)")

    # Tier 2: Exact Title
    hits = [c for c in candidates if exact_title_fires(scene, c["data"])]
    if hits:
        best = min(hits, key=lambda c: (c["job_id"], c["result_index"]))
        logger.info(
            "scrape: tier=exact_title fired=true job=%s idx=%d (n_hits=%d)",
            best["job_id"], best["result_index"], len(hits),
        )
        return best
    logger.info("scrape: tier=exact_title fired=false (n_hits=0 over n=%d)", n_cand)

    # Tier 3: Image — fires when the image-channel composite clears the
    # threshold for this candidate. The scoring path is selected by
    # BRIDGE_NEW_SCORING_ENABLED:
    #   - new (Phase 4+): within-channel formula per CLAUDE.md §13
    #   - legacy: top-K-mean over flat M×N pair set (CLAUDE.md §13 prior)
    use_new = settings.bridge_new_scoring_enabled
    use_multi = bool(use_new and image_channels and len(image_channels) >= 1
                     and set(image_channels) != {"phash"})

    # D-batch + (optional) A/B/C re-rank. Two distinct knobs collapse onto
    # one `K`:
    #   - K  > 0  →  two-pass: D ranks all, A/B/C re-score top-K, composite
    #               combines via §13.3 max+bonus formula.
    #   - K == 0  →  embedding-only: D ranks all, no A/B/C compute, score
    #               for each candidate IS its D batch score. Fast path.
    # When `embedding` isn't in the channel list (or BRIDGE_EMBEDDING_ENABLED
    # is false), neither branch fires and we fall back to legacy single-pass.
    # See CLAUDE.md §13.10 for the D-batch invariant.
    K = settings.bridge_embedding_prefilter_k
    use_d_batch = bool(
        use_multi
        and image_channels and "embedding" in image_channels
        and settings.bridge_embedding_enabled
    )
    use_abc_rerank = use_d_batch and K > 0 and K < n_cand
    d_results: dict[tuple[str, int], dict[str, Any]] = {}
    cands_to_score = candidates
    cached_d_per_cand: dict[tuple[str, int], dict[str, Any]] = {}
    if use_d_batch:
        d_results = await score_image_channel_d_batch(
            scene, candidates, image_mode, sprite_sample_size,
        )
        if use_abc_rerank:
            # Sort all candidates by D score desc; take top K. Tiebreak by
            # (job_id, result_index) for determinism per CLAUDE.md §10.
            ranked = sorted(
                candidates,
                key=lambda c: (
                    -d_results.get((c["job_id"], c["result_index"]), {"S": 0.0})["S"],
                    c["job_id"], c["result_index"],
                ),
            )
            cands_to_score = ranked[:K]
            cached_d_per_cand = {
                (c["job_id"], c["result_index"]): {
                    "embedding": d_results[(c["job_id"], c["result_index"])]
                }
                for c in cands_to_score
            }
            logger.info(
                "scrape: tier=image two-pass D-batch n=%d → A/B/C re-rank top_k=%d",
                n_cand, len(cands_to_score),
            )
        else:
            logger.info(
                "scrape: tier=image embedding-only D-batch n=%d (K=0, A/B/C skipped)",
                n_cand,
            )

    scoring_path = "multi" if use_multi else ("channel_a" if use_new else "legacy")
    if use_abc_rerank:
        scoring_path += "+prefilter"
    elif use_d_batch:
        scoring_path += "+d_only"
    logger.info(
        "scrape: tier=image scoring=%s mode=%s threshold=%.3f n=%d (scored=%d)",
        scoring_path, image_mode, threshold, n_cand, len(cands_to_score),
    )

    matches: list[tuple[dict[str, Any], float]] = []
    raw_top: tuple[float, str, int] = (0.0, "", -1)  # (best raw composite, job, idx)
    for c in cands_to_score:
        if use_multi:
            cand_key = (c["job_id"], c["result_index"])
            # K=0 D-only fast path: skip A/B/C compute, the D batch
            # already produced everything we need.
            if use_d_batch and not use_abc_rerank:
                d = d_results[cand_key]
                raw = d["S"]
            else:
                cached = cached_d_per_cand.get(cand_key)
                res = await score_image_composite(
                    scene, c["job_id"], c["data"], c["result_index"],
                    image_mode, algorithm, hash_size, sprite_sample_size,
                    gamma=image_gamma, count_k=image_count_k,
                    channels=image_channels,
                    min_contribution=image_min_contribution,
                    bonus_per_extra=image_bonus_per_extra,
                    cached_channels=cached,
                )
                raw = res["S"]
            score = raw if raw >= threshold else 0.0
        elif use_new:
            res = await score_image_channel_a(
                scene, c["job_id"], c["data"], image_mode,
                algorithm, hash_size, sprite_sample_size,
                gamma=image_gamma, count_k=image_count_k,
            )
            raw = res["S"]
            score = raw if raw >= threshold else 0.0
        else:
            sims, n_images = await all_pair_sims(
                scene, c["job_id"], c["data"], image_mode,
                algorithm, hash_size, sprite_sample_size,
            )
            score = aggregate_scrape(sims, n_images, threshold)
            raw = score  # legacy aggregate_scrape already applies the threshold
        if raw > raw_top[0]:
            raw_top = (raw, c["job_id"], c["result_index"])
        if score > 0:
            matches.append((c, score))

    if matches:
        matches.sort(key=lambda m: (-m[1], m[0]["job_id"], m[0]["result_index"]))
        winner, score = matches[0]
        logger.info(
            "scrape: tier=image fired=true job=%s idx=%d composite=%.3f n_passing=%d",
            winner["job_id"], winner["result_index"], score, len(matches),
        )
        return winner

    logger.info(
        "scrape: tier=image fired=false best_raw=%.3f (job=%s idx=%d) below threshold=%.3f",
        raw_top[0], raw_top[1] or "-", raw_top[2], threshold,
    )
    logger.info("scrape: cascade exhausted, returning empty")
    return None
