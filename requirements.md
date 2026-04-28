# `stash-extract-db` — Requirements Specification

A bridge service between Stash (`:9999`) and the Site Extractor (`:12000`) that lets a single Stash scraper resolve a scene to extractor-side metadata. The scraper supports both **search** mode (ranked candidates for the user to pick) and **scrape** mode (a single definitive match or empty). The heuristic engine combines text, file-name, and perceptual-image signals.

> See **`CLAUDE.md`** for architectural invariants — the contracts that must hold to keep the system coherent. This document covers *what* to build; CLAUDE.md covers *what must always be true*.

---

## 1. Purpose & Scope

### Problem

Stash is a media organizer with a robust, pHash-keyed scene database (via `stash-box`). Two classes of scene fall outside that index:

1. **Archive captures** — scenes whose canonical sources are no longer reachable (e.g. via archive.org snapshots).
2. **Non-indexed sources** — sample / demo libraries (e.g. `quinticsports.com/sample-videos/`) that aren't present in any scraper-supported database.

The Site Extractor already crawls these sources and emits structured records. What's missing is the bridge: given a Stash scene id, find the right extractor record and return it in the shape Stash's scraper protocol expects.

### Out of scope

- Replicating Stash's stash-box / pHash search (that's already excellent).
- Modifying the Site Extractor itself.
- Writing back to Stash (we return scraper output; Stash applies it).
- Browser-driven login flows, captcha solving, or anything the extractor doesn't already provide.
- A web UI for the bridge — settings live in the scraper's `config.py`.

### Success criteria

- A user invokes "Scrape With → Stash Extract DB" on a scene in Stash. The scraper either:
  - returns a single confident match (scrape mode), populating the scene fields automatically, or
  - returns a ranked list of candidates (search mode) for the user to pick.
- For the example dataset (`Quintic Sports` job, scene 160 = `Horse_Walking.avi`), the bridge correctly identifies the matching extractor record using URL filename + image similarity.

---

## 2. Architecture

```
┌─────────────────┐     scraper.py (script-type Stash scraper)
│   Stash :9999   │ ◄────────────┐
└────────┬────────┘              │
         │ GraphQL               │ HTTP
         ▼                       │
   findPerformers,         ┌─────┴──────────────┐
   findScene, etc.         │ stash-extract-db   │ :13000
   (alias resolution)      │  (FastAPI bridge)  │
                           └─────┬──────────────┘
                                 │ HTTP
                                 ▼
                           ┌────────────────────┐
                           │ Site Extractor     │ :12000
                           │  (existing)        │
                           └────────────────────┘
```

- **Separate compose** with `external: true` `extractor_network` (matches the site-extractor convention).
- **Bridge service**: FastAPI on host port `13000`, container name `stash-extract-db`.
- **Stash reach**: defaults to `http://host.docker.internal:9999`, overridable via `STASH_URL`.
- **Extractor reach**: defaults to `http://extractor-gateway:12000` on the shared network, overridable via `EXTRACTOR_URL`.

---

## 3. Stash Scraper Bundle

Lives at `stash/` in this repository. Users copy it into Stash's scrapers directory and reload.

### 3.1 Layout

```
stash/
├── stash-extract-db.yml        ← Stash scraper manifest
├── scraper.py                  ← script entrypoint
├── config.py                   ← user-edited defaults
└── requirements.txt            ← stashapp-tools, requests
```

### 3.2 `stash-extract-db.yml`

Stash scraper manifest. Declares four actions, all `action: script`:

```yaml
name: "Stash Extract DB"
sceneByFragment:
  action: script
  script: [python3, scraper.py, fragment]
sceneByQueryFragment:
  action: script
  script: [python3, scraper.py, query]
sceneByName:
  action: script
  script: [python3, scraper.py, name]
sceneByURL:
  - action: script
    url: [""]                # users edit to scope to their archive sources
    script: [python3, scraper.py, url]
```

### 3.3 `scraper.py` behavior

A thin client. For each action:

