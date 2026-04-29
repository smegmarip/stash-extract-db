# Multi-Channel Image Scoring — Architecture Proposal

**Status**: Proposal. Not yet implemented.
**Supersedes**: `CLAUDE.md` §13 (top-K mean over flat M×N pair set).
**Companion**: this doc proposes the new contract; the §13 rewrite in section 5 is what lands in `CLAUDE.md` when this is merged.

---

## 1. Scope

### What this proposal changes

| Area                           | Change                                                                                                                                                                                                                                                                                                               |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Bridge**                     | Gains a per-job featurization pipeline that runs eagerly at container startup for any job not in `ready` state; same pipeline re-triggers on cascade invalidation (completed_at advance) and on requests for never-seen jobs. Multi-channel feature compute (pHash + color histogram + low-res tone); per-job corpus statistics (baselines, per-record-image uniqueness). |
| **Bridge — schema**            | New `image_features`, `corpus_stats`, `image_uniqueness`, `job_feature_state` tables. `image_hashes` retired after migration.                                                                                                                                                                                                                                            |
| **Bridge — request lifecycle** | Match requests for a job whose features aren't `ready` return `503 Service Unavailable` + `Retry-After`. Featurization is eager-at-startup so the steady-state hot path is unaffected; the 503 window is bounded to cold-start and cascade-invalidation gaps. Single in-flight task per `job_id`, bounded global concurrency.                                             |
| **Bridge — scoring**           | New within-channel formula (sharpened evidence-union with quality and uniqueness weighting, count saturation, distribution-shape term) applied per channel; cross-channel composition via `max(channels) + bonus_when_≥2_fire` (mirrors `CLAUDE.md` §12 filename pattern). Threshold (§4) now gates the _composite_. |
| **`CLAUDE.md` §13**            | Rewritten — see section 5.                                                                                                                                                                                                                                                                                           |

### What stays unchanged

- **Scraper** — still the source of truth for matching parameters (§1). New parameters added (channel weights, gamma, k, channel-specific thresholds) but the contract that the scraper sends them in every request is preserved.
- **`CLAUDE.md` §1–§12** — unchanged. Studio filter, scrape/search semantics, schema-shape detection, output mapping, tiebreaks, empty-and-null neutrality, filename scoring — all hold.
- **Stash-side image fetch + cache pattern** — sprite/cover hashing remains on-demand and per-scene-cached. The Stash corpus does not become a precompute target.
- **No plugin** — the bridge owns the entire feature lifecycle. Discussed and rejected: a Stash plugin doing eager Stash-library preprocessing. The eager-at-startup + cascade-driven lifecycle scopes featurization to the codomain (records), which is bounded per job and cheap enough for the bridge to handle directly.

### Non-goals (deferred — see section 7)

- Stash-side image uniqueness (sprite-frame IDF). Theoretically valid for a class of false positives ("dark hallway" scenes shared across many videos) but second-order; revisit if real-world failures exhibit it.
- A calibration harness UI. The math allows offline calibration from labeled pairs, but the labeling tooling is its own project.

---

## 2. Data model

### 2.1 New tables

```sql
-- Replaces image_hashes. Stores per-(source, ref, channel) feature blob + intrinsic quality.
CREATE TABLE IF NOT EXISTS image_features (
  source           TEXT NOT NULL,  -- 'stash_cover' | 'stash_sprite' | 'stash_aggregate'
                                   -- | 'extractor_image' | 'extractor_aggregate'
  ref_id           TEXT NOT NULL,  -- 'scene42'  |  'scene42:17'  |  'scene42:agg'
                                   -- | '<job_id>:<image_ref>'  |  '<job_id>:<record_idx>:agg'
  fingerprint      TEXT NOT NULL,  -- invalidation key (oshash, ?t=..., asset URL,
                                   --   composite-of-constituents for aggregates)
  channel          TEXT NOT NULL,  -- 'phash' | 'color_hist' | 'tone'
  algorithm        TEXT NOT NULL,  -- e.g. 'phash:8' | 'color_hist:hsv:4x4x4' | 'tone:gray:8x8'
  feature_blob     BLOB NOT NULL,  -- channel-specific compact binary
  quality          REAL NOT NULL,  -- q_i, intrinsic [0,1]
  computed_at      TEXT NOT NULL,
  last_accessed_at TEXT,           -- for LRU eviction (Stash-side rows only; null on extractor side)
  PRIMARY KEY (source, ref_id, channel, algorithm)
);

CREATE INDEX IF NOT EXISTS idx_features_ref ON image_features(source, ref_id);
CREATE INDEX IF NOT EXISTS idx_features_lru ON image_features(last_accessed_at)
  WHERE last_accessed_at IS NOT NULL;

-- Per-job, per-channel corpus statistics. Job_id-scoped because uniqueness is
-- computed within the record set of a given extractor job.
CREATE TABLE IF NOT EXISTS corpus_stats (
  job_id        TEXT NOT NULL,
  channel       TEXT NOT NULL,
  algorithm     TEXT NOT NULL,
  baseline      REAL NOT NULL,    -- empirical noise floor (random-pair mean)
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (job_id, channel, algorithm),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

-- Per record-image uniqueness (c_i). Separate table because granularity is
-- (job_id, image_ref, channel) — finer than corpus_stats.
CREATE TABLE IF NOT EXISTS image_uniqueness (
  job_id        TEXT NOT NULL,
  ref_id        TEXT NOT NULL,    -- 'image_ref_within_record'
  channel       TEXT NOT NULL,
  uniqueness    REAL NOT NULL,    -- c_i, [0,1]
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (job_id, ref_id, channel),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

-- Featurization lifecycle state per job.
CREATE TABLE IF NOT EXISTS job_feature_state (
  job_id        TEXT PRIMARY KEY,
  state         TEXT NOT NULL,    -- 'featurizing' | 'ready' | 'failed'
  progress      REAL NOT NULL,    -- [0, 1]
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  error         TEXT,
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);
```

