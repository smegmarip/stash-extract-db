# Testing strategy

The bridge's testing lives at three levels: **unit tests** for pure compute and DB invariants, an **integration harness** that runs the bridge end-to-end against a real Stash plus a mock extractor, and a **calibration corpus** built specifically to derive empirical defaults for the multi-channel scoring formula. This document explains what each level covers, when to run it, and how the layers fit together.

> For run-by-run calibration findings, see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md). For the harness operator's runbook, see [`calibration/README.md`](calibration/README.md). For architectural invariants the tests defend, see [`CLAUDE.md`](../CLAUDE.md).

---

## 1. Layout

```
tests/
├── conftest.py                       # shared fixtures (synth_image, clean_settings, bridge_db, reset_worker)
├── unit/
│   ├── test_scoring.py               # within-channel + cross-channel composition (CLAUDE.md §13.2/§13.3)
│   ├── test_channels.py              # color hist + tone feature compute, similarity, q_i (CLAUDE.md §13.4/§13.5)
│   ├── test_db.py                    # schema, cascade, image_features CRUD, feature_state, LRU touch + eviction
│   ├── test_dual_write.py            # image_hashes ↔ image_features parity + Phase 7 retirement flag
│   ├── test_lifecycle.py             # featurize_job, state machine, startup_recover, cascade re-enqueue,
│   │                                 # event-loop responsiveness regression test (CLAUDE.md §14.4)
│   ├── test_phase4_scoring_path.py   # end-to-end channel A score + 400 validation + legacy fallback
│   └── test_phase5_multichannel.py   # channels B+C, composite, multi-channel validation,
│                                     # SearchConfidenceFloor (CLAUDE.md §13.7)
├── integration/
│   └── test_calibration.py           # live harness that walks ground_truth.json against a running bridge
└── calibration/
    ├── gen_dataset.py                # builds Pexels-sourced synthetic dataset
    ├── mock_extractor.py             # FastAPI app that serves the dataset over the extractor contract
    ├── import_to_stash.py            # one-shot importer: copy/symlink videos → Stash library, scan, link studio
    ├── sources.py                    # Pexels query mix
    ├── dataset/                      # gitignored (regenerable)
    ├── .video_cache/                 # gitignored (regenerable)
    └── runs/                         # gitignored — JSONL run-logs
```

**98 unit tests pass in ~8 seconds**, with no external dependencies (no Stash, no extractor, no network). Run them on every change. The integration harness and calibration corpus are heavier — used for evaluating scoring quality, not for catching regressions in the inner code.

---

## 2. Unit tests — what they enforce

Each unit-test file maps to one or more `CLAUDE.md` invariants. The tests are deliberately thin where the invariant is structural (database schema, FK cascades) and detailed where the invariant is numeric (scoring formula behavior at edge cases).

### 2.1 `test_scoring.py` (21 tests)

Exercises the within-channel formula and cross-channel composition from `bridge/app/matching/scoring.py`:

- `sharpen` returns 0 when `sim ≤ baseline`, 1 when `sim = 1`, and `(sim - baseline)/(1 - baseline)` (no exponent) at `γ = 1`.
- `score_frame_channel` collapses to E × count_conf × dist_q with the documented bounds (`dist_q ∈ [0.5, 1]`, `count_conf ∈ [0, 1)`).
- `score_aggregate_channel` returns `m' × q` with no count or distribution adjustment.
- `compose` returns `min(1, max(fired) + bonus × (n_fired - 1))`, drops channels below `min_contribution` from the bonus, returns 0 if no channels fire.
- Rejects malformed input (negative weights, mismatched array lengths).

These are the math invariants that make CLAUDE.md §13.2/§13.3 hold. A regression here means a scoring bug.

### 2.2 `test_channels.py` (24 tests)

Channels B (color histogram) and C (low-res tone) compute, similarity, aggregation:

