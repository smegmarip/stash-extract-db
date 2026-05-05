# Viewer Microservice — Implementation Plan

> **Status: transient.** This plan tracks the work on the `user-interface` branch. On merge to `main`, the durable invariants (performer_index lifecycle, admin endpoint contracts, viewer/bridge boundary) get folded into CLAUDE.md and this file is deleted. Run-by-run state lives in commit history, not here.

---

## 1. Goal

Ship a read-only operator UI for browsing and exercising `stash-extract-db`. Same role as `stash-auto-vision/jobs-viewer` (sibling container, dev-grade dark theme), styled after `experiments/stash-data/stash-viewer`.

### In scope (v1)

- **Records** — list with job filter + unified search (id/title/url/performer) + sort (id/title/date/performer); detail page styled like `SceneDetailPage.tsx`.
- **Performers** — list with job filter + name search; detail page styled like `TagDetailPage.tsx`, listing associated records grouped by job.
- **Status** — fleet + per-job featurization state, polling.
- **Query** — tabbed match-test page (`/match/fragment` + `/match/name`) with full parameter support, tabular results, copy-as-JSON.

### Non-goals

- No write paths to Stash, the extractor, or the bridge cache. The viewer is purely a presenter.
- No alias collapse for performers (case-insensitive equality only — the bridge handles alias resolution at match time, not at browse time).
- No featurization-state gate on browse pages. Per CLAUDE.md §14, the 503/Retry-After contract applies to `/match/*` only. Browse views render directly from `extractor_results` (stable at job fetch time) and surface featurization state as a passive badge.
- No URL-mode tab on the Query page (`/match/url`). Drop unless requested later.
- No "pick a scene" autocomplete on the Query page. User supplies a Stash scene id.

---

## 2. Architecture

```
docker-compose.yml (existing)
├── bridge          (existing FastAPI + SQLite + featurization workers)
├── extractor       (existing, external image)
└── viewer          (NEW; node:20-alpine; Vite SPA + Express proxy)
                    └── network: extractor_network
                    └── depends_on: bridge
                    └── env: BRIDGE_URL, STASH_URL, STASH_API_KEY?, STASH_SESSION_COOKIE?
```

The viewer's Express layer proxies:
- `/api/*` → bridge (`/api/admin/*` for browse data, `/api/featurization/*` and `/match/*` already exist).
- `/api/asset/{job_id}/...` → bridge (already exists; passthrough).
- Optional Stash GraphQL passthrough only if a future page needs it. Not used in v1.

```
stash-extract-db/
├── bridge/                          # existing
├── stash-extract-scraper/           # existing
├── viewer/                          # NEW
│   ├── Dockerfile
│   ├── docker-compose.yml           # (none — wired into root compose)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── tsconfig*.json
│   ├── index.html
│   ├── server/
│   │   ├── index.ts                 # Express bootstrap, request logging, SPA fallback
│   │   └── routes.ts                # bridge proxy + Stash auth header injection (if needed)
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                  # router config
│       ├── index.css                # CSS-variable dark theme (copy from stash-viewer)
│       ├── components/              # Card, Badge, SearchInput, Pagination, Layout, Lightbox, Tooltip, Tabs, Select
│       ├── hooks/                   # useDebounce
│       ├── lib/
│       │   ├── api.ts               # typed client over /api/admin/*
│       │   ├── cn.ts
│       │   └── configStore.ts       # zustand: stashUrl, jobs list cache
│       └── pages/
│           ├── RecordsPage.tsx
│           ├── RecordDetailPage.tsx
│           ├── PerformersPage.tsx
│           ├── PerformerDetailPage.tsx
│           ├── StatusPage.tsx
│           └── QueryPage.tsx
└── docs/VIEWER_PLAN.md              # this file (transient)
```

Stack: Vite + React 18 + TypeScript + Tailwind 3 + Radix (Dialog, Select, Tabs, Tooltip) + TanStack Query 5 + react-router-dom 6 + zustand. Server: Express 4 + tsx. No Redis, no WebSocket. Mirrors `stash-viewer`'s package.json near-verbatim.

---

## 3. Bridge changes

Read-only and additive. No matching code path is touched.

### 3.1 New table: `performer_index`

