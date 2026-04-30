# `stash-extract-db` — Architectural Invariants

This document captures the load-bearing contracts that must hold to keep the system coherent. It is intentionally short. If a change appears to violate any rule below, stop and check with the human before proceeding — you are likely about to introduce a silent corruption.

For _what_ to build, see [`requirements.md`](requirements.md). This file covers _what must always be true_. For run-by-run calibration provenance behind the §13 defaults, see [`docs/calibration/CALIBRATION_RESULTS.md`](docs/calibration/CALIBRATION_RESULTS.md).

---

## 1. Configuration ownership

> **All matching parameters originate in the scraper's `config.py`. The bridge has no fallback.**

Threshold, image mode, search limit, hash algorithm, hash size, sprite sample size — every match-shaping parameter is sent in every request from the scraper. If the bridge receives a request missing a required parameter, it returns `400 Bad Request`. The bridge does **not** ship default values for these.

**Why**: there is exactly one place a user changes behavior — `~/.stash/scrapers/stash-extract-db/config.py`. Bridge env vars are for _infrastructure_ (URLs, auth, data dir, log level) and for the _featurization lifecycle_ (concurrency, eviction budget) — not heuristics. Drift between scraper config and bridge config is a class of bug we refuse to introduce.

**Don't**: add `DEFAULT_THRESHOLD`, `DEFAULT_IMAGE_MODE`, etc. to bridge env. Don't read these from a JSON file on the bridge. The scraper is the single source of truth.

---

## 2. Mode semantics

> **Scrape returns one or none. Search returns ranked. Never blur the line.**

- **Scrape mode** is binary: a definitive signal fires (Studio+Code, Exact Title, or Image≥threshold) → return that record. None fires → return `{}`. Never return a "best-effort" candidate that didn't fire a definitive signal. Stash treats a returned record as a contract; a non-definitive scrape result is a lie.
- **Search mode** is ranked: every candidate gets a composite score, top-N returned. Empty list is allowed when no candidate scored above zero (rare).

