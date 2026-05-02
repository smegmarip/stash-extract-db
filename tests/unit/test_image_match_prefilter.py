"""Unit tests for the Phase 2 prefilter wiring (per docs/SEMANTIC_PREFILTER_PLAN.md).

The two pieces under test are:
  - `score_image_channel_d_batch` — single-matmul scoring across N candidates
  - `score_image_composite`'s `cached_channels` plumbing — when present, D is
    not recomputed.

Heavy machinery (DB, mocked clients) is avoided here by stubbing the
embedding-side helpers directly. The integration check that ties everything
together against a live job is the smoke harness in `scripts/smoke_channel_d.py`.
"""
import io

import numpy as np
import pytest
from PIL import Image  # noqa: F401  (kept for parity with sibling tests)

from bridge.app.matching import image_match as im


# --- score_image_channel_d_batch ----------------------------------------

def _candidate(job, idx, cover, images):
    return {
        "job_id": job,
        "result_index": idx,
        "data": {"cover_image": cover, "images": list(images)},
    }


@pytest.fixture
def fake_embedding_blobs(monkeypatch):
    """Stub the four embedding sources so the batch function can run with
    no DB or HTTP. Each ref maps to a unique synthetic 1024-dim vector;
    the Stash-side cover is shared across all tests in the fixture.

    Returns a function `(stash_vec, ref_to_vec) -> None` that the test
    calls to install vectors for the run.
    """
    state = {"stash": None, "refs": {}}

    async def fake_extractor(job_id, ref):
        v = state["refs"].get((job_id, ref))
        if v is None:
            return None
        # blob_to_embedding will read this back as fp16; serialize accordingly
        return v.astype(np.float16).tobytes()

    async def fake_stash_cover(scene):
        v = state["stash"]
        return v.astype(np.float16).tobytes() if v is not None else None

    async def fake_stash_sprite(scene, sample_size):
        return []  # cover-only is enough for these tests

    monkeypatch.setattr(im, "extractor_image_embedding", fake_extractor)
    monkeypatch.setattr(im, "stash_cover_embedding", fake_stash_cover)
    monkeypatch.setattr(im, "stash_sprite_embeddings", fake_stash_sprite)

    def install(stash_vec, ref_to_vec):
        state["stash"] = stash_vec
        state["refs"] = ref_to_vec

    return install


class TestBatch:
    async def test_empty_stash_returns_zero_for_all(self, fake_embedding_blobs):
        # No stash embeddings → every candidate must score 0 without
        # touching extractor blobs.
        fake_embedding_blobs(stash_vec=None, ref_to_vec={})
        cands = [_candidate("J", 0, "c0", []), _candidate("J", 1, "c1", [])]
        out = await im.score_image_channel_d_batch({}, cands, "cover", 0)
        assert all(out[(c["job_id"], c["result_index"])]["S"] == 0.0 for c in cands)
        # Stash count is reported as 0 so debug envelope is honest
        assert all(out[(c["job_id"], c["result_index"])]["n_stash_hashes"] == 0 for c in cands)

    async def test_no_extractor_blobs_returns_zero(self, fake_embedding_blobs):
        # Stash exists but every candidate's extractor lookup misses.
        # Should short-circuit before the matmul, score 0.
        stash = np.ones(1024, dtype=np.float32) / np.sqrt(1024)
        fake_embedding_blobs(stash_vec=stash, ref_to_vec={})
        cands = [_candidate("J", 0, "c0", ["i0"])]
        out = await im.score_image_channel_d_batch({}, cands, "cover", 0)
        assert out[("J", 0)]["S"] == 0.0
        assert out[("J", 0)]["n_extractor_images"] == 0

    async def test_per_candidate_slicing_aligns(self, fake_embedding_blobs):
        # Build a Stash vector and three candidates with different N_i.
        # Confirm each candidate's S is the mean of its per-image cosines
        # (max over Stash, but here Stash has just one vector).
        rng = np.random.default_rng(0)

        def unit(v):
            return (v / np.linalg.norm(v)).astype(np.float32)

        stash = unit(rng.standard_normal(1024))
        # Candidate 0: 1 image, very similar to stash
        e0a = unit(stash + 0.05 * rng.standard_normal(1024))
        # Candidate 1: 2 images, one similar one orthogonal-ish
        e1a = unit(stash + 0.1 * rng.standard_normal(1024))
        e1b = unit(rng.standard_normal(1024))
        # Candidate 2: 3 images, all moderate
        e2a = unit(stash + 0.3 * rng.standard_normal(1024))
        e2b = unit(stash + 0.3 * rng.standard_normal(1024))
        e2c = unit(stash + 0.3 * rng.standard_normal(1024))

        ref_to_vec = {
            ("J", "c0"): e0a,
            ("J", "c1a"): e1a, ("J", "c1b"): e1b,
            ("J", "c2a"): e2a, ("J", "c2b"): e2b, ("J", "c2c"): e2c,
        }
        fake_embedding_blobs(stash_vec=stash, ref_to_vec=ref_to_vec)

        cands = [
            _candidate("J", 0, "c0", []),
            _candidate("J", 1, "c1a", ["c1b"]),
            _candidate("J", 2, "c2a", ["c2b", "c2c"]),
        ]
        out = await im.score_image_channel_d_batch({}, cands, "cover", 0)

        # Cardinalities reported correctly
        assert out[("J", 0)]["n_extractor_images"] == 1
        assert out[("J", 1)]["n_extractor_images"] == 2
        assert out[("J", 2)]["n_extractor_images"] == 3

        # Candidate 0's S equals its single per-image-max
        assert out[("J", 0)]["S"] == pytest.approx(out[("J", 0)]["per_image_max"][0])

        # Candidate 1's S = mean of its two per-image-max values
        pim = out[("J", 1)]["per_image_max"]
        assert out[("J", 1)]["S"] == pytest.approx(sum(pim) / len(pim))

        # Candidate 0 (matched closely) outscores Candidate 1 (one match,
        # one near-orthogonal) which outscores Candidate 2 (all moderate)
        s0, s1, s2 = (out[("J", i)]["S"] for i in (0, 1, 2))
        assert s0 > s1 > s2