### 2.2 Feature blob format per channel

| Channel                 | `algorithm` example    | Blob layout                                  | Size |
| ----------------------- | ---------------------- | -------------------------------------------- | ---- |
| **A — pHash**           | `phash:8`              | 64-bit pHash, big-endian                     | 8 B  |
| **B — color histogram** | `color_hist:hsv:4x4x4` | 64 uint8 bin counts (quantized + normalized) | 64 B |
| **C — low-res tone**    | `tone:gray:8x8`        | 64 uint8 luminance values                    | 64 B |

Blobs are versioned by their `algorithm` string. A change to channel B's bin scheme (e.g., `hsv:4x4x4` → `hsv:8x8x8`) coexists with the old version on disk; matching reads the algorithm the request specifies.

### 2.3 Quality (`q_i`) is intrinsic; uniqueness (`c_i`) is corpus-relative

`q_i` lives on `image_features` because it's a property of the image content alone (entropy, edge density, dynamic range). It does not change as the corpus grows.

`c_i` lives on `image_uniqueness` because it's a property of the image _relative to the other images in the same job's record set_. It must be recomputed when the job's record set changes (i.e., when `completed_at` advances).

Stash-side images have no `c_i` — there is no "corpus" of Stash images that meaningfully participates in IDF. The matching formula treats Stash-side `c_i = 1` (neutral).

### 2.4 Match-results cache impact

`match_results` table unchanged in shape. Its invalidation (§7 of `CLAUDE.md`) cascades from `extractor_jobs.completed_at` change as before.

---

## 3. Multi-channel scoring contract

### 3.1 Channels

Three channels ship in v1. The architecture supports adding channels without breaking the contract.

| Channel                                  | What it captures                                                                                            | What it misses                                                                 | Pair grid shape                                                              |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| **A — pHash per-frame**                  | Direct structural fingerprint (DCT-based). Robust to small re-encoding artifacts; sensitive to composition. | Scene variation, lighting changes, varied subjects within a video.             | `M × N` (M = sprite frames + optional cover, N = record images)              |
| **B — color histogram, scene-aggregate** | Whole-scene chromatic profile. Survives cuts, lighting changes, compression.                                | Records and scenes that share generic palettes (sepia, low-light, monochrome). | `1 × 1` (one Stash-side aggregate hist vs one extractor-side aggregate hist) |
| **C — low-res tone per-frame**           | Coarse luminance/composition signature. Robust to color shifts; complementary to pHash.                     | High-frequency texture detail.                                                 | `M × N`                                                                      |

Channel B's "aggregate" is computed **per scene (Stash side) and per record (extractor side)** — never per-job, since matching is per-(scene, record). For each HSV bin index, take the per-bin median value of that bin across all usable frames in scope (Stash side: sprite frames + cover for the scene; extractor side: cover_image + images[] for the single record). The resulting bin vector is re-normalized to sum to 1 and stored as a single aggregate row in `image_features` (`source='stash_aggregate'` keyed by scene oshash; `source='extractor_aggregate'` keyed by `<job_id>:<record_idx>`). Per-bin median (not mean) is robust to outlier frames (black frames, fade transitions, blank record placeholders) without needing the §13 binary filters to be perfect. One number out per (scene, record) pair, no `M × N` grid.

### 3.2 Within-channel scoring (applied independently per channel)

Inputs per channel:

- For frame-level channels (A, C): a flat list of pair similarities `sims = [s_11, s_12, ..., s_MN]` and the count `N` of usable extractor images.
- For aggregate channels (B): a single similarity `s_B`.

Plus, per record image `i` in this job:

- `q_i ∈ [0, 1]` — intrinsic quality score for this channel (formula varies per channel — see §3.6)
- `c_i ∈ (0, 1]` — corpus-relative uniqueness score (smoothed reciprocal — see §4.6)

Plus, per channel, from `corpus_stats`:

- `baseline` — empirical noise floor (random-pair mean)

Steps (frame-level channels):

```
# 1. Per-extractor-image best similarity (collapse the M dimension)
for each extractor image i:
    m_i = max over sprite frames j of sim(stash_j, extractor_i)

# 2. Sharpening — subtract noise floor, gamma to suppress fuzzy matches
gamma = 2  # tunable, scraper config
m_i' = max(0, (m_i - baseline) / max(epsilon, 1 - baseline)) ** gamma

# 3. Quality- and uniqueness-weighted contribution
w_i = q_i * c_i
contribution_i = w_i * m_i'

# 4. Evidence union (soft-OR over weighted, sharpened contributions)
E = 1 - product over i of (1 - contribution_i)

# 5. Count saturation — records with N=1 image earn less than N=10 with same evidence
k = 2.0  # tunable, scraper config
effective_N = sum over i of w_i
count_conf = 1 - exp(-effective_N / k)

# 6. Distribution shape — broad coverage > single spike
if count of m_i' > 1 and sum(m_i') > 0:
    p_i = m_i' / sum(m_i')
    H = -sum over i of (p_i * log(p_i + epsilon))   # entropy
    H_max = log(N)
    dist_q = 0.5 + 0.5 * (H / H_max)                # ∈ [0.5, 1]
else:
    dist_q = 0.5                                     # single contribution → conservative

# 7. Channel score
S_channel = E * count_conf * dist_q                  # ∈ [0, 1]
```

