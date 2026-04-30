# How to use `stash-extract-db`

A practical operator's guide for installing the bridge, configuring the matcher, and verifying it works. For _what_ the bridge does, see [`README.md`](../README.md). For _what must always be true_, see [`CLAUDE.md`](../CLAUDE.md). For testing strategy and calibration provenance, see [`TESTING.md`](TESTING.md) and [`calibration/`](calibration/).

This document is task-oriented. If you only want to scrape one scene and verify it works, jump to **§4 Verifying matching works**. If you're debugging a specific match outcome, **§5 Inspecting matches with `?debug=1`**. If you're trying to tune the matcher, **§6 Calibration**.

---

## 1. Dependencies

| Component                          | Version          | Notes                                                                                                             |
| ---------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------- |
| **Docker** + **docker compose v2** | recent           | Ships and runs the bridge.                                                                                        |
| **Stash**                          | 0.27+            | Already running on the host. The bridge calls Stash's GraphQL.                                                    |
| **Site Extractor**                 | matching version | Runs alongside the bridge on the same Docker network (default name `extractor_network`).                          |
| **Python** (for the scraper)       | 3.8+             | The Stash scraper script is stdlib-only — no `pip install` needed beyond what the scraper requirements file pins. |
| **`sqlite3` CLI** (optional)       | any              | Useful for inspecting the bridge's cache during troubleshooting.                                                  |

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

| Variable                                    | Notes                                                                                                                                                     |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `STASH_URL`                                 | Where Stash is reachable from inside the bridge container. On Docker Desktop, `http://host.docker.internal:9999` is the default and works out of the box. |
| `STASH_API_KEY` _or_ `STASH_SESSION_COOKIE` | Whichever your Stash auth uses. Leave blank if Stash is on localhost without auth.                                                                        |
| `EXTRACTOR_URL`                             | Defaults to `http://extractor-gateway:12000` — works if both containers share `extractor_network`.                                                        |
| `DATA_PATH`                                 | Host directory mounted at `/data` in the container. Holds the SQLite cache. Default `./data` (created automatically).                                     |

Set `BRIDGE_LIFECYCLE_ENABLED=true` and `BRIDGE_NEW_SCORING_ENABLED=true` to use the multi-channel scoring path described in [`CLAUDE.md`](../CLAUDE.md) §13–§14. Leave them `false` (default in `.env.example`) to run the legacy single-channel pHash path — useful as a rollback (§7).

### 2.3 Bring up the bridge

```bash
docker compose up -d --build
curl http://localhost:13000/health   # → {"status":"ok"}
```

If the lifecycle is enabled, watch featurization populate the cache for any extractor jobs already on the system:

```bash
docker compose logs -f stash-extract-db | grep -E 'startup_recover|featurization complete'
curl -s http://localhost:13000/api/featurization/status | jq
# {"queued": 12, "in_progress": 4, "ready": 31, "failed": 0, "concurrency_limit": 4, "lifecycle_enabled": true}
```

For a fleet of N jobs, expect `~30 seconds × N` of background work before all are `ready`. The bridge accepts requests immediately; matches against not-yet-`ready` jobs return `503 Service Unavailable + Retry-After` (CLAUDE.md §14). Stash retries them automatically per `Retry-After`.

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

In Stash: **Settings → Scrapers → Reload Scrapers**. The bridge's scraper appears under the **Scrape with…** action menu on individual scenes.

---

## 3. Configuring matching parameters

All matching parameters originate in the scraper's `config.py` per [`CLAUDE.md`](../CLAUDE.md) §1 — the bridge has no fallbacks. Open `~/.stash/scrapers/stash-extract-scraper/config.py`.

### 3.1 Connection + image fetch

