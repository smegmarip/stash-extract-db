# How to use `stash-extract-db`

A practical guide for the bridge's setup, featurization lifecycle, calibration, and verification. For *what* the bridge does, see [`README.md`](README.md). For *what must always be true*, see [`CLAUDE.md`](CLAUDE.md). For the multi-channel scoring design, see [`MULTI_CHANNEL_SCORING.md`](MULTI_CHANNEL_SCORING.md).

This document is task-oriented. If you only want to scrape one scene and verify it works, jump to **§5 Verifying matching works**. If you're trying to tune false-positive rate, **§7 Calibration**. If you're upgrading from the old pHash-only matcher, **§8 Migration order**.

---

## 1. Dependencies

| Component | Version | Notes |
|---|---|---|
| **Docker** + **docker compose v2** | recent | Ships and runs the bridge. |
| **Stash** | 0.27+ | Already running on the host. The bridge calls Stash's GraphQL. |
| **Site Extractor** | matching version | Runs alongside the bridge on the same Docker network (default name `extractor_network`). |
| **Python** (for the scraper) | 3.8+ | The Stash scraper script is stdlib-only — no `pip install` needed beyond what the scraper requirements file pins. |
| **`sqlite3` CLI** (optional) | any | Useful for inspecting the bridge's cache during troubleshooting. |

The bridge image (`Dockerfile`) installs its own Python deps (FastAPI, Pillow, imagehash, numpy, rapidfuzz, etc.) — you don't need a host-side Python venv to run it.

---

## 2. First-time setup

### 2.1 Create the shared Docker network

The bridge talks to the extractor over a user-defined Docker network. Create it once:

```bash
docker network create extractor_network 2>/dev/null || true
```

If you renamed the network (`DOCKER_NETWORK` in `.env`), use that name instead.

### 2.2 Configure the bridge

```bash
cp .env.example .env
$EDITOR .env
```

Minimum required variables:

| Variable | Notes |
|---|---|
| `STASH_URL` | Where Stash is reachable from inside the bridge container. On Docker Desktop, `http://host.docker.internal:9999` is the default and works out of the box. |
| `STASH_API_KEY` *or* `STASH_SESSION_COOKIE` | Whichever your Stash auth uses. Leave blank if Stash is on localhost without auth. |
| `EXTRACTOR_URL` | Defaults to `http://extractor-gateway:12000` — works if both containers share `extractor_network`. |
| `DATA_PATH` | Host directory mounted at `/data` in the container. Holds the SQLite cache. Default `./data` (created automatically). |

Leave the `BRIDGE_*` toggle variables at their defaults for first install — they default to the safe (Phase 0) behavior. You'll flip them in §8.

### 2.3 Bring up the bridge

```bash
docker compose up -d --build
curl http://localhost:13000/health   # → {"status":"ok"}
```

If the health check fails, `docker compose logs stash-extract-db` is the first place to look.

### 2.4 Install the Stash scraper

Stash loads scrapers from `~/.stash/scrapers/`. Adjust the path if your Stash installation puts them elsewhere.

```bash
cp -r stash-extract-scraper/ ~/.stash/scrapers/stash-extract-scraper/
$EDITOR ~/.stash/scrapers/stash-extract-scraper/config.py
# set BRIDGE_URL to where the bridge is reachable from Stash:
#   - Stash in Docker:    "http://host.docker.internal:13000"
#   - Stash native host:  "http://localhost:13000"
```

The scraper script is stdlib-only, but its `requirements.txt` exists for parity with environments that pin transitive dev tooling. Install if you want:

```bash
pip install -r ~/.stash/scrapers/stash-extract-scraper/requirements.txt
```

### 2.5 Reload Stash scrapers

In Stash: **Settings → Scrapers → Reload Scrapers**. The bridge's scraper should appear under the **Scrape with…** action menu on individual scenes.

---

## 3. Configuring matching parameters

All matching parameters originate in the scraper's `config.py` per [`CLAUDE.md`](CLAUDE.md) §1 — the bridge has no fallbacks. Open `~/.stash/scrapers/stash-extract-scraper/config.py`:

