"""Admin / browse endpoints for the viewer microservice.

Read-only projections over the cache tables. Surfaces records and performers
indexed by stash-extract-db so the viewer can render list/detail pages and
the Status / Query pages without re-implementing the bridge's data plane.

Per CLAUDE.md §1, viewer is a presenter; these endpoints add no scoring or
matching logic. They project `extractor_results` + `performer_index` +
`job_feature_state` into display-friendly shapes.

Asset URLs returned here are bridge-relative (`/api/asset/{job_id}/assets/x`).
The viewer's Express layer proxies that path to the extractor so the browser
never needs to reach the extractor directly.

See CLAUDE.md §17.3 for the endpoint contract.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query

from ..cache import db as cdb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["Admin"])


DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100

_VALID_SORTS = {"id", "title", "date", "performer"}
_VALID_DIRS = {"asc", "desc"}


# ----- helpers --------------------------------------------------------------

def _bridge_asset_url(job_id: str, ref: Optional[str]) -> Optional[str]:
    """Rewrite an extractor record reference into a bridge-relative URL.

    Records use either `../assets/<file>` (relative to the record page) or
    a fully-qualified `http(s)://` URL. The viewer's Express proxy serves
    `/api/asset/{job_id}/...` by forwarding to the extractor; the path layout
    mirrors the extractor's own asset endpoint.

    Absolute http(s) URLs are returned as-is — they're already reachable
    by the browser. Empty / falsy refs return None.
    """
    if not ref:
        return None
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    cleaned = ref.lstrip("./")
    if not cleaned.startswith("assets/"):
        cleaned = f"assets/{cleaned.split('/')[-1]}"
    return f"/api/asset/{job_id}/{cleaned}"


def _escape_like(s: str) -> str:
    """Escape SQL LIKE wildcards in user input. Pair with `ESCAPE '\\'`."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _project_record(
    job_id: str,
    job_name: str,
    result_index: int,
    page_url: Optional[str],
    data_json: str,
    feature_state: Optional[str] = None,
    feature_progress: Optional[float] = None,
) -> dict[str, Any]:
    """Display projection of one record. Asset refs are rewritten through the
    bridge proxy URL form. images[] is non-null but may be empty.
    """
    try:
        data = json.loads(data_json)
    except Exception:
        data = {}

    cover = _bridge_asset_url(job_id, data.get("cover_image"))
    raw_images = data.get("images") or []
    images = [
        u for u in (_bridge_asset_url(job_id, r) for r in raw_images if isinstance(r, str))
        if u
    ]
    image_count = len(images) + (1 if cover else 0)

    perfs = [p for p in (data.get("performers") or []) if isinstance(p, str) and p.strip()]

    return {
        "job_id": job_id,
        "job_name": job_name,
        "result_index": result_index,
        "id": (data.get("id") or None),
        "title": data.get("title") or None,
        "details": data.get("details") or None,
        "url": page_url or data.get("url") or None,
        "date": data.get("date") or None,
        "cover_image": cover,
        "images": images,
        "performers": perfs,
        "performer_count": len(perfs),
        "image_count": image_count,
        "feature_state": feature_state,
        "feature_progress": feature_progress,
    }


# ----- /jobs ----------------------------------------------------------------

@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """All cached extractor jobs with record counts and full featurization
    state. The viewer's job filter dropdown reads `feature_state` only; the
    Status page uses the rest. Order: most recent completed_at first.
    """
    rows: list[dict[str, Any]] = []
    async with cdb.db().execute(
        "SELECT j.job_id, j.job_name, j.completed_at, "
        "       (SELECT COUNT(*) FROM extractor_results r WHERE r.job_id = j.job_id) AS rc, "
        "       f.state, f.progress, f.started_at, f.finished_at, f.error "
        "FROM extractor_jobs j "
        "LEFT JOIN job_feature_state f ON f.job_id = j.job_id "
        "ORDER BY j.completed_at DESC, j.job_id ASC"
    ) as cur:
        async for row in cur:
            rows.append({
                "job_id": row[0],
                "job_name": row[1],
                "completed_at": row[2],
                "record_count": int(row[3] or 0),
                "feature_state": row[4],
                "feature_progress": row[5],
                "feature_started_at": row[6],
                "feature_finished_at": row[7],
                "feature_error": row[8],
            })
    return rows