| Setting                       | What it controls                                                              | Typical value                                                          |
| ----------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `BRIDGE_URL`                  | Where the scraper finds the bridge.                                           | `http://host.docker.internal:13000`                                    |
| `IMAGE_MODE`                  | `cover` (Stash screenshot only), `sprite` (sprite frames), or `both` (union). | `cover` is fast; `both` is slowest but most accurate.                  |
| `SEARCH_LIMIT`                | Top-N to return in search mode.                                               | `5`                                                                    |
| `HASH_ALGORITHM`, `HASH_SIZE` | Perceptual hash algorithm + bit size for channel A.                           | `"phash"`, `16`                                                        |
| `SPRITE_SAMPLE_SIZE`          | How many sprite frames to hash per scene in `sprite`/`both` modes.            | `8`                                                                    |
| `REQUEST_TIMEOUT_S`           | How long the scraper waits for the bridge.                                    | `90`                                                                   |

### 3.2 Image-tier scoring (multi-channel)

| Setting                  | What it controls                                                                  | Calibrated default | Adjust if…                                                            |
| ------------------------ | --------------------------------------------------------------------------------- | ------------------ | --------------------------------------------------------------------- |
| `IMAGE_THRESHOLD`        | Composite score required to fire image tier in scrape mode.                       | `0.7`              | Calibrate per corpus (§6).                                            |
| `IMAGE_GAMMA`            | Sharpening exponent on per-image similarities.                                    | `3.5`              | Borderline-noise sims firing → raise. Real matches suppressed → lower. |
| `IMAGE_COUNT_K`          | Count-saturation `k` in `1 - exp(-Σw / k)`.                                       | `0.25`             | Records with many images dominate → raise. Sparse-N losing → lower.   |
| `IMAGE_UNIQUENESS_ALPHA` | Smoothing factor in `c_i = 1 / (1 + α·matches)` at request time.                  | `1.0`              | Logos/title cards still influencing matches → raise.                  |
| `IMAGE_CHANNELS`         | Channels to evaluate.                                                             | `["phash", "color_hist", "tone"]` | Drop `"tone"` for ~33% per-query speedup on mixed-content corpora; tone is silenced by uniqueness collapse on those anyway. |
| `IMAGE_MIN_CONTRIBUTION` | A channel's S must clear this to count as "fired" for the cross-channel bonus.    | `0.05`             | Spurious channels firing → raise. Real channels never firing → lower. |
| `IMAGE_BONUS_PER_EXTRA`  | Bonus added per additional firing channel.                                        | `0.1`              | Multi-channel agreement should dominate → raise.                       |
| `IMAGE_SEARCH_FLOOR`     | Optional floor on image composite below which weak candidates are dropped from search results (definitive signals bypass). | `None` (disabled) | Sharper-corpus deployments may set 0.10–0.20.                          |

The defaults above are calibrated against a 491-video Pexels corpus — see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md) for provenance.

Changes to `config.py` take effect immediately on the next scrape — no Stash reload needed.

---

## 4. Verifying matching works

### 4.1 Smoke-test from inside Stash

Pick a known scene where you remember the correct extractor record. In Stash:

1. Open the scene.
2. **Edit → Scrape with → Stash Extract**.
3. The dialog shows the matched record's title, date, performers, and cover image. Confirm or cancel.

If the dialog is empty when you expected a match, jump to §5 and §6.

### 4.2 Smoke-test the bridge directly

Bypass Stash and curl the bridge:

```bash
curl -s -X POST http://localhost:13000/match/fragment \
  -H 'Content-Type: application/json' \
  -d '{
    "scene_id": "<stash_scene_id>",
    "mode": "search",
    "image_mode": "cover",
    "threshold": 0.05,
    "limit": 5,
    "hash_algorithm": "phash",
    "hash_size": 16,
    "sprite_sample_size": 8,
    "image_gamma": 3.5,
    "image_count_k": 0.25,
    "image_uniqueness_alpha": 1.0,
    "image_channels": ["phash", "color_hist", "tone"],
    "image_min_contribution": 0.05,
    "image_bonus_per_extra": 0.1
  }' | jq
```

If you get `503 Service Unavailable`, featurization isn't ready for one of the candidate jobs. Check `/api/featurization/status` and wait, or hit `/api/extraction/{job_id}/features` for the specific job.