| Setting | What it controls | Typical value |
|---|---|---|
| `BRIDGE_URL` | Where the scraper finds the bridge. | `http://host.docker.internal:13000` |
| `IMAGE_MODE` | `cover` (Stash screenshot only), `sprite` (sprite frames), or `both` (union). | `cover` is fast; `both` is slowest but most accurate. |
| `IMAGE_THRESHOLD` | Image-tier firing threshold in scrape mode. **Calibrate per scoring path** — see §7. | `0.7` (legacy), `0.05–0.15` (new scoring) |
| `SEARCH_LIMIT` | Top-N to return in search mode. | `5` |
| `HASH_ALGORITHM`, `HASH_SIZE` | Perceptual hash algorithm + bit size. | `"phash"`, `8` (must match `BRIDGE_FEATURIZE_*` until Phase 5 — see §6.5) |
| `SPRITE_SAMPLE_SIZE` | How many sprite frames to hash per scene in `sprite`/`both` modes. | `8` |
| `IMAGE_GAMMA`, `IMAGE_COUNT_K`, `IMAGE_UNIQUENESS_ALPHA` | New-scoring within-channel tunables (§7). Sent only when `BRIDGE_NEW_SCORING_ENABLED=true` on the bridge. | `2.0`, `2.0`, `1.0` |
| `IMAGE_CHANNELS` | List of channels to evaluate. `["phash"]` keeps Phase 4 single-channel behavior; `["phash","color_hist","tone"]` enables Phase 5 multi-channel composition. | Full list |
| `IMAGE_MIN_CONTRIBUTION`, `IMAGE_BONUS_PER_EXTRA` | Cross-channel composition tunables (§7). Required when `IMAGE_CHANNELS` has more than `phash`. | `0.3`, `0.1` |
| `REQUEST_TIMEOUT_S` | How long the scraper waits for the bridge. | `90` |

Changes to `config.py` take effect immediately on the next scrape — no Stash reload needed.

---

## 4. Featurization

### 4.1 What featurization is

When `BRIDGE_LIFECYCLE_ENABLED=true`, the bridge precomputes per-image features (pHash + planned color hist + planned tone) and per-job statistics (noise floor, uniqueness) for every extractor job's record set. Without this, scoring computes everything on-demand per request and the new formula can't reach its full effectiveness because `c_i` (uniqueness) requires corpus-level knowledge.

### 4.2 When featurization runs

Three triggers, all using the same worker pool:

1. **Container startup.** The bridge scans `extractor_jobs` ⨝ `job_feature_state` and enqueues every job not in `ready` state.
2. **Cascade invalidation.** When the extractor's `completed_at` advances for a job, the cascade clears the old features and enqueues a re-featurize.
3. **Never-seen job.** A `/match/*` request whose candidate set includes a job with no `job_feature_state` row enqueues that job and returns `503 + Retry-After`.

### 4.3 Enable it

In `.env`:

```bash
BRIDGE_LIFECYCLE_ENABLED=true
```

Restart the bridge:

```bash
docker compose up -d --force-recreate stash-extract-db
docker compose logs -f stash-extract-db
```

You should see lines like:

```
startup_recover: enqueuing 47 jobs for featurization
featurization complete: job=abc123
featurization complete: job=def456
...
```

### 4.4 Monitor progress

```bash
# Fleet-level
curl -s http://localhost:13000/api/featurization/status | jq
# {
#   "queued": 12,
#   "in_progress": 4,
#   "ready": 31,
#   "failed": 0,
#   "concurrency_limit": 4,
#   "lifecycle_enabled": true
# }

# Per job
curl -s http://localhost:13000/api/extraction/<job_id>/features | jq
# {
#   "state": "featurizing",
#   "progress": 0.62,
#   "started_at": "2026-04-28T12:00:00Z",
#   "finished_at": null,
#   "error": null
# }
```

### 4.5 Tuning concurrency

