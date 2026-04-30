"""Mock site-extractor service that satisfies the bridge ↔ extractor contract.

Implements the five endpoints the bridge calls (see
bridge/app/extractor/client.py):

    GET  /api/jobs?status=completed
    GET  /api/jobs/{job_id}
    GET  /api/schemas/{schema_id}
    GET  /api/extraction/{job_id}/results?sort_dir=asc&limit=&offset=
    GET  /api/asset/{job_id}/assets/{filename}

Reads the dataset produced by gen_dataset.py from disk:

    dataset/
      schemas/
        <schema_id>.json
      jobs/
        <job_id>/
          job.json       # {id, name, status, completed_at, extraction_config}
          records.json   # list[record]   record uses ../assets/<file> refs
          assets/
            *.jpg

Multiple jobs side-by-side are supported — each subdir under `jobs/` is
loaded into the response of GET /api/jobs.

Usage:
  python -m tests.calibration.mock_extractor \
    --dataset-dir tests/calibration/dataset \
    --port 12001 \
    --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger(__name__)


def _load_dataset(dataset_dir: Path) -> tuple[dict[str, dict], dict[str, dict], dict[str, list]]:
    """Read all jobs + schemas + records into memory. Returns
    ({job_id: job_dict}, {schema_id: schema_dict}, {job_id: records_list}).
    Re-called on each /api/jobs hit so disk edits show up live."""
    jobs: dict[str, dict] = {}
    records: dict[str, list] = {}
    schemas: dict[str, dict] = {}

    schemas_dir = dataset_dir / "schemas"
    if schemas_dir.is_dir():
        for f in schemas_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                schemas[d["id"]] = d
            except Exception as e:
                logger.warning("bad schema %s :: %s", f, e)

    jobs_dir = dataset_dir / "jobs"
    if jobs_dir.is_dir():
        for jd in sorted(jobs_dir.iterdir()):
            if not jd.is_dir():
                continue
            job_file = jd / "job.json"
            rec_file = jd / "records.json"
            if not job_file.is_file() or not rec_file.is_file():
                continue
            try:
                job = json.loads(job_file.read_text())
                recs = json.loads(rec_file.read_text())
            except Exception as e:
                logger.warning("bad job dir %s :: %s", jd, e)
                continue
            jobs[job["id"]] = job
            records[job["id"]] = recs
    return jobs, schemas, records


def build_app(dataset_dir: Path) -> FastAPI:
    app = FastAPI(title="mock-extractor", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/jobs")
    async def list_jobs(status: str = Query(default=""), limit: int = Query(default=200)) -> dict:
        jobs, _, _ = _load_dataset(dataset_dir)
        out = list(jobs.values())
        if status:
            out = [j for j in out if (j.get("status") or "") == status]
        out = out[:limit]
        return {"jobs": out}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        jobs, _, _ = _load_dataset(dataset_dir)
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        return job

    @app.get("/api/schemas/{schema_id}")
    async def get_schema(schema_id: str) -> dict:
        _, schemas, _ = _load_dataset(dataset_dir)
        schema = schemas.get(schema_id)
        if not schema:
            raise HTTPException(status_code=404, detail=f"schema {schema_id} not found")
        return schema

    @app.get("/api/extraction/{job_id}/results")
    async def list_results(
        job_id: str,
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
        sort_dir: str = Query(default="asc"),
    ) -> dict:
        _, _, records = _load_dataset(dataset_dir)
        recs = records.get(job_id)
        if recs is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        # Wrap each record into the extractor's result shape:
        # {result_index, page_url, data}
        wrapped = [
            {"result_index": i, "page_url": rec.get("url", ""), "data": rec}
            for i, rec in enumerate(recs)
        ]
        if sort_dir == "desc":
            wrapped = list(reversed(wrapped))
        page = wrapped[offset:offset + limit]
        return {"results": page, "total": len(wrapped)}

    @app.get("/api/asset/{job_id}/assets/{filename}")
    async def get_asset(job_id: str, filename: str):
        path = dataset_dir / "jobs" / job_id / "assets" / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"asset {filename} not found")
        return FileResponse(path)

    @app.exception_handler(404)
    async def not_found(request, exc):  # type: ignore[no-redef]
        return JSONResponse(status_code=404, content={"detail": str(exc.detail)})

    return app


def main() -> int:
    p = argparse.ArgumentParser(description="Mock site-extractor for bridge calibration.")
    p.add_argument("--dataset-dir", type=Path, default=Path("tests/calibration/dataset"))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=12001)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    if not args.dataset_dir.is_dir():
        logger.error("dataset dir not found: %s", args.dataset_dir)
        return 1

    import uvicorn
    app = build_app(args.dataset_dir.resolve())
    logger.info("mock-extractor serving %s on http://%s:%d",
                args.dataset_dir.resolve(), args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