If you get `400 Bad Request`, the request is missing one of the new-scoring fields above. Per CLAUDE.md §1, the bridge has no fallbacks for these.

### 4.3 Sanity checks after a deploy

```bash
# 1. Bridge process up
curl -fs http://localhost:13000/health

# 2. Stash GraphQL reachable from inside the container
docker exec stash-extract-db curl -fs "$STASH_URL/graphql" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"query":"query{ stats { scene_count }}"}' \
  | jq '.data.stats.scene_count // "unreachable"'

# 3. Extractor reachable from inside the container
docker exec stash-extract-db curl -fs "$EXTRACTOR_URL/health"

# 4. SQLite cache file exists, non-empty
docker exec stash-extract-db ls -la /data/stash-extract-db.db

# 5. Featurization fleet status
curl -s http://localhost:13000/api/featurization/status | jq
```

---

## 5. Inspecting matches with `?debug=1`

Append `?debug=1` to `/match/*` endpoints in **search mode** (scrape returns single result or empty per CLAUDE.md §2):

```bash
curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
  -H 'Content-Type: application/json' \
  -d '{ /* same body as §4.2 */ }' | jq '.[0]._debug'
```

You get a per-candidate breakdown:

```json
{
  "studio_code": false,
  "exact_title": false,
  "image": {
    "mode": "cover",
    "scoring": "new (multi-channel phash,color_hist,tone)",
    "channels": {
      "phash": {
        "S": 0.71,
        "E": 0.85,
        "count_conf": 0.93,
        "dist_q": 0.9,
        "baseline": 0.5208,
        "n_extractor_images": 5,
        "n_stash_hashes": 1,
        "extractor_refs": ["..."],
        "per_image_max": [0.95, 0.92, 0.4, 0.4, 0.4],
        "m_primes": [0.96, 0.84, 0.0, 0.0, 0.0],
        "qualities": [0.62, 0.58, 0.55, 0.61, 0.6],
        "uniquenesses": [1.0, 1.0, 1.0, 0.5, 1.0]
      },
      "color_hist": {
        "S": 0.41,
        "m_prime": 0.55,
        "sim": 0.83,
        "quality": 0.74,
        "baseline": 0.61,
        "have_stash": true,
        "have_extractor": true
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

The fields under `image.channels.<name>` map directly to the formulas in [`CLAUDE.md`](../CLAUDE.md) §13.2 — `S = E × count_conf × dist_q` for frame-level channels, `S = m' × q` for the aggregate channel. They're the inputs to calibration (§6).

**Channel B (color_hist) baseline is typically much higher** than the others — random unrelated scenes often share ~70–90% histogram intersection because compressed JPEG/PNG distributions cluster. After sharpening, `S_B` ends up modest unless the scene/record share specific palette features. This is expected; the channel's value is in cross-channel corroboration via the bonus, not standalone.

### 5.1 Inspect the SQLite cache directly

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
SELECT ref_id, channel, uniqueness FROM image_uniqueness
 WHERE job_id='<job_id>' ORDER BY channel, uniqueness;