If featurization saturates the extractor's connections (you'll see slow extractor responses or timeouts in the log), lower `BRIDGE_FEATURIZE_CONCURRENCY` from 4 to 2 and restart. Conversely, on a fast extractor with many small jobs, raise it to 8.

`BRIDGE_FEATURIZE_PER_JOB_CONCURRENCY` (default 8) is the per-job parallel asset fetch budget. Lower if a single big job is too aggressive.

### 4.6 Force re-featurization for one job

You can drop the per-job state row and the bridge will pick it up on the next request:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db \
  "DELETE FROM job_feature_state WHERE job_id = '<job_id>';"
```

The next `/match/*` request that touches that job will return 503 once and the worker will start.

To wipe everything and re-featurize from scratch:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db \
  "DELETE FROM job_feature_state; DELETE FROM corpus_stats; DELETE FROM image_uniqueness; \
   DELETE FROM image_features WHERE source IN ('extractor_image', 'extractor_aggregate');"
docker compose restart stash-extract-db
```

### 4.7 Troubleshooting failed jobs

If a job lands in `state='failed'`:

```bash
curl -s http://localhost:13000/api/extraction/<job_id>/features | jq .error
```

Most failures come from:
- **Extractor unreachable** mid-run → fix connectivity, restart bridge (`startup_recover` re-enqueues).
- **All record images 404** → no usable hashes; the job is genuinely unmatchable on images.
- **Corrupt asset bytes** → the bridge logs `featurize: hash failed job=… ref=…` per offending ref.

A `failed` job auto-retries on the next bridge boot or the next `/match/*` request that includes it.

---

## 5. Verifying matching works

### 5.1 Smoke-test from inside Stash

Pick a known scene where you remember the correct extractor record. In Stash:

1. Open the scene.
2. **Edit → Scrape with → Stash Extract**.
3. The dialog should show the matched record's title, date, performers, and cover image. Confirm or cancel.

If the dialog is empty when you expected a match, jump to §5.3 and §7.

### 5.2 Smoke-test the bridge directly

This bypasses Stash entirely and is useful when the Stash UI is unhelpful:

```bash
# Use a scene_id you know exists in Stash
curl -s -X POST http://localhost:13000/match/fragment \
  -H 'Content-Type: application/json' \
  -d '{
    "scene_id": "<stash_scene_id>",
    "mode": "search",
    "image_mode": "cover",
    "threshold": 0.05,
    "limit": 5,
    "hash_algorithm": "phash",
    "hash_size": 8,
    "sprite_sample_size": 8,
    "image_gamma": 2.0,
    "image_count_k": 2.0,
    "image_uniqueness_alpha": 1.0
  }' | jq
```

Returns a ranked list of candidates with `match_score`. The top result should be the correct record.

### 5.3 Inspect why something matched (or didn't)

Append `?debug=1` to the URL in search mode (scrape mode returns one record or empty by contract):

```bash
curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
  -H 'Content-Type: application/json' \
  -d '{ /* same body as above */ }' | jq '.[0]._debug'
