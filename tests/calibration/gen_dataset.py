"""Generate a calibration dataset for the bridge's multi-channel scoring.

Pipeline:
  1. Source videos (sources.fetch — Pexels default, yt-dlp alt).
  2. Per video: probe duration via ffprobe, extract N random thumbnails via
     ffmpeg, where N varies (1, 2, 3, 5, 8) per record so calibration can
     observe count_conf / dist_q behavior across the spectrum.
  3. Generate plausible metadata (title from source, fake performers, dates).
  4. Write `dataset/jobs/{job_id}/{records.json, assets/*.jpg}` and
     `dataset/ground_truth.json` (the labeled fixture).

Distribution refinements baked in (per docs/HOW_TO_USE.md §6 calibration intent):
  - Cover image: ~30% cover-only records, ~50% mixed cover+images,
    ~20% no-cover-just-frames.
  - Negative controls: ~10% records flagged `negative=True` so the
    importer skips them when copying videos to Stash. The matcher should
    NOT find a match for these.
  - Frame timing: thumbnails sampled uniformly across [5%, 95%] of
    runtime to avoid edge artifacts (intro/outro fade frames).

Usage:
  python -m tests.calibration.gen_dataset \
    --source pexels --target 50 \
    --dataset-dir tests/calibration/dataset
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Allow running as a script (`python tests/calibration/gen_dataset.py`)
# or as a module (`python -m tests.calibration.gen_dataset`).
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.calibration import sources  # noqa: E402
from tests.calibration.sources import VideoMeta  # noqa: E402

logger = logging.getLogger(__name__)


# Fake-but-plausible performer name pool for synthetic record metadata.
PERFORMER_POOL = [
    "Alex Rivera", "Jordan Park", "Sam Chen", "Morgan Lee", "Kai Tanaka",
    "Riley Brooks", "Jamie Cole", "Drew Mitchell", "Casey Patel", "Avery Singh",
    "Quinn Davis", "Robin Garcia", "Skyler Reed", "Charlie Wong", "Reese Carter",
    "Sage Adams", "River Hill", "Phoenix Wells", "Sky Bennett", "Tatum Foster",
    "Emerson Hayes", "Hayden Cruz", "Nova Pierce", "Wren Murphy", "Indigo Knox",
    "Cameron Boyd", "Blair Holt", "Harper Vega", "Eden Marsh", "Blake Reyes",
]

ADJECTIVES = ["Quiet", "Sudden", "Crimson", "Golden", "Distant", "Forgotten",
              "Hidden", "Restless", "Frozen", "Drifting", "Endless", "Familiar"]
NOUNS = ["Horizon", "Departure", "Echo", "Threshold", "Field", "Tide",
         "Compass", "Drift", "Wake", "Shadow", "Margin", "Veil"]


def _ffprobe_duration(path: Path) -> Optional[float]:
    """Get a video's duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ffprobe failed on %s :: %s", path, e)
        return None
    s = res.stdout.strip()
    try:
        return float(s)
    except ValueError:
        return None