Why soft-OR is now safe (vs. its rejection in old §13): inputs `m_i'` are post-sharpening — a noise-floor sim of 0.5 becomes ~0 after baseline subtraction and gamma. The single-outlier saturation that bit the old design required _unfiltered_ 1.0 sims; with quality and uniqueness gates, a coincidental 1.0 on a low-quality or non-unique image gets `w_i ≪ 1`, so its union contribution is bounded.

Steps (aggregate channels, e.g., B):

```
# Direct sharpening on the single similarity, no distribution term
m_B' = max(0, (s_B - baseline) / max(epsilon, 1 - baseline)) ** gamma
S_B = m_B' * q_B                                     # ∈ [0, 1]
```

Channel B is treated as a single piece of corroborating evidence. No count saturation (it always represents one summary). No distribution term (no distribution).

### 3.3 Cross-channel composition

```
fired = [S for S in [S_A, S_B, S_C] if S >= MIN_CONTRIBUTION]   # MIN_CONTRIBUTION = 0.3
if not fired:
    composite = 0
else:
    bonus_per_extra_channel = 0.1                     # tunable
    composite = max(fired) + bonus_per_extra_channel * (len(fired) - 1)
    composite = min(composite, 1.0)
```

This is the §12 filename pattern: `max(channels) + structured_bonus`, union-of-evidence, capped at 1.0.

### 3.4 Threshold semantics (relation to §4)

The threshold from `config.py` now gates the _composite_, not individual sims and not individual channels.

- **Scrape mode**: `composite >= threshold` → image tier fires. Below threshold → image tier does not fire (cascade falls through).
- **Search mode**: `composite` contributes to the rank score unconditionally (no threshold gate).

This preserves §4's "threshold is a multiplier-rule in search, an inclusion-rule in scrape" — but applied to the composite.

### 3.5 Required scraper config additions

Per §1 (config ownership), every parameter the bridge consults must arrive in the request. New required fields:

| Field                     | Purpose                                                                      |
| ------------------------- | ---------------------------------------------------------------------------- |
| `image_channels`          | Ordered list of channels to evaluate, e.g. `["phash", "color_hist", "tone"]` |
| `image_gamma`             | Sharpening exponent (default 2)                                              |
| `image_count_k`           | Count-saturation `k` (default 2.0)                                           |
| `image_uniqueness_alpha`  | Smoothing factor in `c_i = 1/(1 + α·matches)` (default 1.0)                  |
| `image_min_contribution`  | Per-channel firing threshold for cross-channel bonus (default 0.3)           |
| `image_bonus_per_extra`   | Bonus weight per extra firing channel (default 0.1)                          |

The single `image_threshold` continues to gate the composite (no per-channel thresholds). If a parameter is missing from a request, bridge returns `400 Bad Request` per §1.

### 3.6 Per-channel `q_i` formulas

`q_i` is intrinsic to the image (no corpus context) but channel-specific — what makes an image "informative" depends on what the channel measures. Each is geometric-mean composed so a weak axis pulls `q_i` down (a uniform-color image fails on multiple axes; the geometric mean returns ~0).

| Channel       | `q_i` formula                                                       | Components                                                                                                                                                                                                                          |
| ------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A (pHash)     | `sqrt(entropy_norm * variance_norm)`                                | `entropy_norm` = Shannon entropy of grayscale histogram (8-bit, 256 bins) ÷ 8 (bits, max). `variance_norm` = `min(1.0, var(grayscale_pixels) / 100²)` — real-image stdev rarely exceeds 100; clamp to [0, 1].                       |
| B (color hist) | `1 - gini(hist_bins)`                                               | Gini coefficient of the normalized HSV histogram bins. A monochromatic image has Gini → 1 (all mass in few bins) → low quality. A varied palette has Gini → 0 → high quality. Cheap to compute alongside the histogram itself.      |
| C (tone)      | `sqrt(entropy_norm * variance_norm)`                                | Same as channel A (also grayscale-derived).                                                                                                                                                                                         |

All `q_i` are bounded to [0, 1]. The §13 binary filters (degenerate-hash, low-pixel-variance) still gate at the extremes; `q_i` provides graded weighting in the middle of the range. Edge density (Sobel) deferred — see §7.4.

### 3.7 Per-channel similarity metrics

Each channel's `sim()` function maps two feature blobs to a similarity in `[0, 1]`:

| Channel       | `sim(a, b)` formula                                                                                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A (pHash)     | `1 - hamming(a, b) / hash_size²` — current behavior preserved.                                                                                                       |
| B (color hist) | `Σ_i min(a[i], b[i])` — histogram intersection. For normalized hists (`Σa = Σb = 1`), naturally bounded `[0, 1]`. 1.0 iff identical distributions; 0 iff disjoint.   |
| C (tone)      | `1 - mean(|a[i] - b[i]| / 255)` — mean L1 distance over 64 luminance values, normalized to `[0, 1]`.                                                                 |

All three return values directly comparable to channel-specific `baseline` from `corpus_stats`, so the sharpening formula `(m_i - baseline) / (1 - baseline)` is well-defined and bounded for every channel.

### 3.8 Debug observability

`?debug=1` on `/match/*` (search mode only — scrape returns single result or empty per §2) returns a `_debug.image` block per candidate:

```json
{
  "_debug": {
    "image": {
      "channels": {
        "phash": {"S": 0.71, "E": 0.85, "count_conf": 0.93, "dist_q": 0.90, "m_primes": [0.82, 0.0, 0.61]},
        "color_hist": {"S": 0.40, "m_prime": 0.50, "q": 0.80},
        "tone":  {"S": 0.55, "E": 0.70, "count_conf": 0.85, "dist_q": 0.92, "m_primes": [...]}
      },
      "fired": ["phash", "tone"],
      "composite": 0.81,
      "threshold_pass": true
    }
  }
}
```