# ----- /records (list) ------------------------------------------------------

def _records_where_and_params(
    job_id: Optional[str], q: Optional[str],
) -> tuple[str, list[Any]]:
    """Build the WHERE clause shared by the records list and its count."""
    conds: list[str] = []
    params: list[Any] = []

    if job_id:
        conds.append("er.job_id = ?")
        params.append(job_id)

    if q:
        # Match if any of: id == q, title LIKE %q%, page_url LIKE %q%, OR
        # any performer for the record matches name_lower LIKE %q%.
        # JSON1's `data->>'id'` extracts the id field; ESCAPE handles user-supplied wildcards.
        like = f"%{_escape_like(q.lower())}%"
        conds.append(
            "(LOWER(COALESCE(er.data_json->>'id', '')) = ? "
            " OR LOWER(COALESCE(er.data_json->>'title', '')) LIKE ? ESCAPE '\\' "
            " OR LOWER(COALESCE(er.page_url, '')) LIKE ? ESCAPE '\\' "
            " OR EXISTS ("
            "      SELECT 1 FROM performer_index pi "
            "      WHERE pi.job_id = er.job_id AND pi.result_index = er.result_index "
            "        AND pi.name_lower LIKE ? ESCAPE '\\'"
            "    ))"
        )
        params.extend([q.lower(), like, like, like])

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _records_order_by(sort: str, dir_: str) -> str:
    """Build ORDER BY honoring §10 deterministic tiebreak (job_id, result_index)."""
    direction = "ASC" if dir_ == "asc" else "DESC"
    if sort == "id":
        col = "COALESCE(er.data_json->>'id', '')"
    elif sort == "title":
        col = "LOWER(COALESCE(er.data_json->>'title', ''))"
    elif sort == "date":
        col = "COALESCE(er.data_json->>'date', '')"
    elif sort == "performer":
        col = ("(SELECT MIN(name_lower) FROM performer_index pi "
               " WHERE pi.job_id = er.job_id AND pi.result_index = er.result_index)")
    else:
        col = "er.result_index"
    return f"ORDER BY {col} {direction}, er.job_id ASC, er.result_index ASC"


