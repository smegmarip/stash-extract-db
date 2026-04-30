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

from fastapi import HTTPException

from ..settings import settings
from .text import (
    studio_and_code_fires, exact_title_fires,
    date_score, performer_score,
)
from .filename import filename_score, filename_score_debug
from .image_match import all_pair_sims, aggregate_search, score_image_channel_a, score_image_composite

logger = logging.getLogger(__name__)


def _stash_basename(scene: dict[str, Any]) -> str:
    files = scene.get("files") or []
    if not files:
        return ""
    return files[0].get("basename") or ""


def _round_channel_debug(channel_name: str, d: dict[str, Any]) -> dict[str, Any]:
    """Format a channel's debug dict for the JSON response. Different
    shapes per channel — frame-level (A, C) carry m_primes + per-image
    arrays; aggregate (B) carries m_prime + sim + quality."""
    out: dict[str, Any] = {"S": round(d.get("S", 0.0), 4)}
    if channel_name == "color_hist":
        out.update({
            "m_prime": round(d.get("m_prime", 0.0), 4),
            "sim": round(d.get("sim", 0.0), 4),
            "quality": round(d.get("quality", 0.0), 4),
            "baseline": round(d.get("baseline", 0.0), 4),
            "have_stash": d.get("have_stash"),
            "have_extractor": d.get("have_extractor"),
        })
    else:
        out.update({
            "E": round(d.get("E", 0.0), 4),
            "count_conf": round(d.get("count_conf", 0.0), 4),
            "dist_q": round(d.get("dist_q", 0.0), 4),
            "baseline": round(d.get("baseline", 0.0), 4),
            "n_extractor_images": d.get("n_extractor_images", 0),
            "n_stash_hashes": d.get("n_stash_hashes", 0),
            "extractor_refs": d.get("extractor_refs", []),
            "per_image_max": [round(v, 4) for v in d.get("per_image_max", [])],
            "m_primes": [round(v, 4) for v in d.get("m_primes", [])],
            "qualities": [round(v, 4) for v in d.get("qualities", [])],
            "uniquenesses": [round(v, 4) for v in d.get("uniquenesses", [])],
        })
    return out


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
    image_gamma: Optional[float] = None,
    image_count_k: Optional[float] = None,
    image_channels: Optional[list[str]] = None,
    image_min_contribution: Optional[float] = None,
    image_bonus_per_extra: Optional[float] = None,
    image_search_floor: Optional[float] = None,
) -> list[tuple[dict[str, Any], float, Optional[dict[str, Any]]]]:
    """Returns list of (candidate, score, debug_or_None)."""

    use_new = settings.bridge_new_scoring_enabled
    use_multi = bool(use_new and image_channels and len(image_channels) >= 1
                     and set(image_channels) != {"phash"})

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

        if use_multi:
            res = await score_image_composite(
                scene, c["job_id"], rec, c["result_index"],
                image_mode, algorithm, hash_size, sprite_sample_size,
                gamma=image_gamma, count_k=image_count_k,
                channels=image_channels,
                min_contribution=image_min_contribution,
                bonus_per_extra=image_bonus_per_extra,
            )
            image_contrib = res["S"]
            image_dbg_extra = res
        elif use_new:
            res = await score_image_channel_a(
                scene, c["job_id"], rec, image_mode,
                algorithm, hash_size, sprite_sample_size,
                gamma=image_gamma, count_k=image_count_k,
            )
            image_contrib = res["S"]
            image_dbg_extra = res
        else:
            sims, n_images = await all_pair_sims(
                scene, c["job_id"], rec, image_mode,
                algorithm, hash_size, sprite_sample_size,
            )
            image_contrib = aggregate_search(sims, n_images)
            image_dbg_extra = {"sims": sims, "n_extractor_images": n_images}

        # Search-mode confidence floor: drop weak image-only candidates
        # before they pollute the result set. Definitive signals
        # (Studio+Code, Exact Title) bypass the floor — they have their
        # own correctness contracts and shouldn't be gated on a weak image.
        # See CALIBRATION_RESULTS.md Run 5 / architectural changes.
        if (
            image_search_floor is not None
            and image_contrib < image_search_floor
            and not sc_fires
            and not et_fires
        ):
            continue

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
            if use_multi:
                image_dbg = {
                    "mode": image_mode,
                    "scoring": "new (multi-channel " + ",".join(image_channels) + ")",
                    "channels": {
                        name: _round_channel_debug(name, ch_dbg)
                        for name, ch_dbg in image_dbg_extra["channels"].items()
                    },
                    "fired": image_dbg_extra["fired"],
                    "composite": round(image_contrib, 4),
                }
            elif use_new:
                image_dbg = {
                    "mode": image_mode,
                    "scoring": "new (channel A only)",
                    "channels": {
                        "phash": _round_channel_debug("phash", image_dbg_extra),
                    },
                    "fired": ["phash"] if image_contrib >= 0.3 else [],
                    "composite": round(image_contrib, 4),
                }
            else:
                sims = image_dbg_extra["sims"]
                n_images = image_dbg_extra["n_extractor_images"]
                image_dbg = {
                    "mode": image_mode,
                    "scoring": "legacy (top-K mean)",
                    "n_images": n_images,
                    "n_pairs": len(sims),
                    "all_pair_sims": [round(s, 4) for s in sorted(sims, reverse=True)[:20]],
                    "aggregation": f"top_k_mean (K={n_images})",
                    "score": round(image_contrib, 4),
                }
            dbg = {
                "studio_code": sc_fires,
                "exact_title": et_fires,
                "image": image_dbg,
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
