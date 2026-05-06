# Channel E — Local feature matching (SuperPoint + LightGlue)

> Transient implementation plan. To be folded into CLAUDE.md §13 + TESTING.md and deleted on merge per `MEMORY.md` planning-doc lifecycle.

## Goal

Replace A/B/C with **E** (local-feature geometric verification) in the K>0 two-pass rerank, targeting the failure class where D ranks the correct candidate at top-1 but composite is below threshold due to viewpoint/perspective/resolution shift between Stash sprite frames and extractor record images.

D-only path (K=0) is unchanged. A/B/C remain on the legacy no-embedding path for backward compatibility.

## Scope

**In scope** (minimum-shippable):
- SuperPoint extraction at featurize-time → keypoints + descriptors stored in `image_features`.
- LightGlue + RANSAC homography at match-time, scoped to top-K candidates per `BRIDGE_EMBEDDING_PREFILTER_K`.
- Composite math: `max(S_D, S_E) + bonus * (n_fired - 1)` (re-uses existing `compose()`).
- New settings, `?debug=1` exposure, unit tests.
- Smoke test against scene 15173 and similar known-failures.

**Out of scope** (deferred):
- Calibration *sweep* (multi-cell parameter exploration). A single before/after pair against the Pexels corpus is in-scope under Phase 4 — that's validation, not calibration.
- Tuning `BRIDGE_LOCAL_FEATURE_MIN_INLIERS` empirically — ship with a defensible default, tune post-data.
- Removing A/B/C from the codebase. They stay on the no-embedding legacy path.
- Storage / latency optimizations beyond the obvious. If precision lifts measurably, optimize. If not, refactor or drop.

## Library