---

## 4. Lifecycle

### 4.1 Job feature states

| State         | Meaning                                                           | Persisted in                                                         |
| ------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------- |
| (no row)      | Never seen / fully invalidated                                    | absence from `job_feature_state`                                     |
| `featurizing` | Queued or in flight (queued = `progress = 0`, in flight = `> 0`)  | `job_feature_state.state = 'featurizing'`, `progress < 1`            |
| `ready`       | Fully featurized; `corpus_stats` and `image_uniqueness` populated | `job_feature_state.state = 'ready'`                                  |
| `failed`      | Featurization errored (e.g., extractor unreachable mid-run)       | `job_feature_state.state = 'failed'`, `error` populated              |

The `featurizing` state covers both "queued, not started" (`progress = 0`) and "in flight" (`progress > 0`). A separate `queued` state was considered and rejected — the in-flight bit is encoded in `progress`, and any waiting request needs `503 + Retry-After` regardless.

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

### 4.2 Request handling state machine

The hot path is intentionally simple: only `ready` produces a 200; everything else produces a 503.

```
on match_request(job_id):
    state = read job_feature_state for job_id

    if state == 'ready':
        proceed with matching using cached features
        return 200 with results

    if state in (None, 'failed'):
        atomically: insert/update job_feature_state to 'featurizing' (progress=0)
        enqueue background featurization task (single-in-flight per job_id — see §4.3)
        # fall through to featurizing branch

    if state == 'featurizing':
        return 503 with Retry-After: ceil(estimated_remaining_seconds)
```

Why no synchronous block budget: featurization runs eagerly at startup (§4.10) and re-runs on cascade invalidation, so the steady-state hot path almost never observes a non-`ready` job. The narrow window where it does (cold start, cascade gap, never-seen-job) is correctly a 503 — a brief sync block would just add a race between the budget timeout and task completion without shortening user-perceived latency in the cases that matter.

### 4.3 Single-in-flight per `job_id` and bounded global concurrency

Featurization tasks are keyed by `job_id`. The bridge maintains an in-process map `{job_id: asyncio.Task}` plus a bounded worker pool (default `BRIDGE_FEATURIZE_CONCURRENCY = 4`). Submission rules:

- A second enqueue for the same `job_id` while a task already exists is a no-op — concurrent requests share state via the DB row and all return 503 until completion.
- The pool serializes featurization work across jobs so a cascade-driven re-featurization of 1000 jobs doesn't open 1000 concurrent extractor connections.

Restart recovery: the in-memory map is reconstructed from the DB on startup (§4.10). `state='featurizing'` rows whose `started_at < now() - STALE_TASK_MS` (default 10 min) are reset to `state='featurizing', progress=0` and re-enqueued — startup treats them identically to never-started rows. Distinguishing "stuck" from "just slow" is the timeout's job; the recovery path is uniform.

### 4.4 Featurization task body

```
featurize(job_id):
    records = load extractor_results for job_id
    refs = unique union of (record.cover_image, record.images[]) across all records

    # Phase 1: compute per-(image, channel) features
    # Asset fetches inside this loop bounded by per-job semaphore (default 8)
    # so a single job can't exhaust extractor connections.
    for each ref (concurrent up to per-job limit):
        bytes = fetch_asset(job_id, ref)
        if not bytes: continue (filter §13: 404)
        for each channel in [phash, color_hist, tone]:
            feature, quality = compute_channel(bytes, channel)
            if feature is None: continue (filter §13: low-variance / degenerate)
            store in image_features
        update progress

    # Phase 2: per-channel corpus statistics
    for each channel:
        baseline = empirical_baseline(features_for_channel)   # see §4.5
        store in corpus_stats

    # Phase 3: per-record-image uniqueness
    for each channel:
        for each ref:
            c_i = uniqueness_score(ref, channel)               # see §4.6
            store in image_uniqueness

    mark job_feature_state = 'ready' (progress=1, finished_at=now)
```

`progress` is updated after each ref completes (Phase 1) and at phase boundaries (Phase 2/3 set progress to 0.85, 0.95 respectively). Used both for status endpoint reporting and for `Retry-After` estimation.

### 4.5 Empirical baseline computation

Per channel, per job: sample non-matching record-image pairs from within the job's record set (random pairs of images from _different_ records — same-record pairs are excluded as potentially related). Compute the mean similarity. Sample size: `min(1000, all_pairs)`.

Why sample from records-only (not records vs. Stash)? The baseline is a property of the metric+content distribution, and the record images are the relevant population. Stash-side mixing would couple baselines to per-user library composition and complicate cache invalidation.

### 4.6 Uniqueness (c_i) computation

For each record image `ref` in a job, for each channel: count how many _other records_ in the job contain a near-duplicate image (similarity ≥ `uniqueness_threshold`, e.g. 0.85). Then:

```
c_i = 1 / (1 + α * matches_in_other_records)         # α from config, default 1.0
```

| matches | c_i (α=1) |
|---------|-----------|
| 0       | 1.00      |
| 1       | 0.50      |
| 2       | 0.33      |
| 4       | 0.20      |

Bounded `(0, 1]`. **Smoothly decays, never zero** — a non-unique record image still contributes evidence, just at reduced weight, so the matcher doesn't lose information when records share generic images.

Why not IDF (`log(N/n)/log(N)`)? Records typically have N ≤ 5 images. IDF collapses too fast at small N: at N=4 with 1 match, `c_i ≈ 0.14`; at N=2 with 1 match, `c_i = 0`. The smoothed reciprocal is robust to small-N regimes and exposes a single tunable (`α`) instead of N-dependent behavior.

