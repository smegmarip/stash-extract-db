"""Featurization worker pool.

Bounded global concurrency (BRIDGE_FEATURIZE_CONCURRENCY) plus a single
in-flight task per job_id. See MULTI_CHANNEL_SCORING.md §4.3 + §4.10.

Lifecycle integration points (wired in main.py):
- startup_recover() — runs after init_db on container start
- enqueue(job_id) — called from request gate (never-seen job) and from the
  cascade hook (completed_at advance)
- shutdown() — cancel all pending tasks; in-flight tasks are left to
  complete or be restarted via stale-task recovery on next boot
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..cache import db as cdb
from ..settings import settings
from .featurization import featurize_job

logger = logging.getLogger(__name__)


_inflight: dict[str, asyncio.Task] = {}
_semaphore: Optional[asyncio.Semaphore] = None
_lru_task: Optional[asyncio.Task] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy initializer — must be called inside an event loop."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.bridge_featurize_concurrency)
    return _semaphore


async def enqueue(job_id: str) -> None:
    """Idempotent enqueue. Safe to call from anywhere (request gate, cascade
    hook, startup scan). No-op if a task for this job_id is already in
    flight, or if the job is already 'ready'.
    """
    if job_id in _inflight and not _inflight[job_id].done():
        return

    state = await cdb.get_feature_state(job_id)
    if state and state["state"] == "ready":
        return

    # Mark queued in the DB before spawning the task — the request gate
    # observes this row and returns 503 immediately.
    await cdb.upsert_feature_state(job_id, "featurizing", 0.0)

    task = asyncio.create_task(_run(job_id), name=f"featurize:{job_id}")
    _inflight[job_id] = task
    task.add_done_callback(lambda t, j=job_id: _inflight.pop(j, None))


async def _run(job_id: str) -> None:
    """Worker body — semaphore-bounded across all jobs."""
    sem = _get_semaphore()
    async with sem:
        # Re-check: cascade or another path may have flipped this to 'ready'
        # between enqueue and acquisition. Short-circuit if so.
        state = await cdb.get_feature_state(job_id)
        if not state or state["state"] == "ready":
            return
        # Mark in-progress (progress > 0 distinguishes from queued)
        await cdb.set_feature_progress(job_id, 0.01)
        await featurize_job(job_id)


async def startup_recover() -> None:
    """Boot-time scan per §4.10. Idempotent — safe to call multiple times.

    Steps:
      1. Reset stale 'featurizing' rows interrupted by previous shutdown.
      2. Find all extractor_jobs that are not 'ready'.
      3. Enqueue each.
    """
    if not settings.bridge_lifecycle_enabled:
        logger.info("startup_recover: BRIDGE_LIFECYCLE_ENABLED=false; skipping")
        return

    cutoff = (datetime.utcnow() - timedelta(milliseconds=settings.bridge_stale_task_ms)).isoformat()
    reset = await cdb.reset_stale_featurizing(cutoff)
    if reset:
        logger.info("startup_recover: reset %d stale featurizing rows", reset)

    job_ids = await cdb.list_jobs_needing_featurization(cutoff)
    if not job_ids:
        logger.info("startup_recover: all known jobs are 'ready'")
        return

    logger.info("startup_recover: enqueuing %d jobs for featurization", len(job_ids))
    for jid in job_ids:
        await enqueue(jid)


async def shutdown() -> None:
    """Cancel pending tasks. In-flight tasks (currently holding the
    semaphore) are left to finish — they're idempotent and partial work is
    cached in image_features. The next boot will re-enqueue if needed via
    stale-task recovery.
    """
    global _lru_task
    pending = [t for t in _inflight.values() if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _inflight.clear()
    if _lru_task is not None and not _lru_task.done():
        _lru_task.cancel()
        try:
            await _lru_task
        except (asyncio.CancelledError, Exception):
            pass
    _lru_task = None


# --- LRU eviction loop (Phase 6) ----------------------------------------

async def start_lru_eviction_loop() -> None:
    """Background task that periodically evicts old Stash-side feature
    rows down to the configured budget. Idempotent — calling twice is a
    no-op while the loop is running.
    """
    global _lru_task
    if _lru_task is not None and not _lru_task.done():
        return
    if not settings.bridge_lifecycle_enabled:
        logger.info("lru_eviction: lifecycle disabled; skipping")
        return
    if settings.bridge_stash_feature_budget_bytes <= 0:
        logger.info("lru_eviction: budget=0 disables eviction; skipping")
        return
    _lru_task = asyncio.create_task(_lru_eviction_loop(), name="lru_eviction")


async def _lru_eviction_loop() -> None:
    interval = max(1, settings.bridge_lru_eviction_interval_s)
    budget = settings.bridge_stash_feature_budget_bytes
    while True:
        try:
            current = await cdb.stash_feature_storage_bytes()
            if current > budget:
                evicted, freed = await cdb.evict_lru_stash_features(budget)
                logger.info(
                    "lru_eviction: was=%d bytes budget=%d → evicted %d rows freed %d bytes",
                    current, budget, evicted, freed,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("lru_eviction: pass failed :: %s", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