```

---

## 6. Calibration

Defaults are calibrated against a Pexels-style mixed-content corpus (see [`calibration/`](calibration/)). On a sufficiently different corpus (monochrome film, surveillance footage, controlled-lighting studio content), you may benefit from re-tuning.

The full calibration runbook — synthetic dataset generation, mock extractor, sweep harness — lives in [`calibration/README.md`](calibration/README.md). Run-by-run findings, including the magnet-record failure mode and the architectural decisions that closed it, are in [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md).

### 6.1 Lightweight calibration without the synthetic corpus

If you don't want to set up the full calibration harness, you can tune by hand from real Stash scenes:

**Step 1** — pick 10–20 scenes you know well. For each, write down the expected extractor record (or "no match"). Mix easy positives, hard positives (sprite-only or color-only matches), easy negatives (no extractor coverage), and hard negatives (similar-looking but wrong record).

**Step 2** — run them all in search mode at a permissive threshold (`0.001`) to observe actual composite scores:

```bash
while read sid; do
  echo "=== $sid ==="
  curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
    -H 'Content-Type: application/json' \
    -d "{
      \"scene_id\":\"$sid\",\"mode\":\"search\",\"image_mode\":\"cover\",
      \"threshold\":0.001,\"limit\":5,
      \"hash_algorithm\":\"phash\",\"hash_size\":16,\"sprite_sample_size\":8,
      \"image_gamma\":3.5,\"image_count_k\":0.25,\"image_uniqueness_alpha\":1.0,
      \"image_channels\":[\"phash\",\"color_hist\",\"tone\"],
      \"image_min_contribution\":0.05,\"image_bonus_per_extra\":0.1
    }" \
    | jq '.[] | {title:.Title, code:.Code, score:.match_score, image_S:._debug.image.composite}'
done < scenes.txt > calibration.log
```

**Step 3** — find the gap. For each scene:

- Note the `image_S` of the **correct** record → "must keep" floor for that scene.
- Note the `image_S` of the **highest-scoring incorrect** record → "must reject" ceiling.

Across all scenes:

- `recall_floor` = min of correct scores.
- `precision_ceiling` = max of incorrect scores.

If `recall_floor > precision_ceiling`, set `IMAGE_THRESHOLD` between them. Done.

If `recall_floor < precision_ceiling`, jump to §6.2.

### 6.2 When the gap doesn't open

Look at `_debug.image.channels.phash` for the worst cases (lowest correct, highest incorrect):

| Pattern                                                                      | Likely cause                                              | Tuning                                                                                     |
| ---------------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Correct record's `m_primes` are all 0 (every per-image sim was below baseline) | Baseline inflated by within-corpus near-duplicates       | Re-featurize with stricter `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` (e.g. 0.95). Or lower `IMAGE_GAMMA` to 2.5. |
| Correct `S` low because `count_conf` ≈ 0.3                                    | Records have N=1 or 2; saturation biting                  | Lower `IMAGE_COUNT_K` further (already 0.25 default; try 0.10).                            |
| Correct `S` low, `dist_q = 0.5` (only 1 nonzero `m_prime`)                   | Only one image truly matches; rest are unrelated          | Inherent in the data. Lower `IMAGE_THRESHOLD` instead.                                     |
| Incorrect records win via shared near-dup not being penalized                 | `c_i` not penalizing shared images enough                 | Raise `IMAGE_UNIQUENESS_ALPHA` to 2.0 (and `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`). Re-featurize per §6.3. |
| Incorrect records win via baseline-floor sims that didn't sharpen to 0       | Baseline too low                                          | Raise `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` and re-featurize, OR raise `IMAGE_GAMMA`.    |
| Channel C (tone) consistently scoring 0 even on real matches                 | Tone uniqueness collapse — silenced on natural-scene corpora | Expected on Pexels-like content. Either drop `"tone"` from `IMAGE_CHANNELS`, or set `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_TONE=0.95` and re-featurize (only helps for controlled-lighting corpora). |

After each tuning change, re-run §6.1 step 2.

### 6.3 Force re-featurization for one job

Drop the per-job state row; the bridge picks it up on the next request:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db \
  "DELETE FROM job_feature_state WHERE job_id = '<job_id>';"
```

The next `/match/*` request that touches that job will return 503 once and the worker will start.