def _ffmpeg_extract_frame(video: Path, t_seconds: float, dest: Path,
                           width: int = 480) -> bool:
    """Extract a single frame at `t_seconds` from `video` and write a
    JPEG to `dest`. Returns True on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{t_seconds:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:-1",
        "-q:v", "3",
        str(dest),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ffmpeg failed on %s :: %s", video, e)
        return False
    return res.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _generate_title(meta: VideoMeta, rng: random.Random) -> str:
    """Generate a plausible scene title — derived from the source title
    when available, otherwise a 3-token fake."""
    src = (meta.title or "").strip()
    if src and len(src) > 5:
        # Truncate aggressively + drop URL-y bits
        src = src.split(" - ")[0].split(" | ")[0]
        if len(src) > 80:
            src = src[:80].rsplit(" ", 1)[0]
        return src
    return f"The {rng.choice(ADJECTIVES)} {rng.choice(NOUNS)}"


def _generate_date(rng: random.Random) -> str:
    """Random ISO date within the last 5 years."""
    today = date.today()
    delta_days = rng.randint(0, 5 * 365)
    return (today - timedelta(days=delta_days)).isoformat()


def _generate_performers(rng: random.Random, count: int) -> list[str]:
    return rng.sample(PERFORMER_POOL, min(count, len(PERFORMER_POOL)))


def _pick_n_images(rng: random.Random) -> int:
    """N (number of record images) varies per record so calibration can
    see count_conf / dist_q behavior across the spectrum.

    Distribution: weighted toward small N because real extractor records
    typically have 1–5 images.
    """
    return rng.choices(
        [1, 2, 3, 5, 8],
        weights=[15, 25, 30, 20, 10],
        k=1,
    )[0]


def _decide_cover_strategy(rng: random.Random) -> str:
    """Return 'cover_only' | 'mixed' | 'no_cover'.

    cover_only: cover_image set, images[] empty
    mixed:      cover_image set, images[] non-empty
    no_cover:   cover_image None, images[] non-empty
    """
    return rng.choices(
        ["cover_only", "mixed", "no_cover"],
        weights=[30, 50, 20],
        k=1,
    )[0]


def _sample_timestamps(duration: float, n: int, rng: random.Random) -> list[float]:
    """Sample N timestamps uniformly in [5%, 95%] of duration. Avoids
    edge fade frames that would skew tone/color baselines.
    """
    if duration <= 0 or n <= 0:
        return []
    lo = duration * 0.05
    hi = duration * 0.95
    if hi <= lo:
        return []
    samples = sorted(rng.uniform(lo, hi) for _ in range(n))
    return samples


def _build_record_for_video(
    video: VideoMeta,
    record_index: int,
    job_assets_dir: Path,
    rng: random.Random,
) -> Optional[dict]:
    """Build one record dict for the records.json. Returns None if we
    couldn't sample any usable thumbnails.
    """
    duration = _ffprobe_duration(video.path)
    if duration is None or duration < 5.0:
        logger.info("skipping %s: duration unknown or <5s", video.path.name)
        return None

    cover_strategy = _decide_cover_strategy(rng)
    n_images = _pick_n_images(rng) if cover_strategy != "cover_only" else 0
    needs_cover = cover_strategy in ("cover_only", "mixed")

    # Sample timestamps. For records with a cover, the cover frame comes
    # from ~10% into runtime (mimics a real "header frame" thumbnail);
    # remaining N images sample uniformly in [5%, 95%].
    extra_ts = _sample_timestamps(duration, n_images, rng)

    # Frame extraction
    refs: dict[str, Optional[str]] = {"cover": None, "images": []}

    if needs_cover:
        cover_t = duration * 0.10
        cover_name = f"{record_index:04d}_cover.jpg"
        cover_path = job_assets_dir / cover_name
        if _ffmpeg_extract_frame(video.path, cover_t, cover_path):
            refs["cover"] = f"../assets/{cover_name}"

    image_names: list[str] = []
    for i, t in enumerate(extra_ts):
        name = f"{record_index:04d}_img_{i}.jpg"
        path = job_assets_dir / name
        if _ffmpeg_extract_frame(video.path, t, path):
            image_names.append(f"../assets/{name}")
    refs["images"] = image_names

    if not refs["cover"] and not refs["images"]:
        # No usable thumbnails extracted — skip this record entirely.
        logger.warning("no thumbnails for %s; skipping record", video.path.name)
        return None

    # Metadata
    title = _generate_title(video, rng)
    n_perf = rng.randint(0, 4)
    record_id = f"scene_{record_index:04d}"
    return {
        "id": record_id,
        "title": title,
        "details": f"Source: {video.source}; tags: {', '.join(video.tags) or 'n/a'}.",
        "date": _generate_date(rng),
        "cover_image": refs["cover"],
        "url": f"https://test-site.example/scene/{record_id}",
        "performers": _generate_performers(rng, n_perf) if n_perf else [],
        "images": refs["images"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a calibration dataset.")
    p.add_argument("--source", choices=["pexels", "ytdlp"], default="pexels")
    p.add_argument("--target", type=int, default=50,
                   help="Total record count (default 50; ~500 for full corpus).")
    p.add_argument("--dataset-dir", type=Path,
                   default=Path("tests/calibration/dataset"),
                   help="Output dataset directory.")
    p.add_argument("--video-cache-dir", type=Path,
                   default=Path("tests/calibration/.video_cache"),
                   help="Where downloaded source videos are kept (re-used across runs).")
    p.add_argument("--job-name", default="Calibration Test Site",
                   help="Studio name. Stash matches studio.name == job.name (CLAUDE.md §5).")
    p.add_argument("--negative-fraction", type=float, default=0.10,
                   help="Fraction of records flagged as negative controls (no Stash counterpart).")
    p.add_argument("--api-key", help="Pexels API key (or set PEXELS_API_KEY env).")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    rng = random.Random(args.seed)
    args.dataset_dir.mkdir(parents=True, exist_ok=True)
    args.video_cache_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: source videos
    logger.info("sourcing %d videos from %s", args.target, args.source)
    videos = sources.fetch(
        args.source, args.target, args.video_cache_dir,
        api_key=args.api_key,
    )
    if not videos:
        logger.error("no videos sourced; aborting")
        return 1

    # Step 2: build the job structure
    job_id = f"calib_{uuid.uuid4().hex[:12]}"
    job_dir = args.dataset_dir / "jobs" / job_id
    job_assets = job_dir / "assets"
    job_assets.mkdir(parents=True, exist_ok=True)

    # Step 3: build records, one per video
    records: list[dict] = []
    ground_truth: list[dict] = []

    for idx, video in enumerate(videos):
        is_negative = rng.random() < args.negative_fraction
        rec = _build_record_for_video(video, idx, job_assets, rng)
        if rec is None:
            continue
        records.append(rec)
        ground_truth.append({
            "video_basename": video.path.name,
            "video_source_url": video.source_url,
            "expected_job_id": job_id,
            "expected_record_index": len(records) - 1,
            "expected_record_id": rec["id"],
            "negative_control": is_negative,
        })

    if not records:
        logger.error("no usable records built; aborting")
        return 1

    # Step 4: write job metadata, schema, records, ground truth
    schema_id = f"video_scene_schema_{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id,
        "name": args.job_name,
        "status": "completed",
        "completed_at": date.today().isoformat() + "T00:00:00",
        "extraction_config": {"schema_id": schema_id},
    }
    schema = {
        "id": schema_id,
        "name": "Video Scene",
        "description": "Calibration test schema (mirrors site-extractor seed template).",
        "is_template": False,
        "fields": [
            {"name": "id", "field_type": "string", "is_array": False, "children": None},
            {"name": "title", "field_type": "string", "is_array": False, "children": None},
            {"name": "details", "field_type": "string", "is_array": False, "children": None},
            {"name": "date", "field_type": "string", "is_array": False, "children": None},
            {"name": "cover_image", "field_type": "string", "is_array": False, "children": None},
            {"name": "url", "field_type": "string", "is_array": False, "children": None},
            {"name": "performers", "field_type": "string", "is_array": True, "children": None},
            {"name": "images", "field_type": "string", "is_array": True, "children": None},
        ],
    }
    (job_dir / "job.json").write_text(json.dumps(job, indent=2))
    (job_dir / "records.json").write_text(json.dumps(records, indent=2))

    schemas_dir = args.dataset_dir / "schemas"
    schemas_dir.mkdir(exist_ok=True)
    (schemas_dir / f"{schema_id}.json").write_text(json.dumps(schema, indent=2))

    (args.dataset_dir / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))

    logger.info(
        "dataset written: %d records, %d negative controls; job=%s",
        len(records), sum(1 for g in ground_truth if g["negative_control"]), job_id,
    )
    logger.info("dataset dir: %s", args.dataset_dir.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