```

You'll get a per-candidate breakdown. Phase 5 multi-channel example:

```json
{
  "studio_code": false,
  "exact_title": false,
  "image": {
    "mode": "cover",
    "scoring": "new (multi-channel phash,color_hist,tone)",
    "channels": {
      "phash": {
        "S": 0.71, "E": 0.85, "count_conf": 0.93, "dist_q": 0.90,
        "baseline": 0.5208,
        "n_extractor_images": 5, "n_stash_hashes": 1,
        "extractor_refs": ["..."],
        "per_image_max": [0.95, 0.92, 0.4, 0.4, 0.4],
        "m_primes":      [0.96, 0.84, 0.0, 0.0, 0.0],
        "qualities":     [0.62, 0.58, 0.55, 0.61, 0.60],
        "uniquenesses":  [1.0, 1.0, 1.0, 0.5, 1.0]
      },
      "color_hist": {
        "S": 0.41,
        "m_prime": 0.55, "sim": 0.83, "quality": 0.74,
        "baseline": 0.61,
        "have_stash": true, "have_extractor": true
      },
      "tone": { /* same shape as phash */ }
    },
    "fired": ["phash", "color_hist"],
    "composite": 0.81
  },
  "image_contribution": 0.81,
  "filename": { /* ... */ },
  "raw_score": 1.94,
  "capped_score": 1.0
}
```

The fields under `image.channels.<name>` map directly to the formulas in [`MULTI_CHANNEL_SCORING.md`](MULTI_CHANNEL_SCORING.md) §3.2. They're the inputs to calibration (§7).

**Channel B (color_hist) baseline is typically much higher** than the others — random unrelated scenes often share ~70-90% histogram intersection because compressed JPEG/PNG distributions cluster. After sharpening, `S_B` ends up modest unless the scene/record share specific palette features. This is expected; the channel's value is in cross-channel corroboration via the bonus, not standalone.

### 5.4 Inspect the SQLite cache directly

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db
```

Useful queries:

```sql
.tables
-- corpus_stats         extractor_results    image_uniqueness   match_results
-- extractor_jobs       image_features       job_feature_state

.schema image_features

-- Featurization status across all known jobs
SELECT j.job_id, j.job_name, f.state, f.progress
  FROM extractor_jobs j LEFT JOIN job_feature_state f USING (job_id);

-- All pHash features for one extractor job
SELECT ref_id, length(feature_blob) AS size, quality, computed_at
  FROM image_features
 WHERE channel='phash' AND ref_id LIKE '<job_id>:%'
 ORDER BY ref_id;

-- Baseline per channel for each job
SELECT * FROM corpus_stats;

-- Uniqueness for one job
SELECT ref_id, uniqueness FROM image_uniqueness WHERE job_id='<job_id>' ORDER BY uniqueness;
```

---

## 6. Testing

There is no formal test suite (`tests/unit/`, `tests/integration/` are empty placeholders). Verification is end-to-end via the smoke commands above and manual inspection.

### 6.1 Sanity checks after a deploy

```bash
# 1. Bridge process up
curl -fs http://localhost:13000/health || echo "bridge not responding"

# 2. Stash GraphQL reachable from inside the container
docker exec stash-extract-db curl -fs "$STASH_URL/graphql" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"query":"query{ stats { scene_count }}"}' \
  | jq '.data.stats.scene_count // "unreachable"'

# 3. Extractor reachable from inside the container
docker exec stash-extract-db curl -fs "$EXTRACTOR_URL/health" \
  || echo "extractor unreachable"

# 4. SQLite cache file exists, non-empty
docker exec stash-extract-db ls -la /data/stash-extract-db.db
```

### 6.2 End-to-end test on a known scene

Pick three scenes you know well — one that should match record X, one that should match record Y, one that should match nothing (no extractor coverage).

```bash
for scene_id in 123 456 789; do
  echo "=== scene $scene_id ==="
  curl -s -X POST "http://localhost:13000/match/fragment?debug=1" \
    -H 'Content-Type: application/json' \
    -d "{
      \"scene_id\": \"$scene_id\",
      \"mode\": \"search\",
      \"image_mode\": \"cover\",
      \"threshold\": 0.05,
      \"limit\": 3,
      \"hash_algorithm\": \"phash\",
      \"hash_size\": 8,
      \"sprite_sample_size\": 8,
      \"image_gamma\": 2.0,
      \"image_count_k\": 2.0,
      \"image_uniqueness_alpha\": 1.0
    }" | jq '.[] | {title:.Title, score:.match_score, code:.Code}'
done
```

For each scene, check that the top result is the expected record.

### 6.3 Forcing a re-scrape after a config change

Stash caches scraper output per scene. To force the bridge to re-run after you changed `IMAGE_THRESHOLD` or another `config.py` value: in Stash, **Edit → Scrape with → Stash Extract** again on the scene. The scraper sends a fresh request to the bridge with the new params.

