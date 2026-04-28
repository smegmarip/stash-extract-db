"""Scrape-mode binary cascade. Per CLAUDE.md §2 + requirements §6.1.

Cascade order: Studio+Code → Exact Title → Image (cheap-first; equivalent outcome).
Returns a single record (dict with job_id, result_index, data) or None.
"""
import logging
from typing import Any, Optional

from .text import studio_and_code_fires, exact_title_fires
from .image_match import per_extractor_image_sims, aggregate_scrape

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

    # Tier 3: Image — fires when at least one extractor image clears the
    # threshold for this candidate. Among firing candidates, the rank score
    # is a distribution-sensitive aggregation (soft-OR) over the above-
    # threshold per-image sims — favors records with multiple strong matches
    # over records with one or two borderline matches (CLAUDE.md §13).
    matches: list[tuple[dict[str, Any], float]] = []
    for c in candidates:
        sims = await per_extractor_image_sims(
            scene, c["job_id"], c["data"], image_mode,
            algorithm, hash_size, sprite_sample_size,
        )
        score = aggregate_scrape(sims, threshold)
        if score > 0:
            matches.append((c, score))
    if matches:
        matches.sort(key=lambda m: (-m[1], m[0]["job_id"], m[0]["result_index"]))
        winner, score = matches[0]
        logger.info("scrape match via Image (%s): job=%s idx=%d agg=%.3f",
                    image_mode, winner["job_id"], winner["result_index"], score)
        return winner

    return None