This is a near-duplicate count, not exact-equal — done with the channel's own similarity metric. Computed once per (job, channel); stored in `image_uniqueness`.

### 4.7 Status endpoints

Per-job status:

```
GET /api/extraction/{job_id}/features
→ 200 {
    "state": "ready" | "featurizing" | "failed",
    "progress": 0.73,
    "started_at": "2026-04-28T10:15:22Z",
    "finished_at": null,
    "error": null
  }
```

Fleet-level status (useful at startup and during cascade-driven re-featurization):

```
GET /api/featurization/status
→ 200 {
    "queued": 12,            # state='featurizing', progress=0
    "in_progress": 4,        # state='featurizing', progress>0 (== concurrency limit at peak)
    "ready": 847,
    "failed": 3,
    "concurrency_limit": 4
  }
```

Both are ops + debugging. Not part of the scraper contract.

### 4.8 Cascade invalidation (extends §7 of `CLAUDE.md`)

When `extractor_jobs.completed_at` advances:

```
ATOMIC TRANSACTION:
    DELETE FROM extractor_results       WHERE job_id = ?
    DELETE FROM image_features          WHERE source = 'extractor_image'
                                          AND ref_id LIKE ? || ':%'
    DELETE FROM corpus_stats            WHERE job_id = ?
    DELETE FROM image_uniqueness        WHERE job_id = ?
    DELETE FROM match_results           WHERE job_id = ?
    DELETE FROM job_feature_state       WHERE job_id = ?
    INSERT new extractor_jobs row
    INSERT new extractor_results rows
COMMIT
```

Stash-side `image_features` rows (source = `'stash_cover'` | `'stash_sprite'`) are _not_ purged — those are keyed by Stash content fingerprint, not by job, and remain valid across job changes.

After commit, the cascade trigger **enqueues a new featurization task for the job** (state=`'featurizing'`, progress=0). The bridge does not wait for the next request to discover the invalidation; it self-heals.

### 4.9 Batch scrape behavior (documented, not a bug)

When a Stash batch scrape queues multiple scrape requests against a job whose features aren't `ready`:

- The first request to observe a non-`ready` state enqueues featurization (no-op if already enqueued).
- All requests in the batch return `503 Service Unavailable` with `Retry-After` until the task finishes.
- Stash batch scrape may or may not respect `Retry-After`; if not, the bridge returns 503 for the duration of featurization. **These appear as errors in the Stash log, not as zero-result responses.** Users investigating "no results" should check for 503 errors in `~/.stash/logs/`.
- Once featurization completes, subsequent scrapes (including Stash retries) succeed normally.

In the typical case, eager-at-startup (§4.10) means the bridge is already `ready` for known jobs by the time scrapes hit it — this 503 window is bounded to cold-start, cascade-invalidation, and never-seen-job cases.

### 4.10 Startup-time featurization

On bridge container startup, after `init_db()`:

```
on startup:
    # 1. Reset stale 'featurizing' rows interrupted by the previous shutdown
    UPDATE job_feature_state
       SET state='featurizing', progress=0, started_at=now(), error=NULL
     WHERE state='featurizing' AND started_at < now() - STALE_TASK_MS

    # 2. Discover all jobs that are not yet 'ready'
    rows = SELECT j.job_id
             FROM extractor_jobs j
        LEFT JOIN job_feature_state f USING (job_id)
            WHERE f.state IS NULL OR f.state != 'ready'

    # 3. Insert/update them as queued (state='featurizing', progress=0)
    for row in rows:
        UPSERT job_feature_state(job_id=row.job_id,
                                 state='featurizing',
                                 progress=0,
                                 started_at=now())

    # 4. Enqueue all of them for the worker pool (bounded by concurrency limit)
    for row in rows:
        worker_pool.submit(featurize_task(row.job_id))

    # bridge starts accepting requests immediately;
    # un-ready jobs return 503 until their task completes
```

Properties:

- **Idempotent** — re-running yields the same state. Restart loops don't corrupt the queue.
- **Fairness vs. fresh requests** — the worker pool is FIFO. A new extractor job arriving mid-startup queue waits behind the boot-time backlog. Acceptable; the alternative (priority-jumping) would let cold-start traffic starve recovery.
- **No `partial` state on startup interrupt** — Phase 1 of `featurize_task` writes per-image features to `image_features` as it goes; on interrupt + restart, the next task run reads what's already cached and only fetches missing refs. No work lost.
- **`failed` jobs auto-retry on next startup** — the `state != 'ready'` predicate includes them. If repeatedly failing (e.g., extractor permanently unreachable), they cycle through retry without blocking other jobs (worker pool is per-job, not per-state).

---

## 5. CLAUDE.md §13 rewrite

The full replacement for the current §13. Lands in `CLAUDE.md` when this proposal is implemented.

