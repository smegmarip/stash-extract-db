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
```

## Architecture

```
Stash :9999 ◄── scraper.py ──► stash-extract-db :13000 ──► extractor :12000
                              (FastAPI bridge + SQLite cache)
```

See `requirements.md` §2.
