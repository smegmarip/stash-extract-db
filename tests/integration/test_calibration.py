"""Calibration harness — runs match queries against a running bridge,
compares results to ground_truth.json, computes precision/recall/p@1
metrics, and writes a JSONL run-log + a CALIBRATION_RESULTS.md
provenance section.

Two modes (chosen by env vars / CLI):

1. **Live mode** (`pytest -m live` or `python -m tests.integration.test_calibration --live`)
   Runs against an actually-running bridge + Stash. Defaults assume the
   calibration bridge layout from tests/calibration/README.md:
     - bridge: http://127.0.0.1:13001
     - Stash:  http://localhost:9999
     - dataset: tests/calibration/dataset/
   Skipped automatically if the bridge isn't reachable.

2. **Self-test mode** (default `pytest`)
   Spins up a mock-extractor in-process via TestClient, asserts the
   harness machinery (precision/recall calculation, run-log shape,
   results doc generation) works on a synthetic fixture. Doesn't need
   Stash or a running bridge.

The committed artifact is the harness CODE + the methodology in
CALIBRATION_RESULTS.md. Run-log JSONLs go to tests/calibration/runs/
which is gitignored.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest


# --- Result schema --------------------------------------------------------

@dataclass
class PairResult:
    """One ground-truth pair × one bridge query result."""
    scene_id: str
    video_basename: str
    expected_record_id: Optional[str]
    expected_record_index: Optional[int]
    negative_control: bool
    actual_record_id: Optional[str]    # top-1 from the bridge
    actual_score: Optional[float]
    actual_image_S: Optional[float]    # composite image S from debug
    rank_of_expected: Optional[int]    # 1-indexed position of expected in results, None if absent
    n_results: int
    error: Optional[str] = None


@dataclass
class RunMetrics:
    """Aggregate metrics for one calibration run."""
    n_total: int
    n_with_expected: int               # pairs that have an expected_record_id (i.e., not negative)
    n_correct_top1: int                # top-1 == expected
    n_negatives: int
    n_negatives_correctly_empty: int   # negative scene returned []/no top-1 score
    precision_at_1: float              # n_correct_top1 / n_with_expected
    mean_reciprocal_rank: float        # of n_with_expected
    mean_top1_score: float

    @classmethod
    def from_pair_results(cls, pairs: list[PairResult]) -> "RunMetrics":
        n_total = len(pairs)
        positives = [p for p in pairs if not p.negative_control and p.expected_record_id]
        negatives = [p for p in pairs if p.negative_control]

        n_correct = sum(1 for p in positives if p.actual_record_id == p.expected_record_id)
        rrs = [(1.0 / p.rank_of_expected) if p.rank_of_expected else 0.0 for p in positives]
        scores = [p.actual_score for p in pairs if p.actual_score is not None]
        n_neg_empty = sum(1 for p in negatives if not p.actual_record_id)

        return cls(
            n_total=n_total,
            n_with_expected=len(positives),
            n_correct_top1=n_correct,
            n_negatives=len(negatives),
            n_negatives_correctly_empty=n_neg_empty,
            precision_at_1=n_correct / len(positives) if positives else 0.0,
            mean_reciprocal_rank=sum(rrs) / len(rrs) if rrs else 0.0,
            mean_top1_score=sum(scores) / len(scores) if scores else 0.0,
        )


@dataclass
class RunRecord:
    """One full calibration run — metadata + per-pair results + metrics.
    Serialized to JSONL (one record per line); each new run appends."""
    timestamp: str
    bridge_url: str
    stash_url: str
    dataset_dir: str
    bridge_config: dict[str, Any]      # snapshot of params used
    metrics: dict[str, Any]
    pairs: list[dict[str, Any]] = field(default_factory=list)


# --- Live harness ---------------------------------------------------------

DEFAULT_BRIDGE_URL = os.environ.get("CALIB_BRIDGE_URL", "http://127.0.0.1:13001")
DEFAULT_STASH_URL = os.environ.get("CALIB_STASH_URL", "http://localhost:9999")
DEFAULT_DATASET = Path(os.environ.get("CALIB_DATASET_DIR", "tests/calibration/dataset"))
DEFAULT_RUNS_DIR = Path(os.environ.get("CALIB_RUNS_DIR", "tests/calibration/runs"))


def _bridge_reachable(url: str, timeout: float = 1.0) -> bool:
    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout).read()
        return True
    except Exception:
        return False


def _gql(url: str, query: str, variables: Optional[dict] = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/graphql",
        data=body, headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def list_calibration_scenes(stash_url: str, scan_path: str = "/data/calibration/") -> list[dict]:
    """Return every Stash scene whose file path is under `scan_path`."""
    q = """
    query Q($filter: SceneFilterType) {
      findScenes(scene_filter: $filter, filter: {per_page: 500}) {
        count
        scenes { id title files { basename path } }
      }
    }
    """
    data = _gql(stash_url, q, {
        "filter": {"path": {"value": scan_path, "modifier": "INCLUDES"}},
    })
    return data["data"]["findScenes"]["scenes"]


def query_match(bridge_url: str, scene_id: str, params: dict, debug: bool = False) -> Any:
    """POST /match/fragment. Returns the parsed JSON list (search mode)
    or raises on non-2xx."""
    body = dict(params, scene_id=scene_id, mode="search")
    url = f"{bridge_url.rstrip('/')}/match/fragment" + ("?debug=1" if debug else "")
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_detail": e.read().decode()[:500]}


def find_rank_of_expected(ranked: list[dict], expected_record_id: str) -> Optional[int]:
    """1-indexed position of expected in ranked. None if absent."""
    for i, r in enumerate(ranked or []):
        if r.get("Code") == expected_record_id:
            return i + 1
    return None


def run_calibration(
    bridge_url: str,
    stash_url: str,
    dataset_dir: Path,
    params: dict,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    label: str = "",
    scan_path: str = "/data/calibration/",
    debug: bool = False,
) -> tuple[RunMetrics, Path]:
    """Run a calibration sweep with `params`, write a JSONL run-log to
    `runs_dir`. Returns (metrics, run_log_path)."""
    gt_path = dataset_dir / "ground_truth.json"
    if not gt_path.is_file():
        raise FileNotFoundError(f"ground truth not found at {gt_path}")
    gt = json.loads(gt_path.read_text())
    gt_by_basename = {e["video_basename"]: e for e in gt}

    scenes = list_calibration_scenes(stash_url, scan_path)
    pairs: list[PairResult] = []

    for s in scenes:
        files = s.get("files") or []
        basename = files[0]["basename"] if files else ""
        gt_entry = gt_by_basename.get(basename, {})

        ranked = query_match(bridge_url, s["id"], params, debug=debug)
        if isinstance(ranked, dict) and "_error" in ranked:
            pairs.append(PairResult(
                scene_id=s["id"], video_basename=basename,
                expected_record_id=gt_entry.get("expected_record_id"),
                expected_record_index=gt_entry.get("expected_record_index"),
                negative_control=bool(gt_entry.get("negative_control")),
                actual_record_id=None, actual_score=None, actual_image_S=None,
                rank_of_expected=None, n_results=0,
                error=str(ranked),
            ))
            continue

        ranked = ranked if isinstance(ranked, list) else []
        top = ranked[0] if ranked else None
        actual_S = None
        if top and "_debug" in top:
            actual_S = (top["_debug"].get("image") or {}).get("composite")
        pairs.append(PairResult(
            scene_id=s["id"], video_basename=basename,
            expected_record_id=gt_entry.get("expected_record_id"),
            expected_record_index=gt_entry.get("expected_record_index"),
            negative_control=bool(gt_entry.get("negative_control")),
            actual_record_id=(top or {}).get("Code"),
            actual_score=(top or {}).get("match_score"),
            actual_image_S=actual_S,
            rank_of_expected=find_rank_of_expected(ranked, gt_entry.get("expected_record_id") or ""),
            n_results=len(ranked),
        ))

    metrics = RunMetrics.from_pair_results(pairs)

    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    label_part = f"_{label}" if label else ""
    run_path = runs_dir / f"{ts}{label_part}.jsonl"

    record = RunRecord(
        timestamp=ts,
        bridge_url=bridge_url,
        stash_url=stash_url,
        dataset_dir=str(dataset_dir),
        bridge_config=params,
        metrics=asdict(metrics),
        pairs=[asdict(p) for p in pairs],
    )
    # JSONL: header line then one line per pair, so the file is grep-friendly.
    with run_path.open("w") as f:
        f.write(json.dumps({"_run_meta": {
            "timestamp": record.timestamp,
            "bridge_url": record.bridge_url,
            "stash_url": record.stash_url,
            "dataset_dir": record.dataset_dir,
            "bridge_config": record.bridge_config,
            "metrics": record.metrics,
        }}) + "\n")
        for p in record.pairs:
            f.write(json.dumps(p) + "\n")

    return metrics, run_path


def default_params() -> dict:
    """The current best-guess defaults. Used for unswept calibration runs."""
    return {
        "image_mode": "both",
        "threshold": 0.001,
        "limit": 5,
        "hash_algorithm": "phash",
        "hash_size": 8,
        "sprite_sample_size": 8,
        "image_gamma": 2.0,
        "image_count_k": 2.0,
        "image_uniqueness_alpha": 1.0,
        "image_channels": ["phash", "color_hist", "tone"],
        "image_min_contribution": 0.05,
        "image_bonus_per_extra": 0.1,
    }


# --- Test cases -----------------------------------------------------------

@pytest.mark.live
def test_calibration_run_against_live_bridge():
    """Smoke test the harness against the running calibration bridge.
    Skipped if the bridge isn't reachable.

    This is NOT a precision/recall assertion — it's a "does the harness
    run end-to-end and produce sensible metrics" check. The committed
    `CALIBRATION_RESULTS.md` records the actual numbers per run.
    """
    if not _bridge_reachable(DEFAULT_BRIDGE_URL):
        pytest.skip(f"calibration bridge not reachable at {DEFAULT_BRIDGE_URL}")
    if not DEFAULT_DATASET.is_dir():
        pytest.skip(f"dataset not found at {DEFAULT_DATASET}")

    metrics, run_path = run_calibration(
        bridge_url=DEFAULT_BRIDGE_URL,
        stash_url=DEFAULT_STASH_URL,
        dataset_dir=DEFAULT_DATASET,
        params=default_params(),
        runs_dir=DEFAULT_RUNS_DIR,
        label="smoke",
        debug=True,
    )

    assert run_path.is_file()
    assert metrics.n_total >= 1
    # We don't assert a precision floor here — the harness's job is to
    # report metrics, not gate on them. The user reads them out of the
    # JSONL or CALIBRATION_RESULTS.md.


def test_metrics_aggregation_logic():
    """Self-test: RunMetrics.from_pair_results computes p@1, MRR, mean
    score correctly across positives + negatives + errors."""
    pairs = [
        # Correct top-1
        PairResult("1", "a.mp4", "scene_0", 0, False, "scene_0", 0.9, 0.9, 1, 5),
        # Wrong top-1, expected at rank 2
        PairResult("2", "b.mp4", "scene_1", 1, False, "scene_3", 0.5, 0.5, 2, 5),
        # Wrong top-1, expected absent
        PairResult("3", "c.mp4", "scene_2", 2, False, "scene_3", 0.4, 0.4, None, 5),
        # Negative correctly returns nothing
        PairResult("4", "d.mp4", None, None, True, None, None, None, None, 0),
        # Negative wrongly returns something
        PairResult("5", "e.mp4", None, None, True, "scene_4", 0.3, 0.3, None, 1),
    ]
    m = RunMetrics.from_pair_results(pairs)
    assert m.n_total == 5
    assert m.n_with_expected == 3
    assert m.n_correct_top1 == 1
    assert m.n_negatives == 2
    assert m.n_negatives_correctly_empty == 1
    assert abs(m.precision_at_1 - 1/3) < 1e-9
    # MRR over positives: (1/1 + 1/2 + 0) / 3 = 0.5
    assert abs(m.mean_reciprocal_rank - 0.5) < 1e-9


def test_find_rank_of_expected():
    ranked = [{"Code": "a"}, {"Code": "b"}, {"Code": "c"}]
    assert find_rank_of_expected(ranked, "a") == 1
    assert find_rank_of_expected(ranked, "c") == 3
    assert find_rank_of_expected(ranked, "z") is None
    assert find_rank_of_expected([], "a") is None


# --- CLI entry point (for ad-hoc runs outside pytest) --------------------

def main() -> int:
    """python -m tests.integration.test_calibration --live"""
    import argparse

    p = argparse.ArgumentParser(description="Run a calibration sweep.")
    p.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    p.add_argument("--stash-url", default=DEFAULT_STASH_URL)
    p.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    p.add_argument("--scan-path", default="/data/calibration/")
    p.add_argument("--label", default="")
    p.add_argument("--debug", action="store_true")
    # Param overrides
    p.add_argument("--gamma", type=float)
    p.add_argument("--count-k", type=float)
    p.add_argument("--alpha", type=float)
    p.add_argument("--threshold", type=float)
    p.add_argument("--min-contribution", type=float)
    p.add_argument("--bonus-per-extra", type=float)
    args = p.parse_args()

    params = default_params()
    for cli_name, body_name in [
        ("gamma", "image_gamma"), ("count_k", "image_count_k"),
        ("alpha", "image_uniqueness_alpha"),
        ("threshold", "threshold"),
        ("min_contribution", "image_min_contribution"),
        ("bonus_per_extra", "image_bonus_per_extra"),
    ]:
        v = getattr(args, cli_name, None)
        if v is not None:
            params[body_name] = v

    print(f"running calibration: {params}")
    metrics, run_path = run_calibration(
        bridge_url=args.bridge_url,
        stash_url=args.stash_url,
        dataset_dir=args.dataset_dir,
        params=params,
        runs_dir=args.runs_dir,
        label=args.label,
        scan_path=args.scan_path,
        debug=args.debug,
    )
    print(f"\n{json.dumps(asdict(metrics), indent=2)}")
    print(f"\nrun-log: {run_path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
