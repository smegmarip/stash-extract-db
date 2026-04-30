"""Featurization status endpoints — see CLAUDE.md §14.7.

Per-job:    GET /api/extraction/{job_id}/features
Fleet:      GET /api/featurization/status

Both are ops + debugging. Not part of the scraper contract.
"""
from fastapi import APIRouter, HTTPException

from ..cache import db as cdb
from ..settings import settings

router = APIRouter(tags=["Featurization"])


@router.get("/api/extraction/{job_id}/features")
async def get_per_job_status(job_id: str) -> dict:
    state = await cdb.get_feature_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no feature_state row for job {job_id}")
    return {
        "state": state["state"],
        "progress": state["progress"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "error": state["error"],
    }


@router.get("/api/featurization/status")
async def get_fleet_status() -> dict:
    counts = await cdb.feature_state_counts()
    return {
        **counts,
        "concurrency_limit": settings.bridge_featurize_concurrency,
        "lifecycle_enabled": settings.bridge_lifecycle_enabled,
    }
