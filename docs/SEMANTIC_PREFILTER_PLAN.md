# Semantic prefilter (Phase 2)

> **Status**: proposal. Review and sign off before implementation.
> **Branch**: continuation of `semantic`.
> **Predecessor**: [`SEMANTIC_MIGRATION_PLAN.md`](SEMANTIC_MIGRATION_PLAN.md) (Phase 1 — channel D).
> **Motivation**: Phase 1 smoke run on the 491-record Pexel job showed channel D lifts scrape-fire rate from 10% (baseline) to 93% and median composite from 0.07 to 0.88, but exposed two practical problems with the current scoring pipeline:
>
> 1. **`mixed` (A/B/C+D) is not deployable** — per-request cost is 50–99 s because A/B/C scoring iterates all candidates in Python loops. Stash's hard scrape timeout is ~90 s, so any request that includes A/B/C will time out on a corpus this size.
> 2. **`embedding-only` has a known failure mode** — D matches the *concept* (e.g. "ink in water", "pottery wheel") but can pick the wrong specific Pexel video when several candidates share the concept. Two of fourteen disagreement scenes (17383, 17401) hit this on the smoke run; both would have been resolved correctly by A/B/C's color histogram + pHash, which anchor on specific frame content.
>
> The fix: **D pre-filters all candidates to a top-K shortlist; A/B/C score only those K; the existing composite formula combines them**. Total cost stays at the embedding-only level (~17 s warm) because A/B/C work shrinks from 1579 candidates to ~10. We retain the corroboration bonus that catches D's concept-only failures.

---

## 1. Goals

- **Stay under Stash's 90 s scrape timeout** on corpora of any size.
- **Resolve D's concept-only failure mode** by re-ranking the top-K with A/B/C's frame-anchored signal.
- **Preserve §13 composition algebra** — `composite = max(fired) + bonus * (n_fired - 1)`. The math doesn't change; only the candidate set it operates on shrinks.
- **Provide a clean fallback** when embedding is disabled or absent — single-pass A/B/C path remains intact for legacy mode.

## 2. Non-goals

- We are not changing the per-channel scoring functions. `score_image_channel_a/b/c/d` are untouched.
- We are not changing the `image_features` schema, the cache cascade, or the featurization lifecycle.
- We are not changing the scraper, the Stash GraphQL contract, or the extractor API contract.
- We are not removing channels A/B/C. The two-stage path is the production path; legacy single-pass remains for `BRIDGE_EMBEDDING_ENABLED=false`.

## 3. Algorithm

```
match(scene, candidates, channels):
    if "embedding" not in channels OR not BRIDGE_EMBEDDING_ENABLED:
        # Legacy single-pass — current behavior
        for c in candidates:
            composite = score_image_composite(scene, c, channels)
        return top-1 by composite

    # Phase 2 two-pass
    # Pass 1: D-only score every candidate (one matmul, ~ms)
    d_scores = score_image_channel_d_batch(scene, candidates)
    top_k = sorted(candidates, key=d_scores.get, reverse=True)[:PREFILTER_K]

    # Pass 2: full per-channel composite over the K survivors only.
    # The cached d_scores are reused (no recompute) so D contributes its
    # already-known value to the composite.
    for c in top_k:
        composite = score_image_composite(scene, c, channels, cached_d=d_scores[c])
    return top-1 by composite
```

### 3.1 Composite reuse

`score_image_composite` already takes per-channel scores and combines them. The two-pass change is:

1. D's per-candidate score is **computed in pass 1 for all candidates** (single matmul over all extractor embeddings against Stash embeddings).
2. `score_image_composite` for the K survivors **accepts D's score as an input** rather than recomputing it. A/B/C still compute lazily as today.

The composite formula is unchanged:
```
fired = [s for s in (S_A, S_B, S_C, S_D) if s >= min_contribution]
composite = min(1.0, max(fired) + bonus_per_extra * (len(fired) - 1))
```

D's score is cached and reused; A/B/C are computed fresh on the K candidates only.

### 3.2 Why this resolves the concept-only failure

Smoke evidence:

| Scene | Stash | D top-1 (wrong) | True match | A/B/C signal |
|---|---|---|---|---|
| 17383 | green ink in water | scene_0090 (blue+red ink) | scene_0261 (green ink) | color hist clobbers 0090 (wrong palette), nails 0261 |
| 17401 | pottery, blue patterned pants | scene_0111 (white sleeves, different person) | scene_0333 (same patterned pants) | pHash + color hist nail 0333 (same frame composition) |

In both cases, D's top-K=10 almost certainly contains the correct candidate (D ranks all related concepts together), and A/B/C's frame-anchored signal correctly re-ranks within the K. The bonus mechanism then rewards corroboration: when D and A/B/C agree on the top-1, composite goes higher than D-alone; when they disagree, the candidate where multiple channels fire wins.

