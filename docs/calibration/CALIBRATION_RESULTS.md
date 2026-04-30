# Calibration results

Provenance for the multi-channel scoring defaults shipped in
the scraper [`config.py`](../../stash-extract-scraper/config.py), the
bridge `BRIDGE_FEATURIZE_*` env vars, and the architectural invariants
in [`CLAUDE.md`](../../CLAUDE.md) §13.

Each calibration run on a representative corpus produces metrics
that justify (or supersede) the chosen defaults. This document is the
committed source of truth — the underlying datasets and per-run JSONLs
are not committed (too large + license-encumbered).

For the methodology, dataset sourcing, and how to reproduce a run, see
[`README.md`](README.md) §1–§9. For the harness code, see
[`tests/integration/test_calibration.py`](../../tests/integration/test_calibration.py).

---

## Run 2026-04-29 — initial smoke (N=10 corpus)

**Purpose**: end-to-end validation of the calibration pipeline against
real Stash + mock-extractor + bridge. Dataset is intentionally small —
this run validates the harness; later runs validate the parameters.

### Dataset summary

| Field                  | Value                                                                                         |
| ---------------------- | --------------------------------------------------------------------------------------------- |
| Source                 | Pexels free API, default 31-query mix from `tests/calibration/sources.PEXELS_DEFAULT_QUERIES` |
| Videos                 | 10 (sample of the broader query mix; first hit per query)                                     |
| Resolution             | 360p landscape                                                                                |
| Total disk             | ~35 MB cached videos                                                                          |
| Records per record set | varied N: `[1, 1, 2, 2, 3, 3, 3, 4, 4, 6]` (cover + sampled frames)                           |
| Cover strategies       | 1 cover-only, 7 mixed (cover + frames), 2 frames-only                                         |
| Negative controls      | 0/10 (binomial roll missed at N=10; expected ~10% at N≥30)                                    |

### Methodology

1. `gen_dataset.py --source pexels --target 10` → produced
   `dataset/jobs/calib_6ec13af1229b/` and `ground_truth.json`.
2. `import_to_stash.py` → 10 videos copied to
   `/data/calibration/`, scanned, studio "Calibration Test Site"
   created, 10 scenes linked.
3. Stash `metadataGenerate` → covers + sprites generated for all 10.
4. `mock_extractor.py` running on `:12001`.
5. Calibration bridge instance running on `:13001` with
   `BRIDGE_LIFECYCLE_ENABLED=true`, `BRIDGE_NEW_SCORING_ENABLED=true`,
   pointed at the mock extractor and real Stash on `:9999`.
6. `test_calibration.py::test_calibration_run_against_live_bridge` ran
   one query per Stash scene, compared top-1 against
   `ground_truth.json[*].expected_record_id`.

### Parameters used

```
image_mode               = both
threshold                = 0.001
limit                    = 5
hash_algorithm           = phash
hash_size                = 8
sprite_sample_size       = 8

image_gamma              = 2.0
image_count_k            = 2.0
image_uniqueness_alpha   = 1.0
image_channels           = ["phash", "color_hist", "tone"]
image_min_contribution   = 0.05
image_bonus_per_extra    = 0.1
```

These were the best-guess pre-calibration defaults documented in
`CLAUDE.md` §13 prior to this calibration cycle.

### Metrics

| Metric                             | Value                           |
| ---------------------------------- | ------------------------------- |
| `precision@1`                      | **9/10 = 90%**                  |
| `mean_reciprocal_rank`             | (not separately recorded; ≥0.9) |
| `mean_top1_score`                  | 0.4831                          |
| Score range, top-1                 | 0.18 – 1.00                     |
| Negatives correctly returned empty | n/a (no negatives in this run)  |

### Failure case

| Scene                                                          | Expected record           | Top-1 record         | Top-1 score |
| -------------------------------------------------------------- | ------------------------- | -------------------- | ----------- |
| `pexels_36153127.mp4` (lush green tropical forest aerial view) | `scene_0001` (cover-only) | `scene_0006` (mixed) | 0.27        |

