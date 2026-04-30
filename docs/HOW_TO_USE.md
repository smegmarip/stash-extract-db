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

The shipped `.env.example` enables the multi-channel pipeline by default with calibrated values from a 491-video Pexels corpus sweep (see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md)). For most users, the only variables that need editing are connection-side: `STASH_URL`, optionally `STASH_API_KEY` or `STASH_SESSION_COOKIE`, and `EXTRACTOR_URL` if you've moved the extractor off its default Docker network.

The calibrated scoring values (γ, k, threshold, hash size, etc.) are also in `.env`, grouped separately under "calibrated scoring values" — re-calibration against a different corpus = re-run the sweep harness and update these values. See §9 for the full reference.

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

## 3. Configuring the scraper

The scraper is a metadata transport: it reads a scene fragment from Stash on stdin, POSTs `{scene_id, mode}` to the bridge, returns the bridge's response. All scoring config lives on the bridge ([`CLAUDE.md`](../CLAUDE.md) §1). The scraper's `config.py` has only two settings:

| Setting             | What it controls                            | Default                              |
| ------------------- | ------------------------------------------- | ------------------------------------ |
| `BRIDGE_URL`        | Where the scraper finds the bridge.         | `http://host.docker.internal:13000`  |
| `REQUEST_TIMEOUT_S` | How long the scraper waits for the bridge. | `90`                                 |

Changes take effect on the next scrape — no Stash reload needed.

For bridge-side configuration, see §9.

---

## 4. Verifying matching works

### 4.1 Smoke-test from inside Stash

Pick a known scene where you remember the correct extractor record. In Stash:

1. Open the scene.
2. **Edit → Scrape with → Stash Extract**.
3. The dialog shows the matched record's title, date, performers, and cover image. Confirm or cancel.

If the dialog is empty when you expected a match, jump to §5 and §6.

### 4.2 Smoke-test the bridge directly

Bypass Stash and curl the bridge — minimal request, all scoring uses bridge defaults:

```bash
curl -s -X POST http://localhost:13000/match/fragment \
  -H 'Content-Type: application/json' \
  -d '{ "scene_id": "<stash_scene_id>", "mode": "search" }' | jq
```

If you get `503 Service Unavailable`, featurization isn't ready for one of the candidate jobs. Check `/api/featurization/status` and wait, or hit `/api/extraction/{job_id}/features` for the specific job.

For ad-hoc experimentation, the request body can override any bridge default per-call (e.g. `"image_mode": "both"`, `"threshold": 0.05`). Production scrapers send only `{scene_id, mode}`.

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

The bridge ships with calibrated defaults from a 491-video Pexels corpus sweep — see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md). These are the bridge's behavior, not user-facing knobs. They're internal to the service.

If your corpus systematically misbehaves (consistent wrong matches, persistent magnet records, etc.) that's a calibration regression worth investigating, not a knob to flip per-deploy. The dev-time tooling for re-running calibration against a different corpus is in [`calibration/README.md`](calibration/README.md).

### 6.1 Force re-featurization for one job

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
| Wrong record returned                         | §5 `?debug=1` to inspect which formula component is misbehaving. Calibrated values are bridge-internal — empirical regressions are calibration bugs, not deploy-time knobs.                          |
| `503` errors in Stash log during batch scrape | Expected during first featurization wave. See CLAUDE.md §14.8.                                                                            |
| Featurization stuck at `progress: 0`          | Bridge restart didn't see the row as stale yet — wait `BRIDGE_STALE_TASK_MS` (default 10 min), or manually delete the `job_feature_state` row. |
| Scoring same scene differently after restart  | `corpus_stats` regenerated — baseline shifts slightly. Expected; absolute scores not stable across re-featurization, but ranks should be. |
| Bridge unresponsive (`/health` timing out) during heavy work | Possible regression of the asyncio.to_thread fix — see CLAUDE.md §14.4. Run `pytest tests/unit/test_lifecycle.py::TestEventLoopResponsiveness`. |
| Need to nuke everything and start fresh      | `docker compose down -v` won't delete `./data/`. Do `rm -rf ./data && docker compose up -d --build`.                                       |

---

## 9. Environment variable reference

Every environment variable consumed by the bridge or `docker-compose.yml`, in two groups: **operational** (depends on hardware / deployment, edit per-deploy) and **calibrated** (depends on corpus characteristics, do not edit without empirical evidence). Both live in the same `.env` file — re-calibration *is* configuration; treating them differently was the mistake the earlier design made.

### 9.1 Operational — deployment-time

