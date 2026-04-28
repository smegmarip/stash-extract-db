"""Search-mode composite weighted score. Per requirements §6.2 + CLAUDE.md §4.

Score = clip(
    (1.0 if Studio+Code else 0)
  + (1.0 if Exact Title else 0)
  + aggregate_search(per_extractor_image_sims)   # distribution-sensitive (soft-OR)
  + 0.2 * filename_score                          # multi-channel; matching/filename.py
  + 0.3 * (0.5 * performer_score + 0.5 * date_score)
, 0, 1)

Image contribution rewards both peak strength and number of matches: a
record with 4 images scoring [0.1, 0.1, 0.1, 1.0] outranks one scoring
[0.13, 0.13, 0.13, 0.13] (CLAUDE.md §13). The threshold no longer gates
the search image contribution — it applies only to scrape (CLAUDE.md §4).
"""
import logging
from typing import Any, Optional

from .text import (
    studio_and_code_fires, exact_title_fires,
    date_score, performer_score,
)
from .filename import filename_score, filename_score_debug
from .image_match import per_extractor_image_sims, aggregate_search

logger = logging.getLogger(__name__)


def _stash_basename(scene: dict[str, Any]) -> str:
    files = scene.get("files") or []
    if not files:
        return ""
    return files[0].get("basename") or ""


async def search(
    scene: dict[str, Any],
    candidates: list[dict[str, Any]],
    used_studio_filter: bool,
    image_mode: str,
    threshold: float,
    algorithm: str,
    hash_size: int,
    sprite_sample_size: int,
    limit: int,
    alias_resolver,
    debug: bool = False,
) -> list[tuple[dict[str, Any], float, Optional[dict[str, Any]]]]:
    """Returns list of (candidate, score, debug_or_None)."""

    stash_basename = _stash_basename(scene)
    scored: list[tuple[dict[str, Any], float, Optional[dict[str, Any]]]] = []

    for c in candidates:
        rec = c["data"]
        score = 0.0

        sc_fires = studio_and_code_fires(scene, rec, used_studio_filter)
        if sc_fires:
            score += 1.0

        et_fires = exact_title_fires(scene, rec)
        if et_fires:
            score += 1.0

        sims = await per_extractor_image_sims(
            scene, c["job_id"], rec, image_mode,
            algorithm, hash_size, sprite_sample_size,
        )
        image_contrib = aggregate_search(sims)
        score += image_contrib

        if debug:
            fname_dbg = filename_score_debug(stash_basename, rec.get("url") or "")
            fname = fname_dbg["score"]
        else:
            fname = filename_score(stash_basename, rec.get("url") or "")
            fname_dbg = None
        score += 0.2 * fname

        ds = date_score(scene.get("date"), rec.get("date"))
        ps = await performer_score(scene, rec, alias_resolver)
        score += 0.3 * (0.5 * ps + 0.5 * ds)

        raw_score = score
        if score > 1.0:
            score = 1.0

        dbg = None
        if debug:
            dbg = {
                "studio_code": sc_fires,
                "exact_title": et_fires,
                "image": {
                    "mode": image_mode,
                    "per_extractor_image_sims": [round(s, 4) for s in sims],
                    "aggregation": "soft_or",
                    "score": round(image_contrib, 4),
                },
                "image_contribution": round(image_contrib, 4),
                "filename": fname_dbg,
                "filename_contribution": round(0.2 * fname, 4),
                "performer_score": round(ps, 4),
                "date_score": round(ds, 4),
                "soft_contribution": round(0.3 * (0.5 * ps + 0.5 * ds), 4),
                "raw_score": round(raw_score, 4),
                "capped_score": round(score, 4),
            }
        scored.append((c, score, dbg))

    scored.sort(key=lambda t: (-t[1], t[0]["job_id"], t[0]["result_index"]))
    return scored[:limit]