**Don't**: in scrape mode, return the highest-scoring candidate when no definitive signal fired. **Don't** in search mode, gate by the image threshold (it's a multiplier-rule, not an inclusion-rule — see §4).

---

## 3. Scrape cascade order is cheap-first by intent

> **Studio+Code → Exact Title → Image. The order is an optimization, not a priority.**

All three signals are equivalently definitive. Reordering does not change _which_ records can match — only _how fast_ a request returns. Image hashing is the expensive operation; defer it.

**If you change the order**, you must ensure: (a) the new order is still cheap-first, and (b) the outcome remains identical (which it will, given binary signals).

---

## 4. Image: threshold-gated in scrape, unconditional in search; distribution-sensitive in both

> **Scrape uses above-threshold soft-OR. Search uses unconditional soft-OR. The threshold applies only to scrape.**

For each candidate, the engine first computes **per-extractor-image similarities** — for each extractor image (`cover_image` + `images[]`), the best similarity against the configured Stash-side hash set (cover, sprite frames, or union per `image_mode`). This produces an array of N sims, one per extractor image. See §13 for the why.

Then aggregation differs by mode:

- **Scrape** — `aggregate_scrape(sims, threshold)`: filter to sims ≥ threshold, then soft-OR. Returns 0 if no sim clears the threshold (the candidate doesn't fire the image tier). Among firing candidates, the aggregate is the rank score; tiebreak by `result_index`.
- **Search** — `aggregate_search(sims)`: unconditional soft-OR over all per-image sims. The threshold is **not** consulted in search.

This replaces the prior `raw_sim if ≥ threshold else 0.5*raw_sim` rule. The new rule is simpler and stronger: in search every match contributes proportional to its strength and the number of matches; in scrape only above-threshold matches count, but multiple of them outrank a single borderline one.

**Don't**: filter search candidates by `image_sim >= threshold`. **Don't**: re-introduce the 0.5-multiplier rule. **Don't**: collapse the per-image sims to a single max before aggregation — the distribution carries the signal (§13).

---

## 5. Studio is the only job-level filter

> **Match by case-insensitive equality of `job.name` and `scene.studio.name`. No fuzzy match. No alias table. No fallback.**

- Scene has studio AND a job's name matches (case-insensitive) → search domain = that **one** job.
- Scene has studio AND no job matches → return empty (`{}` for scrape, `[]` for search).
- Scene has no studio → search domain = **all** scene-shaped jobs ("caveat utilitor").

**Don't** add a fuzzy-match fallback "to be helpful" — it loses determinism. **Don't** silently widen the search domain on no-studio-match — return empty and let the user notice.

---

## 6. Schema-shape detection is by superset, not by template id

> **A job qualifies as "scene-shaped" iff its schema fields are a superset of `{title, url, cover_image, images, performers, date, details, id}`.**

Users can clone, rename, or manually construct schemas. The seeded `"Video Scene"` template has a known id, but a user might clone it and add fields, or build the same shape from scratch. Field-set superset check is the durable contract.

**Don't** check `schema.is_template`. **Don't** check `schema.name == "Video Scene"`. **Don't** hard-code the seeded template's id.

---

## 7. Cache invalidation triggers

> **Each cache layer has exactly one invalidation key. Mixing them is a corruption hazard.**

| Cache                                                | Invalidation key                                                                       |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `extractor_jobs` row                                 | extractor `completed_at` change                                                        |
| `extractor_results` rows                             | cascade from `extractor_jobs` (`ON DELETE CASCADE`)                                    |
| `image_features` (Stash cover) / legacy `image_hashes` | `?t=<epoch>` query parameter on screenshot URL                                       |
| `image_features` (Stash sprite) / legacy `image_hashes` | `oshash` from `files[].fingerprints`                                                |
| `image_features` (extractor)                         | asset `etag` or `content_hash` response header                                         |
| `image_features` (`*_aggregate`)                     | composite of constituent ref strings; rebuilt on cascade                               |
| `corpus_stats`                                       | cascade from `extractor_jobs` (`ON DELETE CASCADE`)                                    |
| `image_uniqueness`                                   | cascade from `extractor_jobs` (`ON DELETE CASCADE`)                                    |
| `job_feature_state`                                  | cascade from `extractor_jobs` (`ON DELETE CASCADE`); transitions per §14               |
| `match_results`                                      | composite of scene fingerprint + job `completed_at`                                    |

**Don't** invalidate caches manually on a hunch. **Don't** add a TTL to any of these — TTLs hide bugs that the fingerprint-based invalidation would catch.

When the extractor job's `completed_at` advances, all of: result rows, extractor-side image features and aggregates for that job, corpus stats, image uniqueness, job feature state, and match_results referencing that job — must be purged together. This is the one cross-table invalidation; treat it as an atomic transaction. After commit, the cascade trigger **enqueues a new featurization task for the job** (§14) — the bridge does not wait for the next request to discover the invalidation; it self-heals.

Stash-side `image_features` rows (`source IN ('stash_cover', 'stash_sprite', 'stash_aggregate')`) are **not** purged by this cascade — they're keyed by Stash content fingerprint, not by job, and remain valid across job changes. Stash-side rows are bounded by LRU eviction (§14).

---

## 8. Output mapping rules

> **`Studio` is echoed back. `Code` is the extractor `id`. `images[]` is matching-only — never returned.**

| Stash output   | Source                                                    | Notes                                         |
| -------------- | --------------------------------------------------------- | --------------------------------------------- |
| `Studio.Name`  | echo of input studio                                      | Stash already has it; we confirm by echoing   |
| `Code`         | extractor `data.id`                                       | omit if extractor id is null                  |
| `Image`        | extractor `data.cover_image`                              | base64 data URI, fetched via `/api/asset/...` |
| `Performers[]` | extractor `data.performers`, alias-resolved against Stash | each entry is `{Name, Aliases?}`              |
| `images[]`     | (input only)                                              | matching signal; never appears in output      |

**Don't** put extractor `id` into Stash `URL`. **Don't** fold `images[]` into `Details` or any other output field. **Don't** override `Studio.Name` with extractor data — the user already chose the studio in Stash.

---

## 9. The bridge never modifies Stash

> **The bridge is a read-only proxy on the Stash side. All writes go through Stash's normal scraper apply path.**

Bridge GraphQL operations: `findScene`, `findPerformers`. Nothing else. No mutations, no scene patching, no tag creation, no studio creation.

**Don't** add a "write back" mode. **Don't** create performers or studios on the fly. If a user wants to customize the apply step, they do it in Stash's scrape UI.

---

## 10. Tiebreaks are deterministic

> **Equal scores → lowest `result_index` from `/api/extraction/{job_id}/results?sort_dir=asc` ascending.**

This applies in both scrape (cascade tier ties) and search (composite score ties). The bridge never randomizes, never uses creation order from a different sort, never uses the extractor record's `id` (often null).

**Don't** introduce alternate tiebreak rules per request type. One rule, applied everywhere.

---

## 11. Empty-and-null is not penalty

> **Missing data on either side neutralizes the relevant signal — never penalizes.**

When `scene.title` is empty: title signal does not fire (and does not contribute to search score). It does _not_ subtract from the score. Same for `scene.code`, `scene.date`, `scene.performers`, and any extractor-side null.

**Why**: penalizing absent data biases against scenes with thin metadata — exactly the population this bridge exists to help.

**Don't** add negative score components. **Don't** penalize a candidate for lacking a field the scene also lacks.

---

## 12. Filename score is `max(channels) + structured_bonus`, never `mean`

> **Filename comparison is multi-channel: a clean naive match must never be dragged down by a poor guessit parse, and vice versa.**

Channels:

1. **Naive normalize → RapidFuzz `WRatio`** — robust on short, clean filenames.
2. **Guessit-parsed title → RapidFuzz `token_set_ratio`** — strips release/resolution/codec/group noise.
3. **Structured field exact matches** (`year`, `season`, `episode`, `screen_size`) — small additive bonuses when both sides parsed a non-null value AND the values match.

Composition: `min(1.0, max(naive, guessit_title) + structured_bonus)`.

**Why `max` and not `mean`**: when one parser's strong path applies, the other's weak path is a _failure of analysis_, not a contradiction. Mean would punish the file for being analyzable in only one way. Max preserves the strongest available signal.

**Why structured bonus is _additive_**: agreement on year/episode is independent corroborating evidence, not redundant with text similarity. A file that scores 0.85 on text _and_ matches year-episode should score higher than one that just scores 0.85 on text.

**Don't**: switch to mean to "smooth out" outliers — short clip filenames will silently regress. **Don't**: turn structured bonuses into multiplicative weights — they degrade to 0 when one side is null. **Don't**: add a new channel without ensuring it can fail to 0 cleanly — channels are union-of-evidence, not intersection.

The full breakdown is observable via `?debug=1` on `/match/*` endpoints (search mode only — scrape returns single result or empty).

---

## 13. Image scoring is multi-channel; channels compose by `max + bonus`, never by averaging or product

> **Three channels evaluate the (scene, record) pair independently. Each produces a score in [0, 1]. The composite is `max(fired_channels) + bonus_per_extra_firing_channel`, capped at 1.0. The threshold in `config.py` gates the composite, not individual channels and not individual pair sims.**

Single-channel pHash matching has a structural failure mode: it works for visually monolithic content (one stable subject) and degrades to noise the moment a video has scene changes, lighting variation, or varied subjects — exactly the population the bridge exists to serve. No aggregation algebra over a single channel rescues this. The fix is structural: provide complementary signals (chromatic, tonal) that survive what pHash misses, and compose them as union-of-evidence, mirroring §12's filename pattern.

### 13.1 The three channels

| Channel                                   | What it catches                                    | What it misses                                                   |
| ----------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------- |
| **A — pHash per-frame**                   | Direct structural match (DCT-based).               | Scene variation, lighting, varied subjects.                      |
| **B — color histogram, scene-aggregate**  | Whole-scene chromatic profile, robust across cuts. | Records sharing generic palettes (sepia, low-light, monochrome). |
| **C — low-res tone (8×8 gray) per-frame** | Coarse composition/luminance.                      | High-frequency texture.                                          |

Channel B's "aggregate" is per-scene (Stash side) and per-record (extractor side) — for each HSV bin index, take the per-bin **median** value across all usable frames in scope (Stash: sprite frames + cover; extractor: cover_image + images[] for the single record). Re-normalized to sum to 1, stored as a single aggregate row in `image_features` (`source='stash_aggregate'` keyed by scene oshash; `source='extractor_aggregate'` keyed by `<job_id>:<record_idx>`). Per-bin (not per-frame) median is robust to outlier frames (black, fade transitions, blank placeholders) without depending on the binary filters being perfect. One number out per (scene, record) pair, no `M × N` grid.

### 13.2 Within-channel scoring (per channel, applied independently)

For frame-level channels (A, C): per record image `i`, take `m_i = max_j sim(stash_j, ext_i)`. For aggregate channel (B): one similarity `s_B` between Stash-side and extractor-side aggregate features. Then in both:

1. **Sharpen**: `m_i' = max(0, (m_i - baseline) / (1 - baseline))^γ` with γ from request config (default **3.5**, calibrated). Subtracts the per-channel noise floor; gamma suppresses fuzzy near-baseline matches.
2. **Weight by quality + uniqueness**: `w_i = q_i * c_i`. `q_i` is intrinsic, channel-specific (§13.4). `c_i` is corpus-relative (§13.5). Stash-side `c_i = 1` (neutral — no Stash corpus participates in IDF).
3. **Evidence-union** (frame-level channels): `E = 1 - Π(1 - w_i * m_i')`. Soft-OR is now safe because `m_i'` is post-sharpening and `w_i` suppresses low-quality / non-unique images — the single-outlier saturation that broke the prior top-K-mean model is gated away by the weights.
4. **Count saturation**: `count_conf = 1 - exp(-Σw_i / k)` with `k` from request config (default **0.25**, calibrated). A record with N=1 image earns less than N=10 with the same evidence — sparse evidence is less reliable, but a prior k=2.0 over-weighted that and was decisively beaten in calibration.
5. **Distribution shape**: `dist_q = 0.5 + 0.5 * normalized_entropy(m_i')` — `dist_q ∈ [0.5, 1]`. Single contribution → conservative `dist_q = 0.5`. Broad coverage outranks single spikes.
6. **Channel score**: `S_channel = E * count_conf * dist_q`. For aggregate channel B (no distribution): `S_B = m_B' * q_B`.

### 13.3 Cross-channel composition

```
fired = [S for S in (S_A, S_B, S_C) if S >= min_contribution]   # default 0.05
composite = min(1.0, max(fired) + bonus_per_extra * (len(fired) - 1))   # bonus default 0.1
```

This is the §12 filename pattern: union-of-evidence with a structured bonus for corroboration. Each channel weak alone, union strong.

### 13.4 Per-channel `q_i` formulas

`q_i ∈ [0, 1]` is intrinsic to the image (no corpus context) but channel-specific — what makes an image "informative" depends on what the channel measures. Each is geometric-mean composed so a weak axis pulls `q_i` down; a uniform-color image fails on multiple axes and `q_i` returns ~0.

| Channel       | `q_i` formula                        | Components                                                                                                                                                                                                         |
| ------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A (pHash)     | `sqrt(entropy_norm * variance_norm)` | `entropy_norm` = Shannon entropy of grayscale histogram (8-bit, 256 bins) ÷ 8 (bits, max). `variance_norm` = `min(1.0, var(grayscale_pixels) / 100²)` — real-image stdev rarely exceeds 100; clamp to [0, 1].      |
| B (color hist) | `1 - gini(hist_bins)`                | Gini coefficient of the normalized HSV histogram bins. A monochromatic image has Gini → 1 (mass in few bins) → low quality. A varied palette has Gini → 0 → high quality. Cheap to compute alongside the histogram. |
| C (tone)      | `sqrt(entropy_norm * variance_norm)` | Same formula as channel A (also grayscale-derived).                                                                                                                                                                |

The §13.7 binary filters (degenerate-hash, low-pixel-variance) still gate at the extremes; `q_i` provides graded weighting in the middle of the range.

### 13.5 Per-channel similarity formulas

Each channel's `sim()` function maps two feature blobs to a similarity in `[0, 1]`:

| Channel        | `sim(a, b)` formula                                                                                                                                                  |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A (pHash)      | `1 - hamming(a, b) / hash_size²`                                                                                                                                     |
| B (color hist) | `Σ_i min(a[i], b[i])` — histogram intersection. For normalized hists (`Σa = Σb = 1`), naturally bounded `[0, 1]`. 1.0 iff identical distributions; 0 iff disjoint.   |
| C (tone)       | `1 - mean(|a[i] - b[i]| / 255)` — mean L1 distance over 64 luminance values, normalized to `[0, 1]`.                                                                 |

All three return values directly comparable to the channel-specific `baseline` from `corpus_stats`, so the sharpening formula `(m_i - baseline) / (1 - baseline)` is well-defined and bounded for every channel.

### 13.6 Empirical baseline + uniqueness (the corpus-relative half)

**Baseline** (per channel, per job): the empirical noise floor — mean similarity over `min(1000, all_pairs)` random non-matching record-image pairs from within the job's record set (random pairs of images from _different_ records — same-record pairs are excluded as potentially related). Stored in `corpus_stats`. Sample-from-records-only (not records vs. Stash): the baseline is a property of the metric+content distribution, and the record images are the relevant population. Stash-side mixing would couple baselines to per-user library composition.

**Uniqueness** `c_i` (per record image, per job, per channel): count how many _other records_ in the job contain a near-duplicate image (similarity ≥ `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`, default 0.85). Then:

```
c_i = 1 / (1 + α * matches_in_other_records)         # α default 1.0
```

Bounded `(0, 1]`, **smoothly decays, never zero**. A non-unique record image still contributes evidence, just at reduced weight, so the matcher doesn't lose information when records share generic images. The threshold and α are configurable globally and per-channel (`*_PHASH`, `*_TONE` overrides — useful for corpora where one channel needs distinct tuning, e.g., monochrome film where tone is a strong discriminator).

Why not IDF (`log(N/n)/log(N)`)? Records typically have N ≤ 5 images. IDF collapses too fast at small N: at N=4 with 1 match, `c_i ≈ 0.14`; at N=2 with 1 match, `c_i = 0`. The smoothed reciprocal is robust to small-N regimes and exposes a single tunable (`α`).

This is a near-duplicate count, not exact-equal — done with the channel's own similarity metric. Computed once per (job, channel); stored in `image_uniqueness`. **Stash-side images have no `c_i`** — there is no Stash corpus that meaningfully participates in IDF; matching treats Stash-side `c_i = 1`.

### 13.7 Threshold gates the composite

- **Scrape**: `composite >= threshold` → image tier fires. Otherwise, falls through.
- **Search**: `composite` contributes to the rank score; no threshold gate. **Optional** search-mode confidence floor `image_search_floor` drops candidates whose composite is below the floor _unless_ a definitive signal (Studio+Code or Exact Title) fired. Defaults to `None` (disabled) — calibration on mixed-content corpora showed weak-correct and weak-incorrect composites overlap; users with sharper corpora can enable at 0.10–0.20.

### 13.8 Filtering — must happen before any aggregation

The per-image filtering rules apply per-image and per-channel:

1. **404 / fetch failure** (`extractor/client.fetch_asset` returns `None`). Drop the ref entirely from all channels.
2. **Low pixel variance at hash time** (`imgmatch/image_comparison.hash_image_bytes` returns `None`). Catches all-black sprite frames at the source.
3. **Degenerate-hash check at sim time** (`_is_degenerate_hash`). Belt-and-braces for any pHash with bit-density outside `[10%, 90%]`. Affects only channel A.

A filter failure on one channel does not exclude the image from other channels — channel A might filter a frame for low pHash variance while channel B still uses its color histogram.

### 13.9 Calibrated defaults (provenance: [`docs/calibration/CALIBRATION_RESULTS.md`](docs/calibration/CALIBRATION_RESULTS.md))

| Parameter                                  | Default | Source             |
| ------------------------------------------ | ------- | ------------------ |
| `image_gamma`                              | 3.5     | Run 3a peak (concave; γ=2.0 default lost 27 points to γ=3.5) |
| `image_count_k`                            | 0.25    | Run 3c peak (sparse-N records were systematically under-weighted at k=2.0) |
| `image_uniqueness_alpha` (request)         | 1.0     | Run 5b flat       |
| `image_min_contribution`                   | 0.05    | Run 3b (higher values exclude weak-but-correct contributions more aggressively than weak-but-incorrect) |
| `image_bonus_per_extra`                    | 0.1     | Unchanged         |
| `image_search_floor`                       | None    | Run 6 (mechanism shipped, default disabled — composite distributions overlap on mixed-content) |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`    | 0.85    | Run 5a confirmed peak (concave, both directions degrade) |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`        | 1.0     | Run 5b flat       |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_*`  | None    | Run 7 (per-channel mechanism shipped; tone-specific override unhelpful on Pexels-style corpora — c_i collapse silences a noisy channel, which is the desired behavior) |

### 13.10 Don'ts

- **Don't** revert to single-channel scoring "because it's simpler" — single-channel was the bug.
- **Don't** compose channels by mean or product. Mean dilutes a strong channel's signal; product zeros the composite when one channel doesn't fire.
- **Don't** introduce per-channel thresholds. The threshold is on the composite. Per-channel `min_contribution` is a _firing-detection_ parameter for the bonus, not a gate on the channel's output.
- **Don't** populate Stash-side `c_i`. There is no Stash corpus that meaningfully participates in IDF; using `c_i = 1` is correct for the Stash side and any other choice introduces hard-to-debug coupling between user library composition and match scores.
- **Don't** featurize synchronously inside the request hot path. The 503/Retry-After protocol (§14) is the contract; eager-at-startup absorbs the latency cost out of band.
- **Don't** use IDF (`log(N/n)/log(N)`) for `c_i`. Records typically have N ≤ 5; IDF collapses too fast at small N (at N=2 with 1 match, c_i = 0). The smoothed reciprocal is the canonical form.
- **Don't** append `0.0` entries for failed/uniform images — they're noise and tempt future maintainers to "fix" them. The list length should equal the number of usable comparisons, not the number of attempted ones.
- **Don't** change the canonical defaults (§13.9) without a corresponding calibration run appended to `docs/calibration/CALIBRATION_RESULTS.md`. The defaults are empirical, not aesthetic.

---

## 14. Featurization lifecycle is eager-at-startup; the hot path is `ready` → 200, else → 503

> **Per-record features and per-job corpus statistics are computed eagerly at container startup and on cascade invalidation. Match requests for a job whose features aren't `ready` return `503 Service Unavailable + Retry-After`. The bridge never featurizes synchronously inside the request path.**

Without this contract, multi-channel scoring's `c_i` (which requires corpus-level knowledge) would be unreachable on first-request — every cold-start scrape would block on tens of seconds of compute. The 503/Retry-After protocol absorbs that latency out of band; in steady state, every request hits `ready` and returns a normal 200.

This is gated by `BRIDGE_LIFECYCLE_ENABLED`. When `false`, the bridge falls back to on-demand caching against `image_hashes` (legacy behavior, no §13 corpus-relative weighting); useful as a rollback path.

### 14.1 Job feature states

| State         | Meaning                                                          | Persisted in                                                |
| ------------- | ---------------------------------------------------------------- | ----------------------------------------------------------- |
| (no row)      | Never seen / fully invalidated                                   | absence from `job_feature_state`                            |
| `featurizing` | Queued or in flight (queued = `progress = 0`, in flight = `> 0`) | `job_feature_state.state = 'featurizing'`, `progress < 1`   |
| `ready`       | Fully featurized; `corpus_stats` and `image_uniqueness` populated | `job_feature_state.state = 'ready'`                        |
| `failed`      | Featurization errored (e.g., extractor unreachable mid-run)      | `job_feature_state.state = 'failed'`, `error` populated     |

The `featurizing` state covers both "queued, not started" (`progress = 0`) and "in flight" (`progress > 0`). A separate `queued` state was rejected — the in-flight bit is encoded in `progress`, and any waiting request needs `503 + Retry-After` regardless.

Transitions:

```
(no row)        → featurizing  (on container startup, cascade invalidation,
                                or request for a never-seen job)
featurizing     → ready        (on successful completion)
featurizing     → failed       (on unrecoverable error)
ready           → (no row)     (on completed_at advance — cascade)
failed          → featurizing  (on next request — auto-retry)
```

The trigger predicate is uniform: **any job that is not `ready` should be queued for featurization.** Three entry points produce that observation (startup scan, cascade, request); they share the same enqueue path.

### 14.2 Request handling state machine

```
on match_request(job_id):
    state = read job_feature_state for job_id

    if state == 'ready':
        proceed with matching using cached features
        return 200 with results

    if state in (None, 'failed'):
        ensure_job_results_fresh(job)              # FK parent must exist before insert
        atomically: insert/update job_feature_state to 'featurizing' (progress=0)
        enqueue background featurization task (single-in-flight per job_id)
        # fall through to featurizing branch

    if state == 'featurizing':
        return 503 with Retry-After: ceil(estimated_remaining_seconds)
```

Definitive signals (Studio+Code, Exact Title) **do not** wait on featurization — they ride on `extractor_results` rows alone. The 503 gate applies only to image-tier matching.

### 14.3 Single-in-flight per `job_id` and bounded global concurrency

Featurization tasks are keyed by `job_id`. The bridge maintains an in-process map `{job_id: asyncio.Task}` plus a bounded worker pool (default `BRIDGE_FEATURIZE_CONCURRENCY = 4`).

- A second enqueue for the same `job_id` while a task already exists is a no-op — concurrent requests share state via the DB row and all return 503 until completion.
- The pool serializes featurization across jobs so a cascade of 1000 jobs doesn't open 1000 concurrent extractor connections.
- Per-job parallel asset fetches inside a single task are bounded by `BRIDGE_FEATURIZE_PER_JOB_CONCURRENCY` (default 8) so one big job can't exhaust extractor connections.

Restart recovery: the in-memory map is reconstructed from the DB on startup. `state='featurizing'` rows whose `started_at < now() - BRIDGE_STALE_TASK_MS` (default 10 min) are reset and re-enqueued — startup treats them identically to never-started rows. Distinguishing "stuck" from "just slow" is the timeout's job; the recovery path is uniform.

### 14.4 CPU-bound compute must run off the event loop

PIL/imagehash/numpy compute is synchronous and CPU-bound. Wrapping these calls in `asyncio.to_thread()` is **not optional** — without it, the event loop blocks for ~50–150 ms per hash operation, and a 500-record featurization stalls the entire FastAPI process for 5–15 minutes (the bridge can't even respond to its own `/health` endpoint, much less emit the 503s its contract promises).

The five hot spots are:
- `bridge/app/matching/image_match.py::_hash_or_compute`
- `bridge/app/matching/image_match.py::stash_sprite_hashes`
- `bridge/app/matching/image_match.py::_features_or_compute_bc`
- `bridge/app/matching/image_match.py::stash_sprite_bc_features`
- `bridge/app/matching/featurization.py` (baseline + uniqueness compute, per-record B aggregate)

Regression test: `tests/unit/test_lifecycle.py::TestEventLoopResponsiveness::test_db_query_latency_stays_low_during_featurization` asserts max DB query latency < 150 ms during featurization.

**Don't** call `hash_image_bytes`, `compute_color_hist`, `compute_tone`, `_compute_baseline_*`, or `_compute_uniqueness_*` directly in an `async` coroutine. They go through `asyncio.to_thread`.

### 14.5 Cascade invalidation (extends §7)

When `extractor_jobs.completed_at` advances, the cascade is one atomic transaction:

```
ATOMIC TRANSACTION:
    DELETE FROM extractor_results       WHERE job_id = ?
    DELETE FROM image_features          WHERE source IN ('extractor_image', 'extractor_aggregate')
                                          AND ref_id LIKE ? || ':%'
    DELETE FROM corpus_stats            WHERE job_id = ?
    DELETE FROM image_uniqueness        WHERE job_id = ?
    DELETE FROM match_results           WHERE job_id = ?
    DELETE FROM job_feature_state       WHERE job_id = ?
    INSERT new extractor_jobs row
    INSERT new extractor_results rows
COMMIT
# then enqueue a fresh featurize_task for this job_id
```

Stash-side rows (`source IN ('stash_cover', 'stash_sprite', 'stash_aggregate')`) are not purged — see §7.

### 14.6 Eager-startup recovery

On bridge container startup, after `init_db()`:

1. Reset stale `featurizing` rows interrupted by the previous shutdown: `UPDATE job_feature_state SET state='featurizing', progress=0, started_at=now(), error=NULL WHERE state='featurizing' AND started_at < now() - STALE_TASK_MS`.
2. Discover all jobs that are not yet `ready`: `SELECT j.job_id FROM extractor_jobs j LEFT JOIN job_feature_state f USING (job_id) WHERE f.state IS NULL OR f.state != 'ready'`.
3. Insert/update them as queued (`state='featurizing'`, `progress=0`).
4. Enqueue all of them for the worker pool (bounded by concurrency limit).

The bridge starts accepting requests immediately; un-ready jobs return 503 until their task completes. This is **idempotent** — re-running yields the same state. **Phase 1 of `featurize_task` writes per-image features to `image_features` as it goes**, so on interrupt + restart, the next task run reads what's already cached and only fetches missing refs. No work lost.

### 14.7 Status endpoints

```
GET /api/extraction/{job_id}/features
  → 200 { state, progress, started_at, finished_at, error }

GET /api/featurization/status
  → 200 { queued, in_progress, ready, failed, concurrency_limit }
```

Ops + debugging only. Not part of the scraper contract.

### 14.8 Batch scrape behavior (documented, not a bug)

When Stash batch-scrape queues many requests against a job whose features aren't `ready`:

- The first request to observe a non-`ready` state enqueues featurization (no-op if already enqueued).
- All requests in the batch return `503 Service Unavailable` with `Retry-After` until the task finishes.
- Stash batch scrape may or may not respect `Retry-After`; if not, the bridge returns 503 for the duration of featurization. **These appear as errors in the Stash log, not as zero-result responses.** Investigating "no results" should check `~/.stash/logs/` for 503s.
- Once featurization completes, subsequent scrapes succeed normally.

Eager-at-startup means the bridge is already `ready` for known jobs by the time scrapes hit it — this 503 window is bounded to cold-start, cascade-invalidation, and never-seen-job cases.

### 14.9 Stash-side LRU eviction

Stash-side `image_features` rows accumulate without bound (they're not job-cascade-cleared). A background task in the bridge runs every `BRIDGE_LRU_EVICTION_INTERVAL_S` (default 3600s) and evicts oldest-accessed Stash rows down to `BRIDGE_STASH_FEATURE_BUDGET_BYTES` (default 1 GB). Eviction key: `last_accessed_at` (a column on `image_features` populated only for Stash-side rows). Set budget to 0 to disable eviction entirely.

Extractor-side rows are never evicted — they're bounded by job count and cleared on cascade.

### 14.10 Don'ts

- **Don't** add a synchronous block budget to the request hot path. Eager-at-startup means the bridge almost never sees a non-`ready` job; the narrow window where it does is correctly a 503. A sync budget would just race the timeout against task completion.
- **Don't** featurize per-channel in serial inside the request handler. The lifecycle is the contract.
- **Don't** populate `last_accessed_at` for extractor-side rows. Eviction logic uses `last_accessed_at IS NOT NULL` as the "is Stash-side" predicate (see `idx_features_lru` partial index in `bridge/app/cache/db.py`).
- **Don't** rely on `extractor_results` to exist before inserting `job_feature_state` — the FK requires the parent row first. The gate calls `ensure_job_results_fresh(job)` before any state insert (this was a bug, captured by `tests/unit/test_lifecycle.py::TestWorker::test_enqueue_creates_state`).

---

## 15. Cache schema invariants

> **Each table has a primary key shape, an invalidation key (§7), and a unique role. Mixing roles is a corruption hazard.**

| Table                | PK shape                                | Role                                                                                                                                                                                                              |
| -------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `extractor_jobs`     | `(job_id)`                              | Local mirror of extractor's `/api/jobs`. `completed_at` is the cascade trigger.                                                                                                                                  |
| `extractor_results`  | `(job_id, result_index)`                | Local mirror of extractor's `/api/extraction/{job_id}/results`. Cascades from `extractor_jobs`.                                                                                                                  |
| `image_features`     | `(source, ref_id, channel, algorithm)`  | Per-(source, ref, channel) feature blob + `quality`. `last_accessed_at` populated only for Stash-side rows (used by §14.9 eviction).                                                                             |
| `corpus_stats`       | `(job_id, channel, algorithm)`          | Per-job, per-channel `baseline` (empirical noise floor). Job-scoped because uniqueness is computed within one job's record set.                                                                                  |
| `image_uniqueness`   | `(job_id, ref_id, channel)`             | Per-record-image `c_i`. Granularity is finer than `corpus_stats` (per-image, not per-job).                                                                                                                       |
| `job_feature_state`  | `(job_id)`                              | Featurization lifecycle (§14). FK to `extractor_jobs`.                                                                                                                                                           |
| `match_results`      | composite of scene fingerprint + job CA | Memoized match output per (scene, job). Invalidates on either side change.                                                                                                                                       |
| `image_hashes`       | (legacy)                                | Phase 1 single-channel pHash table. Soft-retired behind `BRIDGE_LEGACY_DUAL_WRITE_ENABLED` (default `true`). Drop is a manual op.                                                                                |

### 15.1 Quality is intrinsic; uniqueness is corpus-relative

`q_i` lives on `image_features` because it's a property of the image content alone (entropy, edge density, dynamic range). It does not change as the corpus grows.

`c_i` lives on `image_uniqueness` because it's a property of the image _relative to the other images in the same job's record set_. It must be recomputed when the job's record set changes (i.e., when `completed_at` advances).

**Don't** store `c_i` in `image_features`. **Don't** store `q_i` in `image_uniqueness`. **Don't** populate Stash-side `image_uniqueness` rows.

### 15.2 Source taxonomy on `image_features.source`

| Value                  | Keyed by                                        | Lifetime                                  |
| ---------------------- | ----------------------------------------------- | ----------------------------------------- |
| `stash_cover`          | scene oshash + screenshot epoch                 | Bounded by §14.9 LRU eviction             |
| `stash_sprite`         | scene oshash + frame index                      | Bounded by §14.9 LRU eviction             |
| `stash_aggregate`      | scene oshash (channel B aggregate)              | Bounded by §14.9 LRU eviction             |
| `extractor_image`      | `<job_id>:<image_ref>`                          | Cleared by `completed_at` cascade (§14.5) |
| `extractor_aggregate`  | `<job_id>:<record_idx>` (channel B aggregate)   | Cleared by `completed_at` cascade (§14.5) |

**Don't** introduce a sixth `source` value without updating the cascade query in §14.5 and the eviction predicate in §14.9.

### 15.3 Phase 7 dual-write retirement

While `BRIDGE_LEGACY_DUAL_WRITE_ENABLED=true` (default): every pHash compute writes to both `image_hashes` (legacy) and `image_features` (new); reads check `image_features` first and fall back to `image_hashes`. Setting it `false` stops the dual-write and skips the legacy fallback. The actual `DROP TABLE image_hashes` is a manual op (see [`docs/HOW_TO_USE.md`](docs/HOW_TO_USE.md)). After dropping, you can't roll back to the legacy scoring path.

---

## 16. Where to look first when something breaks

| Symptom                                       | First place to check                                                                                                                                                                                                                                                                          |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Wrong scene returned in scrape                | `bridge/app/matching/scrape.py` cascade order; check candidate's `id`, `title`, image_sim values in logs                                                                                                                                                                                      |
| Search results in odd order                   | `bridge/app/matching/search.py`; verify `min(score, 1.0)` cap; check tiebreak by `result_index`                                                                                                                                                                                               |
| "No result" when one was expected             | §5 studio filter — does scene have a studio that matches a job name? §6 schema superset — does the job's schema actually have all canonical fields?                                                                                                                                           |
| Image match never fires                       | (a) §13.7 composite below `image_threshold` — request `?debug=1` and inspect `_debug.image.channels.*.S` per channel + `composite`. (b) Asset URL resolution — extractor records use `../assets/<file>` which must rewrite to `/api/asset/{job_id}/assets/<file>`. Check ETag handling.       |
| One channel always scores 0                   | Featurization didn't run for that channel — check `corpus_stats` rows for the job/channel pair. May indicate a per-image compute failure (e.g. PIL can't decode the asset for tone) or an empty `image_features` row set for that channel.                                                    |
| Performer match always 0                      | Alias index freshness — check TTL, check `findPerformers` query uses both `name` and `aliases` filters with `OR`                                                                                                                                                                              |
| Cache returning stale results                 | §7 — `completed_at` change should have triggered cascade. Check `extractor_jobs.completed_at` against fresh `GET /api/jobs/{id}`                                                                                                                                                              |
| 400 Bad Request from bridge                   | §1 — scraper's `config.py` is missing a required parameter; the bridge has no fallback                                                                                                                                                                                                        |
| Filename score lower than expected            | §12 — request with `?debug=1` and inspect `_debug.filename` for naive/guessit/structured channel breakdown; verify guessit isn't mis-parsing the title                                                                                                                                        |
| 503 Service Unavailable                       | §14 — featurization isn't `ready`. `GET /api/featurization/status` for fleet view; `GET /api/extraction/{job_id}/features` for one job. Expected during cold start, cascade, never-seen-job; abnormal if stuck.                                                                              |
| Bridge unresponsive (`/health` times out)     | §14.4 — CPU-bound compute leaked onto the event loop. Profile and confirm `asyncio.to_thread` wraps the affected callsite. The regression test in `test_lifecycle.py` should catch this in CI; if it didn't, the test fixture isn't representative.                                            |
| Featurization stuck at `progress: 0`          | Bridge restart didn't see the row as stale yet — wait `BRIDGE_STALE_TASK_MS` (default 10 min) for auto-recovery, or manually delete the `job_feature_state` row.                                                                                                                              |
| FK violation on `job_feature_state` insert    | §14.2 — `ensure_job_results_fresh(job)` must run before the gate inserts the state row. Captured by `tests/unit/test_lifecycle.py::TestWorker::test_enqueue_creates_state`.                                                                                                                  |
| Storage growing without bound                 | §14.9 — LRU eviction loop disabled (`BRIDGE_STASH_FEATURE_BUDGET_BYTES=0`?) or interval too long. Stash-side rows are the unbounded class; extractor-side rows are job-cascade-bound.                                                                                                          |