| Variable                                  | Default                              | Purpose                                                                                                                                       |
| ----------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `BRIDGE_PORT` *(compose)*                 | `13000`                              | Host port that maps to the bridge container's 13000.                                                                                          |
| `DATA_PATH` *(compose)*                   | `./data`                             | Host directory bind-mounted at `/data` in the container. Holds the SQLite cache.                                                              |
| `DOCKER_NETWORK` *(compose)*              | `extractor_network`                  | External Docker network name. Bridge attaches to it to talk to the extractor.                                                                 |
| `STASH_URL`                               | `http://host.docker.internal:9999`   | Where Stash is reachable from inside the bridge container.                                                                                    |
| `STASH_API_KEY`                           | *(empty)*                            | Stash API key, if auth is enabled. Use this OR session cookie, not both.                                                                      |
| `STASH_SESSION_COOKIE`                    | *(empty)*                            | Stash session cookie, alternative to API key.                                                                                                 |
| `EXTRACTOR_URL`                           | `http://extractor-gateway:12000`     | Where the site-extractor is reachable. Defaults work when both containers share `extractor_network`.                                          |
| `DATA_DIR`                                | `/data`                              | Path inside the container where the SQLite cache lives. Don't change unless you also rewrite the bind mount.                                  |
| `LOG_LEVEL`                               | `INFO`                               | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                                                                    |
| `BRIDGE_LIFECYCLE_ENABLED`                | `true`                               | Master toggle for eager featurization + 503 request gate. Set `false` only as a rollback to the legacy single-channel path (§7.2).            |
| `BRIDGE_NEW_SCORING_ENABLED`              | `true`                               | Master toggle for the multi-channel scoring formula. Set `false` to revert to the legacy top-K-mean (§7.1).                                   |
| `BRIDGE_FEATURIZE_CONCURRENCY`            | `4`                                  | Worker pool size — bounds parallel featurization across jobs. Lower if the extractor saturates.                                               |
| `BRIDGE_FEATURIZE_PER_JOB_CONCURRENCY`    | `8`                                  | Per-job parallel asset fetches. Lower if a single big job is too aggressive on the extractor.                                                 |
| `BRIDGE_STALE_TASK_MS`                    | `600000` (10 min)                    | A `featurizing` row whose `started_at` is older than this is treated as stuck on boot and re-enqueued.                                        |
| `BRIDGE_STASH_FEATURE_BUDGET_BYTES`       | `1073741824` (1 GB)                  | LRU eviction budget for Stash-side `image_features` rows. Set `0` to disable eviction. Extractor-side rows are job-cascade-bound, never LRU.  |
| `BRIDGE_LRU_EVICTION_INTERVAL_S`          | `3600` (1 hour)                      | How often the LRU eviction loop runs.                                                                                                         |
| `BRIDGE_LEGACY_DUAL_WRITE_ENABLED`        | `true`                               | While `true`, every pHash compute writes to both `image_hashes` (legacy) and `image_features`; reads check features first, fall back to legacy. Flip `false` to retire the legacy path (§7.3). |

### 9.2 Calibrated — empirically derived, change with evidence

These are the bridge's calibrated behavior, sourced from a 491-video Pexels corpus sweep (see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md)). Re-calibrating against a different corpus = re-run the sweep harness and update these values. Don't tune them without data.

| Variable                                       | Default                       | Source / purpose                                                                                                                                       |
| ---------------------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `BRIDGE_HASH_ALGORITHM`                        | `phash`                       | Hash algorithm. Used by featurization AND per-request matching — single value.                                                                          |
| `BRIDGE_HASH_SIZE`                             | `16`                          | Hash bit size. Used by featurization AND per-request matching.                                                                                          |
| `BRIDGE_IMAGE_MODE`                            | `cover`                       | `cover` / `sprite` / `both`. Cover is fastest; `both` is most accurate but slow.                                                                        |
| `BRIDGE_IMAGE_THRESHOLD`                       | `0.7`                         | Composite gate for scrape mode. Search mode is unaffected.                                                                                              |
| `BRIDGE_SEARCH_LIMIT`                          | `5`                           | Top-N for search mode.                                                                                                                                  |
| `BRIDGE_SPRITE_SAMPLE_SIZE`                    | `8`                           | Sprite frames sampled per scene in `sprite`/`both` modes.                                                                                               |
| `BRIDGE_IMAGE_GAMMA`                           | `3.5`                         | Sharpening exponent on per-image similarities. **Run 3a peak** (concave; γ=2.0 lost 27 points to γ=3.5).                                               |
| `BRIDGE_IMAGE_COUNT_K`                         | `0.25`                        | Count saturation `k` in `1 - exp(-Σw / k)`. **Run 3c peak** (sparse-N records were systematically under-weighted at k=2.0).                            |
| `BRIDGE_IMAGE_MIN_CONTRIBUTION`                | `0.05`                        | A channel's S must clear this to count as "fired" for the bonus. **Run 3b peak** (higher excludes weak-but-correct contributions too aggressively).    |
| `BRIDGE_IMAGE_BONUS_PER_EXTRA`                 | `0.1`                         | Bonus added per additional firing channel.                                                                                                              |
| `BRIDGE_IMAGE_CHANNELS`                        | `phash,color_hist,tone`       | Comma-separated channel list. Drop `tone` for ~33% per-query speedup on Pexels-style mixed content (Run 7 found tone is silenced via uniqueness collapse there). |
| `BRIDGE_IMAGE_SEARCH_FLOOR`                    | *(unset → disabled)*          | Optional search-mode confidence floor. **Run 6** found no global value separates weak-correct from weak-incorrect on the Pexels corpus; sharper-corpus deployments may set 0.10–0.20. |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`            | `1.0`                         | Smoothing factor in `c_i = 1 / (1 + α·matches)`. **Run 5b flat** — varying didn't help.                                                                |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`        | `0.85`                        | Similarity threshold above which two record images count as a near-duplicate for `c_i`. **Run 5a peak** (concave, both directions degrade).            |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_PHASH`  | *(unset → inherits global)*   | Per-channel override. **Run 7** confirmed defaults are optimal on Pexels; useful only for corpora where one channel needs distinct tuning.             |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_TONE`   | *(unset → inherits global)*   | Per-channel override for tone. Counterintuitively, lifting this above the global *hurts* on natural-scene corpora (Run 7).                              |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA_PHASH`      | *(unset → inherits global)*   | Per-channel α override for pHash.                                                                                                                       |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA_TONE`       | *(unset → inherits global)*   | Per-channel α override for tone.                                                                                                                        |

### 9.3 Why "calibrated" is still in env, not Python code

Earlier iterations of this doc argued these values should be hidden inside `bridge/app/settings.py`. That was wrong: re-calibration is configuration, and configuration belongs in env where it can be updated without code edits, version-controlled per deployment, and compared between corpora. The "don't tune without data" rule is enforced by documentation and the calibration provenance in §9.2's source column — not by hiding the values.
