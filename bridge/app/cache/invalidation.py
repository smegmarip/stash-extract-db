"""Per-request cache freshness check + result load.

Per CLAUDE.md §7: extractor_jobs.completed_at is the only invalidation
trigger for extractor result rows. On mismatch, replace atomically.
"""
import logging
from datetime import datetime
from typing import Any

from . import db as cdb
from ..extractor import client as ex_client

logger = logging.getLogger(__name__)


async def ensure_job_results_fresh(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns the cached results for `job`, refetching if `completed_at`
    has advanced since the last cache write."""
    job_id = job["id"]
    completed_at = job.get("completed_at") or ""
    cached = await cdb.get_cached_job(job_id)

    if cached and cached["completed_at"] == completed_at:
        return await cdb.list_results(job_id)

    # Stale or missing: refetch from extractor
    logger.info("Refetching extractor results for job %s (cached=%s, current=%s)",
                job_id, cached and cached["completed_at"], completed_at)
    results = await ex_client.list_all_results(job_id)
    await cdb.upsert_job_and_results(
        job_id=job_id,
        job_name=job.get("name", ""),
        schema_id=(job.get("extraction_config", {}) or {}).get("schema_id", ""),
        completed_at=completed_at,
        fetched_at=datetime.utcnow().isoformat(),
        results=results,
    )
    return await cdb.list_results(job_id)