```sql
CREATE TABLE IF NOT EXISTS performer_index (
  job_id        TEXT NOT NULL,
  result_index  INTEGER NOT NULL,
  name_lower    TEXT NOT NULL,        -- normalized for search/grouping
  name_original TEXT NOT NULL,        -- as it appeared in the record
  PRIMARY KEY (job_id, result_index, name_lower),
  FOREIGN KEY (job_id, result_index) REFERENCES extractor_results(job_id, result_index) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_performer_index_name ON performer_index(name_lower);
CREATE INDEX IF NOT EXISTS idx_performer_index_job ON performer_index(job_id);
```

**Population**: extend `cdb.upsert_job_and_results` — after inserting `extractor_results` rows in the same transaction, parse `data_json.performers` (list of strings) and insert one row per `(job_id, result_index, lower(name))`. Dedupe within a record (set semantics). Skip null/empty names. Same atomic txn as the rest of the cascade (CLAUDE.md §14.5 compliant).

**Cascade**: FK `ON DELETE CASCADE` chains through `extractor_results` → `extractor_jobs`, so the §14.5 atomic transaction picks up performer_index rows automatically without an explicit DELETE.

**Migration**: `init_db()` runs the `CREATE TABLE IF NOT EXISTS`; for existing deployments, on first startup the table will be empty. Add a one-time backfill: scan `extractor_results` and populate. Idempotent (PK conflict = skip). Wrap in a startup task; logs progress.

### 3.2 New endpoints under `/api/admin/*`

Same FastAPI app, new module `bridge/app/api/admin.py`:

| Endpoint | Query params | Returns |
|---|---|---|
| `GET /api/admin/jobs` | — | `[{ job_id, job_name, completed_at, record_count }]` for scene-shaped jobs only |
| `GET /api/admin/records` | `job_id?`, `q?`, `sort=id\|title\|date\|performer`, `dir=asc\|desc`, `page=1`, `limit=24` | `{ data: Record[], pagination }` |
| `GET /api/admin/records/{job_id}/{result_index}` | — | Full record incl. resolved asset URLs (relative paths rewritten through `/api/asset/...`) |
| `GET /api/admin/performers` | `job_id?`, `q?`, `page=1`, `limit=24` | `{ data: [{ name_lower, name_display, record_count, job_count }], pagination }` |
| `GET /api/admin/performers/{name}` | — | `{ name_display, jobs: [{ job_id, job_name, records: [Record] }] }` |

`Record` shape (display projection):

```ts
{
  job_id: string;
  result_index: number;
  id: string | null;            // extractor data.id (Code in Stash output)
  title: string | null;
  details: string | null;
  url: string | null;           // page_url
  date: string | null;          // YYYY-MM-DD
  cover_image: string | null;   // resolved through /api/asset/{job_id}/...
  images: string[];             // resolved
  performers: string[];         // name_original list
  performer_count: number;
  image_count: number;          // len(images) + (cover_image ? 1 : 0)
  feature_state: 'ready' | 'featurizing' | 'failed' | null;
  feature_progress: number | null;  // 0..1 when featurizing
}
```

`feature_state` is read from `job_feature_state` joined on `job_id`. Display-only. Never blocks.

### 3.3 Search semantics on `/api/admin/records`

`q` is a single string. Match if **any** of: `data.id == q` (exact), `LOWER(data.title) LIKE %q%`, `page_url LIKE %q%`, OR record has any performer where `name_lower LIKE %q%` (via subquery on `performer_index`). Wildcards in user input are escaped (`%` → `\%`, `_` → `\_`). Empty `q` returns all matching `job_id` filter.

### 3.4 Sort semantics on `/api/admin/records`

| Sort key | SQL |
|---|---|
| `id` | `ORDER BY data->>'id'` (sqlite JSON1) |
| `title` | `ORDER BY LOWER(data->>'title')` |
| `date` | `ORDER BY data->>'date'` (ISO YYYY-MM-DD sorts lexicographically) |
| `performer` | `ORDER BY (SELECT MIN(name_lower) FROM performer_index WHERE job_id=er.job_id AND result_index=er.result_index)` |

Tiebreak in all cases: `(job_id, result_index)` ascending — consistent with CLAUDE.md §10.

### 3.5 Pagination

`limit` default 24, max 100 (mirrors stash-viewer). `page` 1-indexed. Total count via `COUNT(*)` over the same WHERE clause.

---

## 4. Viewer pages

### 4.1 Records list (`/`, `/records`)

Card grid (1/2/3 cols responsive). Top bar: job dropdown (Radix Select, populated from `/api/admin/jobs`, "All jobs" option), unified search input (debounced 300ms), sort dropdown, sort-direction toggle. URL state via `useSearchParams` so refreshes preserve filters.