@router.get("/records")
async def list_records(
    job_id: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = Query("id", pattern="^(id|title|date|performer)$"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    if sort not in _VALID_SORTS or dir not in _VALID_DIRS:
        raise HTTPException(status_code=400, detail="invalid sort/dir")

    where, params = _records_where_and_params(job_id, q)

    # Total count
    async with cdb.db().execute(
        f"SELECT COUNT(*) FROM extractor_results er {where}", params,
    ) as cur:
        total_row = await cur.fetchone()
    total = int(total_row[0]) if total_row else 0

    offset = (page - 1) * limit
    order_by = _records_order_by(sort, dir)
    sql = (
        "SELECT er.job_id, j.job_name, er.result_index, er.page_url, er.data_json, "
        "       f.state, f.progress "
        "FROM extractor_results er "
        "JOIN extractor_jobs j ON j.job_id = er.job_id "
        "LEFT JOIN job_feature_state f ON f.job_id = er.job_id "
        f"{where} {order_by} "
        "LIMIT ? OFFSET ?"
    )
    rows: list[dict[str, Any]] = []
    async with cdb.db().execute(sql, [*params, limit, offset]) as cur:
        async for row in cur:
            rows.append(_project_record(
                job_id=row[0], job_name=row[1], result_index=int(row[2]),
                page_url=row[3], data_json=row[4],
                feature_state=row[5], feature_progress=row[6],
            ))

    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "data": rows,
        "pagination": {
            "page": page, "limit": limit,
            "total": total, "totalPages": total_pages,
        },
    }


# ----- /records/{job_id}/{result_index} -------------------------------------

@router.get("/records/{job_id}/{result_index}")
async def get_record(job_id: str, result_index: int) -> dict[str, Any]:
    async with cdb.db().execute(
        "SELECT er.job_id, j.job_name, er.result_index, er.page_url, er.data_json, "
        "       f.state, f.progress "
        "FROM extractor_results er "
        "JOIN extractor_jobs j ON j.job_id = er.job_id "
        "LEFT JOIN job_feature_state f ON f.job_id = er.job_id "
        "WHERE er.job_id = ? AND er.result_index = ?",
        (job_id, result_index),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="record not found")
    return _project_record(
        job_id=row[0], job_name=row[1], result_index=int(row[2]),
        page_url=row[3], data_json=row[4],
        feature_state=row[5], feature_progress=row[6],
    )


# ----- /performers (list) ---------------------------------------------------

@router.get("/performers")
async def list_performers(
    job_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """Aggregate over performer_index. Each row in the response is one
    distinct (case-insensitive) performer name; the display casing comes
    from the lex-min original observed.
    """
    conds: list[str] = []
    params: list[Any] = []
    if job_id:
        conds.append("pi.job_id = ?")
        params.append(job_id)
    if q:
        conds.append("pi.name_lower LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(q.lower())}%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    # Total distinct performers
    async with cdb.db().execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM performer_index pi {where} GROUP BY name_lower)",
        params,
    ) as cur:
        total = int((await cur.fetchone())[0] or 0)

    offset = (page - 1) * limit
    sql = (
        "SELECT pi.name_lower, MIN(pi.name_original) AS name_display, "
        "       COUNT(*) AS record_count, "
        "       COUNT(DISTINCT pi.job_id) AS job_count "
        f"FROM performer_index pi {where} "
        "GROUP BY pi.name_lower "
        "ORDER BY pi.name_lower ASC "
        "LIMIT ? OFFSET ?"
    )
    rows: list[dict[str, Any]] = []
    async with cdb.db().execute(sql, [*params, limit, offset]) as cur:
        async for row in cur:
            rows.append({
                "name_lower": row[0],
                "name_display": row[1],
                "record_count": int(row[2]),
                "job_count": int(row[3]),
            })

    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "data": rows,
        "pagination": {
            "page": page, "limit": limit,
            "total": total, "totalPages": total_pages,
        },
    }


# ----- /performers/{name} ---------------------------------------------------

@router.get("/performers/{name}")
async def get_performer(name: str) -> dict[str, Any]:
    """Records associated with a performer, grouped by job. Match is
    case-insensitive equality on `name_lower`.
    """
    name_lower = name.lower().strip()
    if not name_lower:
        raise HTTPException(status_code=400, detail="empty performer name")

    # Pick the lex-min display casing for the header
    async with cdb.db().execute(
        "SELECT MIN(name_original) FROM performer_index WHERE name_lower = ?",
        (name_lower,),
    ) as cur:
        row = await cur.fetchone()
    if row is None or row[0] is None:
        raise HTTPException(status_code=404, detail="performer not found")
    name_display = row[0]

    sql = (
        "SELECT er.job_id, j.job_name, er.result_index, er.page_url, er.data_json, "
        "       f.state, f.progress "
        "FROM performer_index pi "
        "JOIN extractor_results er ON er.job_id = pi.job_id AND er.result_index = pi.result_index "
        "JOIN extractor_jobs j ON j.job_id = er.job_id "
        "LEFT JOIN job_feature_state f ON f.job_id = er.job_id "
        "WHERE pi.name_lower = ? "
        "ORDER BY j.completed_at DESC, er.job_id ASC, er.result_index ASC"
    )
    jobs: dict[str, dict[str, Any]] = {}
    async with cdb.db().execute(sql, (name_lower,)) as cur:
        async for row in cur:
            j_id = row[0]
            entry = jobs.setdefault(j_id, {
                "job_id": j_id, "job_name": row[1], "records": [],
            })
            entry["records"].append(_project_record(
                job_id=row[0], job_name=row[1], result_index=int(row[2]),
                page_url=row[3], data_json=row[4],
                feature_state=row[5], feature_progress=row[6],
            ))

    return {
        "name_lower": name_lower,
        "name_display": name_display,
        "record_count": sum(len(j["records"]) for j in jobs.values()),
        "job_count": len(jobs),
        "jobs": list(jobs.values()),
    }