- `compute_color_hist` produces a 64-uint8 HSV histogram, sums to 255 within rounding, has `q_i = 1 - gini(bins)` proportional to chromatic spread.
- `compute_tone` produces an 8×8 grayscale tone signature; `q_i` matches the channel A formula on the same input.
- `color_hist_similarity` is histogram intersection bounded `[0, 1]`, 1 iff identical.
- `tone_similarity` is `1 - mean(|a-b|/255)` over 64 bytes.
- `aggregate_color_hist` per-bin median across N hists; robust to one outlier in 5.

These defend CLAUDE.md §13.1 (per-bin median for B aggregate is robust to outlier frames) and §13.4–§13.5 (q_i and sim formulas).

### 2.3 `test_db.py` (~24 tests, including `TestPerChannelUniqueness`)

SQLite schema and CRUD invariants from `bridge/app/cache/db.py`:

- Schema is created idempotently; running `init_db()` twice produces no errors and no row changes.
- The cascade in `upsert_job_and_results` (when `completed_at` advances) atomically clears `extractor_results`, `image_features` (extractor side only), `corpus_stats`, `image_uniqueness`, `match_results`, and `job_feature_state`. Stash-side `image_features` rows survive (CLAUDE.md §7).
- `image_features` PK shape is `(source, ref_id, channel, algorithm)`; conflicting inserts upsert.
- `last_accessed_at` is touched on Stash-side reads only; extractor-side reads don't write.
- LRU eviction respects budget bytes, never evicts extractor-side rows, evicts oldest-touched first.
- `Settings.channel_uniqueness_threshold("phash")` and `channel_uniqueness_alpha("tone")` resolve per-channel overrides correctly; unknown channels fall back to global defaults (covers the architectural mechanism from Run 7).

### 2.4 `test_dual_write.py` (7 tests)

Phase 2 backward-compatibility: pHash compute writes to both `image_hashes` (legacy) and `image_features` (new). Phase 7 retirement flag (`BRIDGE_LEGACY_DUAL_WRITE_ENABLED`) flips between dual-write/dual-read and new-only.

These tests defend the rollback path — flipping the legacy flag back on must produce identical hash values to flipping it off.

### 2.5 `test_lifecycle.py` (~9 tests)

Featurization state machine from `bridge/app/matching/featurization.py` and `bridge/app/matching/worker.py`:

- `featurize_job` populates all three channels; sets `state='ready'` on success, `'failed'` on unrecoverable error.
- `startup_recover` enqueues every job not in `ready`, resets stale `featurizing` rows.
- Worker enforces single-in-flight per `job_id`; concurrent enqueues of the same job are no-ops.
- Cascade re-enqueue: advancing `completed_at` triggers a fresh featurize task.
- `_gate_features_ready` calls `ensure_job_results_fresh(job)` before inserting `job_feature_state` (FK violation regression test).
- **`TestEventLoopResponsiveness`** — runs a featurization on 20 synthetic records (~1s of CPU compute) and samples DB query latency mid-run. Asserts max latency < 150 ms. Without the `asyncio.to_thread` wrapping at the five hot spots in CLAUDE.md §14.4, this test reproduces the bug that hung the bridge for 5–15 minutes during 491-record featurization.

### 2.6 `test_phase4_scoring_path.py` and `test_phase5_multichannel.py` (~9 tests combined)

End-to-end exercises through the request pipeline:

- A request with the new-scoring fields routes through `scoring.py`; channel A produces the right S; non-firing candidates score 0.
- A request missing `image_gamma` / `image_count_k` / `image_min_contribution` returns `400 Bad Request`.
- A request with `image_channels=["phash"]` skips the multi-channel validation (single-channel still works under the new formula).
- The `IMAGE_SEARCH_FLOOR` mechanism (CLAUDE.md §13.7) drops weak candidates whose composite is below the floor; definitive signals (Studio+Code, Exact Title) bypass; `None` disables.

These are the contract tests that protect against regressions at the `/match/*` API surface.