Card content: cover (or first image, or placeholder), title (or "Untitled"), 2-line clamp on details, footer row with date / `N performers` / `M images`. Click → detail.

### 4.2 Record detail (`/records/:job_id/:result_index`)

Two-column layout (mirrors `SceneDetailPage`):
- Left: large cover image, then images[] grid (lightbox on click).
- Right: title (heading), date, url (clickable), code/id, details (multi-paragraph), performers (Badge per name, links to performer detail), and a "Match index" status badge (passive, links to Status page filtered to job).

No raw JSON (per spec).

### 4.3 Performers list (`/performers`)

Same card pattern as Records. Card: name (heading), `N records across M jobs`. Job filter narrows to performers appearing in that job. Search debounced. Click → detail.

### 4.4 Performer detail (`/performers/:name`)

Mirrors `TagDetailPage.tsx`:
- Header: name (URL-decoded), aggregate stats.
- Sections grouped by job: each section is a job heading + a small grid of record cards (smaller variant of the records card).

### 4.5 Status (`/status`)

Two panels:
- **Fleet**: one row of stats from `GET /api/featurization/status` (`queued | in_progress | ready | failed | concurrency_limit`). Auto-refresh every 5s while any in_progress > 0.
- **Per-job**: table of jobs (job_id, name, state, progress bar, started_at, error if any). Pulled from `/api/admin/jobs` joined with `/api/extraction/{job_id}/features`. Filterable by state.

### 4.6 Query (`/query`)

Radix Tabs: **Fragment** / **Name**.

**Fragment tab**:
- Required: `scene_id` (text), `mode` (scrape | search radio).
- Optional collapsible "Advanced" section: `image_mode`, `threshold`, `limit`, `hash_algorithm`, `hash_size`, `sprite_sample_size`, `image_gamma`, `image_count_k`, `image_channels` (multiselect: phash, color_hist, tone, embedding), `image_min_contribution`, `image_bonus_per_extra`, `image_search_floor`. All match the OpenAPI types and ranges. `debug` checkbox.
- Submit → `POST /match/fragment` → render results.

**Name tab**:
- Required: `name` (text), forces `mode=search` (per OpenAPI).
- Same advanced block as Fragment.
- Submit → `POST /match/name`.

**Result rendering**:
- Tabular layout: one row per candidate. Columns: rank, `Title`, `Code`, `Studio`, `Image` (thumbnail), `Performers`, `score` (search) or `definitive` (scrape).
- Below the table: collapsed `<details>` with the raw JSON response and a "Copy" button. When `debug=1` was set, the per-candidate `_debug` block is also rendered as a sub-table (channel-by-channel scoring).
- 503 from bridge → friendly inline message: "Featurization in progress for this job. Retry in N seconds." with a manual retry button. Status badge → links to Status page.

---

## 5. Auth

### 5.1 Stash credentials

Viewer reads `STASH_API_KEY` and `STASH_SESSION_COOKIE` from env (same vars the bridge uses). No new config. No login UI. Single-tenant by deployment.

The viewer's Express layer **does not** call Stash GraphQL in v1 (the bridge already handles all Stash-side work). The env vars are read for forward-compatibility with future pages that might (e.g., scene autocomplete on Query page). Document them in `viewer/.env.example` and reference the bridge's existing `.env`.

### 5.2 Viewer-to-bridge

In-network only (`extractor_network`). No auth between containers in v1. If exposed externally, the host operator is responsible for adding a reverse proxy with auth.

---

## 6. Featurization state surfacing (resolves open question #6)

**Decision**: passive status badge on Record detail and a sortable column on Status page. Never blocks browse.

- Record detail badge variants: `ready` (green), `featurizing X%` (blue, animated), `failed` (red, tooltip with error), `none` (gray, "not yet seen").
- Click → Status page with `job_id` URL param pre-filtering the per-job table.
- Performer detail does not surface state per-record (multiple jobs in scope; would be noisy). If needed later, add a per-job-section badge.

Rationale: §14's 503 contract is about the *match* path. Browse data is stable from `extractor_results` insertion onward; gating display on featurization would be a category error.

---

## 7. Phased delivery

Each phase is independently testable and mergeable.