1. Read JSON fragment from stdin (Stash's protocol).
2. Import `config`, build a request body.
3. Call the matching bridge endpoint (`/match/fragment`, `/match/url`, `/match/name`) with mode = `"scrape"` for `sceneByFragment` / `sceneByURL`, or `"search"` for `sceneByName` / `sceneByQueryFragment`.
4. Print the bridge's response to stdout verbatim.

The script does **no** matching, hashing, or logic — it's purely a transport adapter.

### 3.4 `config.py`

User-edited defaults. The bridge has no fallback for any of these — every request is fully parameterized by the scraper.

```python
BRIDGE_URL          = "http://localhost:13000"
IMAGE_MODE          = "cover"        # "cover" | "sprite" | "both"
IMAGE_THRESHOLD     = 0.7            # 0..1 — applied per image_mode
SEARCH_LIMIT        = 5
HASH_ALGORITHM      = "phash"        # phash | dhash | ahash | whash
HASH_SIZE           = 16
SPRITE_SAMPLE_SIZE  = 8
REQUEST_TIMEOUT_S   = 60
```

### 3.5 Installation

```bash
cp -r stash/ ~/.stash/scrapers/stash-extract-db/
$EDITOR ~/.stash/scrapers/stash-extract-db/config.py    # set BRIDGE_URL etc.
pip install -r ~/.stash/scrapers/stash-extract-db/requirements.txt
# Stash → Settings → Scrapers → Reload Scrapers
```

---

## 4. Bridge HTTP API

All endpoints synchronous JSON-in/JSON-out.

### 4.1 Endpoints

| Method | Path | For Stash action |
|---|---|---|
| `POST` | `/match/fragment` | `sceneByFragment`, `sceneByQueryFragment` |
| `POST` | `/match/url` | `sceneByURL` |
| `POST` | `/match/name` | `sceneByName` |
| `GET` | `/health` | liveness |

### 4.2 Common request body

```json
{
  "scene_id": "160",                 // required for /match/fragment, /match/url
  "name": "...",                     // required for /match/name
  "mode": "scrape",                  // "scrape" | "search"
  "image_mode": "cover",             // "cover" | "sprite" | "both"
  "threshold": 0.7,                  // required — bridge has no fallback
  "limit": 5,                        // search-only; ignored in scrape
  "hash_algorithm": "phash",
  "hash_size": 16,
  "sprite_sample_size": 8
}
```

Missing required parameters → `400 Bad Request`.

### 4.3 Scrape response

Single result (Stash scraper output shape):

```json
{
  "Title": "...",
  "Details": "...",
  "Date": "YYYY-MM-DD",
  "URL": "https://...",
  "Code": "...",
  "Image": "data:image/jpeg;base64,...",
  "Studio": { "Name": "..." },
  "Performers": [ { "Name": "...", "Aliases": "..." } ]
}
```

No definitive signal fired → `{}` (Stash convention for "no result").

### 4.4 Search response

List, ranked descending by score:

```json
[
  { "Title": "...", "URL": "...", "Image": "data:image/...",
    "match_score": 0.93 },
  ...
]
```

`match_score` is non-standard — Stash ignores unknown fields, harmless to include for users tuning thresholds.

### 4.5 Output field mapping

| Stash output field | Source |
|---|---|
| `Title` | extractor `data.title` |
| `Details` | extractor `data.details` |
| `Date` | extractor `data.date` (passthrough; see §6.7) |
| `URL` | extractor `data.url` |
| `Code` | extractor `data.id` |
| `Image` | extractor `data.cover_image`, fetched + base64 data URI |
| `Studio.Name` | echo of input studio (when used as filter) |
| `Performers[].Name`, `Performers[].Aliases` | alias-resolved against Stash performers (§9.2) |

`data.images[]` is **not** in the output — used for matching only.

---

## 5. Job-Filtering Pipeline

Per request:

1. **Pull scene** from Stash via GraphQL (fragment in §9.1).
2. **List jobs** from extractor: `GET /api/jobs?status=completed&limit=200`.
3. **Filter to scene-shaped jobs**: drop any whose schema (via `GET /api/schemas/{schema_id}`) does *not* have a **superset** of canonical fields:
   ```
   {title, url, cover_image, images, performers, date, details, id}
   ```
4. **Studio narrowing**:
   - Scene has a studio AND any job's `name.casefold() == studio.name.casefold()` → search domain = **that job only**.
   - Scene has a studio AND no name match → return empty (or `[]` for search).
   - Scene has no studio → search domain = **all scene-shaped jobs** (caveat utilitor).
5. **Pull results** for each job in the search domain (§8 cache).
6. Run heuristic engine (§6) over the candidate pool.

---

## 6. Heuristic Engine

### 6.1 Scrape mode — binary cascade

Cheap-first ordering. All three signals are equivalently definitive; ordering is purely a performance optimization.

```python
def scrape(scene, candidates, image_mode, threshold):
    # Tier 1: Studio + Code (cheapest — string compare)
    code = (scene.code or "").strip()
    if code:
        hits = [c for c in candidates if (c.id or "") == code]    # case-sensitive
        if hits:
            return min(hits, key=lambda c: c.result_index)

    # Tier 2: Exact Title (strict, no normalization)
    title = (scene.title or "").strip()
    if title:
        hits = [c for c in candidates if (c.title or "") == title]
        if hits:
            return min(hits, key=lambda c: c.result_index)

    # Tier 3: Image — per-extractor-image best-match sims, then
    # above-threshold soft-OR aggregation. A candidate fires when at least
    # one extractor image clears `threshold`. Among firing candidates, the
    # rank score rewards multiple strong matches over single borderline
    # matches (see §7 + CLAUDE.md §13).
    matches = []
    for c in candidates:
        sims = per_extractor_image_sims(scene, c, image_mode)
        score = aggregate_scrape(sims, threshold)   # = soft_or([s for s in sims if s >= threshold])
        if score > 0:
            matches.append((c, score))
    if matches:
        matches.sort(key=lambda m: (-m[1], m[0].job_id, m[0].result_index))
        return matches[0][0]

    return None    # → empty {} response
```

Tiebreak across all tiers: highest aggregate score, then lowest `result_index` (the index from `GET /api/extraction/{job_id}/results?sort_dir=asc`).

### 6.2 Search mode — composite weighted score

Every candidate gets a score:

```python
score = (
    (1.0 if studio_and_code(scene, c) else 0.0)
  + (1.0 if scene.title and scene.title == c.title else 0.0)
  + image_contribution(scene, c, image_mode)              # see §7
  + 0.2 * filename_score(scene, c)                        # multi-channel; §6.3
  + 0.3 * (0.5 * performer_score(scene, c) + 0.5 * date_score(scene, c))
)
score = min(score, 1.0)     # cap at 1.0
```

Where:

```python
def image_contribution(scene, c, image_mode):
    sims = per_extractor_image_sims(scene, c, image_mode)
    return aggregate_search(sims)                         # = soft_or(sims)
```

Image **always** contributes in search — the threshold no longer gates the contribution. The aggregation is distribution-sensitive (soft-OR): a record with `[0.1, 0.1, 0.1, 1.0]` outranks one with `[0.13, 0.13, 0.13, 0.13]` because soft-OR returns 1.0 vs 0.43 (CLAUDE.md §13).

### 6.3 Filename score (multi-channel)

The filename comparator is composed of three independent channels feeding a single score in `[0, 1]`. See `bridge/app/matching/filename.py` and CLAUDE.md §12.

**Channel 1 — naive normalize → RapidFuzz `WRatio`** (preserves the original behavior). Robust on short, clean filenames.

```python
def norm(s):
    s = urllib.parse.unquote(s)
    s, _ = os.path.splitext(s)
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s
naive = WRatio(norm(stash.basename), norm(extractor_url_basename)) / 100
```

**Channel 2 — `guessit` parsed title → RapidFuzz `token_set_ratio`**. Strips release/resolution/codec/group noise; matches the residual title.

```python
g_s, g_e = guessit(stash.basename), guessit(extractor_url_basename)
guessit_title = token_set_ratio(g_s.get("title",""), g_e.get("title","")) / 100
```

**Channel 3 — structured-field exact matches**. Bonuses summed only for fields where both sides parsed non-null AND the values match:

| Field | Weight |
|---|---|
| `year` | 0.05 |
| `episode` | 0.05 |
| `season` | 0.03 |
| `screen_size` | 0.02 |

**Composition**: `min(1.0, max(naive, guessit_title) + structured_bonus)`.

`max` (not `mean`) so a strong signal from either fuzzy channel is never dragged down by the other's miss. Structured bonuses are additive on top — agreement on year/episode is independent corroborating evidence.

### 6.3.1 Debug breakdown

`POST /match/fragment?debug=1` (and `?debug=1` on `/match/url`, `/match/name`) — when `mode=search`, each result item gains a `_debug` object:

```json
{
  "Title": "...",
  "URL": "...",
  "match_score": 0.5008,
  "_debug": {
    "studio_code": false,
    "exact_title": false,
    "image_sim": 0.602,
    "image_contribution": 0.301,
    "filename": {
      "score": 1.0,
      "naive": 1.0,
      "guessit_title": 1.0,
      "structured_bonus": 0.0,
      "matched_fields": [],
      "guessit_stash": { "title": "Horse Walking", "container": "avi" },
      "guessit_extractor": { "title": "Horse Walking", "container": "avi" }
    },
    "filename_contribution": 0.2,
    "performer_score": 0.0,
    "date_score": 0.0,
    "soft_contribution": 0.0,
    "raw_score": 0.5008,
    "capped_score": 0.5008
  }
}
```

Scrape mode ignores the flag — it returns a single result or empty.

### 6.4 Performer score

```python
def performer_score(scene, c):
    if not scene.performers:
        return 0.0
    extractor_names = c.performers or []
    matched = 0
    for p in scene.performers:
        # alias-resolve each extractor name → set of stash performer ids
        for ext_name in extractor_names:
            if ext_name in alias_index_for(p.id):    # p.name + p.aliases
                matched += 1
                break
    return matched / max(len(scene.performers), 1)
```

When extractor `performers` is null → `0.0` (neutral, not penalty — see CLAUDE.md).

### 6.5 Date score

```python
def date_score(scene, c):
    if not scene.date or not c.date:
        return 0.0
    a, b = parse_partial_date(scene.date), parse_partial_date(c.date)
    if a == b:                          return 1.0     # same precision, same value
    if a.year == b.year and a.month == b.month: return 0.5
    if a.year == b.year:                return 0.2
    return 0.0
```

Stash supports partial dates (year-only, year-month). Parser tolerates `YYYY`, `YYYY-MM`, `YYYY-MM-DD`.

### 6.6 Tiebreak (search)

Score desc → tied → lowest `result_index` ascending.

### 6.7 Empty / null handling

| Field | Empty/null behavior |
|---|---|
| `scene.title` empty | Title signal does not fire (no exact match possible). |
| `scene.code` empty | Studio+Code does not fire. |
| `scene.studio` null | All scene-shaped jobs become candidates. |
| `scene.performers` empty | Performer score = 0.0. |
| `scene.date` null | Date score = 0.0. |
| `c.id` null | Studio+Code does not fire for that candidate. |
| `c.title` null | Exact-title does not fire for that candidate. |
| `c.images` empty | Image contribution = 0.0. |
| `c.date` null | Date score = 0.0. |

---

## 7. Image Matching

### 7.1 Reuse from `stash-duplicate-scene-finder`

Lift these modules into `bridge/app/imgmatch/`:

- `image_comparison.py` — `detect_letterbox`, `normalize_image`, `compute_hash`, `hash_distance_to_similarity`, `HASH_FUNCS`.
- `sprite_processor.py` — `parse_vtt`, `fetch_vtt`, `extract_sprite_frames`, `sample_frames`.

### 7.2 Comparable shapes + distribution-sensitive aggregation

The engine first computes **per-extractor-image best-match sims** — for each extractor image (`cover_image` ∪ `images[]`, deduped), the best similarity against the configured Stash-side hash set:

| `image_mode` | Stash-side set | Per-extractor-image score |
|---|---|---|
| `cover`  | `{paths.screenshot}` (size 1)              | `sim(stash_cover, e_i)` |
| `sprite` | `{frame_1 … frame_M}` (M sampled frames)   | `max_j sim(frame_j, e_i)` |
| `both`   | `{paths.screenshot} ∪ {frame_1 … frame_M}` | `max over union` |

This produces an array `sims` of length N (one entry per extractor image). The array is then aggregated:

```python
def soft_or(sims):
    """1 - prod(1 - s) — bounded [0,1], saturates at 1.0 when any sim is 1.0."""
    p = 1.0
    for s in sims:
        p *= max(0.0, 1.0 - min(1.0, s))
    return 1.0 - p

def aggregate_search(sims):     return soft_or(sims)
def aggregate_scrape(sims, t):  return soft_or([s for s in sims if s >= t])
```

**Why per-extractor-image, not flat M×N**: in sprite mode, every (frame, image) pair is evidence, but flattening would let many weak coincidental matches accumulate spurious signal. Collapsing to one score per extractor image first ensures the distribution reflects the record's image set, not the Stash sprite resolution. CLAUDE.md §13.

**Why soft-OR, not max or mean**: it captures both peak strength AND number of matches in a single bounded number. A record with 1×1.0 + 3×0.1 saturates at 1.0; a record with 4×0.13 stays at 0.43. Mean would tie them at 0.325 vs 0.13; max would lose the contribution from the weaker matches when they're meaningful.

### 7.3 Asset fetching

Extractor image references in records are job-relative (`../assets/<file>`). Bridge resolves to `GET /api/asset/{job_id}/assets/<file>` (path is `assets`, plural, per the gateway's rewrite). Bridge honors ETag for conditional requests.

Stash side: bridge fetches `paths.screenshot` and `paths.sprite` + `paths.vtt` directly from Stash, with `ApiKey` / cookie auth from config.

### 7.4 Image preprocessing

Per `image_comparison.normalize_image`:
1. Detect letterbox / pillarbox bars (`detect_letterbox`).
2. Crop bars.
3. Squash to 256×256 (greedy normalization — handles 16:9 vs 4:3 of same scene).
4. Convert to grayscale.
5. Compute `phash` (default) at `hash_size = 16`.

### 7.5 Hash invalidation

| Cache type | Fingerprint key | Source |
|---|---|---|
| Stash cover hash | `screenshot ?t=<epoch>` query | duplicate-finder pattern |
| Stash sprite frames | `oshash` from `files[].fingerprints` | duplicate-finder pattern |
| Extractor image hash | asset `etag` or `content_hash` (response headers) | site-extractor convention |
| Extractor result set | per-job `completed_at` | re-extraction nukes the job's row block |

---

## 8. SQLite Cache

Single SQLite at `${DATA_DIR}/stash-extract-db.db`.

### 8.1 Schema

```sql
-- Per-job snapshot of extractor results (invalidated on completed_at change)
CREATE TABLE extractor_jobs (
  job_id        TEXT PRIMARY KEY,
  job_name      TEXT NOT NULL,
  schema_id     TEXT NOT NULL,
  completed_at  TEXT NOT NULL,
  fetched_at    TEXT NOT NULL
);

CREATE TABLE extractor_results (
  job_id        TEXT NOT NULL,
  result_index  INTEGER NOT NULL,
  page_url      TEXT,
  data_json     TEXT NOT NULL,
  PRIMARY KEY (job_id, result_index),
  FOREIGN KEY (job_id) REFERENCES extractor_jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_jobs_name_lower ON extractor_jobs(LOWER(job_name));

-- Image hashes (both Stash side and extractor side)
CREATE TABLE image_hashes (
  source        TEXT NOT NULL,       -- 'stash_cover' | 'stash_sprite' | 'extractor_image'
  ref_id        TEXT NOT NULL,       -- scene_id, scene_id+frame_idx, or job_id+url_hash
  fingerprint   TEXT NOT NULL,       -- ?t=, oshash, or etag
  algorithm     TEXT NOT NULL,
  hash_size     INTEGER NOT NULL,
  phash_hex     TEXT NOT NULL,
  computed_at   TEXT NOT NULL,
  PRIMARY KEY (source, ref_id, algorithm, hash_size)
);

-- Cached pairwise match results (mainly for sprite mode — expensive)
CREATE TABLE match_results (
  scene_id          TEXT NOT NULL,
  job_id            TEXT NOT NULL,
  result_index      INTEGER NOT NULL,
  image_mode        TEXT NOT NULL,
  similarity        REAL NOT NULL,
  scene_fingerprint TEXT NOT NULL,    -- composite of scene image fingerprints
  job_completed_at  TEXT NOT NULL,
  PRIMARY KEY (scene_id, job_id, result_index, image_mode)
);
```

### 8.2 Invalidation flow

On every match request:

1. `SELECT job_id, completed_at FROM extractor_jobs WHERE job_id IN (...)`.
2. For each candidate job, fetch fresh `GET /api/jobs/{id}` and compare `completed_at`.
3. If stale (or no row): `DELETE FROM extractor_results WHERE job_id = ?`, then refetch all results, then `DELETE FROM match_results WHERE job_id = ?` to drop stale similarity scores.
4. Hash rows are invalidated independently per their own fingerprint columns.

---

## 9. Stash GraphQL Integration

### 9.1 Scene fragment

```graphql
fragment SceneForMatch on Scene {
  id title details date code urls
  files {
    path basename duration width height frame_rate
    fingerprints { type value }
  }
  paths { screenshot preview sprite vtt }
  studio { id name url }
  performers { id name aliases }
  tags { id name }
  stash_ids { endpoint stash_id }
}
```

### 9.2 Performer alias resolution

For each extractor performer name `n`, query:

```graphql
findPerformers(performer_filter: {
  OR: { name: { value: $n, modifier: EQUALS },
        aliases: { value: $n, modifier: INCLUDES } }
})
```

Return any matching performer id. Match against `scene.performers[].id` to score.

**Performance**: alias lookups are O(performers per record) per request. Acceptable for small N. If a request hits >50 performer lookups, prefetch `findPerformers(filter: { per_page: -1 })` once and build an in-memory `name|alias_lower → id` index, refresh on a TTL (default 5 min). Index is a singleton, not per-request.

### 9.3 Authentication

Bridge supports both auth modes from `stashapp-tools`:
- `STASH_API_KEY` env → sent as `ApiKey` header.
- `STASH_SESSION_COOKIE` env → sent as `session=<value>` cookie.

If neither is set, requests are anonymous (works against unauthenticated Stash instances).

---

## 10. Bridge Configuration

Env vars only. No JSON config file.

| Name | Default | Purpose |
|---|---|---|
| `STASH_URL` | `http://host.docker.internal:9999` | Stash GraphQL endpoint |
| `STASH_API_KEY` | (unset) | Optional, sent as `ApiKey` header |
| `STASH_SESSION_COOKIE` | (unset) | Optional, sent as `session` cookie |
| `EXTRACTOR_URL` | `http://extractor-gateway:12000` | Site Extractor base |
| `DATA_DIR` | `/data` | SQLite + working files |
| `LOG_LEVEL` | `INFO` | Standard |

Studio→job mapping is computed at request time via case-insensitive name match (§5). No mapping file.

---

## 11. Repository Layout

```
stash-extract-db/
├── README.md
├── CLAUDE.md                       ← architectural invariants
├── requirements.md                 ← this document
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── bridge/
│   └── app/
│       ├── main.py                 ← FastAPI app, env loading
│       ├── api/
│       │   ├── match.py            ← /match/{fragment,url,name}
│       │   └── health.py
│       ├── stash/
│       │   ├── client.py           ← stashapp-tools wrapper
│       │   ├── fragment.py         ← SceneForMatch + helpers
│       │   └── alias_index.py      ← alias prefetch + TTL refresh
│       ├── extractor/
│       │   ├── client.py           ← jobs, schemas, results, asset fetch
│       │   └── schema_match.py     ← superset check for "Video Scene"
│       ├── matching/
│       │   ├── scrape.py           ← binary cascade (§6.1)
│       │   ├── search.py           ← composite scoring (§6.2)
│       │   ├── text.py             ← norm, rapidfuzz, performer/date
│       │   └── imgmatch/           ← lifted from stash-duplicate-scene-finder
│       │       ├── image_comparison.py
│       │       ├── sprite_processor.py
│       │       └── __init__.py
│       └── cache/
│           ├── db.py               ← SQLite schema + migrations
│           └── invalidation.py     ← completed_at + fingerprint logic
├── stash/                          ← user copies into ~/.stash/scrapers/
│   ├── stash-extract-db.yml
│   ├── scraper.py
│   ├── config.py
│   └── requirements.txt
└── tests/
    ├── unit/                       ← scoring, normalization, alias resolution
    └── integration/                ← against running extractor + Stash
```

---

## 12. Installation & Deployment

### Bridge

```bash
cd stash-extract-db
docker network ls | grep extractor_network || docker network create extractor_network
cp .env.example .env
$EDITOR .env                                     # set STASH_URL, STASH_API_KEY if needed
docker compose up -d --build
curl http://localhost:13000/health
```

### Stash scraper

```bash
cp -r stash/ ~/.stash/scrapers/stash-extract-db/
$EDITOR ~/.stash/scrapers/stash-extract-db/config.py    # set BRIDGE_URL etc.
pip install -r ~/.stash/scrapers/stash-extract-db/requirements.txt
# In Stash UI: Settings → Scrapers → Reload Scrapers
```

---

## 13. Open / Deferred

For follow-up passes after MVP:

- **Studio→job override file** when names diverge (e.g. job `"Quintic Sports Archive"` vs studio `"Quintic Sports"`). Today: case-insensitive exact match only.
- **Multi-job parallelism** when scene has no studio — fetch results across jobs concurrently.
- **Score-debug endpoint** — `?debug=1` returns per-signal breakdown for tuning.
- **Auto-detection of seeded "Video Scene" template by id** — cheaper than superset check when users haven't cloned.
- **HTTP/2 + connection pooling** to Stash and the extractor.
- **Request-level metrics** (Prometheus) — counts, latencies, cache hit rates.
