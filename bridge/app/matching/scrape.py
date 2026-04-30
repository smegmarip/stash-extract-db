"""Scrape-mode binary cascade. Per CLAUDE.md §2 + requirements §6.1.

Cascade order: Studio+Code → Exact Title → Image (cheap-first; equivalent outcome).
Returns a single record (dict with job_id, result_index, data) or None.
"""
import logging
from typing import Any, Optional

from fastapi import HTTPException

from ..settings import settings
from .text import studio_and_code_fires, exact_title_fires
from .image_match import all_pair_sims, aggregate_scrape, score_image_channel_a, score_image_composite

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

    # Tier 1: Studio + Code
    if used_studio_filter:
        hits = [c for c in candidates if studio_and_code_fires(scene, c["data"], True)]
        if hits:
            best = min(hits, key=lambda c: (c["job_id"], c["result_index"]))
            logger.info("scrape match via Studio+Code: job=%s idx=%d", best["job_id"], best["result_index"])
            return best

    # Tier 2: Exact Title
    hits = [c for c in candidates if exact_title_fires(scene, c["data"])]
    if hits:
        best = min(hits, key=lambda c: (c["job_id"], c["result_index"]))
        logger.info("scrape match via Exact Title: job=%s idx=%d", best["job_id"], best["result_index"])
        return best

    # Tier 3: Image — fires when the image-channel composite clears the
    # threshold for this candidate. The scoring path is selected by
    # BRIDGE_NEW_SCORING_ENABLED:
    #   - new (Phase 4+): within-channel formula per CLAUDE.md §13
    #   - legacy: top-K-mean over flat M×N pair set (CLAUDE.md §13 prior)
    use_new = settings.bridge_new_scoring_enabled
    use_multi = bool(use_new and image_channels and len(image_channels) >= 1
                     and set(image_channels) != {"phash"})
    if use_new:
        if image_gamma is None or image_count_k is None:
            raise HTTPException(
                status_code=400,
                detail="image_gamma and image_count_k are required when BRIDGE_NEW_SCORING_ENABLED is true",
            )
    if use_multi and (image_min_contribution is None or image_bonus_per_extra is None):
        raise HTTPException(
            status_code=400,
            detail="image_min_contribution and image_bonus_per_extra are required when image_channels has more than just 'phash'",
        )

    matches: list[tuple[dict[str, Any], float]] = []
    for c in candidates:
        if use_multi:
            res = await score_image_composite(
                scene, c["job_id"], c["data"], c["result_index"],
                image_mode, algorithm, hash_size, sprite_sample_size,
                gamma=image_gamma, count_k=image_count_k,
                channels=image_channels,
                min_contribution=image_min_contribution,
                bonus_per_extra=image_bonus_per_extra,
            )
            score = res["S"] if res["S"] >= threshold else 0.0
        elif use_new:
            res = await score_image_channel_a(
                scene, c["job_id"], c["data"], image_mode,
                algorithm, hash_size, sprite_sample_size,
                gamma=image_gamma, count_k=image_count_k,
            )
            score = res["S"] if res["S"] >= threshold else 0.0
        else:
            sims, n_images = await all_pair_sims(
                scene, c["job_id"], c["data"], image_mode,
                algorithm, hash_size, sprite_sample_size,
            )
            score = aggregate_scrape(sims, n_images, threshold)
        if score > 0:
            matches.append((c, score))
    if matches:
        matches.sort(key=lambda m: (-m[1], m[0]["job_id"], m[0]["result_index"]))
        winner, score = matches[0]
        logger.info("scrape match via Image (%s): job=%s idx=%d agg=%.3f",
                    image_mode, winner["job_id"], winner["result_index"], score)
        return winner

    return None
