"""Match endpoints: /match/fragment, /match/url, /match/name.

All routes:
  1. Resolve scene from Stash (fragment, URL host match, name search) — fallback to a synthesized scene if Stash has no match.
  2. List completed extractor jobs, filter to scene-shaped schemas.
  3. Studio narrowing (case-insensitive name match).
  4. Pull cached results per job (refetch on completed_at change).
  5. Run scrape (binary cascade) or search (composite score).
  6. Transform winning record(s) into Stash scraper output shape.
"""
import asyncio
import base64
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..models import (
    FragmentMatchRequest, UrlMatchRequest, NameMatchRequest,
    ScrapeResult, SearchResult, StashStudioOut, StashPerformerOut,
)
from ..stash import client as stash_client
from ..stash.alias_index import AliasResolver
from ..extractor import client as ex_client
from ..extractor.schema_match import is_scene_shaped
from ..cache import invalidation as inv
from ..matching.scrape import scrape as scrape_match
from ..matching.search import search as search_match

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/match", tags=["Match"])


# ---------- shared pipeline -------------------------------------------

async def _scene_shaped_jobs() -> list[dict[str, Any]]:
    """All completed jobs whose schema is scene-shaped.

    The list endpoint returns a slim view without `extraction_config`, so
    we fetch each job individually to learn its schema_id, then check the
    schema's field set for the canonical superset.
    """
    summaries = await ex_client.list_completed_jobs()
    completed = [j for j in summaries if (j.get("status") or "") == "completed"]
    if not completed:
        return []

    full_jobs: list[dict[str, Any]] = []
    for s in completed:
        try:
            full = await ex_client.get_job(s["id"])
        except Exception as e:
            logger.warning("job fetch failed for %s :: %s", s.get("id"), e)
            continue
        if full:
            full_jobs.append(full)

    schema_ids = {(j.get("extraction_config") or {}).get("schema_id") for j in full_jobs}
    schemas: dict[str, dict[str, Any]] = {}
    for sid in schema_ids:
        if not sid:
            continue
        try:
            s = await ex_client.get_schema(sid)
            if s:
                schemas[sid] = s
        except Exception as e:
            logger.warning("schema fetch failed for %s :: %s", sid, e)

    out: list[dict[str, Any]] = []
    for j in full_jobs:
        sid = (j.get("extraction_config") or {}).get("schema_id")
        s = schemas.get(sid or "")
        if s and is_scene_shaped(s):
            out.append(j)
        else:
            logger.info("job %s (%s) excluded — schema not scene-shaped (sid=%s)",
                        j.get("id"), j.get("name"), sid)
    logger.info("scene-shaped jobs: %d/%d", len(out), len(full_jobs))
    return out


def _select_jobs_by_studio(jobs: list[dict[str, Any]], studio_name: Optional[str]) -> tuple[list[dict[str, Any]], bool]:
    """Per CLAUDE.md §5: case-insensitive equality on job.name vs studio.name.
    Returns (selected_jobs, used_studio_filter)."""
    if not studio_name:
        return jobs, False
    target = studio_name.casefold().strip()
    matched = [j for j in jobs if (j.get("name") or "").casefold().strip() == target]
    return matched, True


