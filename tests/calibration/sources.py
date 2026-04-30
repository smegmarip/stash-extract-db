"""Video source plugins for the calibration dataset generator.

Two backends:
- Pexels (default): free API, license-clean, explicit category diversity.
  Requires a PEXELS_API_KEY (free signup at pexels.com/api).
- yt-dlp (alternate): broader variety via Creative Commons filter.
  Requires `yt-dlp` on PATH.

Each backend exposes a single function:

    fetch(target_count, out_dir, **opts) -> list[VideoMeta]

`VideoMeta` is a dataclass holding the local path + source-provided
metadata (title, description, source URL, etc.) for downstream use by
gen_dataset.py.
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class VideoMeta:
    path: Path
    source: str                      # 'pexels' | 'ytdlp'
    source_id: str                   # Pexels video id or YouTube id
    source_url: str                  # canonical URL on the source platform
    title: str
    duration_s: Optional[float] = None
    tags: list[str] = field(default_factory=list)


# --- Pexels ----------------------------------------------------------------

PEXELS_API_BASE = "https://api.pexels.com/videos"

# Maximally distinct query terms — picked so the resulting corpus exercises
# all three scoring channels (palette variety, composition variety, tone
# variety). Order doesn't matter; we sample uniformly.
PEXELS_DEFAULT_QUERIES = [
    "nature timelapse", "wildlife close-up", "ocean waves", "forest canopy",
    "desert landscape", "snow mountains", "underwater coral",
    "urban street drone", "city skyline night", "subway train interior",
    "highway traffic aerial",
    "cooking ingredients", "coffee preparation", "pizza dough",
    "abstract animation", "smoke macro", "ink water",
    "yoga studio", "dance performance", "skateboarding",
    "factory machinery", "blacksmith forge", "pottery wheel",
    "chemistry experiment", "microscopy",
    "concert lighting", "fireworks night",
    "kids playing park", "office meeting", "library books",
    "autumn leaves",
]


def _pexels_headers(api_key: str) -> dict:
    return {"Authorization": api_key}


def _pexels_search(api_key: str, query: str, per_page: int = 15, page: int = 1) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.get(
            f"{PEXELS_API_BASE}/search",
            headers=_pexels_headers(api_key),
            params={"query": query, "per_page": per_page, "page": page,
                    "size": "small", "orientation": "landscape"},
        )
    r.raise_for_status()
    return r.json()


def _pexels_pick_video_file(video: dict) -> Optional[dict]:
    """Pexels returns a list of `video_files` per video at multiple
    resolutions; pick the smallest landscape MP4 above 360p (saves disk +
    bandwidth, still good enough for sprite generation later).
    """
    candidates = []
    for vf in video.get("video_files") or []:
        if vf.get("file_type") != "video/mp4":
            continue
        h = vf.get("height") or 0
        w = vf.get("width") or 0
        if h < 360:
            continue
        if h > w:                       # portrait — skip, sprites don't generate cleanly
            continue
        candidates.append(vf)
    if not candidates:
        return None
    candidates.sort(key=lambda vf: (vf.get("height") or 9999))
    return candidates[0]


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)


def fetch_pexels(
    target_count: int,
    out_dir: Path,
    api_key: str,
    queries: Optional[list[str]] = None,
) -> list[VideoMeta]:
    """Pull `target_count` videos from Pexels across multiple queries.

    The free Pexels API is rate-limited (~200 req/hr). We make one search
    request per query and round-robin across queries until we hit the
    target. Each search returns up to 15 videos, so 30 queries × 15 =
    450 ceiling per single batch, which is enough headroom for a 500-
    video corpus.
    """
    if not api_key:
        raise SystemExit("Pexels source requires PEXELS_API_KEY (signup at pexels.com/api).")

    queries = queries or PEXELS_DEFAULT_QUERIES
    out_dir.mkdir(parents=True, exist_ok=True)

    # Round-robin pull from each query until we have enough candidates.
    candidates: list[tuple[str, dict]] = []   # (query, video_record)
    seen_ids: set[int] = set()
    per_query_target = max(1, (target_count // len(queries)) + 2)

    for q in queries:
        try:
            data = _pexels_search(api_key, q, per_page=per_query_target, page=1)
        except httpx.HTTPError as e:
            logger.warning("Pexels search failed for %r: %s", q, e)
            continue
        for v in data.get("videos") or []:
            vid = v.get("id")
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            candidates.append((q, v))
        if len(candidates) >= target_count * 1.2:    # small overhead for failed downloads
            break

    if len(candidates) < target_count:
        logger.warning(
            "Pexels returned only %d unique videos across %d queries; target was %d",
            len(candidates), len(queries), target_count,
        )

    # Shuffle so the corpus isn't grouped by query topic.
    random.shuffle(candidates)
    candidates = candidates[:target_count]

    out: list[VideoMeta] = []
    for q, v in candidates:
        vf = _pexels_pick_video_file(v)
        if vf is None:
            continue
        url = vf["link"]
        vid_id = str(v["id"])
        dest = out_dir / f"pexels_{vid_id}.mp4"
        if not dest.exists():
            try:
                _download(url, dest)
            except Exception as e:
                logger.warning("Pexels download failed (%s): %s", url, e)
                continue
        out.append(VideoMeta(
            path=dest,
            source="pexels",
            source_id=vid_id,
            source_url=v.get("url") or "",
            title=(v.get("user") or {}).get("name", "Pexels video") + " — " + q,
            duration_s=v.get("duration"),
            tags=[q],
        ))

    logger.info("Pexels: downloaded %d/%d videos", len(out), target_count)
    return out


# --- yt-dlp ---------------------------------------------------------------

YTDLP_DEFAULT_QUERIES = [
    "nature timelapse 4k", "cooking tutorial", "street photography 4k",
    "abstract animation", "wildlife documentary clip", "urban drone footage",
    "dance performance", "chemistry experiment", "ocean waves",
    "mountain hiking", "skyline aerial", "forest walk", "desert dunes",
    "macro photography", "rain on window",
    "concert performance", "skateboarding park",
    "factory tour", "microscopy", "smoke art",
    "autumn forest", "coffee shop ambient", "city night drive",
    "underwater diving", "fireworks display", "calligraphy demo",
    "pottery making", "glass blowing", "pizza making", "winter snow",
]


def fetch_ytdlp(
    target_count: int,
    out_dir: Path,
    queries: Optional[list[str]] = None,
    max_filesize_mb: int = 200,
    max_height: int = 480,
) -> list[VideoMeta]:
    """yt-dlp source. Searches Creative-Commons-licensed YouTube videos
    across the given queries. Requires `yt-dlp` on PATH.

    The CC filter (`license = creative_commons`) drops videos that don't
    explicitly carry a CC license — this is much smaller than the YouTube
    catalog overall, but ensures the corpus is license-clean for
    redistribution alongside the test fixture if needed.
    """
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp not found on PATH. `pip install yt-dlp` or use --source pexels.")

    queries = queries or YTDLP_DEFAULT_QUERIES
    out_dir.mkdir(parents=True, exist_ok=True)

    per_query = max(1, (target_count // len(queries)) + 2)
    out: list[VideoMeta] = []

    for q in queries:
        if len(out) >= target_count:
            break
        # ytsearch with N hits and CC filter
        cmd = [
            "yt-dlp",
            f"ytsearch{per_query}:{q}",
            "--match-filter", "license = creative_commons",
            "--format", f"best[ext=mp4][height<={max_height}]/best[height<={max_height}]/best",
            "--max-filesize", f"{max_filesize_mb}M",
            "--no-playlist",
            "--print-json",
            "--output", str(out_dir / "ytdlp_%(id)s.%(ext)s"),
            "--no-warnings",
            "--quiet",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp timed out on %r", q)
            continue
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                meta = json.loads(line)
            except json.JSONDecodeError:
                continue
            local_path_str = meta.get("_filename") or meta.get("filepath")
            if not local_path_str:
                continue
            local_path = Path(local_path_str)
            if not local_path.exists():
                # yt-dlp sometimes reports a different extension than what
                # actually lands on disk; do a best-effort glob.
                glob = list(out_dir.glob(f"ytdlp_{meta.get('id')}.*"))
                if not glob:
                    continue
                local_path = glob[0]
            out.append(VideoMeta(
                path=local_path,
                source="ytdlp",
                source_id=str(meta.get("id") or ""),
                source_url=meta.get("webpage_url") or "",
                title=meta.get("title") or "untitled",
                duration_s=meta.get("duration"),
                tags=[q],
            ))
            if len(out) >= target_count:
                break

    logger.info("yt-dlp: downloaded %d/%d videos", len(out), target_count)
    return out


# --- Dispatcher -----------------------------------------------------------

def fetch(source: str, target_count: int, out_dir: Path, **opts) -> list[VideoMeta]:
    if source == "pexels":
        api_key = opts.get("api_key") or os.environ.get("PEXELS_API_KEY", "")
        return fetch_pexels(target_count, out_dir, api_key=api_key,
                            queries=opts.get("queries"))
    if source == "ytdlp":
        return fetch_ytdlp(target_count, out_dir,
                           queries=opts.get("queries"),
                           max_filesize_mb=opts.get("max_filesize_mb", 200),
                           max_height=opts.get("max_height", 480))
    raise SystemExit(f"unknown source: {source!r} — use 'pexels' or 'ytdlp'")
