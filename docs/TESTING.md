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
│   ├── test_phase5_multichannel.py   # channels B+C, composite, multi-channel validation,
│   │                                 # SearchConfidenceFloor (CLAUDE.md §13.7)
│   ├── test_embedding.py             # channel D math: cosine sim, fp16 blob round-trip,
│   │                                 # algorithm-key versioning; GPU-skipped encode tests
│   ├── test_image_match_prefilter.py # D-batch + cached_channels plumbing (CLAUDE.md §13.10)
│   ├── test_frames.py                # animation-aware decode: format allowlist, GIF/WebP/APNG
│   │                                 # frame extraction, uniform temporal sampling, HEIF/AVIF skip
│   │                                 # (CLAUDE.md §13.7/§13.8/§13.8.1)
│   ├── test_imgmatch_animated.py     # synthetic ref expansion, _parent_ref, _resolve_fingerprint_for,
│   │                                 # _collapsed_stash_cover_fetcher, prefetched-bytes featurize path
│   ├── test_local_features.py        # channel E (DISK+LightGlue): algorithm-key versioning,
│   │                                 # blob serialization round-trip, GPU-skipped encode/match smoke
│   ├── test_image_match_channel_e.py # score_image_channel_e shape: empty-side returns 0, below
│   │                                 # min_inliers no-fire, max-across-pairs, composite math with E
│   ├── test_performer_index.py       # performer_index schema, populate hook, FK cascade, dedup,
│   │                                 # null/non-string handling, idempotent backfill (CLAUDE.md §17)
│   └── test_admin_endpoints.py       # /api/admin/* projections: jobs (incl. full feature_state),
│                                     # records (search across id/title/url/performer with LIKE-escape,
│                                     # sort by all four keys, pagination), record detail (asset URL
│                                     # rewrite), performers (aggregation, alphabetical), performer
│                                     # detail (grouping, case-insensitivity)
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

The unit suite runs without external dependencies (no Stash, no extractor, no network). Run it on every change. Tests in `test_embedding.py::TestEncodeOnGPU` are auto-skipped when CUDA isn't available — they run only on the GPU host. The rest of the unit suite (including `test_frames.py`) runs unconditionally; PIL is sufficient for both static and animated decode. The integration harness and calibration corpus are heavier — used for evaluating scoring quality, not for catching regressions in the inner code.

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

### 2.7 `test_embedding.py`

Channel D math from `bridge/app/matching/embedding.py`:

- `cosine_sim` is symmetric, returns 1 for identical vectors, 0 for orthogonal, -1 for antiparallel, and 0 for zero-magnitude inputs (no NaN). fp16 inputs promote to fp32 internally so accumulation doesn't underflow.
- `cosine_sim_matrix(S, E)` produces an `(M, N)` matrix with all entries in `[-1, 1]`; self-similarity diagonal is ~1.0; empty `S` returns a `(0, N)`-shaped result instead of erroring.
- `embedding_to_blob` / `blob_to_embedding` round-trip preserves vectors at fp16 precision regardless of input dtype; serialized size is `dim × 2` bytes.
- `algorithm_key()` formats as `<model>:<dtype>:<dim>` so a model swap creates rows under a fresh key without touching old ones.

A separate `TestEncodeOnGPU` class (decorated `@pytest.mark.skipif(not _has_cuda())`) loads the configured model on the GPU host and asserts encoded shape + dtype on synthetic images. These run only where CUDA is available; on CPU-only dev they're skipped.

### 2.8 `test_image_match_prefilter.py`

D-batch and `cached_channels` plumbing from `bridge/app/matching/image_match.py` (CLAUDE.md §13.10):

- `score_image_channel_d_batch` short-circuits to S=0 for every candidate when Stash side has no embeddings; same when no candidate has any extractor embeddings.
- Per-candidate offsets align correctly under varying N_i across candidates: each candidate's `S` equals the mean of its own per-image-max sims (not bleed from neighbors in the stacked matrix). Candidate counts (`n_extractor_images`) are reported per candidate.
- When `score_image_composite` is given a `cached_channels={"embedding": ...}`, the per-candidate `score_image_channel_d` is **not** called. This is the load-bearing invariant for the K>0 two-pass: A/B/C run lazily on top-K candidates while D's batch result is reused without recompute.

Stubs the embedding-side helpers (`extractor_image_embedding`, `stash_cover_embedding`, `stash_sprite_embeddings`) directly so no DB or HTTP machinery is needed. Live end-to-end checks against a real job are handled by the smoke harness in `scripts/smoke_channel_d.py`.

### 2.9 `test_frames.py` (25 tests)

Animation-aware decode (CLAUDE.md §13.7/§13.8/§13.8.1) from `bridge/app/matching/imgmatch/frames.py`:

- **Format allowlist**: positive identification of `{GIF, WEBP, PNG, JPEG}`. HEIF/AVIF magic bytes return `None` from `load_image_frames` (skip per §13.8 rule 4) regardless of which Pillow plugins happen to be installed in the host. Garbage bytes also return `None`.
- **`load_image_frames`**: static images return a single-frame list; animated GIF/WebP/APNG return all frames as RGB; `max_frames` caps the output.
- **`is_animated_bytes`**: discriminates animated from static and from undecodable bytes.
- **`collapse_stash_cover`**: returns the original bytes unchanged for static input; produces valid still JPEG bytes for animated input (mode `middle` or `median`); returns `None` for unsupported formats.
- **`uniform_sample_frames`**: returns up to `max_frames` JPEG frame bytes at uniform temporal indices for animated input; returns the single static frame for static input; returns `None` on unsupported format or decode failure. Verifies index spread is non-degenerate (frames are not all identical).

All fixtures are PIL-generated in-test — no external assets, no network.

### 2.10 `test_imgmatch_animated.py` (10 tests)

Animated-asset wiring in `bridge/app/matching/image_match.py` and `featurization.py` (CLAUDE.md §13.7/§13.8.1):

- `_parent_ref` strips `#frame_N` from synthetic refs; static refs pass through.
- `_resolve_fingerprint_for` uses the parent asset URL for synthetic refs (cohort invalidation under cascade).
- `_collapsed_stash_cover_fetcher` returns animated bytes collapsed to a single JPEG; static bytes pass through; fetch failures return `None`.
- `extractor_image_hash` accepts a synthetic ref + `prefetched_bytes` and writes the cached row without invoking the fetcher (the load-bearing contract for the featurization fast path on animated assets).
- `_expanded_extractor_refs` expands animated parents to their numerically-sorted synthetic frame refs (`frame_2 < frame_10`, not the lexicographic order); mixes static + animated refs cleanly; static-only inputs pass through.

Uses the `bridge_db` fixture for prefix-scan queries; otherwise mocks `ex_client.fetch_asset` and `stash_client.fetch_image_bytes` directly.

### 2.11 `test_performer_index.py` (14 tests)

Display-only aggregation index for the viewer (CLAUDE.md §17.2):

- **Schema**: `init_db` creates the table and both indexes (`idx_performer_index_name`, `idx_performer_index_job`).
- **Populate**: `upsert_job_and_results` inserts one row per `(job_id, result_index, name_lower)` from `data.performers`. Within-record dedup is case-insensitive (first-occurrence wins for display casing). Null/empty/non-string entries are skipped; whitespace is stripped.
- **Cascade**: rows are picked up automatically by `extractor_results → extractor_jobs` FK chain when `completed_at` advances OR the parent job is directly deleted. Cross-job isolation: cascading j1 leaves j2's rows intact.
- **Backfill**: `backfill_performer_index()` populates pre-migration corpora; idempotent (already-indexed jobs are skipped); zero-performer jobs are scanned but produce no rows; jobs already populated by the `upsert_job_and_results` hook are not re-scanned.

These defend the §17 boundary: the viewer reads from a table that's a derived view of `extractor_results`, not a parallel source of truth.

### 2.12 `test_admin_endpoints.py` (34 tests)

`/api/admin/*` read-only projections (CLAUDE.md §17.3):

- **Jobs**: returns scene-shaped jobs with record counts and full `feature_state` (state/progress/started/finished/error). Most-recent `completed_at` first. LEFT JOIN to `job_feature_state` produces nulls when no row exists.
- **Records list**: `q` searches across id (exact), title (LIKE), url (LIKE), performer (LIKE on `performer_index`). User wildcards (`%`, `_`) are escaped — `q="%charlie%"` does NOT match a title containing "charlie". Sort by `id` / `title` / `date` / `performer` (the latter via correlated MIN subquery on `performer_index`); deterministic tiebreak `(job_id, result_index)` per CLAUDE.md §10. Pagination math (page/totalPages) verified.
- **Record detail**: asset refs rewritten from `../assets/x.jpg` → `/api/asset/{job_id}/assets/x.jpg`; absolute URLs pass through unchanged; bare filenames routed to `assets/`; null/empty refs return `None`. `image_count` includes the cover. 404 on unknown index.
- **Performers**: aggregation produces one row per distinct `name_lower`, with `record_count` and `job_count`. Display casing is the lex-min `name_original`. Job filter narrows. Name search via LIKE on `name_lower`. Pagination works.
- **Performer detail**: case-insensitive lookup (`Alice` / `alice` / `ALICE` produce the same response). Records grouped by job, jobs ordered by `completed_at` DESC. 404 on unknown name. Asset URLs rewritten in nested records.

These defend the §17.3 endpoint contract: shape, filtering, sorting, asset URL rewrite, and the invariant that the viewer never sees raw extractor URLs.

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

## 6. Field observations — corpus content shape vs match quality