To wipe everything and re-featurize from scratch:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db <<'SQL'
DELETE FROM job_feature_state;
DELETE FROM corpus_stats;
DELETE FROM image_uniqueness;
DELETE FROM image_features WHERE source IN ('extractor_image', 'extractor_aggregate');
SQL
docker compose restart stash-extract-db
```

`startup_recover` (CLAUDE.md §14.6) re-enqueues every job and the cache fills back in.

---

## 7. Rollback paths

Every behavioral toggle has an off switch.

### 7.1 Disable the new scoring formula

Reverts image scoring to the legacy top-K-mean. Useful as an emergency fallback:

```bash
# In .env:
BRIDGE_NEW_SCORING_ENABLED=false
docker compose up -d --force-recreate stash-extract-db
```

Reset `IMAGE_THRESHOLD` in `config.py` to its old value (typically `0.7` for the legacy path). The legacy top-K-mean takes over; no scraper restart needed.

`BRIDGE_LIFECYCLE_ENABLED` can stay `true` — featurization keeps populating `image_features` for when you flip back. The legacy path also benefits from the precomputed pHash rows via dual-write.

### 7.2 Disable the lifecycle (no eager featurization)

Reverts to on-demand caching against `image_hashes` (legacy behavior, no `c_i`):

```bash
# In .env:
BRIDGE_LIFECYCLE_ENABLED=false
docker compose up -d --force-recreate stash-extract-db
```

No 503s; features compute lazily inside the request hot path. Slower per-request and missing the corpus-relative scoring weight, but functional.

### 7.3 Retiring the legacy `image_hashes` table

While `BRIDGE_LEGACY_DUAL_WRITE_ENABLED=true` (default), every pHash compute writes to both `image_hashes` and `image_features`; reads check `image_features` first, falling back to `image_hashes` on miss. To stop the dual-write:

```bash
# In .env:
BRIDGE_LEGACY_DUAL_WRITE_ENABLED=false
docker compose up -d --force-recreate stash-extract-db
```

**Safety check before flipping**: confirm `image_features` has the pHash row for every Stash sprite frame and extractor image you care about:

```bash
docker exec stash-extract-db sqlite3 /data/stash-extract-db.db <<'SQL'
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

Any rows returned would re-compute on next request. Leave dual-write on a bit longer if so, or `docker compose restart stash-extract-db` to let `startup_recover` re-featurize unfinished jobs.

After a stable period (a week is reasonable), drop the table:

```bash
docker exec -it stash-extract-db sqlite3 /data/stash-extract-db.db <<'SQL'
DROP TABLE image_hashes;
VACUUM;
SQL
```

After this, you cannot re-enable the legacy path — the dual-write code is still there but has nothing to read from. Re-enabling `BRIDGE_LEGACY_DUAL_WRITE_ENABLED=true` after a `DROP TABLE` will fail noisily.

---

## 8. Where to look first when something is wrong

For deeper architectural questions, [`CLAUDE.md`](../CLAUDE.md) §16 has the symptom→file map for the matching engine itself.

| Symptom                                       | First place to check                                                                                                                       |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `/health` returns nothing                     | `docker compose logs stash-extract-db` — usually a Stash or extractor URL misconfig.                                                      |
| Stash dialog empty for a scene                | §4.2 direct curl. Does the bridge return `[]`, an empty `{}`, or a 503/400?                                                                |
| Wrong record returned                         | §5 `?debug=1`, then §6.2 to identify which formula component is misbehaving.                                                              |
| `503` errors in Stash log during batch scrape | Expected during first featurization wave. See CLAUDE.md §14.8.                                                                            |
| Featurization stuck at `progress: 0`          | Bridge restart didn't see the row as stale yet — wait `BRIDGE_STALE_TASK_MS` (default 10 min), or manually delete the `job_feature_state` row. |
| `400 Bad Request` from bridge                 | A scraper config field is missing — typically a new-scoring field. Update `config.py` per §3.                                              |
| Scoring same scene differently after restart  | `corpus_stats` regenerated — baseline shifts slightly. Expected; absolute scores not stable across re-featurization, but ranks should be. |
| Bridge unresponsive (`/health` timing out) during heavy work | Possible regression of the asyncio.to_thread fix — see CLAUDE.md §14.4. Run `pytest tests/unit/test_lifecycle.py::TestEventLoopResponsiveness`. |
| Need to nuke everything and start fresh      | `docker compose down -v` won't delete `./data/`. Do `rm -rf ./data && docker compose up -d --build`.                                       |
