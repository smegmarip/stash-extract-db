"""SQLite cache. Layout per requirements.md §8."""
import hashlib
import json
import logging
import os
from typing import Any, Optional

import aiosqlite

from ..settings import settings

logger = logging.getLogger(__name__)

_db: Optional[aiosqlite.Connection] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS extractor_jobs (
  job_id        TEXT PRIMARY KEY,
  job_name      TEXT NOT NULL,
  schema_id     TEXT NOT NULL,
  completed_at  TEXT NOT NULL,
  fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractor_results (
  job_id        TEXT NOT NULL,
  result_index  INTEGER NOT NULL,
  page_url      TEXT,
  data_json     TEXT NOT NULL,
  record_uuid   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (job_id, result_index),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_extractor_results_uuid
  ON extractor_results(record_uuid)
  WHERE record_uuid != '';

CREATE INDEX IF NOT EXISTS idx_jobs_name_lower ON extractor_jobs(LOWER(job_name));

CREATE TABLE IF NOT EXISTS image_hashes (
  source        TEXT NOT NULL,
  ref_id        TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,
  algorithm     TEXT NOT NULL,
  hash_size     INTEGER NOT NULL,
  phash_hex     TEXT NOT NULL,
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (source, ref_id, algorithm, hash_size)
);

CREATE TABLE IF NOT EXISTS match_results (
  scene_id          TEXT NOT NULL,
  job_id            TEXT NOT NULL,
  result_index      INTEGER NOT NULL,
  image_mode        TEXT NOT NULL,
  similarity        REAL NOT NULL,
  scene_fingerprint TEXT NOT NULL,
  job_completed_at  TEXT NOT NULL,
  PRIMARY KEY (scene_id, job_id, result_index, image_mode)
);

-- Multi-channel image features. Replaces image_hashes after migration.
-- See CLAUDE.md §15.
CREATE TABLE IF NOT EXISTS image_features (
  source           TEXT NOT NULL,
  ref_id           TEXT NOT NULL,
  fingerprint      TEXT NOT NULL,
  channel          TEXT NOT NULL,
  algorithm        TEXT NOT NULL,
  feature_blob     BLOB NOT NULL,
  quality          REAL NOT NULL,
  computed_at      TEXT NOT NULL,
  last_accessed_at TEXT,
  PRIMARY KEY (source, ref_id, channel, algorithm)
);

CREATE INDEX IF NOT EXISTS idx_features_ref ON image_features(source, ref_id);
CREATE INDEX IF NOT EXISTS idx_features_lru ON image_features(last_accessed_at)
  WHERE last_accessed_at IS NOT NULL;

-- Per-job, per-channel corpus statistics (empirical baseline / noise floor).
CREATE TABLE IF NOT EXISTS corpus_stats (
  job_id        TEXT NOT NULL,
  channel       TEXT NOT NULL,
  algorithm     TEXT NOT NULL,
  baseline      REAL NOT NULL,
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (job_id, channel, algorithm),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

-- Per record-image uniqueness (c_i), corpus-relative within a job's record set.
CREATE TABLE IF NOT EXISTS image_uniqueness (
  job_id        TEXT NOT NULL,
  ref_id        TEXT NOT NULL,
  channel       TEXT NOT NULL,
  uniqueness    REAL NOT NULL,
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (job_id, ref_id, channel),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

-- Featurization lifecycle state per job.
-- progress=0 with state='featurizing' means queued-not-started; >0 means in flight.
CREATE TABLE IF NOT EXISTS job_feature_state (
  job_id        TEXT PRIMARY KEY,
  state         TEXT NOT NULL,
  progress      REAL NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  error         TEXT,
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

-- Display-only performer aggregation index for the viewer service.
-- Populated synchronously with extractor_results inside upsert_job_and_results;
-- cascaded by FK chain through extractor_results -> extractor_jobs.
-- name_lower is used for grouping and search; name_original preserves display casing.
CREATE TABLE IF NOT EXISTS performer_index (
  job_id        TEXT NOT NULL,
  result_index  INTEGER NOT NULL,
  name_lower    TEXT NOT NULL,
  name_original TEXT NOT NULL,
  PRIMARY KEY (job_id, result_index, name_lower),
  FOREIGN KEY (job_id, result_index) REFERENCES extractor_results(job_id, result_index) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_performer_index_name ON performer_index(name_lower);
CREATE INDEX IF NOT EXISTS idx_performer_index_job ON performer_index(job_id);
"""


def compute_record_uuid(
    job_id: str, page_url: Optional[str], result_index: int, data: dict[str, Any],
) -> str:
    """Stable, content-derived record identifier.

    Survives cache invalidation and re-fetch (cascade per §7) when job_id +
    page_url + result_index + data are unchanged. Independent of `data.id`
    (which is an optional scraped field, not a record identity). job_id is
    in the seed so two jobs with identical record content don't collide on
    the global UNIQUE index. 16 hex chars (= 64 bits) — well inside
    collision tolerance.
    """
    canonical = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    seed = f"{job_id}:{page_url or ''}:{result_index}:{canonical}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


async def _migrate_record_uuid(conn: aiosqlite.Connection) -> None:
    """Backfill record_uuid for any existing rows that pre-date the column.

    Idempotent: only touches rows with empty record_uuid. Safe to run on
    every startup. Migration completes before the unique index is enforced
    against populated rows (the partial index excludes empty values).
    """
    async with conn.execute(
        "SELECT job_id, result_index, page_url, data_json "
        "FROM extractor_results WHERE record_uuid = ''"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return
    updates: list[tuple[str, str, int]] = []
    for job_id, result_index, page_url, data_json in rows:
        try:
            data = json.loads(data_json)
        except Exception:
            data = {}
        uuid = compute_record_uuid(str(job_id), page_url, int(result_index), data)
        updates.append((uuid, str(job_id), int(result_index)))
    await conn.executemany(
        "UPDATE extractor_results SET record_uuid = ? "
        "WHERE job_id = ? AND result_index = ?",
        updates,
    )
    await conn.commit()
    logger.info("record_uuid backfill: populated %d row(s)", len(updates))


async def init_db() -> None:
    global _db
    os.makedirs(settings.data_dir, exist_ok=True)
    db_path = os.path.join(settings.data_dir, "stash-extract-db.db")
    # isolation_level=None puts the connection in autocommit mode. The bridge
    # shares one connection across concurrent tasks (lifecycle worker doing
    # featurization DMLs while startup_discover seeds new jobs). With Python's
    # default isolation_level="", every DML implicitly opens a transaction
    # that stays open until commit() — so when one task is mid-DML and another
    # task issues `BEGIN` for an explicit multi-statement transaction
    # (upsert_job_and_results), sqlite errors with "cannot start a transaction
    # within a transaction" and silently drops the second task's seed.
    # Autocommit closes the implicit-txn window; explicit BEGIN/COMMIT pairs
    # in upsert_job_and_results still work as expected.
    _db = await aiosqlite.connect(db_path, isolation_level=None)
    await _db.execute("PRAGMA foreign_keys = ON")
    await _db.executescript(SCHEMA)
    await _db.commit()
    # Existing-DB migration: pre-existing rows have record_uuid='' (from the
    # NOT NULL DEFAULT). Compute and write the deterministic value.
    await _migrate_record_uuid(_db)
    logger.info("SQLite cache ready at %s", db_path)


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("DB not initialized; call init_db()")
    return _db


# --- extractor_jobs ----------------------------------------------------

async def get_cached_job(job_id: str) -> Optional[dict[str, Any]]:
    async with db().execute(
        "SELECT job_id, job_name, schema_id, completed_at, fetched_at FROM extractor_jobs WHERE job_id = ?",
        (job_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"job_id": row[0], "job_name": row[1], "schema_id": row[2],
            "completed_at": row[3], "fetched_at": row[4]}


def _is_substantive_record(data: dict[str, Any]) -> bool:
    """True iff a record carries ≥ 2 fields of structured content.

    Filters soft-404s (CLAUDE.md §18). Sites like the Internet Archive return
    HTTP 200 for missing pages, so the extractor scrapes them and produces
    records that contain only the URL (always captured) plus generic page
    chrome (e.g. <title>Wayback Machine</title>). These records pollute
    scrape/search results because URL-equality, short-title exact match, or
    regex'd `data.id` from URL path can fire spuriously.

    Substantive fields (the §6 canonical schema, minus `url`/`page_url`
    which is trivially captured for any HTTP 200):

        id, title, details, date, cover_image, images[], performers[]

    Requiring ≥ 2 passes legitimately-sparse records (id + performers,
    title + date) while rejecting URL-only and URL+chrome-title soft-404s.
    Single text fields are not enough — title and details are exactly the
    fields most prone to regex contamination from page chrome.
    """
    if not isinstance(data, dict):
        return False
    count = 0
    for key in ("id", "title", "details", "date", "cover_image"):
        v = data.get(key)
        if isinstance(v, str):
            v = v.strip()
        if v:
            count += 1
            if count >= 2:
                return True
    for key in ("images", "performers"):
        v = data.get(key)
        if isinstance(v, list) and any(v):
            count += 1
            if count >= 2:
                return True
    return count >= 2


def _extract_performer_rows(
    job_id: str, result_index: int, data: dict[str, Any],
) -> list[tuple[str, int, str, str]]:
    """Build performer_index rows for a single record.

    Performers are a list of strings on `data.performers`. Skip null/empty;
    dedupe within a record by lowercased name.
    """
    perfs = data.get("performers") or []
    seen: dict[str, str] = {}
    for p in perfs:
        if not isinstance(p, str):
            continue
        name = p.strip()
        if not name:
            continue
        key = name.lower()
        # First occurrence wins for the display casing.
        seen.setdefault(key, name)
    return [(job_id, result_index, key, name) for key, name in seen.items()]


async def upsert_job_and_results(
    job_id: str,
    job_name: str,
    schema_id: str,
    completed_at: str,
    fetched_at: str,
    results: list[dict[str, Any]],
) -> None:
    """Atomically replace the job row + all of its results.

    Cascade per CLAUDE.md §7 + §14.5: deleting the
    extractor_jobs row cascades via FK to extractor_results, corpus_stats,
    image_uniqueness, job_feature_state, and performer_index (transitively
    through extractor_results). match_results and the extractor-side
    image_features rows have no FK and are deleted manually. Stash-side
    image_features rows survive — they're keyed by Stash content fingerprint,
    not by job, and remain valid across job changes.
    """
    # Drop soft-404s before they enter the cache (CLAUDE.md §18). result_index
    # is assigned over survivors only — gap-free indices keep image_features
    # ref_id math (`<job_id>:<record_idx>`) consistent with the row's order.
    filtered: list[dict[str, Any]] = []
    dropped = 0
    for r in results:
        data = r.get("data") or {}
        if _is_substantive_record(data):
            filtered.append(r)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "upsert_job_and_results(%s): dropped %d non-substantive record(s)",
            job_id, dropped,
        )

    conn = db()
    await conn.execute("BEGIN")
    try:
        await conn.execute("DELETE FROM extractor_jobs WHERE job_id = ?", (job_id,))
        await conn.execute(
            "INSERT INTO extractor_jobs(job_id, job_name, schema_id, completed_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, job_name, schema_id, completed_at, fetched_at),
        )
        rows = []
        perf_rows: list[tuple[str, int, str, str]] = []
        for idx, r in enumerate(filtered):
            page_url = r.get("page_url") or ""
            data = r.get("data") or {}
            data_json = json.dumps(data, ensure_ascii=False)
            record_uuid = compute_record_uuid(job_id, page_url, idx, data)
            rows.append((job_id, idx, page_url, data_json, record_uuid))
            perf_rows.extend(_extract_performer_rows(job_id, idx, data))
        await conn.executemany(
            "INSERT INTO extractor_results(job_id, result_index, page_url, data_json, record_uuid) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        if perf_rows:
            await conn.executemany(
                "INSERT INTO performer_index(job_id, result_index, name_lower, name_original) "
                "VALUES (?, ?, ?, ?)",
                perf_rows,
            )
        await conn.execute("DELETE FROM match_results WHERE job_id = ?", (job_id,))
        # Extractor-side image_features rows have no FK to extractor_jobs
        # (Stash-side rows must survive); purge them manually.
        await conn.execute(
            "DELETE FROM image_features "
            "WHERE source IN ('extractor_image', 'extractor_aggregate') "
            "  AND ref_id LIKE ? || ':%'",
            (job_id,),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def purge_soft_404_records() -> int:
    """One-shot startup cleanup of soft-404 records cached before the §18
    filter was introduced. Idempotent — a second run on a clean cache is a
    no-op.

    Scans every cached `extractor_results` row. For each job that contains
    at least one offending row, rebuilds the job via `upsert_job_and_results`
    with only its substantive records. The standard cascade (§14.5) fires:
    `job_feature_state`, `corpus_stats`, `image_uniqueness`, and
    extractor-side `image_features` rows are cleared for the job, and the
    startup lifecycle worker (`startup_recover`) will re-featurize it
    against the cleaned corpus.

    Cost: one-shot re-featurization for affected jobs only. Subsequent
    runs of the bridge skip this work entirely.

    Returns the number of records dropped.
    """
    affected: dict[str, dict[str, Any]] = {}
    async with db().execute(
        "SELECT job_id, result_index, page_url, data_json FROM extractor_results "
        "ORDER BY job_id, result_index ASC"
    ) as cur:
        async for row in cur:
            try:
                data = json.loads(row[3])
            except Exception:
                data = {}
            entry = affected.setdefault(row[0], {"survivors": [], "dropped": 0})
            if _is_substantive_record(data):
                entry["survivors"].append({"page_url": row[1], "data": data})
            else:
                entry["dropped"] += 1

    jobs_with_drops = [jid for jid, e in affected.items() if e["dropped"] > 0]
    if not jobs_with_drops:
        return 0

    total_dropped = sum(affected[jid]["dropped"] for jid in jobs_with_drops)
    logger.info(
        "soft-404 purge: %d job(s) carry %d offending row(s); rebuilding",
        len(jobs_with_drops), total_dropped,
    )
    for job_id in jobs_with_drops:
        job_row = await get_cached_job(job_id)
        if job_row is None:
            continue
        await upsert_job_and_results(
            job_id=job_id,
            job_name=job_row["job_name"],
            schema_id=job_row["schema_id"],
            completed_at=job_row["completed_at"],
            fetched_at=job_row["fetched_at"],
            results=affected[job_id]["survivors"],
        )
    logger.info(
        "soft-404 purge: dropped %d row(s) across %d job(s)",
        total_dropped, len(jobs_with_drops),
    )
    return total_dropped


async def backfill_performer_index() -> int:
    """Populate performer_index from extractor_results rows that are not yet
    indexed. Idempotent — uses INSERT OR IGNORE; safe to run on every startup.
    Returns the number of rows inserted.

    A job is treated as "needs backfill" if it has any extractor_results rows
    but no performer_index rows. This avoids re-scanning jobs whose records
    legitimately have no performers (a non-empty extractor_results paired with
    an empty performer_index is indistinguishable from "not yet backfilled"
    using row counts alone, but INSERT OR IGNORE makes a re-scan cheap).
    """
    import json
    conn = db()
    inserted = 0
    # Find candidate jobs: those with results but zero performer_index rows.
    async with conn.execute(
        "SELECT DISTINCT er.job_id FROM extractor_results er "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM performer_index pi WHERE pi.job_id = er.job_id"
        ")"
    ) as cur:
        candidate_jobs = [row[0] async for row in cur]

    if not candidate_jobs:
        return 0

    logger.info("performer_index backfill: %d candidate job(s)", len(candidate_jobs))
    for job_id in candidate_jobs:
        async with conn.execute(
            "SELECT result_index, data_json FROM extractor_results WHERE job_id = ?",
            (job_id,),
        ) as cur:
            results = await cur.fetchall()
        rows: list[tuple[str, int, str, str]] = []
        for result_index, data_json in results:
            try:
                data = json.loads(data_json)
            except Exception:
                continue
            rows.extend(_extract_performer_rows(job_id, int(result_index), data))
        if not rows:
            continue
        await conn.executemany(
            "INSERT OR IGNORE INTO performer_index(job_id, result_index, name_lower, name_original) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        inserted += len(rows)
    await conn.commit()
    if inserted:
        logger.info("performer_index backfill: inserted %d row(s)", inserted)
    return inserted


async def list_results(job_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with db().execute(
        "SELECT result_index, page_url, data_json, record_uuid "
        "FROM extractor_results WHERE job_id = ? ORDER BY result_index ASC",
        (job_id,),
    ) as cur:
        async for row in cur:
            try:
                data = json.loads(row[2])
            except Exception:
                data = {}
            out.append({
                "result_index": row[0],
                "page_url": row[1],
                "data": data,
                "record_uuid": row[3],
            })
    return out


async def get_result_by_uuid(record_uuid: str) -> Optional[dict[str, Any]]:
    """Look up an extractor record by its content-derived `record_uuid`
    (CLAUDE.md §15.4). Returns None if no match.

    Used by the sceneByQueryFragment round-trip (CLAUDE.md §17): the bridge
    stamps `remote_site_id = record_uuid` on every search result, Stash echoes
    it back when the user picks one, and this lookup resolves it without
    re-running the matching engine."""
    if not record_uuid:
        return None
    async with db().execute(
        "SELECT job_id, result_index, page_url, data_json, record_uuid "
        "FROM extractor_results WHERE record_uuid = ?",
        (record_uuid,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[3])
    except Exception:
        data = {}
    return {
        "job_id": row[0],
        "result_index": row[1],
        "page_url": row[2],
        "data": data,
        "record_uuid": row[4],
    }


# --- image_hashes ------------------------------------------------------

async def get_image_hash(source: str, ref_id: str, fingerprint: str, algorithm: str, hash_size: int) -> Optional[str]:
    async with db().execute(
        "SELECT phash_hex FROM image_hashes WHERE source=? AND ref_id=? AND algorithm=? AND hash_size=? AND fingerprint=?",
        (source, ref_id, algorithm, hash_size, fingerprint),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_image_hash(source: str, ref_id: str, fingerprint: str, algorithm: str, hash_size: int, phash_hex: str) -> None:
    from datetime import datetime
    await db().execute(
        "INSERT OR REPLACE INTO image_hashes(source, ref_id, fingerprint, algorithm, hash_size, phash_hex, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, ref_id, fingerprint, algorithm, hash_size, phash_hex, datetime.utcnow().isoformat()),
    )
    await db().commit()


# --- image_features (multi-channel; will replace image_hashes — see CLAUDE.md §15) -----

_STASH_SOURCES = ("stash_cover", "stash_sprite", "stash_aggregate")


# Sentinel quality value indicating "we tried to compute this feature and
# couldn't" (asset 404, decode failed, low-variance pHash filter, etc.).
# Stored to suppress repeat fetch+compute attempts on every scrape; cleared
# by the same cascade invalidation that clears successful rows.
NEGATIVE_CACHE_QUALITY = -1.0


async def get_image_feature(
    source: str, ref_id: str, fingerprint: str, channel: str, algorithm: str,
) -> Optional[tuple[bytes, float]]:
    """Returns (feature_blob, quality) or None on miss.

    Fingerprint match is required — a row whose stored fingerprint differs from
    the caller's is treated as a miss (the source content has changed; the
    cached blob is stale).

    A row with quality == NEGATIVE_CACHE_QUALITY is a "tried, no usable result"
    sentinel and is also returned as None — but the caller short-circuits the
    fetch+compute retry. Use `is_feature_attempt_cached` to distinguish miss
    from sentinel.

    Side effect: on a Stash-side hit, updates `last_accessed_at` to support
    LRU eviction (CLAUDE.md §14.9). Extractor-side rows skip the touch — they're
    cleared by job-cascade invalidation, not LRU.
    """
    async with db().execute(
        "SELECT feature_blob, quality FROM image_features "
        "WHERE source=? AND ref_id=? AND channel=? AND algorithm=? AND fingerprint=?",
        (source, ref_id, channel, algorithm, fingerprint),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    if row[1] == NEGATIVE_CACHE_QUALITY:
        # Sentinel: prior compute failed; caller should not retry.
        return None
    if source in _STASH_SOURCES:
        from datetime import datetime
        await db().execute(
            "UPDATE image_features SET last_accessed_at=? "
            "WHERE source=? AND ref_id=? AND channel=? AND algorithm=?",
            (datetime.utcnow().isoformat(), source, ref_id, channel, algorithm),
        )
        await db().commit()
    return (row[0], row[1])


async def is_feature_attempt_cached(
    source: str, ref_id: str, fingerprint: str, channel: str, algorithm: str,
) -> bool:
    """True if a row exists for this (source, ref, channel, algo, fingerprint)
    — whether successful or a negative-cache sentinel. Lets callers
    short-circuit the fetch+compute path without first attempting to read
    the (possibly missing) feature blob.
    """
    async with db().execute(
        "SELECT 1 FROM image_features "
        "WHERE source=? AND ref_id=? AND channel=? AND algorithm=? AND fingerprint=? "
        "LIMIT 1",
        (source, ref_id, channel, algorithm, fingerprint),
    ) as cur:
        row = await cur.fetchone()
    return row is not None


async def set_feature_attempt_failed(
    source: str, ref_id: str, fingerprint: str, channel: str, algorithm: str,
) -> None:
    """Write a negative-cache sentinel row indicating "we tried to compute
    this feature and couldn't" (asset 404, decode failure, low-variance
    filter, etc.). Suppresses repeat fetch+compute attempts on every scrape.
    """
    await set_image_feature(
        source, ref_id, fingerprint, channel, algorithm,
        feature_blob=b"", quality=NEGATIVE_CACHE_QUALITY,
    )


async def set_image_feature(
    source: str, ref_id: str, fingerprint: str, channel: str, algorithm: str,
    feature_blob: bytes, quality: float,
) -> None:
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    # Stash-side rows participate in LRU eviction; seed last_accessed_at on
    # write so a newly-cached row has a valid timestamp until first re-read.
    last_accessed = now if source in _STASH_SOURCES else None
    await db().execute(
        "INSERT OR REPLACE INTO image_features"
        "(source, ref_id, fingerprint, channel, algorithm, feature_blob, quality, computed_at, last_accessed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, ref_id, fingerprint, channel, algorithm, feature_blob, quality, now, last_accessed),
    )
    await db().commit()


# --- corpus_stats / image_uniqueness ----------------------------------

async def set_corpus_stat(job_id: str, channel: str, algorithm: str, baseline: float) -> None:
    from datetime import datetime
    await db().execute(
        "INSERT OR REPLACE INTO corpus_stats(job_id, channel, algorithm, baseline, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, channel, algorithm, baseline, datetime.utcnow().isoformat()),
    )
    await db().commit()


async def get_corpus_stat(job_id: str, channel: str, algorithm: str) -> Optional[float]:
    async with db().execute(
        "SELECT baseline FROM corpus_stats WHERE job_id=? AND channel=? AND algorithm=?",
        (job_id, channel, algorithm),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_image_uniqueness(job_id: str, ref_id: str, channel: str, uniqueness: float) -> None:
    from datetime import datetime
    await db().execute(
        "INSERT OR REPLACE INTO image_uniqueness(job_id, ref_id, channel, uniqueness, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, ref_id, channel, uniqueness, datetime.utcnow().isoformat()),
    )
    await db().commit()


async def get_image_uniqueness(job_id: str, ref_id: str, channel: str) -> Optional[float]:
    async with db().execute(
        "SELECT uniqueness FROM image_uniqueness WHERE job_id=? AND ref_id=? AND channel=?",
        (job_id, ref_id, channel),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


# --- job_feature_state -------------------------------------------------

async def get_feature_state(job_id: str) -> Optional[dict[str, Any]]:
    async with db().execute(
        "SELECT job_id, state, progress, started_at, finished_at, error "
        "FROM job_feature_state WHERE job_id=?",
        (job_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"job_id": row[0], "state": row[1], "progress": row[2],
            "started_at": row[3], "finished_at": row[4], "error": row[5]}


async def upsert_feature_state(
    job_id: str, state: str, progress: float,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    from datetime import datetime
    started = started_at or datetime.utcnow().isoformat()
    await db().execute(
        "INSERT INTO job_feature_state(job_id, state, progress, started_at, finished_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "  state=excluded.state, progress=excluded.progress, "
        "  started_at=excluded.started_at, finished_at=excluded.finished_at, error=excluded.error",
        (job_id, state, progress, started, finished_at, error),
    )
    await db().commit()


async def set_feature_progress(job_id: str, progress: float) -> None:
    await db().execute(
        "UPDATE job_feature_state SET progress=? WHERE job_id=?",
        (progress, job_id),
    )
    await db().commit()


async def mark_feature_ready(job_id: str) -> None:
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    await db().execute(
        "UPDATE job_feature_state SET state='ready', progress=1.0, finished_at=?, error=NULL "
        "WHERE job_id=?",
        (now, job_id),
    )
    await db().commit()


async def mark_feature_failed(job_id: str, error: str) -> None:
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    await db().execute(
        "UPDATE job_feature_state SET state='failed', finished_at=?, error=? WHERE job_id=?",
        (now, error, job_id),
    )
    await db().commit()


async def list_jobs_needing_featurization(stale_started_before: str) -> list[str]:
    """Return job_ids that are eligible for featurization on startup or
    cascade. Per §4.10:
      - extractor_jobs rows with no job_feature_state row, OR
      - rows whose state != 'ready'
    Stale 'featurizing' rows (started_at < stale_started_before) are
    eligible too — the caller should reset them before enqueuing.
    """
    rows = []
    async with db().execute(
        "SELECT j.job_id FROM extractor_jobs j "
        "LEFT JOIN job_feature_state f USING (job_id) "
        "WHERE f.state IS NULL OR f.state != 'ready'"
    ) as cur:
        async for row in cur:
            rows.append(row[0])
    return rows


async def reset_stale_featurizing(stale_started_before: str) -> int:
    """Reset 'featurizing' rows interrupted by the previous shutdown.
    Per §4.3: state='featurizing' with started_at < stale_started_before is
    treated as stuck and reset to progress=0 with a fresh started_at so the
    worker re-runs it. Returns the number of rows reset.
    """
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    cur = await db().execute(
        "UPDATE job_feature_state "
        "SET progress=0, started_at=?, error=NULL "
        "WHERE state='featurizing' AND started_at < ?",
        (now, stale_started_before),
    )
    await db().commit()
    return cur.rowcount or 0


async def feature_state_counts() -> dict[str, int]:
    """For the fleet status endpoint."""
    out = {"queued": 0, "in_progress": 0, "ready": 0, "failed": 0}
    async with db().execute(
        "SELECT state, progress, COUNT(*) FROM job_feature_state GROUP BY state, (progress > 0)"
    ) as cur:
        async for state, progress_gt_zero, count in cur:
            if state == "featurizing":
                out["in_progress" if progress_gt_zero else "queued"] += count
            elif state in out:
                out[state] += count
    return out


async def stash_feature_storage_bytes() -> int:
    """Total bytes of feature_blob across all Stash-side rows. Used by LRU
    eviction (Phase 6) — extractor-side rows are excluded since they're
    bounded by job count and cleared on cascade.
    """
    async with db().execute(
        "SELECT COALESCE(SUM(LENGTH(feature_blob)), 0) FROM image_features "
        "WHERE source IN ('stash_cover', 'stash_sprite', 'stash_aggregate')"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0] if row else 0)


async def evict_lru_stash_features(target_bytes: int) -> tuple[int, int]:
    """Evict Stash-side rows ordered by last_accessed_at ASC (oldest first)
    until total storage falls below target_bytes. Rows with NULL
    last_accessed_at are evicted first (treated as "never accessed").

    Returns (rows_evicted, bytes_freed).
    """
    current = await stash_feature_storage_bytes()
    if current <= target_bytes:
        return (0, 0)

    bytes_to_free = current - target_bytes
    # Walk oldest rows in chunks to keep transactions small. Using a single
    # DELETE … ORDER BY LIMIT pattern would be cleaner, but SQLite needs
    # SQLITE_ENABLE_UPDATE_DELETE_LIMIT compiled in. Iterate instead.
    freed = 0
    evicted = 0
    while freed < bytes_to_free:
        async with db().execute(
            "SELECT source, ref_id, channel, algorithm, LENGTH(feature_blob) "
            "FROM image_features "
            f"WHERE source IN {_STASH_SOURCES} "
            "ORDER BY (last_accessed_at IS NULL) DESC, last_accessed_at ASC "
            "LIMIT 100"
        ) as cur:
            batch = await cur.fetchall()
        if not batch:
            break
        for source, ref_id, channel, algorithm, blob_len in batch:
            await db().execute(
                "DELETE FROM image_features "
                "WHERE source=? AND ref_id=? AND channel=? AND algorithm=?",
                (source, ref_id, channel, algorithm),
            )
            freed += int(blob_len)
            evicted += 1
            if freed >= bytes_to_free:
                break
        await db().commit()
    return (evicted, freed)


async def list_extractor_image_refs(job_id: str) -> list[str]:
    """Return the ref_ids for extractor_image rows belonging to this job.
    Used by the featurizer to enumerate already-cached images (idempotent
    re-runs after a partial failure). Format: '<job_id>:<image_ref>'.
    """
    out: list[str] = []
    async with db().execute(
        "SELECT DISTINCT ref_id FROM image_features "
        "WHERE source='extractor_image' AND ref_id LIKE ? || ':%'",
        (job_id,),
    ) as cur:
        async for row in cur:
            out.append(row[0])
    return out


async def list_frame_refs_for_extractor_ref(job_id: str, ref: str) -> list[str]:
    """Return the synthetic frame refs persisted for an animated extractor
    asset. Empty list when the ref is static (or never featurized).

    Synthetic refs are stored as `<job_id>:<ref>#frame_N` in image_features.
    This helper strips the `<job_id>:` prefix and returns refs in the same
    form they appear in record.data (so match-time iteration over
    expanded refs is uniform with iteration over original refs).
    """
    prefix_full = f"{job_id}:{ref}#frame_"
    prefix_ref = f"{ref}#frame_"
    out: list[str] = []
    async with db().execute(
        "SELECT DISTINCT ref_id FROM image_features "
        "WHERE source='extractor_image' AND ref_id LIKE ? "
        "ORDER BY ref_id",
        (f"{prefix_full}%",),
    ) as cur:
        async for row in cur:
            ref_id = row[0]
            if not ref_id.startswith(f"{job_id}:"):
                continue
            out.append(ref_id[len(job_id) + 1:])
    # Sort by frame index numerically — lexicographic gives frame_10 < frame_2
    def _idx(r: str) -> int:
        try:
            return int(r[len(prefix_ref):])
        except (ValueError, IndexError):
            return 0
    out.sort(key=_idx)
    return out
