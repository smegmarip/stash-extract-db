# Semantic image-similarity migration plan

> **Status**: proposal. Review and sign off before implementation.
> **Branch**: `semantic`.
> **Motivation**: pHash + color_hist + tone form a hand-engineered low-level similarity stack whose calibrated parameters are corpus-specific. On corpora that differ from the calibration set (e.g. low-resolution video, different studio styles), match quality degrades sharply. A learned visual-embedding model (DINOv2) produces semantically meaningful similarities in a universal space, eliminating the per-corpus calibration treadmill and significantly cutting per-scrape latency.

---

## 1. Goals

- **Tolerant relative matching**: top-1 ranking quality should not depend on corpus calibration.
- **Drop per-scrape latency below the Stash 90s hard timeout** decisively (target: < 1s per scrape, warm cache).
- **Preserve the existing service contract**: `/match/*` endpoints, scrape/search semantics, featurization lifecycle, 503/Retry-After protocol, cache invalidation cascade.
- **Keep the metadata-transport scraper unchanged**.
- **Provide a clean rollback path** during the transition.

## 2. Non-goals

- We are not changing the scraper, the Stash GraphQL contract, or the extractor API contract.
- We are not removing the existing scoring channels in the same change. They remain alongside the new path behind a feature flag for A/B comparison.
- We are not building a new model. Off-the-shelf DINOv2 weights are pulled from HuggingFace.
- We are not adding text-conditioning or multi-modal queries. Image-only.

## 3. Architecture summary

The bridge gains a fourth scoring channel — **D (embedding)** — alongside the existing A/B/C channels. Each per-image record stores a pre-computed embedding vector. At match time, similarity becomes a single matrix multiplication of Stash-side embeddings against the cached extractor-side embeddings. The existing per-channel composition (`max(fired) + bonus * (n_fired - 1)`) carries over; embedding becomes one more channel that can fire.

After validation, channels A/B/C can be deprecated and embedding becomes the sole channel. That's a separate, smaller follow-up change.

```
                    ┌──────────────────────────────────┐
                    │  bridge/app/matching/embedding.py│  ← NEW
                    │  - load_model() (lazy, GPU/CPU)  │
                    │  - encode(images) → ndarray      │
                    │  - cosine(a, b) → float          │
                    │  - score_image_embedding()       │
                    └──────────────────────────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────────────┐
                    │  featurization.py                │  ← MODIFIED
                    │  - Phase 1 also computes channel │
                    │    D embedding per record image  │
                    └──────────────────────────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────────────┐
                    │  scrape.py / search.py           │  ← MODIFIED
                    │  - score_image_composite gains   │
                    │    optional channel D            │
                    │  - score_image_embedding called  │
                    │    in batch matmul               │
                    └──────────────────────────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────────────┐
                    │  cache/db.py                     │  ← UNCHANGED schema
                    │  image_features table holds      │
                    │  channel='embedding' rows        │
                    └──────────────────────────────────┘
```

## 4. Model choice

**DINOv2 ViT-L/14** (recommended; configurable via env).

| Property | Value |
|---|---|
| Provider | Meta AI, via HuggingFace `facebook/dinov2-large` |
| Parameters | 304M |
| Embedding dim | 1024 |
| Input image size | 224×224 (model handles resize internally) |
| VRAM at FP16 | ~1.2 GB |
| Per-image inference (A4000, FP16, batch=8) | ~10 ms |
| License | Apache 2.0 |

Why DINOv2 over CLIP:
- DINOv2 is self-supervised on image-image discrimination; CLIP shares its embedding space with text and is slightly weaker for pure image-image similarity.
- The bridge has no text input; we'd be wasting CLIP's cross-modal training.
- DINOv2 ViT-L/14 outperforms CLIP ViT-L/14 on standard image retrieval benchmarks.

Configurability: `BRIDGE_EMBEDDING_MODEL` env var. Defaults to `facebook/dinov2-large`. Other tested-OK options: `facebook/dinov2-base` (smaller, faster, comfortable on 8GB+ GPUs), `facebook/dinov2-giant` (largest, ~10GB VRAM, marginal accuracy gain).