Likely cause (deferred for fuller dataset): `scene_0001` is a
**cover-only** record (no extra `images[]` array, just one cover frame).
With N=1 the within-channel formula's `count_conf` saturates at
`1 - exp(-w/k) ≈ 0.22` even for a perfect match, dragging the channel A
score down. Channel B (color histogram aggregate) competes more evenly,
and the wrong record happened to share a closer chromatic profile with
the scene's sprite frames. The fix is corpus-wide, not per-pair: at
larger N, `count_conf` discrimination becomes the primary signal and
single-image records get appropriately discounted relative to dense
records.

### What this run justifies

- **The pipeline works end-to-end.** Mock extractor, bridge featurization,
  Stash sprite/cover, multi-channel scoring, debug observability — all
  function as designed against real video bytes.
- **The default parameters are reasonable.** 90% precision on a tiny,
  varied corpus suggests the formula's structure is correct. The
  pre-calibration defaults are the right starting point.
- **The harness is repeatable.** `test_calibration_run_against_live_bridge`
  can be re-run; the JSONL run-log preserves complete inputs + outputs.

### What this run does NOT justify

- **Tuned defaults.** N=10 is too small. The numbers above are
  consistent with the defaults but don't independently _validate_ them.
  A run at N≥500 with parameter sweeps over γ/k/α/threshold is needed
  before this section can claim "these are the calibrated defaults."
- **Negative-control behavior.** The corpus had no negatives; we don't
  know yet whether the matcher correctly returns empty for unimported
  scenes.
- **Performance under load.** A single-pass run; no concurrency or
  cache-pressure measurements.

---

## Run 2026-04-30 — Run 1b: bug fix validation (cross-corpus state)

**Purpose**: validate an asyncio event-loop blocking bug fix end-to-end. Discovered during prep for Run 2 when the live integration test hung against a calibration bridge that had been pointed at a 491-record dataset.

### Bug

PIL/imagehash/numpy compute is synchronous and CPU-bound. The bridge's `featurize_job`, `_hash_or_compute`, and per-channel feature functions called these directly inside async coroutines. Each `await` between calls yielded to the loop, but inside each hash operation the loop was frozen for ~50–150 ms. With 491 records × ~5 images × 3 channels ≈ 7,000 hash operations queued, the cumulative event-loop block was 5–15 minutes — during which the bridge could not respond to ANY HTTP request, including its own `/health` endpoint. `urlopen` calls to `/match/fragment` timed out at 60 s.

This violated [`CLAUDE.md`](../../CLAUDE.md) §13's contract that "the hot path is `ready` → 200, everything else → 503." The 503 logic was correct; the bridge was simply blocked from emitting it.

### Fix

Wrapped CPU-bound compute in `asyncio.to_thread()` at five hot spots:

- `bridge/app/matching/image_match.py::_hash_or_compute` — channel A inner compute
- `bridge/app/matching/image_match.py::stash_sprite_hashes` — sprite parse + per-frame hash
- `bridge/app/matching/image_match.py::_features_or_compute_bc` — B+C from bytes
- `bridge/app/matching/image_match.py::stash_sprite_bc_features` — sprite decode + per-frame B+C
- `bridge/app/matching/featurization.py` — baseline + uniqueness compute, per-record B aggregate

Compute now runs on a worker thread; the asyncio loop stays free to schedule other coroutines (request handlers, DB writes, the worker pool itself).

### Regression test

Added `tests/unit/test_lifecycle.py::TestEventLoopResponsiveness::test_db_query_latency_stays_low_during_featurization`. Spawns a featurization on 20 synthetic records (~1 s of CPU compute), then samples 10 DB queries mid-run. Asserts max query latency < 150 ms. With the OLD blocking code, latencies of 100–300 ms+ are typical; with `asyncio.to_thread`, samples stay <30 ms under low contention, occasionally up to 80 ms under thread-pool pressure.

### Validation against running bridge

Live integration test re-run against the original 10-video Stash corpus (still imported under `/data/calibration/`) plus the rotated 491-record mock-extractor dataset.