- [ ] **Phase 1** — Bridge: `performer_index` schema + populate hook in `cdb.upsert_job_and_results` + startup backfill + unit tests for cascade behavior.
- [ ] **Phase 2** — Bridge: `bridge/app/api/admin.py` with the five endpoints + unit tests for search, sort, pagination, performer aggregation.
- [ ] **Phase 3** — Viewer scaffold (Dockerfile, vite, Tailwind, Layout/Card/Badge/SearchInput/Pagination copied from stash-viewer, compose entry on `extractor_network`, `/health` proxy works).
- [ ] **Phase 4** — Records list + detail pages.
- [ ] **Phase 5** — Performers list + detail pages.
- [ ] **Phase 6** — Status page (polling, no WebSocket).
- [ ] **Phase 7** — Query page (Fragment + Name tabs, full parameter support, copy-as-JSON, debug rendering).
- [ ] **Phase 8** — Polish: loading skeletons, empty states, error boundaries, README updates.

Phases 1–2 are bridge work and need new pytest coverage. Phases 3–8 are React work; verify by running the dev server and exercising each page in a browser per project guidance ("UI changes verified by hand").

---

## 8. Testing

### 8.1 Bridge

New unit tests under `tests/unit/`:

- `test_performer_index.py`
  - `test_upsert_populates_performer_index` — insert job with 3 records, assert performer_index rows.
  - `test_cascade_clears_performer_index` — advance `completed_at`, assert old rows gone.
  - `test_dedup_within_record` — record with duplicate performer names produces one row.
  - `test_skip_null_empty_names` — record with `null` or `""` performer is skipped.
  - `test_backfill_idempotent` — running backfill twice is safe.

- `test_admin_endpoints.py`
  - `test_records_search_id_title_url_performer` — verify `q` matches across all four fields.
  - `test_records_sort_by_each_field` — assert ordering for all four sort keys.
  - `test_records_pagination` — total/totalPages math.
  - `test_records_job_filter` — narrows correctly.
  - `test_performers_aggregation` — counts records and jobs correctly.
  - `test_performer_detail_groups_by_job` — shape matches spec.
  - `test_record_detail_resolves_asset_urls` — relative paths rewritten.

### 8.2 Viewer

Manual smoke per project guidance — no automated UI test harness in v1. For each page: load, search, paginate, click through, refresh (URL state should persist filters).

Verify against bridge running at `http://192.168.50.93:13000` with real corpus data.

---

## 9. Accepted risks (per user)

- **No alias collapse for performers.** Two records with `"jane Doe"` and `"Jane Doe"` collapse (case-insensitive). Two with `"Jane D"` and `"Jane Doe"` do not. The bridge's match-time alias resolution is unaffected; this is a browse-only convenience.
- **Query page has no scene-id picker.** User must know the Stash scene id.
- **No auth between viewer and outside world by default.** Operator's responsibility if exposed beyond the docker network.
- **Migration backfill for `performer_index` runs on first startup with the new schema.** For large corpora this could take seconds-to-minutes. Logged with progress; no behavioral impact (browse pages return empty results until backfill completes for any given job, which is the same UX as a never-seen job).

---

## 10. Out of scope (could ship later)

- Stash scene autocomplete on the Query page.
- Per-record image features panel (`q_i`, `c_i`, channel breakdown) on Record detail. The Query page's `?debug=1` already exposes this for any test match.
- Performer alias graph (would require either a new index from the bridge's `findPerformers` calls or a manual mapping table).
- WebSocket-driven live status. Polling at 5s is sufficient for v1.
- Internationalization, accessibility audit beyond Radix defaults, mobile-first responsive tuning.
- Authentication / multi-user. Single-tenant deploy assumed.
- URL-mode tab on the Query page.

---

## 11. Folding into CLAUDE.md on merge

When merging to `main`, lift these into CLAUDE.md and delete this file:

- **§17 (new)** "Viewer service is a read-only presenter" — the boundary contract. Mirrors §1's framing for the scraper: viewer adds no scoring, only presents.
- **§7 invalidation table** — add `performer_index` row: cascade from `extractor_results` (transitive `ON DELETE CASCADE` from `extractor_jobs`).
- **§14.5 cascade transaction** — note that `performer_index` rows are picked up automatically via FK chain; no explicit DELETE needed. Confirm with a regression test.
- **§15 cache schema invariants table** — add `performer_index` row: `(job_id, result_index, name_lower)` PK, role "display-only performer aggregation index, populated synchronously with `extractor_results`."
- **§8 output mapping rules** — no change. Viewer's display is independent of Stash output.
- **§16 troubleshooting table** — add row: "Performer not appearing in viewer" → check `performer_index` for the job; if empty, backfill task may have failed (logs).

The viewer source itself is durable code in the repo — its own README will live at `viewer/README.md` and be referenced from the root README's documentation map.
