# `stash-extract-db` — Architectural Invariants

This document captures the load-bearing contracts that must hold to keep the system coherent. It is intentionally short. If a change appears to violate any rule below, stop and check with the human before proceeding — you are likely about to introduce a silent corruption.

For *what* to build, see [`requirements.md`](requirements.md). This file covers *what must always be true*.

---

## 1. Configuration ownership

> **All matching parameters originate in the scraper's `config.py`. The bridge has no fallback.**

Threshold, image mode, search limit, hash algorithm, hash size, sprite sample size — every match-shaping parameter is sent in every request from the scraper. If the bridge receives a request missing a required parameter, it returns `400 Bad Request`. The bridge does **not** ship default values for these.

**Why**: there is exactly one place a user changes behavior — `~/.stash/scrapers/stash-extract-db/config.py`. Bridge env vars are for *infrastructure* (URLs, auth, data dir, log level), not heuristics. Drift between scraper config and bridge config is a class of bug we refuse to introduce.

**Don't**: add `DEFAULT_THRESHOLD`, `DEFAULT_IMAGE_MODE`, etc. to bridge env. Don't read these from a JSON file on the bridge. The scraper is the single source of truth.

---

## 2. Mode semantics

> **Scrape returns one or none. Search returns ranked. Never blur the line.**

- **Scrape mode** is binary: a definitive signal fires (Studio+Code, Exact Title, or Image≥threshold) → return that record. None fires → return `{}`. Never return a "best-effort" candidate that didn't fire a definitive signal. Stash treats a returned record as a contract; a non-definitive scrape result is a lie.
- **Search mode** is ranked: every candidate gets a composite score, top-N returned. Empty list is allowed when no candidate scored above zero (rare).

