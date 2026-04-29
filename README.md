# stash-extract-db

A bridge service for [Stash](https://stashapp.cc) that lets a single Stash scraper resolve a scene to extractor-side metadata. Supports both **search** mode (ranked candidates) and **scrape** mode (single definitive match or empty).

> See [`requirements.md`](requirements.md) for the full functional spec and [`CLAUDE.md`](CLAUDE.md) for architectural invariants.

## Quick start

```bash
# 1. One-time: ensure the shared docker network exists
docker network create extractor_network 2>/dev/null || true

# 2. Bridge service
cp .env.example .env
$EDITOR .env                              # set STASH_URL etc.
docker compose up -d --build
curl http://localhost:13000/health

# 3. Stash scraper
cp -r stash-extract-scraper/ ~/.stash/scrapers/stash-extract-scraper/      # adjust path for your install
$EDITOR ~/.stash/scrapers/stash-extract-scraper/config.py  # set BRIDGE_URL
pip install -r ~/.stash/scrapers/stash-extract-scraper/requirements.txt
# Stash → Settings → Scrapers → Reload Scrapers
```

## How it works

For each scene the user invokes the scraper on:

1. The scraper script reads the scene fragment from stdin and forwards it (plus user config) to the bridge.
2. The bridge pulls the scene from Stash via GraphQL, lists completed extractor jobs, filters to scene-shaped schemas, narrows by studio, and runs the heuristic engine.
3. The bridge returns Stash scraper-shaped JSON (single result for scrape, ranked list for search).

Match signals: Studio + Code (definitive), Exact Title (definitive), Image similarity (definitive in scrape, weighted in search), File-name similarity, Performer + Date.

## API

```
POST /match/fragment   { scene_id, mode, image_mode, threshold, ... }
POST /match/url        { url, mode, ... }
POST /match/name       { name, mode, ... }
GET  /health
GET  /api/extraction/{job_id}/features    (featurization status, per-job)
GET  /api/featurization/status            (featurization status, fleet)
```

## Multi-channel scoring (in-progress)

The bridge is migrating from single-channel pHash matching to a multi-channel scoring model (pHash + color histogram + low-res tone, composed by `max + bonus`). See [`MULTI_CHANNEL_SCORING.md`](MULTI_CHANNEL_SCORING.md) for the full design.

### Feature flag and rollback

Phase 3 onward gates the new lifecycle behind `BRIDGE_LIFECYCLE_ENABLED` (default `false`). When enabled, the bridge:

- Eagerly featurizes all known jobs at container startup.
- Re-featurizes on cascade invalidation (`completed_at` advance).
- Returns `503 Service Unavailable` + `Retry-After` for any match request whose candidate jobs aren't `ready`.

When `false`, the bridge falls back to the pre-existing on-demand caching path (`image_hashes`) — useful as a rollback during phased rollout.

### Phase 3 limitation: featurization algorithm vs. request algorithm

In Phase 3 the bridge featurizes jobs against a server-side default algorithm (`BRIDGE_FEATURIZE_ALGORITHM`, `BRIDGE_FEATURIZE_HASH_SIZE`), while matching requests still use whatever `hash_algorithm` / `hash_size` the scraper sends per request. **If those values diverge, the precomputed `image_features` rows don't accelerate the request and the matcher falls through to the legacy `image_hashes` path.** This is benign — matching still works — but defeats the purpose of eager featurization until Phase 4 unifies the two by making algorithm/hash_size scraper-driven and re-featurizing on demand. Until then, configure `BRIDGE_FEATURIZE_*` to match the scraper's `config.py`.

### Phase 4: thresholds need re-calibration when switching to new scoring

The new within-channel formula (sharpened evidence-union × count saturation × distribution shape) produces a more compressed score range than the legacy top-K mean. A "real match" with one perfectly matching image in a record of N=2 images scores around `0.08`, not `0.5+` — `count_conf` and `dist_q` deliberately discount sparse evidence. **Existing scraper configs setting `IMAGE_THRESHOLD = 0.7` will produce zero scrape matches against the new formula.** Re-calibrate `IMAGE_THRESHOLD` on a labeled corpus when flipping `BRIDGE_NEW_SCORING_ENABLED=true`; rough starting point is `0.05–0.15` for typical extractor records (≤5 images each), then tune based on observed false-positive / false-negative rate.

Phase 4 also has a c_i limitation: per-record-image uniqueness is computed at featurization time using `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`, while the request's `image_uniqueness_alpha` is currently unused. Set them to the same value, or trigger a cascade re-featurize after changing α.

### Phase 5: multi-channel composition

Phase 5 ships channels A (pHash), B (color histogram, scene-aggregate), and C (low-res tone). Cross-channel composition is `max(fired_channels) + bonus_per_extra * (n_fired - 1)` — the §12 filename pattern.

Activated by setting `IMAGE_CHANNELS` in the scraper config to include more than `["phash"]`. The bridge requires `image_min_contribution` and `image_bonus_per_extra` once the channel list goes beyond `phash`. Defaults: `0.3` and `0.1`.

Channel B's per-record aggregate is computed at featurization time and stored as an `extractor_aggregate` row keyed by `<job_id>:<record_idx>`; the per-scene aggregate on the Stash side is computed lazily and cached as a `stash_aggregate` row. Both invalidate via the same cascade as the per-image rows.

**Practical note:** color histogram baselines are typically high (~0.7–0.9 on natural images, since compressed JPEG/PNG distributions cluster). After sharpening, `S_B` tends to be modest standalone — its value is in cross-channel corroboration via the bonus. If B is producing zero or near-zero scores even on real matches, lower `IMAGE_MIN_CONTRIBUTION` or accept that B is a corroboration-only channel for your data.

### Phase 6 + 7: LRU eviction + legacy retirement

**Phase 6** adds bounded storage on the Stash-side `image_features` rows. A background task in the bridge runs every `BRIDGE_LRU_EVICTION_INTERVAL_S` (default 3600s) and evicts oldest-accessed Stash rows down to `BRIDGE_STASH_FEATURE_BUDGET_BYTES` (default 1 GB). Extractor-side rows are never evicted — they're bounded by job count and cleared on cascade.

**Phase 7** soft-retires the legacy `image_hashes` table behind `BRIDGE_LEGACY_DUAL_WRITE_ENABLED` (default `true`). Set to `false` to stop dual-writing pHash and skip the legacy read fallback. The actual `DROP TABLE image_hashes` is a manual operation — see [`HOW_TO_USE.md`](HOW_TO_USE.md) §10 for the safety check + steps.

### Phase 4 environment variables

| Variable                       | Default | Purpose                                                            |
| ------------------------------ | ------- | ------------------------------------------------------------------ |
| `BRIDGE_NEW_SCORING_ENABLED`   | `false` | Flip image scoring from legacy top-K-mean to the new formula. Requires the scraper to send `image_gamma`, `image_count_k`, `image_uniqueness_alpha` (returns 400 otherwise). |

### New environment variables (Phase 3+)

| Variable                             | Default | Purpose                                                            |
| ------------------------------------ | ------- | ------------------------------------------------------------------ |
| `BRIDGE_LIFECYCLE_ENABLED`           | `false` | Master toggle for the featurization lifecycle.                     |
| `BRIDGE_FEATURIZE_CONCURRENCY`       | `4`     | Worker pool size — bounds parallel featurization across jobs.      |
| `BRIDGE_FEATURIZE_PER_JOB_CONCURRENCY` | `8`   | Bounds parallel asset fetches inside a single featurization task. |
| `BRIDGE_FEATURIZE_ALGORITHM`         | `phash` | Hash algorithm for server-side featurization.                      |
| `BRIDGE_FEATURIZE_HASH_SIZE`         | `8`     | Hash size for server-side featurization.                           |
| `BRIDGE_FEATURIZE_UNIQUENESS_ALPHA`  | `1.0`   | Smoothing factor in `c_i = 1/(1 + α·matches)`.                     |
| `BRIDGE_FEATURIZE_UNIQUENESS_THRESHOLD` | `0.85` | Similarity threshold for "near-duplicate" in uniqueness count.    |
| `BRIDGE_STALE_TASK_MS`               | `600000` | A `featurizing` row older than this is treated as stuck on boot.  |

## Architecture

```
Stash :9999 ◄── scraper.py ──► stash-extract-db :13000 ──► extractor :12000
                              (FastAPI bridge + SQLite cache)
```

See `requirements.md` §2.