async def _build_candidate_pool(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each job, ensure the result cache is fresh, then flatten into
    candidates of shape {job_id, result_index, page_url, data}."""
    pool: list[dict[str, Any]] = []
    for j in jobs:
        try:
            results = await inv.ensure_job_results_fresh(j)
        except Exception as e:
            logger.warning("could not refresh results for job %s :: %s", j.get("id"), e)
            continue
        for r in results:
            pool.append({
                "job_id": j["id"],
                "result_index": r["result_index"],
                "page_url": r.get("page_url"),
                "data": r["data"],
            })
    return pool


# ---------- transform extractor record → Stash output -----------------

# Web-scraped details often contain runs of whitespace, blank lines, and
# stray indentation pulled from the source HTML. Stash treats Details as
# free-form text but renders consecutive whitespace verbatim, so noise
# comes through visibly. Collapse to "at most one consecutive newline,
# at most one consecutive horizontal-whitespace character" and strip
# leading/trailing whitespace. Single line breaks are preserved (so
# paragraph structure survives); single spaces are preserved.
_RUN_NEWLINE = re.compile(r"(?:[ \t\f\v]*\r?\n[ \t\f\v]*)+")
_RUN_HSPACE = re.compile(r"[ \t\f\v]+")


def _sanitize_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = _RUN_NEWLINE.sub("\n", s)
    s = _RUN_HSPACE.sub(" ", s)
    s = s.strip()
    return s or None


async def _record_to_scrape_result(
    candidate: dict[str, Any],
    studio_name: Optional[str],
    alias_resolver: AliasResolver,
) -> ScrapeResult:
    rec = candidate["data"]
    job_id = candidate["job_id"]

    # Cover image → base64 data URI (best-effort; absence is non-fatal)
    image_data_uri: Optional[str] = None
    cover_ref = rec.get("cover_image") or (rec.get("images") or [None])[0]
    if cover_ref:
        b = await ex_client.fetch_asset(job_id, cover_ref)
        if b:
            ext = "jpeg"
            r_lower = cover_ref.lower()
            if r_lower.endswith(".png"): ext = "png"
            elif r_lower.endswith(".gif"): ext = "gif"
            elif r_lower.endswith(".webp"): ext = "webp"
            image_data_uri = f"data:image/{ext};base64,{base64.b64encode(b).decode('ascii')}"

    performers_out: Optional[list[StashPerformerOut]] = None
    perf_names = rec.get("performers") or []
    if perf_names:
        performers_out = []
        for n in perf_names:
            if not isinstance(n, str):
                continue
            ids = await alias_resolver.resolve(n)
            performers_out.append(StashPerformerOut(Name=n, Aliases=None) if ids else StashPerformerOut(Name=n))

    code = (rec.get("id") or "").strip() or None

    return ScrapeResult(
        Title=rec.get("title") or None,
        Details=_sanitize_text(rec.get("details")),
        Date=rec.get("date") or None,
        URL=rec.get("url") or None,
        Code=code,
        Image=image_data_uri,
        Studio=StashStudioOut(Name=studio_name) if studio_name else None,
        Performers=performers_out,
    )


# ---------- endpoints --------------------------------------------------

@router.post("/fragment")
async def match_by_fragment(req: FragmentMatchRequest, debug: bool = Query(False)):
    return await _match_by_scene_id(req.scene_id, req, debug=debug)


@router.post("/url")
async def match_by_url(req: UrlMatchRequest, debug: bool = Query(False)):
    """For sceneByURL — Stash passes the URL directly (no scene fragment).
    First try exact URL match against the candidate pool (definitive in scrape).
    Falls back to filename similarity via the synthesized-scene path."""
    # URL exact match pre-tier — applies whether scrape or search.
    jobs = await _scene_shaped_jobs()
    if jobs:
        pool = await _build_candidate_pool(jobs)
        url = req.url
        hits = [c for c in pool if (c["data"].get("url") or "") == url]
        if hits:
            hits.sort(key=lambda c: (c["job_id"], c["result_index"]))
            alias_resolver = AliasResolver()
            if req.mode == "scrape":
                out = await _record_to_scrape_result(hits[0], None, alias_resolver)
                return out.model_dump(exclude_none=True)
            # search: prepend exact-URL hits with score=1.0; then continue.
            results = []
            for h in hits[: req.limit]:
                sr = await _record_to_scrape_result(h, None, alias_resolver)
                d = sr.model_dump(exclude_none=True)
                d["match_score"] = 1.0
                results.append(d)
            return results

    synth = {"id": "", "title": "", "details": "", "code": "", "date": None,
             "performers": [], "studio": None, "files": [{"basename": req.url, "fingerprints": []}],
             "paths": {}, "urls": [req.url]}
    return await _match_with_scene(synth, req, studio_for_filter=None, debug=debug)


@router.post("/name")
async def match_by_name(req: NameMatchRequest, debug: bool = Query(False)):
    """For sceneByName — Stash passes a search query (no fragment, no scene id).
    Synthesize a scene with title=name and run the engine. Forces 'search' mode."""
    if req.mode != "search":
        raise HTTPException(status_code=400, detail="/match/name requires mode=search")
    synth = {"id": "", "title": req.name, "details": "", "code": "", "date": None,
             "performers": [], "studio": None, "files": [{"basename": req.name, "fingerprints": []}],
             "paths": {}, "urls": []}
    return await _match_with_scene(synth, req, studio_for_filter=None, debug=debug)


# ---------- shared body -----------------------------------------------

async def _match_by_scene_id(scene_id: str, req, debug: bool = False):
    if not scene_id:
        raise HTTPException(status_code=400, detail="scene_id is required")
    scene = await stash_client.find_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail=f"Stash scene {scene_id!r} not found")
    studio = (scene.get("studio") or {}).get("name") if scene.get("studio") else None
    return await _match_with_scene(scene, req, studio_for_filter=studio, debug=debug)


async def _match_with_scene(
    scene: dict[str, Any],
    req,
    studio_for_filter: Optional[str],
    debug: bool = False,
):
    jobs = await _scene_shaped_jobs()
    if not jobs:
        return [] if req.mode == "search" else {}

    selected, used_filter = _select_jobs_by_studio(jobs, studio_for_filter)
    if used_filter and not selected:
        # Per CLAUDE.md §5: studio set, no match → empty
        return [] if req.mode == "search" else {}

    pool = await _build_candidate_pool(selected)
    if not pool:
        return [] if req.mode == "search" else {}

    alias_resolver = AliasResolver()

    if req.mode == "scrape":
        winner = await scrape_match(
            scene, pool, used_filter,
            req.image_mode, req.threshold,
            req.hash_algorithm, req.hash_size, req.sprite_sample_size,
        )
        if not winner:
            return {}
        out = await _record_to_scrape_result(winner, studio_for_filter, alias_resolver)
        return out.model_dump(exclude_none=True)

    # search
    ranked = await search_match(
        scene, pool, used_filter,
        req.image_mode, req.threshold,
        req.hash_algorithm, req.hash_size, req.sprite_sample_size,
        req.limit, alias_resolver, debug=debug,
    )
    out: list[dict[str, Any]] = []
    for cand, score, dbg in ranked:
        if score <= 0:
            continue
        sr = await _record_to_scrape_result(cand, studio_for_filter, alias_resolver)
        d = sr.model_dump(exclude_none=True)
        d["match_score"] = round(score, 4)
        if dbg is not None:
            d["_debug"] = dbg
        out.append(d)
    return out
