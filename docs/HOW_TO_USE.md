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

The bridge image (`Dockerfile`) installs its own Python deps (FastAPI, Pillow, imagehash, numpy, rapidfuzz, etc.) — you don't need a host-side Python venv to run it. Animated extractor assets are decoded via PIL (uniform temporal sampling, CLAUDE.md §13.8.1); no system binaries beyond standard Python image tooling are required.

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

The compose file also brings up the **viewer** sibling container — a read-only operator UI at `http://localhost:13100` (configurable via `VIEWER_PORT`). It's optional for the scraper path; if you only need the bridge, `docker compose up -d --build stash-extract-db` skips it. See `viewer/README.md` for what the UI does and CLAUDE.md §17 for why it exists.

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

| Setting             | What it controls                           | Default                             |
| ------------------- | ------------------------------------------ | ----------------------------------- |
| `BRIDGE_URL`        | Where the scraper finds the bridge.        | `http://host.docker.internal:13000` |
| `REQUEST_TIMEOUT_S` | How long the scraper waits for the bridge. | `90`                                |

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
      "tone": {
        /* same shape as phash */
      }
    },
    "fired": ["phash", "color_hist"],
    "composite": 0.81
  },
  "image_contribution": 0.81,
  "filename": {
    /* ... */
  },
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