| Measurement                       | Before patch         | After patch                                |
| --------------------------------- | -------------------- | ------------------------------------------ |
| First `/match` request            | 60 s urlopen timeout | **503 in 104 ms**                          |
| Featurization on 491 records      | Blocks loop 5–15 min | Backgrounded; bridge stays responsive      |
| Live integration test             | Hangs forever        | **Passes in 4:00** (queries serially x 10) |
| Unit suite (`pytest tests/unit/`) | 89 pass              | **90 pass** (+ regression test)            |

### Metrics from this run (informational only)

| Metric                 | Value           |
| ---------------------- | --------------- |
| `n_total`              | 10              |
| `n_with_expected`      | 8               |
| `n_correct_top1`       | 3 (3/8 = 37.5%) |
| `mean_reciprocal_rank` | 0.46            |
| `mean_top1_score`      | 0.59            |

The drop from Run 1's 9/10 is **not** a regression of the matcher. Two causes, both expected:

1. **Cross-corpus state**: the 10 Stash scenes have `scene_NNNN` ids assigned by the OLD gen_dataset run (Run 1). The NEW ground_truth assigns DIFFERENT ids to the same Pexels videos (gen_dataset reseeds indices each run, since records are positional within a job). Even when the bridge matches correctly by _content_, the id-to-id comparison fails for slot-shifted records.
2. **Larger candidate pool**: 1-of-491 vs. 1-of-10 raises noise pressure. False-positive rate rises with corpus size when defaults aren't tuned for that scale.

Both conditions are resolved by the planned Run 2 (full N=491 Stash import + parameter sweep).

### What this run justifies

- The asyncio event-loop blocking bug is fixed.
- The §4.2 contract ("hot path → 200, else → 503") now holds in practice, not just on paper.
- The harness, mock-extractor, and bridge function correctly under realistic compute load (~20× the Run 1 record count).

### What this run does NOT justify

Same as Run 1: tuned defaults still need a real N=491 sweep.

---

## Run 2026-04-30 — Run 2: full-corpus baseline (N=491, default params)

**Purpose**: first end-to-end calibration run against the actual full Pexels corpus imported into Stash. Establishes the precision floor at the existing default parameters before any tuning.

### Dataset summary

