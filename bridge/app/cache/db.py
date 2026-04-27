"""SQLite cache. Layout per requirements.md §8."""
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
  PRIMARY KEY (job_id, result_index),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

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
"""


async def init_db() -> None:
    global _db
    os.makedirs(settings.data_dir, exist_ok=True)
    db_path = os.path.join(settings.data_dir, "stash-extract-db.db")
    _db = await aiosqlite.connect(db_path)
    await _db.execute("PRAGMA foreign_keys = ON")
    await _db.executescript(SCHEMA)
    await _db.commit()
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


async def upsert_job_and_results(
    job_id: str,
    job_name: str,
    schema_id: str,
    completed_at: str,
    fetched_at: str,
    results: list[dict[str, Any]],
) -> None:
    """Atomically replace the job row + all of its results."""
    import json
    conn = db()
    await conn.execute("BEGIN")
    try:
        # Drop old results (cascades)
        await conn.execute("DELETE FROM extractor_jobs WHERE job_id = ?", (job_id,))
        await conn.execute(
            "INSERT INTO extractor_jobs(job_id, job_name, schema_id, completed_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, job_name, schema_id, completed_at, fetched_at),
        )
        rows = []
        for idx, r in enumerate(results):
            page_url = r.get("page_url") or ""
            data_json = json.dumps(r.get("data") or {}, ensure_ascii=False)
            rows.append((job_id, idx, page_url, data_json))
        await conn.executemany(
            "INSERT INTO extractor_results(job_id, result_index, page_url, data_json) VALUES (?, ?, ?, ?)",
            rows,
        )
        # Drop now-stale match_results rows that referenced the prior completed_at
        await conn.execute("DELETE FROM match_results WHERE job_id = ?", (job_id,))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def list_results(job_id: str) -> list[dict[str, Any]]:
    import json
    out: list[dict[str, Any]] = []
    async with db().execute(
        "SELECT result_index, page_url, data_json FROM extractor_results WHERE job_id = ? ORDER BY result_index ASC",
        (job_id,),
    ) as cur:
        async for row in cur:
            try:
                data = json.loads(row[2])
            except Exception:
                data = {}
            out.append({"result_index": row[0], "page_url": row[1], "data": data})
    return out


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