**Don't**: in scrape mode, return the highest-scoring candidate when no definitive signal fired. **Don't** in search mode, gate by the image threshold (it's a multiplier-rule, not an inclusion-rule — see §5).

---

## 3. Scrape cascade order is cheap-first by intent

> **Studio+Code → Exact Title → Image. The order is an optimization, not a priority.**

All three signals are equivalently definitive. Reordering does not change *which* records can match — only *how fast* a request returns. Image hashing is the expensive operation; defer it.

**If you change the order**, you must ensure: (a) the new order is still cheap-first, and (b) the outcome remains identical (which it will, given binary signals).

---

## 4. Image: threshold-gated in scrape, unconditional in search; distribution-sensitive in both

> **Scrape uses above-threshold soft-OR. Search uses unconditional soft-OR. The threshold applies only to scrape.**

For each candidate, the engine first computes **per-extractor-image similarities** — for each extractor image (`cover_image` + `images[]`), the best similarity against the configured Stash-side hash set (cover, sprite frames, or union per `image_mode`). This produces an array of N sims, one per extractor image. See §13 for the why.

Then aggregation differs by mode:

- **Scrape** — `aggregate_scrape(sims, threshold)`: filter to sims ≥ threshold, then soft-OR. Returns 0 if no sim clears the threshold (the candidate doesn't fire the image tier). Among firing candidates, the aggregate is the rank score; tiebreak by `result_index`.
- **Search** — `aggregate_search(sims)`: unconditional soft-OR over all per-image sims. The threshold is **not** consulted in search.

This replaces the prior `raw_sim if ≥ threshold else 0.5*raw_sim` rule. The new rule is simpler and stronger: in search every match contributes proportional to its strength and the number of matches; in scrape only above-threshold matches count, but multiple of them outrank a single borderline one.

**Don't**: filter search candidates by `image_sim >= threshold`. **Don't**: re-introduce the 0.5-multiplier rule. **Don't**: collapse the per-image sims to a single max before aggregation — the distribution carries the signal (§13).

---

## 5. Studio is the only job-level filter

> **Match by case-insensitive equality of `job.name` and `scene.studio.name`. No fuzzy match. No alias table. No fallback.**

- Scene has studio AND a job's name matches (case-insensitive) → search domain = that **one** job.
- Scene has studio AND no job matches → return empty (`{}` for scrape, `[]` for search).
- Scene has no studio → search domain = **all** scene-shaped jobs ("caveat utilitor").

**Don't** add a fuzzy-match fallback "to be helpful" — it loses determinism. **Don't** silently widen the search domain on no-studio-match — return empty and let the user notice.

---

## 6. Schema-shape detection is by superset, not by template id

> **A job qualifies as "scene-shaped" iff its schema fields are a superset of `{title, url, cover_image, images, performers, date, details, id}`.**

Users can clone, rename, or manually construct schemas. The seeded `"Video Scene"` template has a known id, but a user might clone it and add fields, or build the same shape from scratch. Field-set superset check is the durable contract.

**Don't** check `schema.is_template`. **Don't** check `schema.name == "Video Scene"`. **Don't** hard-code the seeded template's id.

---

## 7. Cache invalidation triggers

> **Each cache layer has exactly one invalidation key. Mixing them is a corruption hazard.**

| Cache | Invalidation key |
|---|---|
| `extractor_jobs` row | extractor `completed_at` change |
| `extractor_results` rows | cascade from `extractor_jobs` (`ON DELETE CASCADE`) |
| `image_hashes` (Stash cover) | `?t=<epoch>` query parameter on screenshot URL |
| `image_hashes` (Stash sprite) | `oshash` from `files[].fingerprints` |
| `image_hashes` (extractor) | asset `etag` or `content_hash` response header |
| `match_results` | composite of scene fingerprint + job `completed_at` |

**Don't** invalidate caches manually on a hunch. **Don't** add a TTL to any of these — TTLs hide bugs that the fingerprint-based invalidation would catch.

When the extractor job's `completed_at` advances, all of: result rows, extractor-side image hashes for that job, and match_results referencing that job — must be purged together. This is the one cross-table invalidation; treat it as an atomic transaction.

---

## 8. Output mapping rules

> **`Studio` is echoed back. `Code` is the extractor `id`. `images[]` is matching-only — never returned.**

| Stash output | Source | Notes |
|---|---|---|
| `Studio.Name` | echo of input studio | Stash already has it; we confirm by echoing |
| `Code` | extractor `data.id` | omit if extractor id is null |
| `Image` | extractor `data.cover_image` | base64 data URI, fetched via `/api/asset/...` |
| `Performers[]` | extractor `data.performers`, alias-resolved against Stash | each entry is `{Name, Aliases?}` |
| `images[]` | (input only) | matching signal; never appears in output |

**Don't** put extractor `id` into Stash `URL`. **Don't** fold `images[]` into `Details` or any other output field. **Don't** override `Studio.Name` with extractor data — the user already chose the studio in Stash.

---

## 9. The bridge never modifies Stash

> **The bridge is a read-only proxy on the Stash side. All writes go through Stash's normal scraper apply path.**

Bridge GraphQL operations: `findScene`, `findPerformers`. Nothing else. No mutations, no scene patching, no tag creation, no studio creation.

**Don't** add a "write back" mode. **Don't** create performers or studios on the fly. If a user wants to customize the apply step, they do it in Stash's scrape UI.

---

## 10. Tiebreaks are deterministic

> **Equal scores → lowest `result_index` from `/api/extraction/{job_id}/results?sort_dir=asc` ascending.**

This applies in both scrape (cascade tier ties) and search (composite score ties). The bridge never randomizes, never uses creation order from a different sort, never uses the extractor record's `id` (often null).

**Don't** introduce alternate tiebreak rules per request type. One rule, applied everywhere.

---

## 11. Empty-and-null is not penalty

> **Missing data on either side neutralizes the relevant signal — never penalizes.**

When `scene.title` is empty: title signal does not fire (and does not contribute to search score). It does *not* subtract from the score. Same for `scene.code`, `scene.date`, `scene.performers`, and any extractor-side null.

**Why**: penalizing absent data biases against scenes with thin metadata — exactly the population this bridge exists to help.

**Don't** add negative score components. **Don't** penalize a candidate for lacking a field the scene also lacks.

---

## 12. Filename score is `max(channels) + structured_bonus`, never `mean`

> **Filename comparison is multi-channel: a clean naive match must never be dragged down by a poor guessit parse, and vice versa.**

Channels:
1. **Naive normalize → RapidFuzz `WRatio`** — robust on short, clean filenames.
2. **Guessit-parsed title → RapidFuzz `token_set_ratio`** — strips release/resolution/codec/group noise.
3. **Structured field exact matches** (`year`, `season`, `episode`, `screen_size`) — small additive bonuses when both sides parsed a non-null value AND the values match.

Composition: `min(1.0, max(naive, guessit_title) + structured_bonus)`.

**Why `max` and not `mean`**: when one parser's strong path applies, the other's weak path is a *failure of analysis*, not a contradiction. Mean would punish the file for being analyzable in only one way. Max preserves the strongest available signal.

**Why structured bonus is *additive***: agreement on year/episode is independent corroborating evidence, not redundant with text similarity. A file that scores 0.85 on text *and* matches year-episode should score higher than one that just scores 0.85 on text.

**Don't**: switch to mean to "smooth out" outliers — short clip filenames will silently regress. **Don't**: turn structured bonuses into multiplicative weights — they degrade to 0 when one side is null. **Don't**: add a new channel without ensuring it can fail to 0 cleanly — channels are union-of-evidence, not intersection.

The full breakdown is observable via `?debug=1` on `/match/*` endpoints (search mode only — scrape returns single result or empty).

---

## 13. Image scores are aggregated distribution-sensitively (soft-OR), never collapsed to max

> **Per-extractor-image best-match sims are computed first; then aggregated. The aggregation rewards both peak strength and number of matches.**

Exact image matches between independently-encoded scenes are rare. A scene that genuinely matches an extractor record will typically produce multiple medium-to-strong similarities across that record's image set, while an unrelated scene will produce a flat distribution of low scores even with many images.

Concrete: a record with `[0.1, 0.1, 0.1, 1.0]` outranks one with `[0.13, 0.13, 0.13, 0.13]`, despite the second having a higher mean and the same `max(sim) - mean(sim)` spread. Distribution-sensitive aggregation captures this; `max()` alone would tie them at 1.0 vs 0.13 (still right here) but loses information when no perfect match exists.

The aggregation is **soft-OR** (`1 - prod(1 - s for s in sims)`):
- Bounded `[0, 1]` so it composes cleanly with the search score (`+= contribution`, capped at 1.0).
- Saturates at 1.0 when any sim is 1.0 — a perfect match dominates regardless of weak siblings.
- Multiple weak matches accumulate but never overwhelm a single strong match.
- Symmetric in input order; deterministic.

For sprite mode (`M` Stash sprite frames × `N` extractor images), we collapse the M×N matrix **per extractor image** — for each extractor image, the best matching Stash sprite frame is its score. Aggregation then runs over those `N` per-image scores, mirroring cover mode's structure. We do **not** flatten all M×N pairs into one bag — that would let many weak frame-image pairs accumulate spurious signal.

**Don't**: replace soft-OR with arithmetic or geometric mean — both lose the "one strong match dominates" property. **Don't**: aggregate over Stash-side dimensions (frames) — only over extractor images. **Don't**: skip the per-image step and pre-flatten — the distribution would dissolve into a single max.

---

## 14. Where to look first when something breaks

| Symptom | First place to check |
|---|---|
| Wrong scene returned in scrape | `bridge/app/matching/scrape.py` cascade order; check candidate's `id`, `title`, image_sim values in logs |
| Search results in odd order | `bridge/app/matching/search.py`; verify `min(score, 1.0)` cap; check tiebreak by `result_index` |
| "No result" when one was expected | §5 studio filter — does scene have a studio that matches a job name? §6 schema superset — does the job's schema actually have all canonical fields? |
| Image match never fires | Asset URL resolution — extractor records use `../assets/<file>` which must rewrite to `/api/asset/{job_id}/assets/<file>`. Check ETag handling. |
| Performer match always 0 | Alias index freshness — check TTL, check `findPerformers` query uses both `name` and `aliases` filters with `OR` |
| Cache returning stale results | §7 — `completed_at` change should have triggered cascade. Check `extractor_jobs.completed_at` against fresh `GET /api/jobs/{id}` |
| 400 Bad Request from bridge | §1 — scraper's `config.py` is missing a required parameter; the bridge has no fallback |
| Filename score lower than expected | §12 — request with `?debug=1` and inspect `_debug.filename` for naive/guessit/structured channel breakdown; verify guessit isn't mis-parsing the title |
