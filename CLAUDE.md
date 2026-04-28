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

## 13. Image scores are aggregated as `top-K mean over the flat M×N pair set`, with K = number of extractor images

> **Every Stash-side hash compared against every extractor-side hash. The flat M×N similarity set is then aggregated by taking the mean of the top K values, where K is the number of usable extractor images. Threshold gates the *aggregate*, not individual pair sims.**

Exact image matches between independently-encoded scenes are rare. A scene that genuinely matches an extractor record produces multiple medium-to-strong similarities scattered through the M×N pair grid; an unrelated scene produces a flat distribution of low values with the occasional spurious outlier (hash collision, shared decorative asset, etc.). The aggregation must distinguish "K consistent strong pairs" (real match) from "one outlier strong pair + many weak" (false positive).

### The aggregation: `top_k_mean(sims, k=N)` where N = |extractor images|

```
score = mean(sorted(sims, reverse=True)[:N])
```

- Bounded `[0, 1]` — composes cleanly with the search score (`+= contribution`, capped at 1.0).
- For each extractor image, you'd hope to see at least one strong sprite-frame match in a real match. Top-N picks the N best pairs in the entire grid as the record's strongest evidence; mean averages them.

### Worked examples

Real match (M=8 sprite frames × N=5 extractor images = 40 pairs; ~5 strong pairs at 0.85, ~35 weak at 0.15):
- top-5 ≈ [0.85, 0.85, 0.85, 0.85, 0.85] → **0.85**

False match (one shared/coincident image hashing identically to one sprite frame; the rest of the record's images are unrelated):
- top-5 = [1.0, 0.15, 0.15, 0.15, 0.15] → **0.36**

A 0.5 threshold then admits the real match and rejects the false. Note the threshold is on the **aggregate**, not on individual pair sims — that's what kills the one-outlier false positive.

### Why we ditched the previous strategies

- **Per-extractor-image collapse + peak+mean** (previous attempt): per-image collapse takes max-across-frames for each extractor image, producing N values; peak+mean aggregates those. Worked on the user's `[0.1, 0.1, 0.1, 1.0]` toy example (scored 0.66) but didn't reflect the user's actual intent — they want the full M×N pair distribution evaluated, not the dimension-collapsed N-vector.
- **Soft-OR** (`1 - prod(1 - s)`): saturates at 1.0 the moment any single sim is 1.0. One shared/coincident image (hash collision on real-but-unrelated content) pinned the aggregate to 1.0 regardless of the rest of the record. Variance and degeneracy filters help but can't catch real-content collisions.
- **Max alone**: same failure mode as soft-OR — single outlier dominates.
- **Mean over flat M×N**: ignores the strong-match signal. A real match's strong pairs get diluted by 30+ weak frame×unrelated-image pairs.
- **Peak + mean over flat M×N**: the dilution from M×N pairs makes mean tiny, so the score collapses to roughly `(peak + small)/2 ≈ peak/2`. Single-outlier false positives still win — verified arithmetically on the field-observed case.

Top-K mean dodges all these:
- Outlier 1.0 in a sea of low sims → top-K still includes the K-1 weak runners-up → mean drops it.
- Mean over the full flat set → would average 35+ weak pairs in; top-K avoids that.
- Max → ignores corroborating matches; top-K rewards them.

### K = number of extractor images, not min(M,N) or max(M,N)

The semantics are "for each extractor image we'd hope to see one strong sprite-frame match." That's N expected strong pairs. K = N is the natural fit. Note that top-N over an M×N grid can pick multiple strong pairs from the same extractor image (when adjacent sprite frames are visually similar) — this is fine; redundant matches are still corroborating evidence, just less than N truly independent matches.

When the record has only N=1 extractor image, K=1 and the aggregate degenerates to `max(sims)` — for a 1-image record that's the only sensible answer, and the threshold guards against accepting a single weak match as definitive.

### Threshold gates the aggregate, not individual sims

In scrape mode the threshold is now applied to the top-K mean: `aggregate >= threshold → fires`. This is a deliberate change from the prior "any sim ≥ threshold" gate. With the new gate:
- A single 1.0 sim no longer bypasses the threshold automatically.
- A consistent set of moderate matches (e.g. all sims ≈ 0.65 with threshold 0.6) DOES fire, because the aggregate carries evidence that one sim alone wouldn't.

**Don't**: switch back to per-extractor-image collapse — the user's intent is full M×N evaluation. **Don't**: switch back to soft-OR — saturation re-introduces the false-positive class. **Don't**: gate the threshold on individual sims — that re-introduces the one-outlier vector. **Don't**: change K without thinking through whether the new value preserves "one expected strong match per extractor image" semantics.

### Input filtering — must happen *before* aggregation

Soft-OR's "one strong match dominates" property cuts both ways: a single bad sim of 1.0 will saturate the aggregate to 1.0 regardless of every other signal. So images that can produce spurious 1.0 sims must be filtered out before they ever reach `_sim`. Three filters apply, in order:

1. **404 / fetch failure** (`extractor/client.fetch_asset` returns `None`). Not every record's `images[]` was successfully downloaded by the extractor. Callers must drop refs whose hash is `None` from the per-image sims list — *not* append a 0.0 entry.
2. **Low pixel variance at hash time** (`imgmatch/image_comparison.hash_image_bytes` returns `None` if `np.var(normalized) < LOW_VARIANCE_THRESHOLD`). Catches all-black sprite frames (fade-in/out) and blank/placeholder extractor images at the source — they never produce a hash, never get cached, never participate.
3. **Degenerate-hash check at sim time** (`_is_degenerate_hash`). Belt-and-braces: a pHash with bit-density outside `[10%, 90%]` came from a near-uniform source. Catches anything that snuck through (e.g. a hash cached before filter #2 was added). `_sim` returns 0 on either operand being degenerate.

**Why this ordering**: filter #1 saves bandwidth (don't fetch what's known-missing), filter #2 saves cache and compute (don't hash uniform pixels), filter #3 is the safety net. All three result in the same downstream behavior: the ref is *omitted* from the per-image sims list. The list length should equal the number of usable comparisons, not the number of attempted ones.

**Don't**: append `0.0` entries for failed/uniform images — they're noise in debug output and tempt future maintainers to "fix" them. **Don't**: cache the string `"None"` as a hash — old code path; check that any new caller of `_hash_or_compute` handles `h is None` correctly (skip the cache write, return None to caller).

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
