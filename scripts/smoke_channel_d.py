"""Channel D smoke harness.

Iterates Stash scenes filtered by studio, calls /match/fragment three times
per scene with different image_channels overrides, writes raw debug payloads
to a JSONL file, and prints an aggregate summary comparing configs.

Per CLAUDE.md §1, image_channels is a per-request override the bridge accepts;
the bridge's BRIDGE_IMAGE_CHANNELS env value is irrelevant when the request
body sets it. So one running bridge serves all three configs in one pass.

Without an external ground_truth.json (calibration's pairing fixture), absolute
precision can't be computed — Stash scenes carry Pexel-id basenames while
extractor records carry synthetic descriptive titles, with no textual link.
Instead the harness reports cross-config metrics that don't need ground truth:

  - match rate per config (any candidate fired)
  - composite distribution per config (median, p25, p75, p95)
  - cross-config agreement (% of scenes where all configs returned same id)
  - explicit list of disagreements for manual spot-checking

That's enough to answer the gating question: "does D change the answer or the
confidence relative to A/B/C alone?"

Usage:
    python scripts/smoke_channel_d.py \\
        --stash-url http://192.168.50.93:9999 \\
        --stash-api-key <key> \\
        --bridge-url http://192.168.50.93:13000 \\
        --studio "Pexel Video" \\
        --limit 50 \\
        --output runs/smoke-$(date +%s)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx


CONFIGS = {
    "baseline":  ["phash", "color_hist", "tone"],
    "mixed":     ["phash", "color_hist", "tone", "embedding"],
    "embedding": ["embedding"],
}


def candidate_id(candidate: dict | None) -> str | None:
    """Stable identifier for cross-config agreement comparison.

    The bridge's scrape result has Code (extractor data.id) per CLAUDE.md §8;
    falls back to URL or Title hash if Code is empty.
    """
    if not candidate:
        return None
    return candidate.get("Code") or candidate.get("URL") or candidate.get("Title") or None


def composite_score(debug: dict | None) -> float | None:
    """Pull composite from the bridge's ?debug=1 envelope. Shape:
        debug = {"image": {"channels": {...}, "composite": 0.x}, ...}
    Returns None if structure missing.
    """
    if not isinstance(debug, dict):
        return None
    img = debug.get("image") or {}
    return img.get("composite")


SCENES_QUERY = """
query FindScenes($studio: ID!, $page: Int!, $perPage: Int!) {
  findScenes(
    scene_filter: { studios: { value: [$studio], modifier: INCLUDES } }
    filter: { page: $page, per_page: $perPage, sort: "id", direction: ASC }
  ) {
    count
    scenes {
      id
      title
      files { basename path }
    }
  }
}
"""


STUDIO_QUERY = """
query FindStudios {
  findStudios(filter: { per_page: 1000 }) {
    studios { id name }
  }
}
"""


def stash_graphql(client: httpx.Client, url: str, query: str, variables: dict) -> dict:
    r = client.post(f"{url}/graphql", json={"query": query, "variables": variables})
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def find_studio_id(client: httpx.Client, stash_url: str, name: str) -> str:
    data = stash_graphql(client, stash_url, STUDIO_QUERY, {})
    studios = data["findStudios"]["studios"]
    for s in studios:
        if s["name"].lower() == name.lower():
            return s["id"]
    available = [s["name"] for s in studios]
    raise SystemExit(f"Studio {name!r} not found. Available: {available[:20]}")


def fetch_scenes(client: httpx.Client, stash_url: str, studio_id: str, limit: int) -> list[dict]:
    scenes: list[dict] = []
    page = 1
    per_page = min(100, limit)
    while len(scenes) < limit:
        data = stash_graphql(
            client, stash_url, SCENES_QUERY,
            {"studio": studio_id, "page": page, "perPage": per_page},
        )
        batch = data["findScenes"]["scenes"]
        if not batch:
            break
        scenes.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return scenes[:limit]


def _strip_image(d: dict | None) -> dict | None:
    """Drop base64 Image field — bloats logs by ~50KB/row."""
    if isinstance(d, dict) and "Image" in d:
        d = {k: v for k, v in d.items() if k != "Image"}
    return d


def call_bridge(
    client: httpx.Client, bridge_url: str, scene_id: str, channels: list[str]
) -> tuple[int, dict | None]:
    """Search mode top-1 — surfaces composite scores even when scrape would
    return empty (composite < threshold). The top-1 candidate IS what scrape
    would return if the threshold gate passed, so we can derive both the
    scrape-fire-rate and the composite distribution from one call."""
    body = {"scene_id": scene_id, "mode": "search", "limit": 1, "image_channels": channels}
    try:
        r = client.post(f"{bridge_url}/match/fragment?debug=1", json=body, timeout=120.0)
    except httpx.RequestError as e:
        return -1, {"error": f"request_error: {e}"}
    if r.status_code == 503:
        return 503, None
    if not r.is_success:
        return r.status_code, {"error": r.text[:500]}
    try:
        body = r.json()
    except Exception:
        return 200, {"raw": r.text[:500]}
    # search returns a list — take top-1 if present
    if isinstance(body, list):
        return 200, _strip_image(body[0]) if body else None
    return 200, _strip_image(body)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(rows: list[dict], configs: list[str], threshold: float) -> str:
    """Per-config aggregates + cross-config agreement.

    Per-config: match rate, scrape-fire-rate (composite >= threshold),
                composite distribution, error count.
    Cross-config: % of scenes where every config returned the same candidate id;
                  list of disagreement scenes.
    """
    by_cfg: dict[str, dict] = {
        c: {"total": 0, "returned": 0, "above_thresh": 0, "errored": 0, "503": 0, "composites": []}
        for c in configs
    }
    # scene_id -> {cfg -> candidate_id}
    by_scene: dict[str, dict[str, str | None]] = {}

    for r in rows:
        cfg = r["config"]
        scene_id = r["scene_id"]
        by_cfg[cfg]["total"] += 1
        status = r.get("status")
        if status == 503:
            by_cfg[cfg]["503"] += 1
        elif status != 200:
            by_cfg[cfg]["errored"] += 1
        candidate = r.get("candidate")
        cid = candidate_id(candidate)
        if candidate:
            by_cfg[cfg]["returned"] += 1
            comp = r.get("composite")
            if comp is not None:
                by_cfg[cfg]["composites"].append(comp)
                if comp >= threshold:
                    by_cfg[cfg]["above_thresh"] += 1
        by_scene.setdefault(scene_id, {})[cfg] = cid

    lines = []
    lines.append(f"PER-CONFIG SUMMARY (threshold={threshold} for scrape-fire-rate)")
    lines.append("config       total  returned  >=thr  scrape%  comp_p25  comp_med  comp_p75  comp_p95  503  err")
    lines.append("-" * 105)
    for cfg in configs:
        s = by_cfg[cfg]
        scrape_rate = (s["above_thresh"] / s["total"] * 100) if s["total"] else 0.0
        comps = s["composites"]
        p25 = _percentile(comps, 0.25)
        p50 = _percentile(comps, 0.50)
        p75 = _percentile(comps, 0.75)
        p95 = _percentile(comps, 0.95)
        lines.append(
            f"{cfg:<11}  {s['total']:>5}  {s['returned']:>8}  {s['above_thresh']:>5}  "
            f"{scrape_rate:>6.1f}  "
            f"{p25:>8.3f}  {p50:>8.3f}  {p75:>8.3f}  {p95:>8.3f}  {s['503']:>3}  {s['errored']:>3}"
        )

    # Agreement: only scenes where every config has a non-None candidate
    fully_returned = [
        (sid, cmap) for sid, cmap in by_scene.items()
        if all(cmap.get(c) is not None for c in configs)
    ]
    if len(configs) > 1 and fully_returned:
        agree = sum(1 for _, cmap in fully_returned if len({cmap[c] for c in configs}) == 1)
        rate = agree / len(fully_returned) * 100
        lines.append("")
        lines.append("CROSS-CONFIG AGREEMENT (only scenes where all configs returned a candidate)")
        lines.append(f"  scenes_with_all_returned={len(fully_returned)}  agree={agree}  rate={rate:.1f}%")

        disagreements = [
            (sid, cmap) for sid, cmap in fully_returned
            if len({cmap[c] for c in configs}) > 1
        ]
        if disagreements:
            lines.append("")
            lines.append(f"DISAGREEMENTS ({len(disagreements)} scenes — top-1 differs across configs)")
            header = "  scene_id     " + "  ".join(f"{c:<12}" for c in configs)
            lines.append(header)
            for sid, cmap in disagreements[:30]:
                row = f"  {sid:<12}" + "  ".join(f"{(cmap[c] or '-')[:12]:<12}" for c in configs)
                lines.append(row)
            if len(disagreements) > 30:
                lines.append(f"  ... ({len(disagreements) - 30} more — see results.jsonl)")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stash-url", required=True)
    ap.add_argument("--stash-api-key", default="")
    ap.add_argument("--bridge-url", required=True)
    ap.add_argument("--studio", required=True, help="Stash studio name to filter scenes by")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--output", required=True, help="Output dir; results.jsonl + summary.txt")
    ap.add_argument("--configs", default=",".join(CONFIGS), help="Comma-separated configs to run")
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="Composite threshold for the scrape-fire-rate metric (default 0.7)")
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    summary_path = out_dir / "summary.txt"

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    for c in configs:
        if c not in CONFIGS:
            sys.exit(f"Unknown config {c!r}. Choose from: {list(CONFIGS)}")

    headers = {}
    if args.stash_api_key:
        headers["ApiKey"] = args.stash_api_key

    rows: list[dict] = []
    with httpx.Client(headers=headers, timeout=30.0) as client:
        print(f"[1/3] resolving studio {args.studio!r}", flush=True)
        studio_id = find_studio_id(client, args.stash_url, args.studio)
        print(f"      studio_id={studio_id}", flush=True)

        print(f"[2/3] fetching up to {args.limit} scenes", flush=True)
        scenes = fetch_scenes(client, args.stash_url, studio_id, args.limit)
        print(f"      got {len(scenes)} scenes", flush=True)

        print(f"[3/3] running {len(configs)} configs against {len(scenes)} scenes "
              f"= {len(configs) * len(scenes)} requests", flush=True)
        t0 = time.time()
        with results_path.open("w") as f:
            for i, scene in enumerate(scenes, 1):
                title = scene.get("title") or ""
                files = scene.get("files") or [{}]
                basename = files[0].get("basename") or ""
                for cfg in configs:
                    channels = CONFIGS[cfg]
                    status, body = call_bridge(client, args.bridge_url, scene["id"], channels)
                    candidate = body if (status == 200 and isinstance(body, dict) and body.get("Title")) else None
                    debug = (body or {}).get("_debug") if isinstance(body, dict) else None
                    row = {
                        "scene_id": scene["id"],
                        "scene_title": title,
                        "scene_basename": basename,
                        "config": cfg,
                        "channels": channels,
                        "status": status,
                        "candidate": candidate,
                        "candidate_id": candidate_id(candidate),
                        "match_score": (candidate or {}).get("match_score"),
                        "composite": composite_score(debug),
                        "debug": debug,
                    }
                    rows.append(row)
                    f.write(json.dumps(row) + "\n")
                if i % 10 == 0 or i == len(scenes):
                    elapsed = time.time() - t0
                    rate = (i * len(configs)) / elapsed if elapsed else 0
                    print(f"      {i}/{len(scenes)} scenes — {rate:.1f} req/s", flush=True)

    summary = summarize(rows, configs, args.threshold)
    summary_path.write_text(summary + "\n")
    print()
    print(summary)
    print()
    print(f"Raw results: {results_path}")
    print(f"Summary:     {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