> ## 13. Image scoring is multi-channel; channels compose by `max + bonus`, never by averaging or product
>
> > **Three channels evaluate the (scene, record) pair independently. Each produces a score in [0, 1]. The composite is `max(fired_channels) + bonus_per_extra_firing_channel`, capped at 1.0. The threshold in `config.py` gates the composite, not individual channels and not individual pair sims.**
>
> Single-channel pHash matching has a structural failure mode: it works for visually monolithic content (one stable subject) and degrades to noise the moment a video has scene changes, lighting variation, or varied subjects — exactly the population the bridge exists to serve. No aggregation algebra over a single channel rescues this. The fix is structural: provide complementary signals (chromatic, tonal) that survive what pHash misses, and compose them as union-of-evidence, mirroring §12's filename pattern.
>
> ### The three channels
>
> | Channel                                   | What it catches                                    | What it misses                                                   |
> | ----------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------- |
> | **A — pHash per-frame**                   | Direct structural match (DCT-based).               | Scene variation, lighting, varied subjects.                      |
> | **B — color histogram, scene-aggregate**  | Whole-scene chromatic profile, robust across cuts. | Records sharing generic palettes (sepia, low-light, monochrome). |
> | **C — low-res tone (8×8 gray) per-frame** | Coarse composition/luminance.                      | High-frequency texture.                                          |
>
> ### Within-channel scoring (per channel, applied independently)
>
> For frame-level channels (A, C): per record image `i`, take `m_i = max_j sim(stash_j, ext_i)`. For aggregate channels (B): one similarity `s_B` between Stash-side and extractor-side aggregate features. Then in both:
>
> 1. **Sharpen**: `m_i' = max(0, (m_i - baseline) / (1 - baseline))^γ` with γ from config (default 2). Subtracts the per-channel noise floor; gamma suppresses fuzzy near-baseline matches.
> 2. **Weight by quality + uniqueness**: `w_i = q_i * c_i`. `q_i` is intrinsic, channel-specific: pHash and tone use `sqrt(grayscale_entropy_norm * variance_norm)`; color histogram uses `1 - gini(hist_bins)`. `c_i` is corpus-relative, computed as the smoothed reciprocal `1 / (1 + α * matches_in_other_records)` with α from config (default 1.0). Stash-side `c_i = 1`.
> 3. **Evidence-union** (frame-level channels): `E = 1 - Π(1 - w_i * m_i')`. Soft-OR is now safe because `m_i'` is post-sharpening and `w_i` suppresses low-quality / non-unique images — the single-outlier saturation that broke the old top-K-mean model is gated away by the weights.
> 4. **Count saturation**: `count_conf = 1 - exp(-Σw_i / k)` with `k` from config (default 2.0). A record with N=1 image earns less than N=10 with the same evidence — sparse evidence is less reliable.
> 5. **Distribution shape**: `dist_q = 0.5 + 0.5 * normalized_entropy(m_i')`. Broad coverage outranks single spikes.
> 6. **Channel score**: `S_channel = E * count_conf * dist_q`. For aggregate channel B (no distribution): `S_B = m_B' * q_B`. Channel B's aggregates are computed via per-bin median across frames (see `MULTI_CHANNEL_SCORING.md` §3.1).
>
> ### Cross-channel composition
>
> ```
> fired = [S for S in (S_A, S_B, S_C) if S >= min_contribution]
> composite = min(1.0, max(fired) + bonus_per_extra * (len(fired) - 1))
> ```
>
> This is the §12 filename pattern: union-of-evidence with a structured bonus for corroboration. Each channel weak alone, union strong.
>
> ### Threshold gates the composite
>
> - **Scrape**: `composite >= threshold` → image tier fires. Otherwise, falls through.
> - **Search**: `composite` contributes to the rank score; no threshold gate.
>
> ### Filtering — must happen before any aggregation
>
> The §13 filtering rules (404, low-variance, degenerate-hash) still apply. They run per-image per-channel, and a filter failure on one channel does not exclude the image from other channels — channel A might filter a frame for low pHash variance while channel B still uses its color histogram.
>
> ### Featurization lifecycle
>
> Per-record features and per-job corpus statistics (baselines, uniqueness) are computed eagerly at container startup for any job not in `ready` state, and re-computed on cascade invalidation (`completed_at` advance) and on first request for never-seen jobs. The bridge returns `503 Service Unavailable` + `Retry-After` for any non-`ready` job — the hot path is `ready` → 200, everything else → 503. Single in-flight task per `job_id`; bounded global concurrency (`BRIDGE_FEATURIZE_CONCURRENCY`). See `MULTI_CHANNEL_SCORING.md` §4 for the state machine.
>
> ### Don'ts
>
> - **Don't** revert to single-channel scoring "because it's simpler" — single-channel was the bug.
> - **Don't** compose channels by mean or product. Mean dilutes a strong channel's signal; product zeros the composite when one channel doesn't fire.
> - **Don't** introduce per-channel thresholds. The threshold is on the composite. Per-channel `min_contribution` is a _firing-detection_ parameter for the bonus, not a gate on the channel's output.
> - **Don't** populate Stash-side `c_i`. There is no Stash corpus that meaningfully participates in IDF; using `c_i = 1` is correct for the Stash side and any other choice introduces hard-to-debug coupling between user library composition and match scores.
> - **Don't** featurize synchronously inside the request hot path. The 503/Retry-After protocol is the contract; eager-at-startup absorbs the latency cost out of band.
> - **Don't** use IDF (`log(N/n)/log(N)`) for `c_i`. Records typically have N ≤ 5; IDF collapses too fast at small N (at N=2 with 1 match, c_i = 0). The smoothed reciprocal is the canonical form.

---

## 6. Storage sizing

### 6.1 Per-image, per-channel

| Channel                          | Algorithm              | Blob size | + metadata overhead      |
| -------------------------------- | ---------------------- | --------- | ------------------------ |
| pHash                            | `phash:8`              | 8 B       | ~32 B                    |
| Color histogram                  | `color_hist:hsv:4x4x4` | 64 B      | ~32 B                    |
| Low-res tone                     | `tone:gray:8x8`        | 64 B      | ~32 B                    |
| **Per-image total (3 channels)** |                        | **136 B** | **~96 B** = ~232 B/image |

### 6.2 Per-job (extractor side)