| Symptom                                                      | First place to check                                                                                                                                                        |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/health` returns nothing                                    | `docker compose logs stash-extract-db` — usually a Stash or extractor URL misconfig.                                                                                        |
| Stash dialog empty for a scene                               | §4.2 direct curl. Does the bridge return `[]`, an empty `{}`, or a 503/400?                                                                                                 |
| Wrong record returned                                        | §5 `?debug=1` to inspect which formula component is misbehaving. Calibrated values are bridge-internal — empirical regressions are calibration bugs, not deploy-time knobs. |
| `503` errors in Stash log during batch scrape                | Expected during first featurization wave. See CLAUDE.md §14.8.                                                                                                              |
| Featurization stuck at `progress: 0`                         | Bridge restart didn't see the row as stale yet — wait `BRIDGE_STALE_TASK_MS` (default 10 min), or manually delete the `job_feature_state` row.                              |
| Scoring same scene differently after restart                 | `corpus_stats` regenerated — baseline shifts slightly. Expected; absolute scores not stable across re-featurization, but ranks should be.                                   |
| Bridge unresponsive (`/health` timing out) during heavy work | Possible regression of the asyncio.to_thread fix — see CLAUDE.md §14.4. Run `pytest tests/unit/test_lifecycle.py::TestEventLoopResponsiveness`.                             |
| Need to nuke everything and start fresh                      | `docker compose down -v` won't delete `./data/`. Do `rm -rf ./data && docker compose up -d --build`.                                                                        |
| Animated cover/asset never matches                           | Confirm `image_features` has `<job_id>:<ref>#frame_*` rows for the animated extractor ref. If absent, featurization saw the bytes as static or PIL decode failed; treat as decode failure. HEIF/AVIF are hard-skipped by design (CLAUDE.md §13.8.1).   |
| Viewer Performers list missing a name you expect             | CLAUDE.md §17 / §16 — `performer_index` empty for that job. Check `SELECT COUNT(*) FROM performer_index WHERE job_id='<job>'`. If 0, restart the bridge to re-run `backfill_performer_index()` (logs progress at startup). The viewer itself is a passive renderer; data gaps are always upstream. |
| Viewer Query page returns ERR_EMPTY_RESPONSE                 | Regression of the `viewer/server/index.ts` no-body-parser rule (CLAUDE.md §17.4). `express.json()` consumes the POST stream before http-proxy-middleware can forward it; the bridge sees an empty request and the proxy hangs.                       |
| Channel E never fires                                        | §11.3. Common causes: (a) `BRIDGE_LOCAL_FEATURES_ENABLED=false`; (b) `local_features` not in `BRIDGE_IMAGE_CHANNELS`; (c) `BRIDGE_EMBEDDING_PREFILTER_K=0` (E doesn't run in K=0 mode by design); (d) keypoints rows missing because featurization predates flag flip — delete `image_features` for the affected job and restart to re-featurize. |

---

## 9. Environment variable reference

Every environment variable consumed by the bridge or `docker-compose.yml`, in two groups: **operational** (depends on hardware / deployment, edit per-deploy) and **calibrated** (depends on corpus characteristics, do not edit without empirical evidence). Both live in the same `.env` file — re-calibration _is_ configuration; treating them differently was the mistake the earlier design made.

### 9.1 Operational — deployment-time

| Variable                               | Default                            | Purpose                                                                                                                                                                                        |
| -------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BRIDGE_PORT` _(compose)_              | `13000`                            | Host port that maps to the bridge container's 13000.                                                                                                                                           |
| `VIEWER_PORT` _(compose)_              | `13100`                            | Host port for the viewer microservice (read-only operator UI; CLAUDE.md §17). The container always listens on 3100 internally.                                                                 |
| `DATA_PATH` _(compose)_                | `./data`                           | Host directory bind-mounted at `/data` in the container. Holds the SQLite cache.                                                                                                               |
| `DOCKER_NETWORK` _(compose)_           | `extractor_network`                | External Docker network name. Bridge attaches to it to talk to the extractor.                                                                                                                  |
| `STASH_URL`                            | `http://host.docker.internal:9999` | Where Stash is reachable from inside the bridge container.                                                                                                                                     |
| `STASH_API_KEY`                        | _(empty)_                          | Stash API key, if auth is enabled. Use this OR session cookie, not both.                                                                                                                       |
| `STASH_SESSION_COOKIE`                 | _(empty)_                          | Stash session cookie, alternative to API key.                                                                                                                                                  |
| `EXTRACTOR_URL`                        | `http://extractor-gateway:12000`   | Where the site-extractor is reachable. Defaults work when both containers share `extractor_network`.                                                                                           |
| `DATA_DIR`                             | `/data`                            | Path inside the container where the SQLite cache lives. Don't change unless you also rewrite the bind mount.                                                                                   |
| `LOG_LEVEL`                            | `INFO`                             | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                                                                                                                     |
| `BRIDGE_LIFECYCLE_ENABLED`             | `true`                             | Master toggle for eager featurization + 503 request gate. Set `false` only as a rollback to the legacy single-channel path (§7.2).                                                             |
| `BRIDGE_NEW_SCORING_ENABLED`           | `true`                             | Master toggle for the multi-channel scoring formula. Set `false` to revert to the legacy top-K-mean (§7.1).                                                                                    |
| `BRIDGE_FEATURIZE_CONCURRENCY`         | `4`                                | Worker pool size — bounds parallel featurization across jobs. Lower if the extractor saturates.                                                                                                |
| `BRIDGE_FEATURIZE_PER_JOB_CONCURRENCY` | `8`                                | Per-job parallel asset fetches. Lower if a single big job is too aggressive on the extractor.                                                                                                  |
| `BRIDGE_STALE_TASK_MS`                 | `600000` (10 min)                  | A `featurizing` row whose `started_at` is older than this is treated as stuck on boot and re-enqueued.                                                                                         |
| `BRIDGE_STASH_FEATURE_BUDGET_BYTES`    | `1073741824` (1 GB)                | LRU eviction budget for Stash-side `image_features` rows. Set `0` to disable eviction. Extractor-side rows are job-cascade-bound, never LRU.                                                   |
| `BRIDGE_LRU_EVICTION_INTERVAL_S`       | `3600` (1 hour)                    | How often the LRU eviction loop runs.                                                                                                                                                          |
| `BRIDGE_LEGACY_DUAL_WRITE_ENABLED`     | `true`                             | While `true`, every pHash compute writes to both `image_hashes` (legacy) and `image_features`; reads check features first, fall back to legacy. Flip `false` to retire the legacy path (§7.3). |
| `BRIDGE_ANIMATED_FRAMES_MAX`           | `8`                                | Cap on extractor-side uniform-sampled frames per animated asset (CLAUDE.md §13.8.1). Mirrors `BRIDGE_SPRITE_SAMPLE_SIZE` so animated records don't blow out `count_conf`. Frames are picked at uniform temporal indices; corpus-level dedup is via `c_i`.       |
| `BRIDGE_ANIMATED_COVER_MODE`           | `middle`                           | Stash-cover collapse for animated screenshots. `middle` picks the temporal middle frame (one real frame); `median` produces a per-pixel median across frames (synthetic, robust to outliers but ghosts on motion). |

### 9.2 Calibrated — empirically derived, change with evidence

These are the bridge's calibrated behavior, sourced from a 491-video Pexels corpus sweep (see [`calibration/CALIBRATION_RESULTS.md`](calibration/CALIBRATION_RESULTS.md)). Re-calibrating against a different corpus = re-run the sweep harness and update these values. Don't tune them without data.

| Variable                                      | Default                     | Source / purpose                                                                                                                                                                        |
| --------------------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BRIDGE_HASH_ALGORITHM`                       | `phash`                     | Hash algorithm. Used by featurization AND per-request matching — single value.                                                                                                          |
| `BRIDGE_HASH_SIZE`                            | `16`                        | Hash bit size. Used by featurization AND per-request matching.                                                                                                                          |
| `BRIDGE_IMAGE_MODE`                           | `cover`                     | `cover` / `sprite` / `both`. Cover is fastest; `both` is most accurate but slow.                                                                                                        |
| `BRIDGE_IMAGE_THRESHOLD`                      | `0.7`                       | Composite gate for scrape mode. Search mode is unaffected.                                                                                                                              |
| `BRIDGE_SEARCH_LIMIT`                         | `5`                         | Top-N for search mode.                                                                                                                                                                  |
| `BRIDGE_SPRITE_SAMPLE_SIZE`                   | `8`                         | Sprite frames sampled per scene in `sprite`/`both` modes.                                                                                                                               |
| `BRIDGE_IMAGE_GAMMA`                          | `3.5`                       | Sharpening exponent on per-image similarities. **Run 3a peak** (concave; γ=2.0 lost 27 points to γ=3.5).                                                                                |
| `BRIDGE_IMAGE_COUNT_K`                        | `0.25`                      | Count saturation `k` in `1 - exp(-Σw / k)`. **Run 3c peak** (sparse-N records were systematically under-weighted at k=2.0).                                                             |
| `BRIDGE_IMAGE_MIN_CONTRIBUTION`               | `0.05`                      | A channel's S must clear this to count as "fired" for the bonus. **Run 3b peak** (higher excludes weak-but-correct contributions too aggressively).                                     |
| `BRIDGE_IMAGE_BONUS_PER_EXTRA`                | `0.1`                       | Bonus added per additional firing channel.                                                                                                                                              |
| `BRIDGE_IMAGE_CHANNELS`                       | `phash,color_hist,tone`     | Comma-separated channel list. Drop `tone` for ~33% per-query speedup on Pexels-style mixed content (Run 7 found tone is silenced via uniqueness collapse there).                        |
| `BRIDGE_IMAGE_SEARCH_FLOOR`                   | _(unset → disabled)_        | Optional search-mode confidence floor. **Run 6** found no global value separates weak-correct from weak-incorrect on the Pexels corpus; sharper-corpus deployments may set 0.10–0.20.   |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`           | `1.0`                       | Smoothing factor in `c_i = 1 / (1 + α·matches)`. **Run 5b flat** — varying didn't help.                                                                                                 |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD`       | `0.85`                      | Similarity threshold above which two record images count as a near-duplicate for `c_i`. **Run 5a peak** (concave, both directions degrade).                                             |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_PHASH` | _(unset → inherits global)_ | Per-channel override. **Run 7** confirmed defaults are optimal on Pexels; useful only for corpora where one channel needs distinct tuning.                                              |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD_TONE`  | _(unset → inherits global)_ | Per-channel override for tone. Counterintuitively, lifting this above the global _hurts_ on natural-scene corpora (Run 7).                                                              |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA_PHASH`     | _(unset → inherits global)_ | Per-channel α override for pHash.                                                                                                                                                       |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA_TONE`      | _(unset → inherits global)_ | Per-channel α override for tone.                                                                                                                                                        |
| `BRIDGE_EMBEDDING_ENABLED`                    | `false`                     | Channel D (DINOv2 semantic embedding). Disabled by default; flip to `true` and add `embedding` to `BRIDGE_IMAGE_CHANNELS` to engage. See §10.                                           |
| `BRIDGE_EMBEDDING_MODEL`                      | `facebook/dinov2-large`     | HuggingFace model id. Determines embedding dim + algorithm string. Common alternatives: `facebook/dinov2-base` (faster, 768-dim), `facebook/dinov2-giant` (best, 1536-dim, ~10GB VRAM). |
| `BRIDGE_EMBEDDING_DEVICE`                     | `auto`                      | `auto` / `cuda` / `cpu`. `auto` picks CUDA if available, else CPU.                                                                                                                      |
| `BRIDGE_EMBEDDING_DTYPE`                      | `fp16`                      | `fp16` / `fp32`. fp16 halves VRAM and is faster on Ampere+ GPUs (A4000). Forced to fp32 when running on CPU.                                                                            |
| `BRIDGE_EMBEDDING_BATCH_SIZE`                 | `16`                        | Featurize-time forward-pass batch. Per-request scoring uses sprite-batch sizes naturally.                                                                                               |
| `BRIDGE_EMBEDDING_THRESHOLD`                  | `0.7`                       | Cosine-similarity gate for scrape mode (when channel D fires alone). Cross-corpus stable.                                                                                               |
| `BRIDGE_EMBEDDING_PREFILTER_K`                | `10`                        | `K>0`: two-pass — D ranks all candidates in one matmul, A/B/C re-score top-K. `K=0`: embedding-only — A/B/C compute skipped entirely (fastest). See §10.5.                              |
| `BRIDGE_LOCAL_FEATURES_ENABLED`               | `false`                     | Channel E (SuperPoint + LightGlue local-feature matching). Disabled by default; flip to `true` and add `local_features` to `BRIDGE_IMAGE_CHANNELS` to engage. See §11 + CLAUDE.md §13.E.|
| `BRIDGE_LOCAL_FEATURE_MODEL`                  | `superpoint`                | Currently the only option. Reserved for future extractors (ALIKED, DISK).                                                                                                              |
| `BRIDGE_LOCAL_FEATURE_DEVICE`                 | `auto`                      | `auto` / `cuda` / `cpu`. Mirrors `BRIDGE_EMBEDDING_DEVICE`.                                                                                                                              |
| `BRIDGE_LOCAL_FEATURE_MAX_KEYPOINTS`          | `512`                       | SuperPoint paper default. Storage scales linearly: ~260 KB/image at 512, ~130 KB at 256.                                                                                                |
| `BRIDGE_LOCAL_FEATURE_MIN_INLIERS`            | `15`                        | RANSAC inlier count below which Channel E does not fire (S=0). Tuned for SuperPoint+LightGlue+RANSAC self-match floor; lower values produce false fires on unrelated images.            |
| `BRIDGE_LOCAL_FEATURE_TARGET_INLIERS`         | `50`                        | Inlier count where S_E saturates at 1.0. Computed as `clip(max_inliers / target_inliers, 0, 1)` once min_inliers is met.                                                                |
| `BRIDGE_LOCAL_FEATURE_RANSAC_THRESH`          | `5.0`                       | RANSAC reprojection threshold (pixels). Tighter values reject more candidate inliers; looser values accept more spurious ones.                                                          |
| `DOCKER_RUNTIME` _(compose)_                  | `nvidia`                    | GPU passthrough. Override to `runc` on dev hosts without a CUDA GPU; the bridge falls back to CPU embedding (~10× slower, still functional).                                            |

### 9.3 Why "calibrated" is still in env, not Python code

Earlier iterations of this doc argued these values should be hidden inside `bridge/app/settings.py`. That was wrong: re-calibration is configuration, and configuration belongs in env where it can be updated without code edits, version-controlled per deployment, and compared between corpora. The "don't tune without data" rule is enforced by documentation and the calibration provenance in §9.2's source column — not by hiding the values.

---

## 10. Channel D: semantic embedding (DINOv2)

For corpora where pHash + color_hist + tone produce weak signals (low-resolution video, content that differs structurally from the calibration set), the bridge supports a fourth scoring channel using a learned visual embedding model. Cosine similarity in DINOv2's embedding space approximates semantic similarity — "do these two images depict the same scene" — independent of corpus characteristics.

See [`CLAUDE.md`](../CLAUDE.md) §13.10 for the architectural invariants.

### 10.1 Activation

```bash
# In .env:
DOCKER_RUNTIME=nvidia                   # or runc on CPU-only hosts
BRIDGE_EMBEDDING_ENABLED=true
BRIDGE_IMAGE_CHANNELS=phash,color_hist,tone,embedding
# (or BRIDGE_IMAGE_CHANNELS=embedding to use embedding alone)

# Then:
docker compose up -d --build --force-recreate stash-extract-db
```

First boot downloads the model weights to a persistent docker volume (`huggingface_cache`); subsequent recreates reuse the cache. Featurization for any non-`ready` job proceeds as usual but now also computes channel-D embeddings — adds ~15s per 1500-record job on a CUDA GPU, ~5–10 minutes on CPU.

### 10.2 GPU passthrough setup

The bridge container needs the NVIDIA Container Toolkit on the host:

```bash
# Ubuntu / Debian:
sudo apt-get install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify:
docker run --rm --runtime=nvidia --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

Once the host is configured, `docker compose up -d --build` engages the GPU automatically because `runtime: ${DOCKER_RUNTIME:-nvidia}` in `docker-compose.yml` defaults to nvidia.

### 10.3 CPU fallback

For dev hosts without a CUDA GPU:

```bash
# In .env:
DOCKER_RUNTIME=runc
BRIDGE_EMBEDDING_DEVICE=cpu
```

Same code path; ~10× slower per-image inference. Featurization moves from ~15s to several minutes; per-scrape latency rises from ~300 ms to ~1–3s. Still well under the Stash 90s timeout.

### 10.4 Verifying the channel is firing

```bash
curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
  -H 'Content-Type: application/json' \
  -d '{"scene_id":"<id>","mode":"search","image_channels":["embedding"]}' \
  | jq '.[0]._debug.image.channels.embedding'
```

You should see `S` (cosine mean), `per_image_max` (per-record-image best cosines), `n_extractor_images`, `n_stash_hashes`. If `n_extractor_images=0`, the record's embeddings haven't been featurized yet (check `/api/featurization/status`). If `n_stash_hashes=0`, the Stash side couldn't fetch sprite/cover; check the bridge logs.

### 10.5 D-batch + (optional) A/B/C re-rank — `BRIDGE_EMBEDDING_PREFILTER_K`

When `BRIDGE_IMAGE_CHANNELS` includes `embedding` AND `BRIDGE_EMBEDDING_ENABLED=true`, the request hot path runs a single D-batch matmul over every candidate (~ms regardless of corpus size) and then dispatches based on `K`:

- **`K > 0` — two-pass**: take the top-K candidates by D score, run A/B/C over those K, combine via the existing `max(fired) + bonus * (n_fired - 1)` composite. Catches D's concept-only failure mode (a wrong specific video that D ranks confidently because it shares the matched concept; A/B/C's color histogram + pHash discriminate on the specific frame and the corroboration bonus elevates the right candidate).
- **`K == 0` — embedding-only**: every candidate's image score IS its D batch score. A/B/C compute is skipped entirely. Fastest path; same scoring behavior as Phase 1's embedding-only default.

Choose K based on the trade you want:

- `K=0` for pure speed; accept D's concept-only failures.
- `K=10` (default) for a comfortable ~500-record corpus; A/B/C re-rank work is bounded.
- `K=50` for ~10K corpora; `K=100` for ~100K.

Inspect log lines to confirm which path is engaged:

- `scoring=multi+prefilter` → two-pass
- `scoring=multi+d_only` → K=0 embedding-only

When `embedding` is not in `BRIDGE_IMAGE_CHANNELS`, K is ignored and the legacy single-pass A/B/C path runs across every candidate. That path scales with corpus size (each candidate gets per-image hash compute) and is only viable when total cost stays under your scrape timeout budget.

---

## 11. Channel E: local-feature matching (SuperPoint + LightGlue)

For corpora where channel D ranks the right candidate at top-1 but the global-embedding cosine sits below threshold — same scene, different camera angle / lens / resolution — local-feature matching adds geometric corroboration that survives viewpoint shift. Channel E uses SuperPoint (kornia) for keypoint extraction, LightGlue for matching, and RANSAC homography to count inliers.

See [`CLAUDE.md`](../CLAUDE.md) §13.E for the architectural invariants and [`docs/CHANNEL_E_PLAN.md`](CHANNEL_E_PLAN.md) for the implementation/validation plan.

### 11.1 Activation

```bash
# In .env:
DOCKER_RUNTIME=nvidia                              # or runc on CPU-only hosts
BRIDGE_LOCAL_FEATURES_ENABLED=true
BRIDGE_IMAGE_CHANNELS=phash,color_hist,tone,embedding,local_features
# (or BRIDGE_IMAGE_CHANNELS=embedding,local_features for D + E only)

# Then:
docker compose build stash-extract-db                # picks up the kornia dep
docker compose up -d --force-recreate stash-extract-db
```

First boot adds ~50 MB of SuperPoint + LightGlue weights to the existing `huggingface_cache` volume; subsequent recreates reuse it. Featurization for any non-`ready` job recomputes — adds ~25–50 s per ~500-record job on CUDA, several minutes on CPU. You'll see fresh 503s on `/match/*` until featurization completes (CLAUDE.md §14 contract).

### 11.2 Gating: K must be > 0

Channel E only fires inside the K>0 two-pass. When `BRIDGE_EMBEDDING_PREFILTER_K=0`, `score_image_composite` is never called and E is silently absent regardless of the channel-list config. This is intentional — K=0 is the embedding-only fast path and per-pair LightGlue cost has no place there. If you need E and were running K=0, raise to K=10 (or higher).

### 11.3 Verifying the channel is firing

```bash
curl -s -X POST 'http://localhost:13000/match/fragment?debug=1' \
  -H 'Content-Type: application/json' \
  -d '{"scene_id":"<id>","mode":"search","image_channels":["embedding","local_features"]}' \
  | jq '.[0]._debug.image.channels.local_features'
```

Expected fields: `S` (0 or in `[min_inliers/target_inliers, 1]`), `max_inliers`, `n_inliers_per_pair` (a 2D array of inlier counts per record_image × stash_frame), `n_extractor_images`, `n_stash_hashes`, `extractor_refs`. If `n_inliers_per_pair` is empty, no keypoints rows are cached for the candidate's images — featurization didn't run. Check `BRIDGE_LOCAL_FEATURES_ENABLED` was on when featurization happened (cascade-invalidate by deleting `image_features` rows for the affected job and restarting if not).

### 11.4 Latency expectations

A single `match_pair` is ~30–50 ms on a recent CUDA GPU. At default K=10, N=5 record images, M=8 sprite frames the worst case is K × N × M × 40 ms ≈ 16 s per request — bounded by the existing two-pass prefilter so it never scales with corpus size. CPU-only hosts run roughly 5–10× slower; the request will exceed Stash's default 90 s scraper timeout if combined corpus + N + M is large.

If precision lifts but latency hurts, the sprite-pruning optimization (one pair per record_image, picked by D's `per_image_max`) drops worst-case to ~K × N × 40 ms ≈ 2 s. Not in the minimum-shippable; tracked in `CHANNEL_E_PLAN.md` Phase 5.

### 11.5 CPU fallback

`BRIDGE_LOCAL_FEATURE_DEVICE=cpu` works — kornia's SuperPoint and LightGlue have CPU paths, just slower. Featurization moves from ~25 s to several minutes per ~500-record job; per-request E adds ~150–500 ms per pair instead of ~50 ms. For dev/eval where the GPU isn't allocated to the bridge.

### 11.6 Storage and rollback

Storage at default 512 max_keypoints: ~260 KB/image. Halve to ~130 KB by setting `BRIDGE_LOCAL_FEATURE_MAX_KEYPOINTS=256` — slight quality loss for matching, acceptable for most corpora. Cascade invalidation reuses the existing `extractor_image` source (CLAUDE.md §15.2), so per-job clear works without code change.

Rollback: flip `BRIDGE_LOCAL_FEATURES_ENABLED=false`. Existing `keypoints` rows survive but are inert (no scoring path reaches them) until cascade-invalidated. To reclaim storage immediately:

```sql
-- Delete only Channel E rows; leaves D / A / B / C alone.
DELETE FROM image_features WHERE channel = 'keypoints';
```

(Run inside the bridge's SQLite via `docker exec stash-extract-db sqlite3 /data/stash-extract-db.db`. Won't affect the legacy `image_hashes` table.)
