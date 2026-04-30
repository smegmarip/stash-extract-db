"""Import calibration videos into Stash, create the studio, link scenes.

Workflow:
  1. Read ground_truth.json from the dataset.
  2. Copy each non-negative-control video from .video_cache/ to a target
     subdirectory of the Stash library (skips negatives by design — they
     stay absent from Stash so the matcher should NOT find them).
  3. Trigger Stash's metadataScan (GraphQL) over the new subdirectory.
  4. Poll until the scan job completes and Stash has indexed the new files.
  5. Create the studio matching the calibration job's `name` (CLAUDE.md §5
     studio match is case-insensitive equality on job.name vs studio.name).
  6. For each imported video, find its scene in Stash (by file basename)
     and set the studio.

Requires the Stash GraphQL endpoint reachable + an API key (or session
cookie) when Stash auth is enabled. Reads STASH_URL / STASH_API_KEY from
the environment by default; CLI flags override.

Usage:
  python -m tests.calibration.import_to_stash \
    --dataset-dir tests/calibration/dataset \
    --video-cache-dir tests/calibration/.video_cache \
    --stash-target /path/to/stash/library/calibration/

Note: --stash-target must be a path Stash can read (i.e., on the same host
or mounted into the Stash container). The script copies videos there;
metadataScan handles the rest.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger(__name__)


# --- Stash GraphQL helpers ------------------------------------------------

class StashClient:
    def __init__(self, url: str, api_key: str = "", session_cookie: str = ""):
        self.url = url.rstrip("/") + "/graphql"
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["ApiKey"] = api_key
        self.cookies = {}
        if session_cookie:
            self.cookies["session"] = session_cookie

    def query(self, q: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": q, "variables": variables or {}}
        with httpx.Client(timeout=60) as client:
            r = client.post(self.url, json=payload, headers=self.headers, cookies=self.cookies)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"Stash GraphQL errors: {data['errors']}")
        return data["data"]

    def metadata_scan(self, paths: list[str]) -> str:
        """Trigger a scan over the given paths. Returns the job_id."""
        q = """
        mutation MetadataScan($input: ScanMetadataInput!) {
          metadataScan(input: $input)
        }
        """
        data = self.query(q, {"input": {"paths": paths}})
        return data["metadataScan"]

    def find_jobs(self, job_id) -> list[dict]:
        """Return any jobQueue entries matching `job_id` (compared
        loosely — Stash returns it as an int but older versions used
        strings). When the queue is empty Stash returns null for
        `jobQueue`; we normalize that to an empty list (the job has
        dropped off → it's done)."""
        q = """
        query JobQueue {
          jobQueue { id status description progress }
        }
        """
        data = self.query(q)
        queue = data.get("jobQueue") or []
        target = str(job_id)
        return [j for j in queue if str(j.get("id")) == target]

    def wait_for_job(self, job_id: str, timeout_s: int = 1800, poll_s: int = 5) -> bool:
        start = time.time()
        while time.time() - start < timeout_s:
            jobs = self.find_jobs(job_id)
            if not jobs:
                # Job has dropped off the queue → finished.
                return True
            j = jobs[0]
            if j["status"].lower() in ("finished", "failed", "stopped", "cancelled"):
                logger.info("scan job %s ended: %s", job_id, j["status"])
                return j["status"].lower() == "finished"
            logger.info("scan job %s: %s (%.0f%%)", job_id, j["status"],
                        (j.get("progress") or 0.0) * 100)
            time.sleep(poll_s)
        logger.warning("scan job %s did not finish within %ds", job_id, timeout_s)
        return False

    def studio_create(self, name: str) -> str:
        q = """
        mutation StudioCreate($input: StudioCreateInput!) {
          studioCreate(input: $input) { id name }
        }
        """
        data = self.query(q, {"input": {"name": name}})
        return data["studioCreate"]["id"]

    def find_studio_by_name(self, name: str) -> Optional[str]:
        q = """
        query FindStudios($filter: StudioFilterType) {
          findStudios(studio_filter: $filter, filter: {per_page: 5}) {
            studios { id name }
          }
        }
        """
        data = self.query(q, {"filter": {"name": {"value": name, "modifier": "EQUALS"}}})
        out = data["findStudios"]["studios"]
        return out[0]["id"] if out else None

    def find_scene_by_basename(self, basename: str) -> Optional[str]:
        """Find one scene whose file path ends with the given basename."""
        q = """
        query FindScenes($filter: SceneFilterType, $f: FindFilterType) {
          findScenes(scene_filter: $filter, filter: $f) {
            scenes { id files { path basename } }
          }
        }
        """
        data = self.query(q, {
            "filter": {"path": {"value": basename, "modifier": "INCLUDES"}},
            "f": {"per_page": 10},
        })
        for s in data["findScenes"]["scenes"]:
            for f in s.get("files") or []:
                if f.get("basename") == basename or f.get("path", "").endswith(basename):
                    return s["id"]
        return None

    def scene_update_studio(self, scene_id: str, studio_id: str) -> None:
        q = """
        mutation SceneUpdate($input: SceneUpdateInput!) {
          sceneUpdate(input: $input) { id }
        }
        """
        self.query(q, {"input": {"id": scene_id, "studio_id": studio_id}})


# --- Importer body --------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Import calibration videos into Stash.")
    p.add_argument("--dataset-dir", type=Path, default=Path("tests/calibration/dataset"))
    p.add_argument("--video-cache-dir", type=Path, default=Path("tests/calibration/.video_cache"))
    p.add_argument("--stash-target", type=Path, required=True,
                   help="Directory inside Stash's library where videos will be copied (host path).")
    p.add_argument("--stash-scan-path", type=str, default=None,
                   help="Path to scan as Stash sees it (container path, if Stash runs in Docker). "
                        "Defaults to --stash-target. Required when Stash is in a container with a "
                        "bind-mounted library that the host and container see at different paths.")
    p.add_argument("--stash-url", default=os.environ.get("STASH_URL", "http://localhost:9999"))
    p.add_argument("--stash-api-key", default=os.environ.get("STASH_API_KEY", ""))
    p.add_argument("--stash-session-cookie", default=os.environ.get("STASH_SESSION_COOKIE", ""))
    p.add_argument("--symlink", action="store_true",
                   help="Symlink instead of copying (faster, less disk; requires Stash to follow symlinks).")
    p.add_argument("--scan-timeout-s", type=int, default=1800)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    gt_path = args.dataset_dir / "ground_truth.json"
    if not gt_path.is_file():
        logger.error("ground_truth.json not found in %s — run gen_dataset.py first", args.dataset_dir)
        return 1
    ground_truth = json.loads(gt_path.read_text())

    # Need the job_name from the job.json — pick the first job in the dataset.
    jobs_dir = args.dataset_dir / "jobs"
    job_dirs = [d for d in jobs_dir.iterdir() if d.is_dir()]
    if not job_dirs:
        logger.error("no jobs in %s — run gen_dataset.py first", jobs_dir)
        return 1
    job = json.loads((job_dirs[0] / "job.json").read_text())
    studio_name = job["name"]

    args.stash_target.mkdir(parents=True, exist_ok=True)

    # Step 1: copy/symlink non-negative videos
    copied: list[Path] = []
    skipped_negative = 0
    for entry in ground_truth:
        if entry.get("negative_control"):
            skipped_negative += 1
            continue
        src = args.video_cache_dir / entry["video_basename"]
        if not src.is_file():
            logger.warning("missing source video %s; skipping", src)
            continue
        dst = args.stash_target / entry["video_basename"]
        if dst.exists():
            logger.debug("already at target: %s", dst)
            copied.append(dst)
            continue
        if args.symlink:
            os.symlink(src.resolve(), dst)
        else:
            shutil.copy2(src, dst)
        copied.append(dst)
    logger.info("staged %d videos under %s; skipped %d negative controls",
                len(copied), args.stash_target, skipped_negative)
    if not copied:
        logger.error("nothing to import")
        return 1

    # Step 2-4: trigger scan + wait
    stash = StashClient(args.stash_url, args.stash_api_key, args.stash_session_cookie)
    scan_path = args.stash_scan_path or str(args.stash_target.resolve())
    logger.info("triggering metadataScan over %s (Stash sees: %s)",
                args.stash_target, scan_path)
    scan_job_id = stash.metadata_scan([scan_path])
    logger.info("scan job_id=%s — waiting up to %ds", scan_job_id, args.scan_timeout_s)
    if not stash.wait_for_job(scan_job_id, timeout_s=args.scan_timeout_s):
        logger.warning("scan didn't report finished cleanly; continuing anyway")

    # Step 5: create or find the studio
    studio_id = stash.find_studio_by_name(studio_name)
    if studio_id:
        logger.info("studio %r exists: id=%s", studio_name, studio_id)
    else:
        studio_id = stash.studio_create(studio_name)
        logger.info("studio %r created: id=%s", studio_name, studio_id)

    # Step 6: link each imported scene to the studio
    linked = 0
    missing = 0
    for entry in ground_truth:
        if entry.get("negative_control"):
            continue
        basename = entry["video_basename"]
        scene_id = stash.find_scene_by_basename(basename)
        if not scene_id:
            logger.warning("Stash didn't index %s — Stash needs more time, or scan missed it", basename)
            missing += 1
            continue
        try:
            stash.scene_update_studio(scene_id, studio_id)
            linked += 1
        except Exception as e:
            logger.warning("studio link failed for %s :: %s", basename, e)

    logger.info("linked %d scenes to studio %r; %d Stash scenes missing",
                linked, studio_name, missing)
    return 0 if linked else 1


if __name__ == "__main__":
    sys.exit(main())
