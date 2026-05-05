# `viewer/` — operator UI for stash-extract-db

A read-only React SPA + Express proxy that browses the bridge's cached
records and performers, surfaces featurization status, and exercises the
match endpoints. Sibling container to the bridge; mounted on the same
`extractor_network`.

> Architectural plan: [`docs/VIEWER_PLAN.md`](../docs/VIEWER_PLAN.md).
> Bridge invariants: [`CLAUDE.md`](../CLAUDE.md).
> Read-only: no writes to Stash, the extractor, or the bridge cache.

---

## Pages

| Route                                           | What it shows                                                                                   |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `/records`                                      | Card grid of cached extractor records with job filter, unified search, and sort.                |
| `/records/:job_id/:result_index`                | Detail view: cover, images, performers, code, date, URL, match-index status badge.              |
| `/performers`                                   | Card grid of distinct performers (case-insensitive), aggregating record + job counts.           |
| `/performers/:name`                             | Detail view: associated records grouped by job.                                                 |
| `/status`                                       | Fleet stats + per-job featurization table. Auto-polls every 5 s while any job is in flight.     |
| `/query`                                        | Run a match against the bridge — Fragment (scene_id) or Name tabs, full parameter support, raw JSON. |

---

## Architecture

```
browser ──► viewer (Vite + Express, this container) ──► bridge or extractor
              port 3100                                  /api/admin/*, /match/*, /api/asset/*
```

- The Express layer proxies `/api/admin/*`, `/api/featurization/*`,
  `/api/extraction/*`, and `/match/*` to the **bridge**.
- It proxies `/api/asset/{job_id}/...` to the **extractor**, so the browser
  never reaches the extractor directly.
- `/health` is served locally — the viewer reports ready even when the
  bridge is restarting.

---

## Run it

### Via docker compose (production-like)

The root `docker-compose.yml` already includes the viewer service.

```bash
cp .env.example .env
$EDITOR .env                              # set STASH_URL etc.
docker compose up -d --build viewer
open http://localhost:13100
```

`VIEWER_PORT` (default 13100) is the host-side port. The container always
listens on 3100 internally.

### Local dev (hot reload)

From `viewer/`:

```bash
npm install
BRIDGE_URL=http://localhost:13000 \
EXTRACTOR_URL=http://localhost:12000 \
npm run dev
```

This starts:
- Vite dev server at <http://localhost:5174> (proxies `/api`, `/match`, `/health` to the Express server).
- Express server at <http://localhost:3100> (proxies upstream).

Open <http://localhost:5174>.

---

## Environment

| Variable                | Default                              | Purpose                                                                  |
| ----------------------- | ------------------------------------ | ------------------------------------------------------------------------ |
| `BRIDGE_URL`            | `http://localhost:13000`             | Where to reach the bridge service.                                       |
| `EXTRACTOR_URL`         | `http://localhost:12000`             | Where to reach the extractor (for asset proxying).                       |
| `PORT`                  | `3100`                               | Express listen port (container-internal).                                |
| `STASH_URL`             | `http://host.docker.internal:9999`   | Forward-compat (v1 viewer doesn't call Stash directly).                  |
| `STASH_API_KEY`         | (empty)                              | Forward-compat (see plan §5).                                            |
| `STASH_SESSION_COOKIE`  | (empty)                              | Forward-compat.                                                          |
| `NODE_ENV`              | `production` (in image)              | Switches Express to serve the built SPA from `dist/client`.              |

---

## Why featurization state never blocks browsing

`extractor_results` is the source of truth for *what to display* — its rows
are stable from the moment the bridge first fetches a job (CLAUDE.md §7).
Featurization is a *matching* concern (§14), gated behind the 503 contract
on `/match/*`.

The viewer surfaces feature state as a passive badge on Record detail and
as a sortable column on the Status page; it never gates rendering on it.
This is documented in the plan §6 as the resolution to the open question
about whether to spin / defer / omit when the index isn't ready.

---

## Project layout

```
viewer/
├── Dockerfile               # multi-stage node:20-alpine, port 3100
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig*.json
├── index.html
├── server/                  # Express bootstrap + proxy routes
│   ├── index.ts
│   └── routes.ts
└── src/
    ├── main.tsx             # ErrorBoundary + QueryClientProvider + BrowserRouter
    ├── App.tsx              # Route table
    ├── index.css            # CSS-variable dark theme
    ├── components/          # Card, Badge, SearchInput, Pagination, Lightbox,
    │                        # Tooltip, Layout, Select, RecordCard,
    │                        # FeatureStateBadge, ErrorBoundary
    ├── hooks/useDebounce.ts
    ├── lib/
    │   ├── api.ts           # Typed client over /api/admin/*, /match/*, /api/featurization/*
    │   └── cn.ts
    └── pages/               # RecordsPage, RecordDetailPage, PerformersPage,
                             # PerformerDetailPage, StatusPage, QueryPage
```

---

## Don'ts

- **Don't** write to Stash, the extractor, or the bridge cache from this
  service. The viewer is a presenter.
- **Don't** add scoring fields to the API client. All scoring lives on the
  bridge per CLAUDE.md §1; the Query page only forwards user-supplied
  values to `/match/*`.
- **Don't** access the extractor's URL directly from the browser — use the
  bridge-relative `/api/asset/{job_id}/...` form returned by the admin
  endpoints, which the Express layer rewrites to the extractor.