| Quantity                | Calc                           | Total       |
| ----------------------- | ------------------------------ | ----------- |
| Records per job (avg)   | given                          | 1,000       |
| Images per record (avg) | given                          | 5           |
| Features per record     | 5 × 232 B                      | ~1.2 KB     |
| Features per job        | 1,000 × 1.2 KB                 | **~1.2 MB** |
| `image_uniqueness` rows | 1,000 × 5 × 3 channels × ~64 B | **~960 KB** |
| `corpus_stats` rows     | 3 channels × ~64 B             | negligible  |
| **Per-job total**       |                                | **~2.2 MB** |

### 6.3 Per-bridge-instance (extractor side)

| Quantity            | Calc           | Total       |
| ------------------- | -------------- | ----------- |
| Jobs (typical user) | given          | 100         |
| Jobs (heavy user)   | given          | 1,000       |
| Storage (typical)   | 100 × 2.2 MB   | **~220 MB** |
| Storage (heavy)     | 1,000 × 2.2 MB | **~2.2 GB** |

Acceptable for v1. SQLite handles this volume comfortably. If the heavy-user storage becomes a problem, we have headroom in the channel B histogram bin scheme (drop to `hsv:3x3x3` = 27 B at minor accuracy cost).

### 6.4 Stash side (per-scene, on-demand cache, unchanged in lifecycle)

| Quantity           | Calc                | Total       |
| ------------------ | ------------------- | ----------- |
| Frames per scene   | 1 cover + 81 sprite | 82          |
| Features per scene | 82 × 232 B          | ~19 KB      |
| 10K scenes         | 10,000 × 19 KB      | **~190 MB** |