The calibration in §5 fixed the scoring math against one well-behaved corpus class. Manual testing against a wider variety of corpora has surfaced patterns the calibration could not see, because they're properties of the input data rather than the algorithm. These notes do not change defaults; they shape expectations and inform threshold choices in deployment.

### 6.1 Embedding-only zero-shot quality is corpus-content-bound

Sprite mode + channel D embedding-only (`BRIDGE_EMBEDDING_PREFILTER_K=0`) was tested across three structurally distinct corpora. Top-1 composite distribution per class:

| Corpus class                                                                                                      | Top-1 composite | Threshold-fire rate |
| ----------------------------------------------------------------------------------------------------------------- | --------------- | ------------------- |
| Natural-image, both sides depicting the same content (calibration-style)                                          | 0.75 – 0.98     | ≈100% over 0.7      |
| Static-image-rich curated, multiple within-scene stills per record                                                | 0.65 – 0.85     | most over 0.7       |
| Stylized within-studio, extractor records carry off-scene staged promos rather than within-scene stills           | 0.45 – 0.65     | rarely over 0.7     |

The third class is hard not because the math is wrong but because cosine similarity between a video screenshot and a staged promo of the same performer/scene is genuinely lower than between two within-scene stills. Sprite mode (M=8) reduces but does not close this gap. Lowering the threshold for that corpus class accepts more false-positive fires; the right knob is content (request additional within-scene stills upstream) or threshold per deployment, not the bridge defaults.

### 6.2 Animated extractor assets — uniform sampling vs scene-detect

An earlier implementation used ffmpeg scene-detection to extract frames from animated assets, anticipating scenic content (cuts, motion). In production this collapsed nearly every animated asset to a single frame at the default scene-change threshold — animated assets in real corpora are typically short action loops with no scene cuts, not scenic content. Switching to PIL uniform temporal sampling restored the multi-frame contribution (≈8 frames per asset at the default cap), and corpus-level dedup via `c_i` covers the redundancy concern. See CLAUDE.md §13.8.1 for the invariants.

The mechanical fix did not lift threshold-fire rate on stylized within-studio corpora — the mean-of-max math was correctly exposing that animated frames in such corpora are *also* off-scene staged content, not within-scene moments. This is consistent with §6.1: animated handling is a faithful conduit; it cannot manufacture similarity that isn't in the underlying frames.

### 6.3 What this means for new corpora

Before tuning anything for a new corpus, run a 10–30 scene sample with `?debug=1` and inspect the top-1 composite distribution. If composites cluster ≥0.7, calibration transferred; ship as-is. If composites cluster 0.5–0.65, treat the corpus as the third class above — tune threshold downward for that deployment if the precision/recall trade is acceptable, but understand that a stylized-promo corpus will not reach calibration-class precision regardless of what the bridge does. If composites cluster <0.4 across the board, suspect a featurization or routing bug rather than a content issue, and check `image_features` synth row counts and the debug envelope's `_debug.image.channels.embedding.extractor_refs` to confirm the expected refs are flowing through.

---

## 7. When to run what

| Change                                   | Run                                              |
| ---------------------------------------- | ------------------------------------------------ |
| Code change in scoring math              | `pytest tests/unit/test_scoring.py`              |
| Code change in channel compute           | `pytest tests/unit/test_channels.py`             |
| Code change in DB schema or CRUD         | `pytest tests/unit/test_db.py`                   |
| Code change in lifecycle / worker        | `pytest tests/unit/test_lifecycle.py`            |
| Code change in `match.py` API surface    | `pytest tests/unit/test_phase4_scoring_path.py tests/unit/test_phase5_multichannel.py` |
| Code change in `embedding.py`            | `pytest tests/unit/test_embedding.py` (GPU-only encode tests auto-skip on CPU) |
| Code change in D-batch / prefilter wiring | `pytest tests/unit/test_image_match_prefilter.py` |
| Code change in performer_index populate / backfill | `pytest tests/unit/test_performer_index.py` |
| Code change in `local_features.py` or channel E scoring | `pytest tests/unit/test_local_features.py tests/unit/test_image_match_channel_e.py` (GPU-only encode/match tests auto-skip on CPU) |
| Code change in `bridge/app/api/admin.py` or viewer-facing projections | `pytest tests/unit/test_admin_endpoints.py` |
| Any code change                          | `pytest tests/unit/` (full suite)                |
| Changing scoring defaults                | Re-run a calibration sweep, append to `CALIBRATION_RESULTS.md` (CLAUDE.md §13.11 don't) |
| Suspicious match behavior in production  | Reproduce with `?debug=1` per `HOW_TO_USE.md` §5; if calibration suite has a cell that fits, re-run it |
| Major refactor                           | Full unit suite + at least one calibration cell against the live bridge |

The calibration corpus is **not** part of CI — it requires Stash + a Pexels API key and takes hours to scaffold from scratch. It's a development tool used at major scoring-related milestones.