The bridge itself does not cache scraper *results* — `match_results` is keyed by `(scene_fingerprint, job_completed_at)`, which already invalidates correctly when the scene or job changes. You don't need to clear anything in the cache after a `config.py` change.

---

## 7. Calibration

The new scoring formula's score range is more compressed than the legacy top-K mean. Defaults that worked for the old formula will produce zero matches against the new one. Calibrate after flipping `BRIDGE_NEW_SCORING_ENABLED=true`.

### 7.1 What you're tuning

Six knobs control image-tier firing in scrape mode (four for within-channel scoring, two for cross-channel composition):

| Knob | In | What it does | Move it if… |
|---|---|---|---|
| `IMAGE_THRESHOLD` | `config.py` | Composite score required to fire scrape image tier. | False positives → raise. False negatives → lower. |
| `IMAGE_GAMMA` | `config.py` | Sharpening exponent on per-image similarities. Default 2.0. | Borderline-noise sims still firing → raise to 3.0. Real matches getting suppressed → lower to 1.5. |
| `IMAGE_COUNT_K` | `config.py` | Count-saturation k. Default 2.0. | Records with many images dominate → raise. Sparse records (N=1, 2) underweighted → lower to 1.0. |
| `IMAGE_UNIQUENESS_ALPHA` | `config.py` *and* `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA` in `.env` | `c_i = 1 / (1 + α·matches)`. | Logos/title cards still influencing matches → raise. (Set both to the same value and re-featurize per §4.6.) |
| `IMAGE_CHANNELS` | `config.py` | Which channels participate in composition. | Channel B/C noisy on your data → drop them temporarily by listing only `["phash"]`. |
| `IMAGE_MIN_CONTRIBUTION` | `config.py` | A channel's S must clear this to count as "fired" for the bonus. | Real channel contributions below 0.3 → lower to 0.1 (common for synthetic-looking record sets). Spurious channels firing → raise to 0.5. |
| `IMAGE_BONUS_PER_EXTRA` | `config.py` | Bonus added per additional firing channel. | Multi-channel agreement should dominate → raise to 0.2. Single-strongest-channel should win → lower to 0.05. |

### 7.2 Empirical workflow

You don't have a labeled corpus, so calibration is by observation.

**Step 1 — Pick a calibration set.** 10–20 scenes you know well. For each, write down the expected extractor record (or "no match"). Mix:
- Easy positives (perfect cover image match)
- Hard positives (only sprite frames or color match)
- Easy negatives (no extractor coverage at all)
- Hard negatives (similar-looking but wrong record — share studio, similar palette, etc.)

**Step 2 — Run them all in search mode with a permissive threshold.** Threshold of `0.001` means everything fires; you observe the actual composite scores.

```bash
# scenes.txt: one Stash scene_id per line
while read sid; do
  echo "=== $sid ==="
  curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
    -H 'Content-Type: application/json' \
    -d "{\"scene_id\":\"$sid\",\"mode\":\"search\",\"image_mode\":\"cover\",\"threshold\":0.001,\"limit\":5,\"hash_algorithm\":\"phash\",\"hash_size\":8,\"sprite_sample_size\":8,\"image_gamma\":2.0,\"image_count_k\":2.0,\"image_uniqueness_alpha\":1.0}" \
    | jq '.[] | {title:.Title, code:.Code, score:.match_score, image_S:._debug.image.composite}'
done < scenes.txt > calibration.log
```

**Step 3 — Find the gap.** Open `calibration.log`. For each scene:
- Note the `image_S` of the **correct** record (this is the lowest "must keep" score for that scene).
- Note the `image_S` of the **highest-scoring incorrect** record (this is the highest "must reject").

Across all scenes:
- `recall_floor` = min of correct scores → threshold below this loses matches.
- `precision_ceiling` = max of incorrect scores → threshold above this avoids false positives.

If `recall_floor > precision_ceiling`, set `IMAGE_THRESHOLD` between them — perfect separation. Done.

If `recall_floor < precision_ceiling`, the formula isn't separating cleanly on your data. Move on to §7.3.

