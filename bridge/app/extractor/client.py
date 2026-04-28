import logging
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from ..settings import settings

logger = logging.getLogger(__name__)


def _base() -> str:
    return settings.extractor_url.rstrip("/")


async def list_completed_jobs() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{_base()}/api/jobs", params={"status": "completed", "limit": 200})
    r.raise_for_status()
    data = r.json()
    return data.get("jobs") or data if isinstance(data, list) else (data.get("jobs") or [])


async def get_job(job_id: str) -> Optional[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{_base()}/api/jobs/{job_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def get_schema(schema_id: str) -> Optional[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{_base()}/api/schemas/{schema_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def list_results(job_id: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{_base()}/api/extraction/{job_id}/results",
            params={"limit": limit, "offset": offset, "sort_dir": "asc"},
        )
    r.raise_for_status()
    data = r.json()
    return data.get("results") or []


async def list_all_results(job_id: str) -> list[dict[str, Any]]:
    """Fetch all results for a job (paginated, max 500/page)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await list_results(job_id, limit=500, offset=offset)
        if not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return out


def resolve_asset_url(job_id: str, ref: str) -> str:
    """Convert a record reference like `../assets/abc_xxx.jpg` to the
    extractor's asset endpoint."""
    if not ref:
        return ""
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    # Records use ../assets/<file> — gateway serves /api/asset/{job}/assets/<file>
    cleaned = ref.lstrip("./")
    if not cleaned.startswith("assets/"):
        cleaned = f"assets/{cleaned.split('/')[-1]}"
    return f"{_base()}/api/asset/{job_id}/{cleaned}"


async def fetch_asset(job_id: str, ref: str) -> Optional[bytes]:
    """Fetch a single extractor asset. Returns None on missing/broken
    assets (404 in particular is expected — not every record's image[]
    set was successfully downloaded by the extractor). Callers must skip
    these refs from per-image matching (see matching/image_match.py)."""
    url = resolve_asset_url(job_id, ref) if not ref.startswith("http") else ref
    if not url:
        return None
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            r = await client.get(url)
            if r.status_code == 404:
                logger.debug("extractor asset 404: %s", url)
                return None
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.warning("Extractor asset fetch failed: %s :: %s", url, e)
            return None