**kornia** (`kornia.feature.SuperPoint`, `kornia.feature.LightGlue`). Same VRAM as cvg/LightGlue (~50 MB combined, dwarfed by DINOv2-L's ~700 MB + activations). One pip dep, RANSAC utilities included. cvg/LightGlue would be the upstream alternative — pick later if kornia's release cadence becomes a problem.

## Architecture

### Featurize-time

For each image (extractor record images, Stash sprite frames, Stash cover) when `BRIDGE_LOCAL_FEATURES_ENABLED=true`:

1. Decode bytes → grayscale tensor (HW1).
2. SuperPoint forward → `(N_kp, 2)` keypoints (xy float) + `(N_kp, 256)` descriptors.
3. Truncate to `BRIDGE_LOCAL_FEATURE_MAX_KEYPOINTS` by score, descending.
4. Serialize: `keypoints fp16 + scores fp16 + descriptors fp16` → single blob, length-prefixed.
5. Store in `image_features`:
   - `source` = existing taxonomy (`extractor_image`, `stash_cover`, `stash_sprite`)
   - `channel = 'keypoints'`
   - `algorithm = 'superpoint:N={max_kp}:fp16'`

Cascade invalidation, animated `#frame_N` expansion, `_expanded_extractor_refs` — all unchanged. Channel E inherits the existing per-channel storage contract.

### Match-time (K > 0 only)

```
on /match/* request with embedding ∈ channels and K > 0:
    1. D-batch over all candidates (existing)
    2. select top-K by S_D
    3. for each top-K candidate:
         3a. load extractor (kpts, descs) for each record image
         3b. load Stash (kpts, descs) for cover + sprite frames per image_mode
         3c. for each (record_image, stash_frame) pair:
                LightGlue → matches
                cv2.findHomography(matches, USAC_MAGSAC, ransac_thresh)
                inliers = mask.sum()
         3d. S_E = aggregate(inliers across all pairs)   # see §13.E.2 below
    4. composite = compose({D: S_D, E: S_E}, min_contribution, bonus_per_extra)
```

Channel E aggregation candidate (Phase 2 decision — start with §a, fall back to §b if needed):

a. `S_E = clip(max_pair_inliers / target_inliers, 0, 1)` with `target_inliers = 50`. Simple, monotonic in match strength, single tunable.
b. `S_E = sigmoid((max_pair_inliers - threshold) / scale)`. Smoother, two tunables. Use only if (a) saturates badly.

E "fires" when `S_E >= min_contribution` (existing 0.05). Below that, E contributes nothing — `compose()` already handles this via the firing predicate.

### Cost analysis (Phase 0 budget)

| Layer | Budget |
|---|---|
| Featurize per image (SuperPoint fwd) | ~30–80 ms GPU |
| Storage per image (512 kp × 256 desc fp16) | ~260 KB |
| Total storage on a 3000-image corpus | ~800 MB |
| Featurize wall-clock per ~500-record job | +25–50 s |
| Match-time per (record_image, stash_frame) pair (LightGlue + RANSAC) | ~30–50 ms |
| Worst-case match-time at K=10, N=5 record images, M=8 sprite frames | K × N × M × 40 ms ≈ 16 s |

**16 s/request is the honest worst-case.** If smoke testing confirms the precision lift, Phase 5 optimizations are ready to pull (sprite-frame pruning to top-1-per-record-image via D's `per_image_max` collapses 16s → ~2s).

## Settings

New (in `bridge/app/settings.py` under the operational/calibrated split):

| Setting | Default | Group | Notes |
|---|---|---|---|
| `BRIDGE_LOCAL_FEATURES_ENABLED` | `false` | operational | Master toggle. Off by default. |
| `BRIDGE_LOCAL_FEATURE_MODEL` | `superpoint` | operational | Future: aliked, disk. |
| `BRIDGE_LOCAL_FEATURE_DEVICE` | `auto` | operational | Mirrors `BRIDGE_EMBEDDING_DEVICE`. |
| `BRIDGE_LOCAL_FEATURE_MAX_KEYPOINTS` | `512` | calibrated | Storage/quality knob. SuperPoint paper default. |
| `BRIDGE_LOCAL_FEATURE_MIN_INLIERS` | `15` | calibrated | "Fires" threshold. Below this, S_E ≈ 0. |
| `BRIDGE_LOCAL_FEATURE_RANSAC_THRESH` | `5.0` | calibrated | Pixels, RANSAC reprojection threshold. |
| `BRIDGE_LOCAL_FEATURE_TARGET_INLIERS` | `50` | calibrated | S_E saturation point. |

Re-uses `BRIDGE_EMBEDDING_PREFILTER_K` for top-K selection — same K controls both D's prefilter and E's verification scope.

## Implementation phases

### Phase 1 — Scaffolding (no scoring) — ~½ day

- [ ] Create `bridge/app/matching/local_features.py` mirroring `embedding.py`'s shape: lazy `_load_models()`, `_resolve_device()`, `algorithm_key()`, `encode_bytes_async(data)` returning `(kpts, scores, descs)` numpy arrays.
- [ ] Serialization helpers: `lf_to_blob(kpts, scores, descs) -> bytes`, `blob_to_lf(blob) -> (kpts, scores, descs)`.
- [ ] Match helper: `match_pair(query_lf, ref_lf) -> (matches, n_inliers)` running LightGlue + cv2 RANSAC. Run via `asyncio.to_thread` (CLAUDE.md §14.4).
- [ ] Add settings to `settings.py` with provenance comments pointing here.
- [ ] Wire `featurization.py` to call `local_features.encode_bytes_async` when enabled, alongside the existing embedding path. Use the same `prefetched_bytes` pattern so animated assets don't re-fetch.
- [ ] Verify startup recovery on a fresh DATA_DIR.

### Phase 2 — Match-time scoring — ~1 day

- [ ] Add `score_image_channel_e` to `image_match.py` mirroring `score_image_channel_d`'s output shape. Returns `{S, n_inliers_per_pair, n_extractor_images, n_stash_hashes, fired_pairs, scoring: "local_features"}`.
- [ ] Wire `score_image_composite` to call E in the K>0 path when `BRIDGE_LOCAL_FEATURES_ENABLED` and `BRIDGE_EMBEDDING_PREFILTER_K > 0`. E replaces A/B/C in the K>0 rerank.
- [ ] `search.py` and `scrape.py`: pass E result through to the composite. K=0 path unchanged.
- [ ] `?debug=1` envelope: `_debug.image.channels.local_features.{S, n_inliers_per_pair, ...}`.
- [ ] Logging line: `scoring=multi+prefilter+E` when E engages.

### Phase 3 — Tests — ~½ day

- [ ] `tests/unit/test_local_features.py`:
  - SuperPoint encode round-trip on synthetic image (deterministic on same input).
  - Blob serialization round-trip preserves keypoints + descriptors at fp16.
  - `match_pair` on identical image returns N >> threshold inliers.
  - `match_pair` on unrelated images returns ~0 inliers.
  - `match_pair` on rotated/cropped/resized variant returns survivable inliers (the actual point of the channel).
  - GPU-skipped encode test class à la `test_embedding.py::TestEncodeOnGPU`.
- [ ] `tests/unit/test_image_match_channel_e.py`:
  - `score_image_channel_e` on a record with no kp rows → S=0.
  - Composite math: `max(S_D, S_E) + bonus * (n_fired - 1)` exercised at all firing combinations.
  - Existing `test_image_match_prefilter.py` still passes.
- [ ] `tests/unit/test_lifecycle.py`: featurize task on a job with `BRIDGE_LOCAL_FEATURES_ENABLED=true` writes keypoints rows alongside embedding rows.
- [ ] Existing unit suite passes with default `BRIDGE_LOCAL_FEATURES_ENABLED=false`.

### Phase 4 — Validation against RLS + Pexels regression — ~1 day

Two corpora, two roles. The improvement gate is RLS (studio 426); Pexels (studio 1090) is a regression check only.

**Why two corpora**: per CLAUDE.md §13.10, D saturates near-1.0 on Pexels-shape content — there's no headroom for E to demonstrate lift, and a "no improvement" result on Pexels is uninformative. RLS is the population where D's rank-1 sits below threshold (e.g., scene 15173 at 0.2998). E's whole reason for existing is whether it lifts those.

Stash studio queries:

```graphql
# RLS — improvement corpus
query { findScenes(filter: { per_page: -1 },
    scene_filter: { studios: { modifier: EQUALS, value: "426" } }) { scenes { id } } }

# Pexels — regression corpus
query { findScenes(filter: { per_page: -1 },
    scene_filter: { studios: { modifier: EQUALS, value: "1090" } }) { scenes { id } } }
```

#### Phase 4a — RLS improvement test (the gate)

RLS has no auto-generated `ground_truth.json` (it's real production data, not synthetic). Two paths to a test fixture:

- [ ] **Build a small hand-labeled fixture**: human curates 20–30 RLS scenes via the viewer Query tool, records the expected extractor record id per scene in a `tests/calibration/rls_fixture.jsonl` file. Format mirrors `ground_truth.json[*]`: `{stash_scene_id, expected_record_id}` per row.
- [ ] **Extend `test_calibration.py`** with a `--fixture <path>` flag that loads a JSONL alternative to `ground_truth.json`. No other changes; the per-scene query loop and metrics computation are agnostic to the fixture source.
- [ ] **Run A1 — RLS baseline**: `BRIDGE_LOCAL_FEATURES_ENABLED=false`, current production state. `--image-channels embedding,phash,color_hist,tone --fixture tests/calibration/rls_fixture.jsonl --label e_rls_baseline`.
- [ ] **Run B1 — RLS E-enabled**: `BRIDGE_LOCAL_FEATURES_ENABLED=true`, restart bridge. Same fixture. `--image-channels embedding,local_features --label e_rls_on`.
- [ ] Compare:
  - Δ precision@1
  - Per-scene composite delta on cases where Run A1's rank-1-of-correct sat in [0.40, 0.69] (the threshold-borderline band E targets)
  - Rank-stability on the [0.70, 1.0] band (E shouldn't reorder cases D already gets right)
- [ ] Append a "Run 8 — Channel E validation (RLS)" section to `docs/calibration/CALIBRATION_RESULTS.md`. The corpus shape and harness extension is per-protocol; the addition is the fixture, not the methodology.

#### Phase 4b — Pexels regression check

- [ ] **Run A2 — Pexels baseline**: same params as the canonical Run 7. 30-scene seed=0 sample. `--image-channels embedding,phash,color_hist,tone --label e_pexels_baseline` against `ground_truth.json`.
- [ ] **Run B2 — Pexels E-enabled**: same sample, `--image-channels embedding,local_features --label e_pexels_on`.
- [ ] Confirm Run B2's precision@1 is within ±2% of Run A2 (no regression). If E hurts Pexels precision, the inlier-aggregation formula in §13.E.2 needs revisiting — E shouldn't fire on D's clean wins.

#### Latency

- [ ] Log end-to-end `/match/fragment` latency for all four runs. Confirm under Stash 90 s timeout per request.

**Decision criteria** (binary, drives whether E ships or refactors):
- **Ship** if (1) Run B1 precision@1 ≥ Run A1 precision@1, (2) ≥half the cases in Run A1's [0.40, 0.69] correct-rank-1 band show composite lift ≥ +0.1 in Run B1, (3) Run B2 precision@1 within ±2% of Run A2.
- **Refactor / drop** if any of the three fails. RLS regression means E's signal isn't the right shape for this content; Pexels regression means E is competing with D's correct rankings on easy cases.

## Acceptance criteria (minimum-shippable)

- All existing unit tests pass.
- D-only path (K=0) byte-identical when `BRIDGE_LOCAL_FEATURES_ENABLED=false`.
- New unit tests pass on synthetic image pairs (related → high inliers, unrelated → low).
- Phase 4 Run B (E enabled) at parity-or-better on precision@1 vs Run A (E disabled) on the 30-scene seed=0 Pexels sample, **and** measurable composite lift in the [0.40, 0.69] rank-1 band. Specific ship/drop bar in Phase 4.

## Risks / open questions

1. **LightGlue model download on first encode.** Caches into the `huggingface_cache` volume; first cold start adds ~10 s to featurization. No different from DINOv2's pattern; mention in CLAUDE.md §13.E when written.
2. **RANSAC determinism.** cv2's RANSAC is stochastic. Use `cv2.USAC_MAGSAC` with a fixed seed in `match_pair`, or kornia's RANSAC with explicit seed. Tests must not flake.
3. **Animated extractor assets.** `_expanded_extractor_refs` already gives synthetic `#frame_N` refs to the score function; E reads cached keypoints under those refs same as D reads embeddings. No new logic needed.
4. **What if E doesn't fire on the failure cases?** The "homography exists between same scene different angle" thesis is empirical. If smoke testing shows E doesn't fire on the rank-1-below-threshold cases, the conclusion is that the failure mode isn't actually angle-shift but something subtler (resolution gap, content gap). Plan deletes; we go a different direction.
5. **Latency budget.** If 16 s/request is too slow even at K=10, the sprite-pruning optimization (only run E on the (record_image, sprite_frame) pair D's `per_image_max` already identified as best) is ready and brings cost to ~K × N × 40 ms ≈ 2 s. Tracked as a Phase 5 follow-up, not a Phase 1–4 item.
6. **Storage on large corpora.** ~260 KB/image at 512 max_kp. A 50K-image corpus is ~13 GB. Budget-tracking and an LRU layer for keypoints rows are out of scope here; mention in §15 risks if shipping for large deployments.

## Documentation deltas (when folding back into CLAUDE.md)

- **§13** new subsection §13.E: Channel E (local features). Math + don'ts.
- **§13.10** invariant updated: K>0 path uses E (not A/B/C) when local features enabled.
- **§14** featurization: keypoints rows joined to the eager-startup recovery scan.
- **§15.2** `image_features.source` taxonomy unchanged; new `channel='keypoints'` enumerated.
- **§16** symptom map: "E never fires on a known same-scene-different-angle pair" → debug envelope `n_inliers_per_pair` inspection.
- `docs/HOW_TO_USE.md` §9.2: settings table appended.
- `docs/TESTING.md`: Phase 3 test files added to the layout map.