---

## 3. Integration test — `test_calibration.py`

Lives at `tests/integration/test_calibration.py`. Walks `ground_truth.json` from a calibration dataset, queries each Stash scene against a running bridge, computes `precision@1`, `mean_reciprocal_rank`, `mean_top1_score`, and `n_negatives_correctly_empty`, writes a JSONL run-log to `tests/calibration/runs/`.

Three test modes:

| Mode                                  | What it does                                                                          | When to run                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `test_dataset_loads_ground_truth`     | Pure smoke; loads the JSON fixture and verifies its shape.                            | Always — no bridge required.                                             |
| `test_mock_extractor_serves_jobs`     | Starts `mock_extractor.py`, hits `/api/jobs`, validates response shape.               | Always — no bridge required.                                             |
| `test_calibration_run_against_live_bridge` | Marked `@pytest.mark.live`; auto-skips if `bridge-url` is unreachable. Runs a 30-scene seed=0 sample, asserts `precision@1 > 0.5`. | Manually, after standing up the calibration harness (see [calibration/README.md](calibration/README.md)). |

The `live` marker isolates the test from CI: run only with `pytest -m live` or via the harness CLI.

---

## 4. Calibration corpus — methodology

### 4.1 The labeling problem

The multi-channel scoring formula has six tunable parameters: `image_gamma`, `image_count_k`, `image_uniqueness_alpha`, `image_min_contribution`, `image_bonus_per_extra`, plus `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`. There's no first-principles derivation — these are calibration parameters, tuned empirically against labeled `(scene, expected_record)` pairs. Real adult content can't be checked into the repo (license-encumbered) and labeling is expensive (manual review of every match).

### 4.2 The synthetic-dataset trick

Use the **same source videos** for both Stash-side scenes and extractor-side records:

- `gen_dataset.py` pulls Pexels CC-0 stock videos, samples random frames per video to build extractor-shaped records (`cover_image` + `images[]`), and writes a `ground_truth.json` mapping `video_basename → expected_record_id`.
- `mock_extractor.py` serves the dataset over the bridge↔extractor contract.
- `import_to_stash.py` copies the same videos into Stash's library, triggers `metadataScan`, and links them to a studio.

The same Pexels video produces a Stash sprite-sheet on one side and N record images on the other. Ground-truth pairing is automatic: scene_X (made from `pexels_42.mp4`) should match the record made from sprite frames of the same `pexels_42.mp4`. Manual labeling is unnecessary.

**Negative controls** — videos imported into the extractor's record set but **not** into Stash — measure false-positive rate. ~12% of the corpus by design.

### 4.3 Sweep harness

`test_calibration.py` accepts CLI overrides for every scoring parameter. A sweep over γ × k × min_contribution looks like:

```bash
for gamma in 2.0 2.5 3.0 3.5; do
  for k in 0.25 0.5 1.0 2.0; do
    python -m tests.integration.test_calibration \
      --gamma $gamma --count-k $k \
      --label "g${gamma}_k${k}"
  done
done
```

Each cell writes a JSONL with per-pair inputs and outputs; comparing across cells reveals the parameter peak. The 30-scene `random.seed=0` sample makes runs comparable: the same 30 scenes are queried in every cell, so the only varying factor is the parameter under test.

---

## 5. History — what calibration found

The defaults shipped today are calibrated, not aesthetic. The path from initial implementation to the current state, summarized:

| Phase | What it was | What changed | Where to read more |
|-------|-------------|--------------|---------------------|
| **Run 1 (N=10)** | Smoke test of the harness. precision@1 = 9/10. | Validated the pipeline (mock extractor, bridge featurization, sprite/cover, multi-channel scoring all worked end-to-end). Did not validate parameters. | CALIBRATION_RESULTS Run 1 |
| **Run 1b (asyncio bug)** | First N=491 attempt hung. | Fixed PIL/imagehash/numpy event-loop blocking — wrapped in `asyncio.to_thread()` at five hot spots. Captured by `TestEventLoopResponsiveness`. | CLAUDE.md §14.4, CALIBRATION_RESULTS Run 1b |
| **Run 2 (N=491 baseline)** | precision@1 = 50% at the original defaults. | Reproduced the "magnet record" failure mode the user predicted. Established the precision floor before tuning. | CALIBRATION_RESULTS Run 2 |
| **Run 3 (γ × min_c × k sweep)** | 17-cell sweep. Peak at γ=3.5, k=0.25, min_c=0.05 → precision@1 = 96.2%, +46 points. | Tuned the per-request scoring parameters. Revealed sparse-N records were systematically under-weighted at k=2.0. | CALIBRATION_RESULTS Run 3 |
| **Run 4 (failure-mode inspection)** | At tuned defaults, 1 positive miss + 3 negative-control returns. | Diagnosed the remaining miss as a single magnet record whose 9 pHash images all have c_i=1.0. Diagnosed 2 of 3 negatives as weak-match-returned, 1 as ground-truth issue. | CALIBRATION_RESULTS Run 4 |
| **Run 5 (re-featurization sweep)** | Sweep of `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` and `_ALPHA`. | Confirmed 0.85/1.0 are empirically optimal. Concave around 0.85; α flat. The remaining miss isn't reachable by global tuning. | CALIBRATION_RESULTS Run 5 |
| **Run 6 (search confidence floor — architectural B)** | Implemented `IMAGE_SEARCH_FLOOR`. Validated at floor=0.15. | Mechanism shipped, default disabled. On Pexels-style corpora the weak-correct and weak-incorrect composite distributions overlap, so no global floor separates them without dropping legitimate positives. | CALIBRATION_RESULTS Run 6, CLAUDE.md §13.7 |
| **Run 7 (per-channel uniqueness — architectural A)** | Implemented `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_PHASH/_TONE`. Swept tone threshold. | Mechanism shipped, defaults inherit global. Counterintuitively, tone *should* be silenced on natural-scene corpora — its noise outranks pHash's signal when c_i collapse is removed. | CALIBRATION_RESULTS Run 7, CLAUDE.md §13.6 |

**Final**: 25/26 = 96.2% precision@1 on a 30-scene seed=0 sample of the 491-video Pexels corpus, MRR 0.962. Wilson confidence at 25/26 is `[82%, 99%]`. Full run-by-run breakdown lives in [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md).

The remaining 1 miss + 3 negative-control behaviors are outside the reach of any explored knob on this corpus; closing them would require a richer feature set (e.g., higher-resolution pHash, learned per-channel weights) — out of scope.

---

## 6. When to run what

| Change                                   | Run                                              |
| ---------------------------------------- | ------------------------------------------------ |
| Code change in scoring math              | `pytest tests/unit/test_scoring.py`              |
| Code change in channel compute           | `pytest tests/unit/test_channels.py`             |
| Code change in DB schema or CRUD         | `pytest tests/unit/test_db.py`                   |
| Code change in lifecycle / worker        | `pytest tests/unit/test_lifecycle.py`            |
| Code change in `match.py` API surface    | `pytest tests/unit/test_phase4_scoring_path.py tests/unit/test_phase5_multichannel.py` |
| Any code change                          | `pytest tests/unit/` (full suite, ~8 s)          |
| Changing scoring defaults                | Re-run a calibration sweep, append to `CALIBRATION_RESULTS.md` (CLAUDE.md §13.10 don't) |
| Suspicious match behavior in production  | Reproduce with `?debug=1` per `HOW_TO_USE.md` §5; if calibration suite has a cell that fits, re-run it |
| Major refactor                           | Full unit suite + at least one calibration cell against the live bridge |

The calibration corpus is **not** part of CI — it requires Stash + a Pexels API key and takes hours to scaffold from scratch. It's a development tool used at major scoring-related milestones.
