# stash-extract-db

A bridge service for [Stash](https://stashapp.cc) that lets a single Stash scraper resolve a scene to extractor-side metadata. Supports both **search** mode (ranked candidates) and **scrape** mode (single definitive match or empty).

> **Documentation map**
> - [`docs/HOW_TO_USE.md`](docs/HOW_TO_USE.md) — operator's runbook (install, configure, verify, debug).
> - [`docs/TESTING.md`](docs/TESTING.md) — testing strategy and calibration history.
> - [`docs/calibration/`](docs/calibration/) — calibration harness + run-by-run results.
> - [`CLAUDE.md`](CLAUDE.md) — architectural invariants (project memory).
> - [`requirements.md`](requirements.md) — full functional spec.

---

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

The shipped `.env.example` enables the multi-channel pipeline by default with calibrated tuning values. See [`docs/HOW_TO_USE.md`](docs/HOW_TO_USE.md) for the full runbook and §9 for every environment variable.

---

## How it works

For each scene the user invokes the scraper on:

1. The scraper script reads the scene fragment from stdin and forwards it (plus user config) to the bridge.
2. The bridge pulls the scene from Stash via GraphQL, lists completed extractor jobs, filters to scene-shaped schemas, narrows by studio (CLAUDE.md §5–§6), and runs the heuristic engine.
3. The bridge returns Stash scraper-shaped JSON (single result for scrape, ranked list for search).

**Match signals**:

- **Studio + Code** (definitive)
- **Exact Title** (definitive)
- **Image similarity** — multi-channel composite of pHash + color histogram + low-res tone, with sharpened evidence-union, count saturation, distribution-shape weighting, and a cross-channel bonus. Definitive in scrape (composite ≥ threshold), weighted in search. See CLAUDE.md §13.
- **File-name similarity** — multi-channel composite of naive normalize, guessit-parsed title, plus structured field bonuses (CLAUDE.md §12).
- **Performer + Date** — alias-resolved against Stash's performer list.

The image-tier scoring relies on a **featurization lifecycle**: per-job features (per-image quality, per-channel baseline, per-image uniqueness) are computed eagerly at container startup and on cascade invalidation, then cached in SQLite. Match requests against jobs whose features aren't ready return `503 Service Unavailable + Retry-After`. See CLAUDE.md §14.

---

## Architecture

```
Stash :9999 ◄── scraper.py ──► stash-extract-db :13000 ──► extractor :12000
                              (FastAPI bridge + SQLite cache + featurization workers)
```

The bridge is **read-only** on the Stash side (CLAUDE.md §9): it only calls `findScene` / `findPerformers` and never mutates. All writes go through Stash's normal scraper apply path.

---

## API

```
POST /match/fragment   { scene_id, mode, ...scoring fields }   # primary entry; from Stash scraper
POST /match/url        { url, mode, ... }                       # match a record by URL
POST /match/name       { name, mode, ... }                      # match by free-text name (search-only)
GET  /health
GET  /api/extraction/{job_id}/features    # featurization status, per-job
GET  /api/featurization/status            # featurization status, fleet
```

All `/match/*` endpoints accept `?debug=1` in search mode for a per-candidate breakdown of which signals fired and the per-channel scoring inputs. See `docs/HOW_TO_USE.md` §5.

---

## Configuration

All matching configuration lives on the bridge per CLAUDE.md §1. The scraper is a metadata transport — its `config.py` has only `BRIDGE_URL` and `REQUEST_TIMEOUT_S`. Bridge configuration:

- [`.env.example`](.env.example) — operational env (connection, auth, lifecycle toggles, concurrency, storage budgets).
- [`bridge/app/settings.py`](bridge/app/settings.py) — calibrated scoring values (internal service behavior).

Defaults are calibrated against a 491-video Pexels corpus (precision@1 = 96.2%); see [`docs/calibration/CALIBRATION_RESULTS.md`](docs/calibration/CALIBRATION_RESULTS.md) for provenance.

---

## Rollback

The new scoring formula and featurization lifecycle are independently togglable:

- `BRIDGE_NEW_SCORING_ENABLED=false` → reverts image scoring to the legacy top-K-mean.
- `BRIDGE_LIFECYCLE_ENABLED=false` → reverts to on-demand caching against the legacy `image_hashes` table. No 503s, no corpus-relative weighting.
- `BRIDGE_LEGACY_DUAL_WRITE_ENABLED=false` → stops dual-writing pHash to `image_hashes`. After a stable period, you can `DROP TABLE image_hashes` manually.

See `docs/HOW_TO_USE.md` §7.

---

## Testing

98 unit tests pass in ~8 seconds:

```bash
pytest tests/unit/
```

The integration test (`tests/integration/test_calibration.py`) auto-skips when the calibration bridge isn't running. Full testing strategy lives in [`docs/TESTING.md`](docs/TESTING.md).
