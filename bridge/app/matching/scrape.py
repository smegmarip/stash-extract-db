"""Scrape-mode binary cascade. Per CLAUDE.md §2 + requirements §6.1.

Cascade order: Studio+Code → Exact Title → Image (cheap-first; equivalent outcome).
Returns a single record (dict with job_id, result_index, data) or None.
"""
import logging
from typing import Any, Optional

from .text import studio_and_code_fires, exact_title_fires
from .image_match import best_image_similarity

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

    # Tier 3: Image >= threshold (highest similarity wins; index breaks ties)
    matches: list[tuple[dict[str, Any], float]] = []
    for c in candidates:
        sim = await best_image_similarity(
            scene, c["job_id"], c["data"], image_mode,
            algorithm, hash_size, sprite_sample_size,
        )
        if sim >= threshold:
            matches.append((c, sim))
    if matches:
        matches.sort(key=lambda m: (-m[1], m[0]["job_id"], m[0]["result_index"]))
        winner, sim = matches[0]
        logger.info("scrape match via Image: job=%s idx=%d sim=%.3f",
                    winner["job_id"], winner["result_index"], sim)
        return winner

    return None