The mixed-bonus inversion observed at 17397 (where A/B/C corroborated a wrong candidate D didn't even prefer) **cannot happen in the two-pass design** — A/B/C only see candidates D already ranked highly.

## 4. Settings

One new env var:

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_IMAGE_PREFILTER_K` | `10` | Number of D-ranked candidates to re-score with A/B/C. Set to 0 to disable two-pass and use D-only. |

A K of 10 is comfortable on the Pexel-scale corpus (491 records); larger corpora (10K+) may want K=50–100. Cost scales linearly with K because A/B/C is the slow part.

The `min_contribution`, `bonus_per_extra`, and `image_threshold` settings from §13.9 are unchanged.

## 5. Code changes

### 5.1 New: batch D scoring

`bridge/app/matching/image_match.py::score_image_channel_d_batch`

```python
async def score_image_channel_d_batch(scene, candidates) -> dict[candidate_id, float]:
    """Returns {candidate_id: D-score} for every candidate in one matmul.

    Stacks all extractor-side embeddings into one matrix E (sum of N_i × D),
    Stash-side embeddings into S (M × D), computes sims = S @ E.T, then
    splits per-candidate via offsets to compute per-extractor-image max
    and final mean per candidate. ~10–50 ms total regardless of N.
    """
```

The current `score_image_channel_d` already does single-candidate matmul. The batch version is a refactor that shares the Stash-side encode and stacks all candidate embeddings.

### 5.2 Modify: composite accepts cached D

`bridge/app/matching/image_match.py::score_image_composite`

Add an optional `cached_channels: dict[str, dict] | None = None` parameter. When provided, the function uses the cached per-channel score dict instead of computing that channel. Today only D would be passed, but the parameter is general.

### 5.3 Modify: candidate iteration in scrape/search

`bridge/app/matching/scrape.py` and `bridge/app/matching/search.py`

Replace the existing single-pass candidate loop with the two-pass algorithm from §3. Both share the same shape — extract into a helper `_score_candidates(scene, candidates, channels)` to avoid duplicating the dispatch logic.

The fall-back single-pass path remains for the `BRIDGE_EMBEDDING_ENABLED=false` or `embedding not in channels` cases.

### 5.4 Settings + docs

- `bridge/app/settings.py`: add `bridge_image_prefilter_k: int = 10`
- `.env.example`: document under "Channel D" section
- `docker-compose.yml`: add env passthrough
- `docs/HOW_TO_USE.md`: §10 (Channel D) gains a subsection on K
- `CLAUDE.md`: §13.10 gains a subsection on the two-pass invariant — "D pre-filters; A/B/C corroborate; bonus rewards agreement on the K candidates D already ranked highly"

## 6. Edge cases

- **K ≥ N_candidates**: degrade to "score all", same as legacy single-pass. No off-by-one risk.
- **K = 0**: skip the second pass entirely — D-only output, equivalent to today's `image_channels=["embedding"]`.
- **D not in channels list**: fall through to legacy single-pass A/B/C. Unchanged path.
- **Embedding disabled but in channels**: D contributes 0 to every candidate; second pass runs on top-K=10 candidates ranked by 0, which is just the first 10 in iteration order. Configuration error; log a warning at startup.
- **Stash-side encode fails for some sprite frames**: existing negative-cache sentinel; D-batch uses only the frames that decoded.
- **One candidate has no extractor embeddings** (e.g. all assets failed to fetch): D score = 0 for that candidate; it falls out of the top-K naturally. No special handling.
- **Search mode top-N where N > K**: K is the prefilter for the *full-composite* re-rank. Search top-N reads from the K survivors. If the user asks for N > K (rare), return whatever the K re-rank produced; trailing positions get D-only scores (same shape as today's embedding-only search results).

## 7. Caching considerations

D's per-candidate score in pass 1 doesn't need persistent caching — it's already fast (single matmul). It does need **request-scoped caching** so pass 2 reuses it without recompute. The `cached_channels` parameter on `score_image_composite` handles this.

The Stash-side embedding encode is already cached (image_features rows, channel='embedding') — same path as today. No change needed.

## 8. Testing

### 8.1 Unit tests

- `tests/unit/test_image_match_prefilter.py` (new)
  - `test_two_pass_when_embedding_present`: mock D batch, mock A/B/C, assert A/B/C called only K times.
  - `test_legacy_single_pass_when_d_disabled`: assert no D batch call, all candidates scored.
  - `test_k_zero_returns_d_only`: assert composite == D score for all candidates.
  - `test_k_larger_than_n_scores_all`: assert no error, all N scored.
  - `test_cached_d_passed_to_composite`: assert `cached_channels` propagates through scrape/search.
  - `test_concept_failure_resolved_by_two_pass`: synthetic case where D ranks a wrong candidate first but right one in top-K, then A/B/C re-ranks correctly. (This is the 17383/17401 regression.)

### 8.2 Integration test

`tests/integration/test_calibration.py` already has the harness shape. Extend with a `--prefilter-k` parameter and run the same Pexel job in two modes:
- D-only baseline (current Phase 1 behavior)
- Two-pass with K=10

Assert two-pass precision-at-1 ≥ D-only precision-at-1, and per-request latency stays within 2× of D-only.

### 8.3 Smoke harness

`scripts/smoke_channel_d.py` gains a new config: `prefilter` (D + A/B/C re-rank top-10). Run on the Pexel job to verify:
- Same scrape-fire rate as embedding-only (~93%)
- Higher precision on the 14 disagreement scenes (17383 and 17401 should flip from "wrong" to "correct")
- Per-request latency comparable to embedding-only

## 9. Risks

| Risk | Mitigation |
|---|---|
| A/B/C still slow at K=10 — say 5 s per scrape | Tested already on the smoke run: A/B/C alone takes ~50 s for 1579 candidates, so 10 candidates = ~0.3 s. Comfortable. |
| D scores in pass 1 are *too good* — top-K never includes the right candidate when D was confidently wrong | At K=10 across 491 candidates this is implausible. We can spot-check the 14 disagreement scenes' top-10 D rankings before merging. |
| The `cached_channels` parameter pollutes `score_image_composite`'s signature | Keep it strictly internal — only `_score_candidates` passes it. Public callers don't see it. |
| Bonus inversion still possible at K=10 if A/B/C strongly favor a non-best D candidate | Non-zero risk. The bonus ensures corroboration agreement gets +0.1 per extra channel — but if D's #2 has both A and B firing strongly while D's #1 has none, #2 can win. Mitigation: if observed in practice, gate the bonus on D firing too (i.e. require D's contribution to count the bonus). Defer that decision to post-merge data. |
| K is per-corpus optimal — wrong default for sharper or noisier corpora | Make K configurable, default 10. Users can tune via env. Document in HOW_TO_USE that K should grow with corpus size — rough rule N/100, capped at 100. |

## 10. Rollout

Single PR on the `semantic` branch. No staged rollout needed because:

- New behavior gated by existing `BRIDGE_EMBEDDING_ENABLED` (already in place)
- New `BRIDGE_IMAGE_PREFILTER_K` defaults to 10; setting to 0 disables two-pass
- Legacy single-pass path preserved for `BRIDGE_EMBEDDING_ENABLED=false`

Once merged + verified on the Pexel corpus:

1. Re-run the smoke harness with `prefilter` config; expect 17383 and 17401 to flip to correct.
2. User's manual 240p production validation: test `prefilter` config; should be the same fast latency as embedding-only with the resolution-tolerant signal of D plus the specificity of A/B/C.
3. If 240p shows the same kind of concept-only failures embedding-only had, two-pass should clean them up automatically.

## 11. Files touched (estimate)

| File | Lines added | Lines changed | Lines removed |
|---|---|---|---|
| `bridge/app/matching/image_match.py` | ~80 | ~20 | 0 |
| `bridge/app/matching/scrape.py` | ~10 | ~10 | 0 |
| `bridge/app/matching/search.py` | ~10 | ~10 | 0 |
| `bridge/app/settings.py` | 1 | 0 | 0 |
| `.env.example` | ~8 | 0 | 0 |
| `docker-compose.yml` | 1 | 0 | 0 |
| `docs/HOW_TO_USE.md` | ~15 | 0 | 0 |
| `CLAUDE.md` (§13.10) | ~10 | 0 | 0 |
| `tests/unit/test_image_match_prefilter.py` | ~150 | 0 | 0 |
| `scripts/smoke_channel_d.py` | ~5 | ~5 | 0 |
| **Total** | **~290** | **~45** | **0** |

Single-session change.

## 12. Sign-off checklist

- [ ] Smoke harness `prefilter` config flips 17383 and 17401 to correct
- [ ] 30-scene Pexel run shows ≥ 93% scrape-fire rate, ≥ 89% precision-at-1 (matches or exceeds embedding-only)
- [ ] Per-request latency stays ≤ 25 s warm cache (within 2× of embedding-only)
- [ ] `tests/unit/test_image_match_prefilter.py` passes
- [ ] CLAUDE.md §13.10 updated to document the two-pass invariant
- [ ] `BRIDGE_IMAGE_PREFILTER_K` documented in HOW_TO_USE.md and .env.example