**Step 4 — Set `IMAGE_THRESHOLD`** in `~/.stash/scrapers/stash-extract-scraper/config.py`. Re-scrape your calibration scenes; verify scrape mode now returns the right answers.

### 7.3 When the gap doesn't open

If correct and incorrect scores overlap, look at `_debug.image.channels.phash` for the worst-cases (lowest correct, highest incorrect):

| Pattern in debug | Likely cause | Tuning |
|---|---|---|
| Correct record's `m_primes` are all 0 (every per-image sim was below `baseline`) | Baseline is too high — possibly inflated by within-corpus near-duplicates | Re-featurize with a tighter `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` (e.g. `0.95`) so near-dup images contribute less to the random-pair sample. Or lower `IMAGE_GAMMA` to 1.5. |
| Correct `S` low, mostly because `count_conf` ≈ 0.3 | Records have too few images (N=1 or 2) and saturation is biting | Lower `IMAGE_COUNT_K` to 1.0 or 0.5. |
| Correct `S` low, `dist_q` = 0.5 (only 1 nonzero `m_prime`) | Only one image truly matches; rest are unrelated | Inherent in the data — you can't fix this without adding more matching evidence. Lower `IMAGE_THRESHOLD` instead. |
| Incorrect records have high `S` from one near-dup that's not unique to one record | `c_i` not penalizing the shared image enough | Raise `IMAGE_UNIQUENESS_ALPHA` to 2.0 (in both `config.py` *and* `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`). Re-featurize per §4.6. |
| Incorrect records have high `S` from sims at the noise floor that didn't get sharpened to 0 | Baseline too low | Raise `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` and re-featurize, OR raise `IMAGE_GAMMA` to 3.0 (sharper suppression at the same baseline). |

After each tuning change, re-run §7.2 step 2.

### 7.4 Rolling back to legacy scoring

If calibration isn't converging:

```bash
# In .env:
BRIDGE_NEW_SCORING_ENABLED=false
docker compose up -d --force-recreate stash-extract-db
```

Reset `IMAGE_THRESHOLD` in `config.py` to its old value (typically `0.7`). The legacy top-K-mean path takes over; no scraper restart needed.

`BRIDGE_LIFECYCLE_ENABLED` can stay `true` — featurization keeps running in the background, populating `image_features` for when you flip back. The legacy path also benefits from the precomputed pHash rows via Phase 2 dual-write.

---

## 8. Migration order (safe sequence)

If you're running this on a populated Stash + extractor today and want to roll forward without breaking anything:

```
Phase 0 (current default)       — both flags false. Behavior identical to pre-Phase-3.
       │
       ▼
Phase 3 (lifecycle on)          — BRIDGE_LIFECYCLE_ENABLED=true, BRIDGE_NEW_SCORING_ENABLED=false
                                  Featurization populates new tables in the background.
                                  Matching still uses legacy top-K-mean. No score changes.
                                  Watch for: 503s on first scrape after restart.
                                  Verify with: §4.4 status endpoints.
       │
       ▼
Phase 4 (new scoring on)        — Both flags true. Calibrate IMAGE_THRESHOLD per §7.
                                  Watch for: false positives, false negatives, threshold mismatch.
                                  Roll back with: BRIDGE_NEW_SCORING_ENABLED=false (no data loss).
       │
       ▼
Phase 5+ (channels B and C)     — Not implemented yet. Doc only.
```

Each step is independently revertable. **Don't skip Phase 3** — it populates the corpus stats the new scoring depends on. Skipping straight to Phase 4 means `c_i = 1` and `baseline = 0.5` (neutral defaults), which works but doesn't catch logo/duplicate-image false positives.

---

## 9. Where to look first when something is wrong