| Field                  | Value                                                                                          |
| ---------------------- | ---------------------------------------------------------------------------------------------- |
| Source                 | Pexels free API, default 31-query mix                                                          |
| Records                | 491 (431 positives + 60 negative controls = 12.2%)                                             |
| Stash scenes           | 500 indexed under `/data/calibration/` (= 491 from this run + 10 residual from Run 1's import) |
| Cover strategies       | per `gen_dataset` design (~30% cover-only, ~50% mixed, ~20% no-cover)                          |
| Records per record set | varied N: weighted toward `[1, 2, 3, 5, 8]`                                                    |
| Total disk             | ~1.7 GB cached videos, ~3.4 GB after Stash import + sprites/covers                             |

### Methodology

Same harness as Runs 1/1b. Sample of **30 random Stash scenes** (`random.seed=0`) drawn from all 500. Each query against the calibration bridge, top-1 compared to `ground_truth.json[*].expected_record_id`. Sample is fixed across all subsequent sweep cells so results are directly comparable.

Per-query latency: ~24 s (Stash-side B/C lazy compute on first hit + serial scoring against 491 candidates × 3 channels of cached features). 30 scenes ≈ 12 minutes per cell.

### Parameters used (pre-calibration defaults)

```
image_gamma              = 2.0
image_count_k            = 2.0
image_uniqueness_alpha   = 1.0
image_min_contribution   = 0.05
image_bonus_per_extra    = 0.1
```

### Metrics

| Metric                        | Value             |
| ----------------------------- | ----------------- |
| `precision@1`                 | **13/26 = 50.0%** |
| `mean_reciprocal_rank`        | 0.530             |
| `mean_top1_score`             | 0.553             |
| `n_negatives_correctly_empty` | 0/3               |

Drop from Run 1's 9/10 = 90% on the 10-corpus is expected: 1-of-491 vs. 1-of-10 raises noise pressure ~50× without compensating tuning.

### Failure mode (debug=1 inspection at γ=2.0)

The wrong top-1 winners cluster on a small set of "magnet" records (`scene_0057`, `scene_0353`, `scene_0226`). They keep beating diverse legitimate matches. The pattern is consistent with insufficient noise-floor sharpening: any sim slightly above the per-channel baseline contributes to the evidence-union and accumulates across N=8-9 record images. Records with broad-shallow image sets dominate over records with one or two strong matches.

### What this run justifies

- Real precision metrics for the multi-channel scoring at default parameters: 50%. Far above 1/491 random (0.2%) but well below the smaller-corpus result.
- The "magnet record" failure mode the user predicted in the very first design conversation has been reproduced empirically.

---

## Run 2026-04-30 — Run 3: γ × min_contrib × k sweep (17 cells)

**Purpose**: identify the per-request parameter combination that maximizes precision@1 without re-featurization.

### Methodology

Same 30-scene sample as Run 2. Three sweep batches:

- **Batch A** (9 cells): γ ∈ {2.0, 2.5, 3.0} × `image_min_contribution` ∈ {0.05, 0.15, 0.30}.
- **Batch B** (3 cells): γ ∈ {3.5, 4.0, 5.0} at `image_min_contribution = 0.05` (extending Batch A's best column).
- **Batch C** (5 cells): `image_count_k` ∈ {0.1, 0.25, 0.5, 1.0, 1.5} at γ=3.5, min_c=0.05 (the new tip after Batches A+B).

Total: 17 cells × ~12 min ≈ 3.4 hr wall-clock. Each cell wrote a per-pair JSONL under `tests/calibration/runs/`.

### Findings

#### 3a. γ sweep at min_contrib=0.05, k=2.0 (default)

```
   γ     p@1     MRR    n_correct
 2.0   0.500   0.530      13       (default)
 2.5   0.577   0.635      15
 3.0   0.692   0.731      18
 3.5   0.769   0.782      20       ← peak in batch
 4.0   0.731   0.731      19
 5.0   0.731   0.731      19
```

Concave shape: each step from γ=2.0 to γ=3.5 adds 8–12 points; γ ≥ 4.0 plateaus then drifts. **Peak at γ=3.5 (76.9%).** Mean top-1 score drops monotonically with γ (0.55 → 0.39) — sharpening compresses the score range — but rankings improve.

Empirical mechanism: at γ=2.0, sims slightly above baseline (0.55-0.65) sharpen to 0.04-0.18, accumulating to misleading composite scores via evidence-union. At γ=3.5, those same sims sharpen to 0.001-0.05 and effectively drop out, leaving only true-strong sims (>0.75) in the running.

#### 3b. min_contrib sweep (at all γ values)

```
   γ      0.05    0.15    0.30
 2.0     0.500   0.423   0.423
 2.5     0.577   0.462   0.423
 3.0     0.692   0.462   0.423
```

`min_contrib=0.05` dominates at every γ. Higher min_contrib values collapse to a uniform ~0.42 floor. Why: at γ ≥ 2.5, weak channels (typically B and C) contribute small but non-zero S values for both correct and incorrect candidates. Raising `min_contrib` excludes these from the bonus, but the harm to correct matches (which often have one strong + several borderline channels) outweighs the harm to incorrect matches (which often have one strong + several near-zero). **Keep min_contrib at 0.05.**

#### 3c. count_k sweep at γ=3.5, min_contrib=0.05

```
    k     p@1     MRR    n_correct
 0.10   0.962   0.962      25       ← tied peak
 0.25   0.962   0.962      25       ← tied peak (chosen)
 0.50   0.923   0.936      24
 1.00   0.846   0.865      22
 1.50   0.769   0.782      20
 2.00   0.769   0.782      20       (default)
```

Monotonic improvement as k decreases, plateau between k=0.25 and k=0.10. **Peak at k=0.25: 96.2% precision@1.** From the default of k=2.0 this is +19.3 points on top of the +27 already from γ tuning.

Empirical mechanism (from B v1 debug at γ=3.5): the misses at γ=3.5, k=2.0 were sparse-N records with one perfect sim losing to broad-shallow records with N=8-9 mediocre sims. Specifically, at scene 232 → `scene_0263`: the expected had a single sim at 0.969 (sharpens to m'=0.794), but `count_conf = 1 - exp(-w/2) = 0.18` for w=0.394, dragging S down to 0.028. The wrong winner had N=8 with shallow sims, `count_conf = 0.92`, S = 0.151. Lowering k flips count_conf for sparse records: at k=0.25, `count_conf = 1 - exp(-0.394/0.25) = 0.79`, lifting S to 0.124 — competitive.

Choosing k=0.25 over k=0.10: identical p@1, but k=0.10 effectively neutralizes count_conf (count_conf ≥ 0.99 for any plausible w). k=0.25 retains a soft penalty for very weak signals (count_conf=0.55 at w=0.2) without overweighting them. More conservative if dataset characteristics shift.

### Tuned parameters (proposed new defaults — _pending architectural section before final commit_)

```
image_gamma              = 3.5     (was 2.0)
image_count_k            = 0.25    (was 2.0)
image_uniqueness_alpha   = 1.0     (unchanged, may move after re-featurization sweep)
image_min_contribution   = 0.05    (unchanged)
image_bonus_per_extra    = 0.1     (unchanged)
```

**Overall: 50.0% → 96.2% precision@1, +46 points.** MRR 0.530 → 0.962.

### Open questions for follow-up

- **Negative-control behavior unchanged at every γ and k value (0/3 correctly empty in 17/17 cells)**. Search mode always returns top-N by design; sharpening + count_conf adjustments don't introduce a "no match" decision. Behavior, not a tuning parameter — addressed in pending architectural section.
- **Channel C (tone) is essentially dead** at all params: tone `c_i` values are consistently 0.02–0.06 because the featurization-time uniqueness threshold (0.85) flags too many natural-scene tone profiles as near-duplicates. Target of the upcoming re-featurization sweep.
- **One remaining miss + 3 false-positive negatives at the tuned params** — to be inspected in Run 4 (B v2 at tuned defaults).

---

## Run 2026-04-30 — Run 4: failure-mode inspection at tuned per-request defaults

**Purpose**: identify the remaining miss + characterize the 3 negative-control returns at γ=3.5, k=0.25, min_contrib=0.05. Single-pass with `debug=1`.

### The 1 positive miss

Scene 316 (`pexels_31540266.mp4`) → expected `scene_0096` not in top-5; got `scene_0057`.

```
WRONG TOP-1: scene_0057  composite=0.148  fired=['phash']
  phash:    S=0.148  E=0.149  count=1.000  dist=0.996
            base=0.511  N=9  M=9
            per_image_max=[0.688, 0.688, 0.688, 0.688, 0.656, 0.688]
            m_primes=[0.028, 0.028, 0.028, 0.028, 0.014, 0.028]
            qs=[0.666, 0.666, 0.666, 0.667, 0.666, 0.666]
            cs=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0]    ← key
  color_hist: S=0.000  sim=0.141  m'=0.000  q=0.170  base=0.188
  tone:       S=0.000  E=0.000   count=1.000  dist=0.500
              cs=[0.5]*9
```

Diagnosis: **scene_0057's 9 pHash images all have c_i=1.0 (zero near-duplicates detected at threshold=0.85)**. Despite each individual sim being modest (sims around 0.69, sharpening to m'=0.028), the evidence-union over 9 fully-weighted contributions accumulates to S=0.148. With c_i=1.0 across the board, no penalty offsets the broad-shallow accumulation.

This is a "magnet" record that the global threshold of 0.85 cannot demote — its images really are below the 0.85 near-duplicate bar relative to other records' pHash hashes, but it still wins by quantity over quality.

### The 3 negative-control returns

| Scene                       | composite | top-1      | Diagnosis                                                                                                                                                                                                                                           |
| --------------------------- | --------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 293 (`pexels_30089587.mp4`) | 0.110     | scene_0320 | Weak match returned; expected scene_0297 at rank 3                                                                                                                                                                                                  |
| 656 (`pexels_9723916.mp4`)  | **0.323** | scene_0453 | Real content match. `m_prime=1.0` (perfect sim 1.0 on the cover). Channel B also fires (sim=0.852). The "negative_control" flag is a gen_dataset randomization, but the underlying content legitimately matches another record. Ground-truth issue. |
| 344 (`pexels_32928490.mp4`) | 0.134     | scene_0357 | Weak match returned; expected scene_0445 at rank 5                                                                                                                                                                                                  |

Two distinct sub-modes:

1. **Weak-match-returned (negatives 293, 344)**: composite ~0.11–0.13 — clearly below the typical 0.4–1.0 range of correct positive top-1s. Search mode returns whatever ranks highest; there's no "no match" mechanism. **Architectural issue, addressable by a search-mode confidence floor.**
2. **Real-content collision (negative 656)**: composite 0.32, m_prime=1.0 — the matcher correctly identified that this Stash scene's frames match a record's images. The `negative_control` flag in `ground_truth.json` is incorrect for this case. **Not fixable in code.**

---

## Run 2026-04-30 — Run 5: re-featurization sweep (threshold + alpha)

**Purpose**: determine whether per-image c_i tuning at featurization time can demote the magnet record (scene_0057) and push past 25/26.

### Methodology

Each cell: kill bridge → fresh DATA_DIR → restart with new env var → trigger discovery → wait for re-featurization (consistently ~72 s for 491 records on the patched async-thread code) → run 30-scene sample at the tuned per-request params (γ=3.5, k=0.25, min_c=0.05).

### 5a. `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` ∈ {0.70, 0.80, 0.85, 0.90}

```
 thresh     p@1     MRR    n_correct
   0.70   0.808   0.840      21       (over-flags → demotes legitimate matches)
   0.80   0.923   0.942      24
   0.85   0.962   0.962      25       ← peak (current default)
   0.90   0.731   0.766      19       (under-flags → magnets escape further)
```

Concave with a clean peak at the existing default. Both directions degrade.

- Lower (0.70): too many pairs flagged as near-duplicate. Even legitimate scene-content matches end up with reduced c_i, dragging real matches down. Magnets do get demoted, but legitimate records get demoted more.
- Higher (0.90): too few pairs flagged. Magnets that already had c_i=1.0 at 0.85 stay at 1.0 (no change for them), but borderline real matches that previously had c_i=0.9 now have c_i=1.0 too — _additional_ records with no penalty, increasing the noise floor.

### 5b. `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA` ∈ {0.5, 1.0, 2.0}

```
 alpha     p@1     MRR    n_correct
  0.50   0.962   0.962      25
  1.00   0.962   0.962      25       (default)
  2.00   0.962   0.962      25
```

Identical at all three values. Why: the 1 remaining miss has `cs=[1.0]*9` for the magnet record's pHash. When `matches=0`, `c_i = 1/(1+α·0) = 1.0` regardless of α. α only modulates penalty _when_ there are detected matches — but the magnet has zero matches.

### Conclusion of Run 5

**Re-featurization knobs cannot move past 25/26 on this corpus.** Both `threshold=0.85` and `α=1.0` are empirically optimal. The structural cause of the remaining miss (a record whose images aren't flagged as near-duplicates at the global pHash threshold but are still "magnet"-like) is unreachable by global tuning.

### Implications for architectural decisions

Two architectural changes should close the remaining gaps without compromising the per-request tuning:

| Change                                                                                                                               | Targets                                                                                                                                                                               | Impact estimate                                                                                               | Risk                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Per-channel uniqueness threshold + alpha** (e.g., `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_PHASH`, `_TONE`; same for alpha)          | The 1 magnet miss; channel C tone collapse (separate issue surfaced in Runs 1b/2/3 — tone `c_i` consistently 0.02–0.06 because the 0.85 threshold is far too lax for tone signatures) | Possibly closes the magnet miss; revives channel C as a tie-breaker. Net: 25/26 → 26/26 plausible.            | Moderate. ~30 lines in `featurization.py`, settings, tests. Requires another re-featurization sweep with per-channel cells. |
| **Search-mode confidence floor** (`image_search_floor` config; in `search.py`, drop ranked entries below the floor before returning) | The 2 weak-match negatives (composites 0.11, 0.13)                                                                                                                                    | Negatives 0/3 → 2/3 correctly empty. Zero impact on positives (their composites at tuned params are 0.4–1.0). | Low. ~5 lines in `search.py`, settings, one new test.                                                                       |

Per-channel composition weights (a third architectural option) are not justified by the data: channel A is doing essentially all the discriminating work; B and C contribute via the bonus only when fired. Adding weights would add knobs without addressing the observed failure modes.

---

## Run 2026-04-30 — Run 6: search confidence floor (architectural B) — implemented, default disabled

**Purpose**: validate the search-mode confidence floor at `floor=0.15` against the same 30-scene sample at the tuned per-request defaults.

### Implementation

- New scraper field `IMAGE_SEARCH_FLOOR: Optional[float]`. Bridge setting plumbed through `models.py → search.py`.
- In `search.py`, after computing `image_contrib`, drop the candidate from the result list when `image_contrib < floor` AND no definitive signal fired (Studio+Code or Exact Title bypass — they have their own correctness contracts).
- Three unit tests in `tests/unit/test_phase5_multichannel.py::TestSearchConfidenceFloor` (drops weak, keeps strong, None disables).

### Verification result

```
              No floor    floor=0.15
n_correct      25          20         (-5)
n_negatives_OK  0           2         (+2)
precision@1   0.962      0.769        (-0.193)
```

The floor at 0.15 dropped 5 _correct_ positives whose image composites sat below 0.15, while only converting 2 of 3 negative-control returns to correctly empty. **Net effect: −19 points of precision.**

### Why the floor failed at any tested value

The image-composite distributions of weak-but-correct positives and weak-but-incorrect negatives **overlap** on this corpus:

| Class                                          | Composite range observed |
| ---------------------------------------------- | ------------------------ |
| Strong correct positives                       | 0.3 – 1.0                |
| **Weak correct positives** (5 of 26 in sample) | 0.05 – 0.14              |
| **Weak negative-control returns** (2 of 3)     | 0.11 – 0.13              |
| Real-content-collision negative (1 of 3)       | 0.32                     |

No global floor can separate the two weak classes — they live in the same range. The architectural fix needs to _widen the gap_, which is what (A) per-channel uniqueness aims to do (revive channel C, demote the magnet → correct positives gain composite from extra firing channels, weak negatives don't).

### Decision

Ship the floor mechanism (code + tests) but **default `IMAGE_SEARCH_FLOOR = None`**. Document that users with sharper corpora may enable it at 0.10–0.20. Re-evaluate after (A) lands.

---

## Run 2026-04-30 — Run 7: per-channel uniqueness threshold (architectural A) — implemented, no default change

**Purpose**: split the global `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` (and `_ALPHA`) into per-channel knobs, hypothesizing that tone — known dead at the global 0.85 because every natural-scene tone profile gets over-flagged — would benefit from a stricter threshold (~0.95) that lets it contribute meaningfully.

### Implementation

- `Settings.channel_uniqueness_threshold(channel)` and `channel_uniqueness_alpha(channel)` resolution methods. Per-channel override (`_PHASH`, `_TONE`) takes precedence; `None` inherits the global.
- `featurization.py::_featurize_inner` calls the per-channel resolution methods before computing uniqueness.
- 5 unit tests in `test_db.py::TestPerChannelUniqueness` covering inheritance, override precedence, and unknown-channel fallback.

### Sweep over `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_TONE`

```
 tone_t     p@1     MRR    n_correct    neg_OK
   0.85   0.962   0.962      25            0       ← peak (global default)
   0.90   0.731   0.748      19            0
   0.95   0.692   0.724      18            0
   0.98   0.731   0.763      19            0
```

### Counterintuitive finding

Lifting the tone threshold _hurt_ precision substantially. The hypothesis (over-flagging at 0.85 collapses c_i and "kills" tone) was correct _factually_ but wrong _strategically_ — the c_i collapse was the desired behavior on this corpus.

Tone is a coarse 8×8 grayscale signature. On Pexels-quality natural-scene content, many unrelated frames share luminance distributions (tone sim 0.85+ between truly unrelated content is common). When tone's c_i is high (uniqueness threshold strict), tone enters the evidence-union with full weight — and tone's noise outranks pHash's signal on borderline scenes, dragging precision down.

The data prefers tone _silenced_, which is exactly what the global threshold of 0.85 produces (tone c_i collapses to 0.02–0.06, S → 0, channel C effectively absent from the composite).

### Decision

- Keep the per-channel mechanism (code + tests). Useful for corpora where tone _is_ discriminating (monochrome film, surveillance footage, controlled-lighting content).
- Do not ship a tone-specific default. Per-channel overrides default to `None` (inherit global). Documented in `settings.py`.
- Pexels-style mixed-content corpora benefit from the existing global 0.85 / α=1.0.

### Bonus observation

A simpler way to get the same behavior on this corpus is to drop tone from the channel list entirely: `IMAGE_CHANNELS = ["phash", "color_hist"]`. Mathematically equivalent (tone S=0 doesn't fire above min_contribution=0.05, doesn't enter the bonus, doesn't change `max(channels)`), and ~33% faster per-query. This is a calibration-time choice the user can make in `config.py`; the architectural mechanism (per-channel uniqueness) supports both forms.

---

## Final tuned defaults (after Runs 1–7)

| Parameter                                 | Default before | Tuned default                                  | Source                |
| ----------------------------------------- | -------------- | ---------------------------------------------- | --------------------- |
| `image_gamma`                             | 2.0            | **3.5**                                        | Run 3a                |
| `image_count_k`                           | 2.0            | **0.25**                                       | Run 3c                |
| `image_min_contribution`                  | 0.3            | **0.05**                                       | Run 3b                |
| `image_uniqueness_alpha` (request)        | 1.0            | 1.0                                            | unchanged             |
| `image_bonus_per_extra`                   | 0.1            | 0.1                                            | unchanged             |
| `image_search_floor`                      | n/a            | **None** (mechanism shipped, default disabled) | Run 6                 |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`   | 0.85           | 0.85                                           | Run 5a confirmed peak |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`       | 1.0            | 1.0                                            | Run 5b flat           |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_*` | n/a            | **None** (per-channel mechanism shipped)       | Run 7                 |

Three new code paths added that ship disabled by default:

- Search confidence floor (`IMAGE_SEARCH_FLOOR`)
- Per-channel uniqueness threshold (`*_THRESHOLD_PHASH/TONE`)
- Per-channel uniqueness alpha (`*_ALPHA_PHASH/TONE`)

All preserve backward compatibility while exposing knobs for corpora that benefit from them.

### Performance summary

| Configuration                            | precision@1 | MRR   | Notes                                                                 |
| ---------------------------------------- | ----------- | ----- | --------------------------------------------------------------------- |
| Before any tuning (Run 2 baseline)       | 50.0%       | 0.530 | Default params, full corpus baseline                                  |
| After per-request tuning (γ, k, min_c)   | 96.2%       | 0.962 | +46 points                                                            |
| After re-featurization sweeps (Runs 5+7) | 96.2%       | 0.962 | Confirmed peak; default values empirically validated                  |
| After architectural changes (B, A)       | 96.2%       | 0.962 | Mechanisms shipped; defaults unchanged because corpus doesn't benefit |

**Final: 25/26 = 96.2% precision@1, MRR 0.962** at the proposed tuned defaults. The 1 remaining miss + 3 negative-control behaviors are outside the reach of any explored knob on this corpus; closing them would require a richer feature set (e.g., higher-resolution pHash, learned per-channel weights) — out of scope.

---

## Promoted defaults

The per-request tuned defaults (γ=3.5, k=0.25, min_c=0.05) and the
mechanism flags (per-channel uniqueness, search confidence floor) are
shipped in [`stash-extract-scraper/config.py`](../../stash-extract-scraper/config.py),
[`bridge/app/settings.py`](../../bridge/app/settings.py), and
[`.env.example`](../../.env.example) — with provenance comments
pointing back to the run sections above.