# --- score_image_composite cached_channels plumbing ---------------------

class TestCachedChannels:
    async def test_cached_embedding_skips_recompute(self, monkeypatch):
        """When score_image_composite is given a cached embedding dict,
        the per-candidate score_image_channel_d MUST NOT be called.
        That's the whole point of the cache — saving the expensive
        per-candidate compute path that the batch already paid for."""

        called = {"d": 0}

        async def boom(*args, **kwargs):
            called["d"] += 1
            raise AssertionError(
                "score_image_channel_d should not be called when cached "
                "embedding is provided"
            )

        # Stub the per-candidate D scorer; if composite calls it, test fails
        monkeypatch.setattr(im, "score_image_channel_d", boom)
        # And the A/B/C scorers, since we're not asserting their behavior
        async def neutral(*args, **kwargs):
            return {
                "S": 0.0, "E": 0.0, "count_conf": 1.0, "dist_q": 1.0,
                "m_primes": [], "per_image_max": [], "qualities": [],
                "uniquenesses": [], "extractor_refs": [],
                "n_extractor_images": 0, "n_stash_hashes": 0,
                "m_prime": 0.0, "sim": 0.0, "quality": 0.0,
                "have_stash": True, "have_extractor": True,
                "baseline": 0.0, "scoring": "stub",
            }
        monkeypatch.setattr(im, "score_image_channel_a", neutral)
        monkeypatch.setattr(im, "score_image_channel_b", neutral)
        monkeypatch.setattr(im, "score_image_channel_c", neutral)

        cached_d = {
            "embedding": {
                "S": 0.85, "per_image_max": [0.85], "extractor_refs": ["x"],
                "n_extractor_images": 1, "n_stash_hashes": 1,
                "scoring": "embedding",
            }
        }
        out = await im.score_image_composite(
            scene={}, job_id="J", record={"id": "r0"}, record_idx=0,
            image_mode="cover", algorithm="phash", hash_size=16,
            sprite_sample_size=8, gamma=3.5, count_k=0.25,
            channels=["embedding"],
            min_contribution=0.05, bonus_per_extra=0.1,
            cached_channels=cached_d,
        )
        assert called["d"] == 0
        # Composite for embedding-only is just D's score
        assert out["S"] == pytest.approx(0.85)
        assert out["channels"]["embedding"]["S"] == 0.85