## 5. Settings (all new env vars)

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_EMBEDDING_ENABLED` | `false` | Master toggle. When false, channel D is dormant — featurization skips embedding compute, scoring ignores it. |
| `BRIDGE_EMBEDDING_MODEL` | `facebook/dinov2-large` | HuggingFace model ID. Determines algorithm string in `image_features` rows. |
| `BRIDGE_EMBEDDING_DEVICE` | `auto` | `auto` / `cuda` / `cpu`. `auto` picks cuda if available, else cpu. |
| `BRIDGE_EMBEDDING_BATCH_SIZE` | `16` | Forward-pass batch size for featurization. Per-request scoring uses Stash-side batch (cover + sprite frames, ~9). |
| `BRIDGE_EMBEDDING_DTYPE` | `fp16` | `fp16` / `fp32`. fp16 halves VRAM and is faster on Ampere+ GPUs (A4000). |
| `BRIDGE_EMBEDDING_THRESHOLD` | `0.7` | Composite gate for scrape mode. Cosine similarity scale. Cross-corpus stable. |

The existing `BRIDGE_IMAGE_*` settings remain unchanged. They control channels A/B/C only.

## 6. Cache schema

**No new tables.** The existing `image_features` table fits cleanly:

```
source           ref_id              channel       algorithm                      feature_blob   quality
───────────────  ──────────────────  ────────────  ─────────────────────────────  ─────────────  ───────
extractor_image  <job>:<ref>         embedding     dinov2-large:fp16:1024         <2KB>          (q_i)
stash_cover      <scene>             embedding     dinov2-large:fp16:1024         <2KB>          (q_i)
stash_sprite     <scene>:<idx>       embedding     dinov2-large:fp16:1024         <2KB>          (q_i)
```

- `feature_blob` stores the embedding as 1024 × 2 bytes (FP16) = 2 KB.
- `quality` is the L2 norm of the embedding (used for normalization at scoring time, not for q_i weighting).
- `algorithm` versions the model + dtype; a model swap creates rows under a different algorithm string and falls through to recompute. No schema migration needed.

Storage estimate: 1500 extractor + 100 Stash scenes × 9 frames = 2400 embeddings × 2 KB = 5 MB. Same magnitude as current pHash storage.

Cascade invalidation, LRU eviction, and dual-write retirement (Phase 7) all carry over without change.

## 7. Featurization lifecycle

The eager-startup + per-job cascade lifecycle from CLAUDE.md §14 is unchanged. The only modification is to the per-job featurization task body in `featurization.py::featurize_job`:

When `BRIDGE_EMBEDDING_ENABLED=true`:
- Phase 1 (per-image features) gains a channel-D pass:
  - Collect all unfeaturized image references for the job.
  - In batches of `BRIDGE_EMBEDDING_BATCH_SIZE`, fetch + decode + encode → store one row per image with `channel='embedding'`.
  - Negative-cache sentinels on fetch/decode failure (existing pattern).
- Phase 2 (corpus_stats baseline): **skip for embedding channel**. Cosine similarity is corpus-independent.
- Phase 3 (uniqueness c_i): **skip for embedding channel**. Embeddings encode semantic content directly; the c_i mechanism was designed to compensate for pHash's weakness against generic content. Not needed here.

Featurization time on A4000 GPU: ~15s for 1500 images (one-time per job, eager at startup). Same 503/Retry-After contract during the window.

## 8. Match-time scoring

New function `score_image_embedding(scene, job_id, record, ...)`:

```
1. Stash-side embeddings:
     - Fetch + encode the scene's cover + N sprite frames (cached after first call).
     - Stack into matrix S ∈ R^(M × 1024).

2. Extractor-side embeddings:
     - Look up cached embeddings for the record's images from image_features.
     - Stack into matrix E ∈ R^(N × 1024).

3. If M = 0 or N = 0: return S=0 (same convention as channels A/C).

4. Pairwise cosine similarity:
     - S_norm = S / |S|     (L2 normalize per row, using stored quality field)
     - E_norm = E / |E|
     - sims = S_norm @ E_norm.T                     (M × N matrix)

5. Per-extractor-image best similarity (collapse M):
     - per_image_max = sims.max(axis=0)             (length N)

6. Aggregate (no sharpening, no count_conf, no dist_q — these were
   pHash-specific compensations):
     - S_embedding = per_image_max.mean()           (or top-K mean for robustness)