| Symptom | First place to check |
|---|---|
| `/health` returns nothing | `docker compose logs stash-extract-db` — usually a Stash or extractor URL misconfig |
| Stash dialog empty for a scene | §5.2 direct curl — does the bridge return `[]` or just nothing? |
| Wrong record returned | §5.3 `?debug=1`, then §7.3 to see which formula component is misbehaving |
| `503` errors in Stash log during batch scrape | Expected during first featurization wave — see [`MULTI_CHANNEL_SCORING.md`](MULTI_CHANNEL_SCORING.md) §4.9 |
| Featurization stuck at `progress: 0` | Bridge restart didn't see the row as stale yet — wait `BRIDGE_STALE_TASK_MS` (10 min default), or manually re-trigger per §4.6 |
| `400 Bad Request` from bridge | A scraper config field is missing — typically `IMAGE_GAMMA` etc. when `BRIDGE_NEW_SCORING_ENABLED=true`. Update `config.py` per §3 |
| Scoring same scene differently after restart | `corpus_stats` regenerated — baseline shifted slightly. Expected; absolute scores not stable across re-featurization, but ranks should be |
| Need to nuke everything and start fresh | `docker compose down -v` won't delete `./data/`; do `rm -rf ./data && docker compose up -d --build` |

For deeper architectural questions, [`CLAUDE.md`](CLAUDE.md) §14 has the symptom→file map for the matching engine itself.

---

## 10. Retiring the legacy `image_hashes` table

The bridge dual-writes pHash to both `image_hashes` (legacy) and `image_features` (new) for backward compatibility through Phase 6. Once you're confident in the new path, you can disable the legacy mirror in two steps:

### 10.1 Disable the dual-write (`BRIDGE_LEGACY_DUAL_WRITE_ENABLED=false`)

This stops new writes to `image_hashes` and removes the legacy fallback on read. Existing rows in `image_hashes` are no longer consulted but remain on disk.

```bash
# In .env:
BRIDGE_LEGACY_DUAL_WRITE_ENABLED=false
docker compose up -d --force-recreate stash-extract-db
```

**Safety check before flipping**: confirm that `image_features` has the pHash row for every Stash sprite frame and extractor image you care about. Any miss will trigger a re-fetch + re-compute on next request — slow, but not corrupting.

```bash
docker exec stash-extract-db sqlite3 /data/stash-extract-db.db <<'SQL'
-- Hash rows present in legacy but not in features (would re-compute on next request):
SELECT h.source, h.ref_id, h.algorithm, h.hash_size
  FROM image_hashes h
  LEFT JOIN image_features f
    ON f.source = h.source AND f.ref_id = h.ref_id
   AND f.channel = 'phash' AND f.algorithm = h.algorithm || ':' || h.hash_size
   AND f.fingerprint = h.fingerprint
 WHERE f.ref_id IS NULL
 LIMIT 20;
SQL
```

If that returns rows, leave the dual-write on a bit longer; the in-flight workers will eventually populate `image_features`. Or run a featurization pass: `docker compose restart stash-extract-db` — `startup_recover` will re-featurize any non-`ready` jobs and the cache will fill in.

### 10.2 Drop the table (manual, irreversible)

Once `image_hashes` has been unused for a stable period (a week is a reasonable window), drop it:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db <<'SQL'
DROP TABLE image_hashes;
VACUUM;  -- reclaims the space; otherwise the file stays the same size
SQL
```

After this, you cannot roll back to legacy scoring — the dual-write code is still there but has nothing to read from. Do this only when you've committed to the multi-channel scoring path. Re-enabling `BRIDGE_LEGACY_DUAL_WRITE_ENABLED=true` after a `DROP TABLE` will fail noisily on the next request that tries to read from `image_hashes`.

### 10.3 Rolling back

To revert before doing the `DROP TABLE`:

```bash
# In .env:
BRIDGE_LEGACY_DUAL_WRITE_ENABLED=true
docker compose up -d --force-recreate stash-extract-db
```

Dual-write resumes; `image_hashes` rows stay valid. New computes write to both tables again.

---

*Last updated against Phase 7 implementation (Phase 7.2 — soft retirement via flag). The actual `DROP TABLE image_hashes` is the user's call when they're confident.*