LRU eviction on Stash-side rows when total exceeds a configurable budget (default 1 GB). Eviction key: `last_accessed_at` (new column on `image_features` for Stash-side rows; not needed for extractor rows since they're job-cascade-bound).

### 6.5 Compression knobs available without re-architecture

- Channel B bin scheme: `hsv:8x8x8 → 4x4x4 → 3x3x3` halves storage twice
- Quantize tone channel to 4-bit (32 B per blob, half size)
- Drop channels per-job if their `S_channel` distribution is degenerate (config flag)

---

## 7. Open questions

| #        | Question                                                                                            | Why it matters                                                                           | Disposition                                                                                                                                                          |
| -------- | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **7.1**  | Stash-side image uniqueness — should sprite-frame IDF be computed across the Stash library?         | Catches "dark hallway scene shared across many videos" false positives.                  | **Defer.** Second-order. Revisit only if such failures appear in production logs.                                                                                       |
| **7.2**  | `c_i` formula — IDF vs. softer alternatives.                                                        | IDF collapses at small N (records typically ≤ 5 images).                                 | **Resolved: smoothed reciprocal `1 / (1 + α * matches)` with α=1 default.** Bounded (0,1], smooth, never zero. See §4.6.                                                |
| **7.3**  | Channel weights in cross-channel composition — should `max(channels)` be `max(weighted)`?           | Some channels may be more reliable than others; equal max may bias.                      | **Defer.** Start unweighted; if calibration shows systematic bias, add per-channel weights to scraper config.                                                           |
| **7.4**  | `q_i` feature set — what feeds the intrinsic quality score?                                         | More features = more accurate but more compute. Start simple.                            | **Resolved: per-channel formulas in §3.6.** pHash + tone use `sqrt(entropy_norm * variance_norm)`; color hist uses `1 - gini(bins)`. Edge density (Sobel) deferred.     |
| **7.5**  | Channel B aggregation method — mean of histograms, median, or robust statistic?                     | Mean is sensitive to outlier frames (e.g., black frames). Median is more expensive.      | **Resolved: per-bin median across frames** (re-normalized). Robust to fade transitions and blank placeholders without depending on §13 binary filters being perfect.    |
| **7.6**  | Sprite-frame near-duplicate dedup before hashing — adjacent frames are often identical-ish.         | Reduces wasted compute and noise in M-dimension.                                         | Ship without dedup in v1. Re-evaluate after observing real M-distributions.                                                                                             |
| **7.7**  | Re-featurization on `algorithm` change in config — full purge, or version per-(channel, algorithm)? | If user changes `hash_size` or bin count, do we keep both?                               | **Version per-(channel, algorithm).** Old features remain on disk; new algorithm computes fresh. Eviction picks up old rows via LRU.                                    |
| **7.8**  | Calibration corpus — how do we acquire labeled known-good and known-bad pairs?                      | Required to set defaults for γ, k, α, `min_contribution`, `bonus_per_extra`.             | **Resolved: best-guess defaults with calibration-driven tuning later.** Defaults shipped as listed in §3 and §4. Phase 4–5 acceptance gates depend on label collection. |
| **7.9**  | Synchronous block budget for fast featurization?                                                    | Hide latency on cheap jobs without 503 round-trips.                                      | **Resolved: dropped.** Eager-at-startup means hot-path almost never sees a non-`ready` job; the narrow window where it does is correctly a 503. Removes a config knob.  |
| **7.10** | Failure mode: extractor unreachable mid-featurization.                                              | Some refs fetch, others 404. Do we mark `failed` or `partial`?                           | **Treat as `failed`**; on retry, the cache hits the already-computed refs. No `partial` state needed.                                                                   |
| **7.11** | Boot-time backlog vs. fresh requests — fairness.                                                    | Fresh extractor jobs may wait behind 1000-job recovery queue.                            | FIFO worker pool is the v1 answer. A priority bump for "request-driven" featurization is possible but adds queue complexity; defer until observed.                      |
| **7.12** | Project-level risk — calibration corpus is load-bearing.                                            | Phase 4 → 5 acceptance gates can't be measured without labeled pairs.                    | **Project risk, not a per-question item.** Recommend starting label collection in parallel with Phase 1–2 implementation.                                               |

---

## 8. Migration plan

Phased rollout. Each phase is independently revertable.

### Phase 1 — Schema migration (additive, no behavior change)

- Add `image_features`, `corpus_stats`, `image_uniqueness`, `job_feature_state` tables.
- Keep `image_hashes` in place; do not delete.
- Code change: `cache/db.py` SCHEMA additions only.
- Risk: low. Read paths still hit `image_hashes`.

### Phase 2 — Channel A via new schema (parity test)

- Implement pHash compute via `image_features` (channel = `'phash'`).
- Dual-write: every pHash compute writes to _both_ `image_hashes` and `image_features`.
- Read from `image_features` first; fall back to `image_hashes` on miss.
- Validate: hash values match between tables for the same `(source, ref_id, fingerprint)`.
- Risk: low. Single channel, no scoring change yet.

### Phase 3 — Featurization lifecycle (still single-channel)

- Implement `job_feature_state` machine, worker pool with `BRIDGE_FEATURIZE_CONCURRENCY`, 503/Retry-After handler.
- Implement startup-time featurization scan (§4.10) and cascade re-enqueue (§4.8).
- Featurization computes channel A only; computes `corpus_stats` baseline and `image_uniqueness` for channel A.
- Scoring still uses old top-K-mean (no within-channel formula yet); `q_i`, `c_i`, baselines computed but unused.
- Risk: medium. New request flow + new background worker pool; verify under load that single-in-flight semantics hold and startup queue drains cleanly.
- Rollback: feature flag `BRIDGE_LIFECYCLE_ENABLED=false` reverts to on-demand caching against `image_hashes`.

### Phase 4 — Within-channel scoring (channel A only)

- Replace `_top_k_mean` with the within-channel formula from §3.2 — but only channel A in play.
- New scraper config fields shipped: `image_gamma`, `image_count_k`, `image_uniqueness_alpha`. Bridge returns 400 if missing.
- Scraper bumps version to require these fields.
- §13 partially rewritten: single-channel form acknowledged as transitional.
- Risk: medium. Scoring change is observable in match results.
- Rollback: feature flag `BRIDGE_NEW_SCORING_ENABLED=false` reverts to top-K-mean. Both formulas coexist in code.
- Acceptance: false-positive rate (against labeled corpus) decreases; recall does not drop more than 5%.

### Phase 5 — Add channels B and C; cross-channel composition

- Implement channel B (color histogram, scene-aggregate) and channel C (low-res tone).
- Implement `max + bonus` composition.
- New scraper config fields: `image_channels`, `image_min_contribution`, `image_bonus_per_extra`.
- Full §13 rewrite lands.
- Risk: high. Most behavior change concentrated here.
- Rollback: `image_channels = ["phash"]` in scraper config reverts to channel-A-only behavior. Math composes correctly with one channel.
- Acceptance: false-positive rate decreases further; recall does not drop more than 5% from phase-4 baseline.

### Phase 6 — Stash-side multi-channel features

- Stash-side `image_features` rows extend to all three channels.
- Add LRU eviction on Stash-side rows (new `last_accessed_at` column).
- Risk: medium. Storage growth on Stash side; verify eviction.
- Rollback: feature flag to disable channels B/C on Stash side; falls back to phase-5 behavior with single-channel Stash hashing (still works because per-channel scoring tolerates missing channels).

### Phase 7 — Retire `image_hashes`

- Stop dual-writing.
- DROP TABLE `image_hashes` after one stable release window.
- Risk: low. By this point all reads have moved to `image_features`.

### Acceptance gates between phases

| Gate        | Criterion                                                                               |
| ----------- | --------------------------------------------------------------------------------------- |
| Phase 2 → 3 | Parity test passes for ≥ 7 days against a sample of 100 known-good scrapes.             |
| Phase 3 → 4 | Lifecycle handles batch scrape of 50 jobs without deadlock or stuck `featurizing` rows. |
| Phase 4 → 5 | False-positive rate decrease ≥ 20% on labeled corpus; recall regression ≤ 5%.           |
| Phase 5 → 6 | False-positive rate decrease ≥ 50% (cumulative from phase 0); recall regression ≤ 5%.   |
| Phase 6 → 7 | All `image_hashes` reads return cache misses for ≥ 7 days.                              |

### Calibration harness (used between phases 4 and 5)

A small offline script in `tests/calibration/`:

- Loads labeled `(scene_id, expected_record)` pairs from a JSONL fixture
- Runs each through the bridge with current config
- Reports per-pair: which channels fired, channel scores, composite, threshold pass/fail
- Aggregates: false-positive rate, false-negative rate, precision@1
- Optionally sweeps γ, k, `min_contribution` over a grid; reports best params

Not user-facing; ops-and-developer tooling.

---

## Appendix — terminology cross-walk

| Term in proposal | Term in `CLAUDE.md` (current)                  | Term in ChatGPT writeup       |
| ---------------- | ---------------------------------------------- | ----------------------------- |
| `m_i`            | per-extractor-image best similarity            | `m_i = max_j similarity(...)` |
| `m_i'`           | (not in current §13)                           | sharpened similarity          |
| `q_i`            | (binary filters in current §13)                | signal quality                |
| `c_i`            | (not present)                                  | uniqueness / coverage         |
| Channel A        | the entire image-matching pipeline             | (implicit single channel)     |
| Composite score  | `aggregate_search` / `aggregate_scrape` output | `final_score`                 |
| Featurization    | (no concept; on-demand caching)                | (no concept)                  |
| Worker pool      | (no concept)                                   | (no concept)                  |

---

_End of proposal._