7. Return: { S, per_image_max, n_extractor_images: N, n_stash_hashes: M, ... }
```

Cross-channel composition (`max(fired) + bonus`) carries over. `BRIDGE_IMAGE_CHANNELS` env var grows to accept `embedding` as a valid channel name. When `embedding` is in the channel list, channel D fires alongside A/B/C; the composite takes the max.

Performance: per-scrape critical path is one matrix multiply on GPU. For mode=both with 1500 candidates, ~5 ms of GPU compute. With cache hits on the Stash side (same scene scraped twice), even faster.

## 9. GPU integration

Pattern follows `stash-auto-vision/docker-compose.yml` (runtime-based, proven on the same target host).

Bridge container needs:

1. **Base image change**: from `python:3.11-slim` to `pytorch/pytorch:2.x-cuda12.x-cudnn-runtime` (or equivalent). Adds ~3 GB image size; one-time cost.
2. **NVIDIA Container Toolkit on host** (one-time setup; already present on `192.168.50.93` per the working `stash-auto-vision` deployment).
3. **`docker-compose.yml` changes** (modeled on the working pattern):
   ```yaml
   services:
     stash-extract-db:
       build: .
       container_name: stash-extract-db
       runtime: ${DOCKER_RUNTIME:-nvidia}              # ← GPU access via runtime
       restart: unless-stopped
       ports:
         - "${BRIDGE_PORT:-13000}:13000"
       environment:
         # ... existing env ...
         BRIDGE_EMBEDDING_ENABLED: "${BRIDGE_EMBEDDING_ENABLED:-false}"
         BRIDGE_EMBEDDING_MODEL: "${BRIDGE_EMBEDDING_MODEL:-facebook/dinov2-large}"
         BRIDGE_EMBEDDING_DEVICE: "${BRIDGE_EMBEDDING_DEVICE:-auto}"
         BRIDGE_EMBEDDING_DTYPE: "${BRIDGE_EMBEDDING_DTYPE:-fp16}"
         BRIDGE_EMBEDDING_BATCH_SIZE: "${BRIDGE_EMBEDDING_BATCH_SIZE:-16}"
         BRIDGE_EMBEDDING_THRESHOLD: "${BRIDGE_EMBEDDING_THRESHOLD:-0.7}"
       volumes:
         - "${DATA_PATH:-./data}:/data"
         - torch_cache:/root/.cache/torch                # ← persist torch hub
         - huggingface_cache:/root/.cache/huggingface    # ← persist HF model files
       healthcheck:
         test: ["CMD", "curl", "-fs", "http://localhost:13000/health"]
         interval: 30s
         timeout: 10s
         retries: 3
         start_period: 120s                               # ← model load takes time
       # ... rest unchanged ...

   volumes:
     # ... existing volumes ...
     torch_cache:
     huggingface_cache:
   ```
   Key choices justified:
   - **`runtime: ${DOCKER_RUNTIME:-nvidia}`** — defaults to nvidia for prod, override `DOCKER_RUNTIME=runc` on dev hosts without a GPU. Same pattern your `stash-auto-vision` stack uses.
   - **`torch_cache` + `huggingface_cache` volumes** — model weights persist across `docker compose up -d --force-recreate`. Without these, every container recreate re-downloads ~1.2 GB. Same pattern as `stash-auto-vision`'s `frame-server` and `semantics-service`.
   - **`start_period: 120s`** — generous boot window for model load. Bridge accepts requests once `/health` returns 200, but featurization tasks may queue behind model warm-up.

4. **CPU fallback**: if `cuda.is_available()` returns false (e.g., `DOCKER_RUNTIME=runc` for dev), the model loads on CPU. Same code path; ~10× slower per-image. Featurization then takes minutes instead of seconds; per-scrape ~1–3s instead of 0.3s. Still under the 90s budget.

The CPU fallback is necessary because dev machines (including the local one for this work) often lack a CUDA GPU, and the unit-test suite needs to run somewhere. Tests pin device to CPU explicitly via `BRIDGE_EMBEDDING_DEVICE=cpu`.

## 10. Phasing

**Phase 1 — Coexistence (this PR)**:
- Add `embedding.py` module with model load + encode + scoring.
- Add embedding compute to featurization (gated by `BRIDGE_EMBEDDING_ENABLED`).
- Wire as channel D in `score_image_composite`.
- `?debug=1` shows per-channel breakdown including D.
- Both old and new scoring active simultaneously. User opts in by setting `IMAGE_CHANNELS=phash,color_hist,tone,embedding` (or any subset).
- Calibration harness gains an embedding-channel sweep cell.

**Phase 2 — Validate (separate session, post-merge)**:
- Run existing calibration harness against the Pexels corpus with `IMAGE_CHANNELS=embedding` only, capturing top-1 ranking quality.
- Run smoke test against user's actual production corpus (low-quality 240p) with `IMAGE_CHANNELS=embedding` only.
- Compare to current 3-channel results.
- Decide threshold default empirically.

**Phase 3 — Cutover (separate PR)**:
- If validation confirms embedding alone outperforms the 3-channel composite across both corpora:
  - Default `BRIDGE_IMAGE_CHANNELS=embedding`.
  - Mark channels A/B/C as legacy. Code stays for rollback.
- If validation shows the composite of A/B/C+D outperforms either alone:
  - Default `BRIDGE_IMAGE_CHANNELS=phash,color_hist,tone,embedding`.
- Either way, the old channels remain available as opt-in for rollback or comparison.

## 11. Files touched

| File | Change |
|---|---|
| `bridge/app/matching/embedding.py` | **NEW**. Module with `get_model()`, `encode_batch()`, `cosine()`, `score_image_embedding()`. |
| `bridge/app/matching/featurization.py` | Add channel D pass when `BRIDGE_EMBEDDING_ENABLED`. |
| `bridge/app/matching/image_match.py` | Add `extractor_image_embedding()`, `stash_cover_embedding()`, `stash_sprite_embeddings()` parallel to existing per-channel functions. |
| `bridge/app/matching/scrape.py` | Pass embedding option through to scoring. |
| `bridge/app/matching/search.py` | Same. |
| `bridge/app/matching/scoring.py` | Add `score_aggregate_channel`-style helper for embedding (cosine-mean rather than sharpened evidence-union). |
| `bridge/app/api/match.py` | `_resolve_match_params` includes embedding settings. Debug output extends. |
| `bridge/app/settings.py` | Add `BRIDGE_EMBEDDING_*` settings. |
| `bridge/app/cache/db.py` | No schema change. New `_EMBEDDING_ALGO` constant for convenience. |
| `pyproject.toml` | Add `torch`, `transformers`, `pillow` (already present), `numpy` (present). |
| `Dockerfile` | Switch base to `pytorch/pytorch:cuda` runtime. |
| `docker-compose.yml` | Add GPU resource reservation. |
| `.env.example` | Add `BRIDGE_EMBEDDING_*` block, default disabled. |
| `tests/unit/test_embedding.py` | **NEW**. Unit tests for model load (skip if no GPU and model too large), encode determinism, cosine math, scoring shape. |
| `tests/unit/test_lifecycle.py` | Add featurization-with-embedding test (CPU mode, small model). |
| `docs/HOW_TO_USE.md` | New section for embedding setup + GPU passthrough. |
| `CLAUDE.md` | New §13.x for embedding channel; threshold notes. |

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| CUDA driver / Docker GPU passthrough fails on remote host | CPU fallback. Bridge runs slowly but functionally. Document driver setup in HOW_TO_USE. |
| Model download fails at startup (no internet, HuggingFace down) | Cache model weights in image at build time (or mounted volume). Fail loudly at startup with actionable error. |
| Embedding cosine similarities cluster too tightly to be discriminating on this corpus (unlikely but possible) | Validation phase (Phase 2) catches it before cutover. The 3-channel path remains as fallback. |
| Bridge startup time grows (model load + warm-up) | Acceptable: featurization-while-503 protocol handles the "not ready yet" window. Lazy model load on first match request also possible if startup latency matters. |
| Image format mismatch (PIL decode → DINOv2 input) | Use `transformers` AutoImageProcessor for the model — handles resize, normalize, tensor conversion uniformly. |
| Test suite needs GPU to run | All tests pin `device=cpu`; use `dinov2-base` or smaller for tests to keep CPU runs fast. CI runs on CPU. Manual GPU tests in `tests/integration/` marked `@pytest.mark.gpu`. |
| Schema migration on existing cache | None needed. New rows under new `algorithm` string; old rows untouched. |

## 13. Out of scope

- Replacing `text` matching (filename/title scoring) — embedding is image-only. Title and filename scoring stays as-is.
- Multi-modal text-prompt matching ("find scenes that look like this description").
- Online fine-tuning / model adaptation per corpus.
- Multi-model ensemble. One model at a time.
- Model auto-download mirror infrastructure (HuggingFace direct is fine for now).

## 14. Testing approach (given local has no GPU)

- **Local development & unit tests**: CPU-only, with `dinov2-small` (22M params) for fast iteration. All non-GPU tests run here.
- **Remote integration testing**: deploy to `192.168.50.93` which has the A4000. Run the existing calibration harness + manual smoke against the user's production corpus.
- **CI (if added later)**: CPU only, `dinov2-small`, tolerated slower run.

## 15. Sign-off checklist

Before implementation begins:

- [ ] User confirms model choice (DINOv2 ViT-L/14, configurable).
- [ ] User confirms phasing (coexist first, cutover later).
- [ ] User confirms Docker base-image switch is acceptable (~3 GB growth).
- [ ] User confirms GPU passthrough setup on remote host.
- [ ] User confirms feature flag `BRIDGE_EMBEDDING_ENABLED=false` default is OK during Phase 1.
- [ ] User has reviewed file list and is OK with breadth of changes.
