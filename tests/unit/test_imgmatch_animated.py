"""Tests for animated-asset wiring in image_match.py + featurization.py
(CLAUDE.md §13.7/§13.8).

Covers:
  - `_parent_ref` strips `#frame_N` from synthetic refs.
  - `_expanded_extractor_refs` expands animated parents to synthetic
    children when image_features rows exist; static refs pass through.
  - `_resolve_fingerprint_for` uses the parent asset URL for synthetic refs.
  - `_collapsed_stash_cover_fetcher` returns animated bytes collapsed,
    static bytes unchanged.
  - `extractor_image_hash` accepts synthetic refs + prefetched bytes
    without invoking the fetcher.

DB-backed tests use the `bridge_db` fixture so prefix-scan queries are real.
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from bridge.app.matching import image_match as im
from bridge.app.matching.imgmatch import frames as F


def _gradient_bytes(seed: int, fmt: str = "PNG") -> bytes:
    rng = np.random.default_rng(seed)
    base = np.tile(
        np.linspace(20 + (seed * 7) % 50, 200 + (seed * 11) % 50, 64, dtype=np.uint8),
        (64, 1),
    )
    noise = rng.integers(0, 30, size=(64, 64), dtype=np.uint8)
    arr = np.clip(base.astype(np.int32) + noise.astype(np.int32), 0, 255).astype(np.uint8)
    arr_rgb = np.stack([arr, arr, arr], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(arr_rgb, mode="RGB").save(buf, format=fmt)
    return buf.getvalue()


def _animated_gif_bytes(n: int = 4) -> bytes:
    frames = []
    for i in range(n):
        rng = np.random.default_rng(i + 1000)
        arr = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
        frames.append(Image.fromarray(arr, mode="RGB"))
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0,
    )
    return buf.getvalue()


# --- _parent_ref + _resolve_fingerprint_for --------------------------------


class TestParentRef:
    def test_static_ref_unchanged(self):
        assert im._parent_ref("foo.jpg") == "foo.jpg"
        assert im._parent_ref("../assets/x.png") == "../assets/x.png"

    def test_synthetic_ref_strips_frame_suffix(self):
        assert im._parent_ref("foo.gif#frame_0") == "foo.gif"
        assert im._parent_ref("foo.gif#frame_12") == "foo.gif"
        assert im._parent_ref("../assets/x.webp#frame_3") == "../assets/x.webp"

    def test_resolve_fingerprint_uses_parent(self):
        # Synthetic frame refs must resolve to the parent asset URL so
        # all frames invalidate as one cohort under cascade.
        fp_static = im._resolve_fingerprint_for("J", "../assets/x.png")
        fp_synth = im._resolve_fingerprint_for("J", "../assets/x.png#frame_3")
        assert fp_synth == fp_static


# --- _collapsed_stash_cover_fetcher ----------------------------------------


class TestCollapsedStashCoverFetcher:
    async def test_static_bytes_pass_through(self, monkeypatch):
        original = _gradient_bytes(1, fmt="PNG")
        async def fake_fetch(_url):
            return original
        monkeypatch.setattr(im.stash_client, "fetch_image_bytes", fake_fetch)

        out = await im._collapsed_stash_cover_fetcher("http://example/cover.png?t=1")
        assert out is original

    async def test_animated_bytes_collapse(self, monkeypatch):
        original = _animated_gif_bytes(n=5)
        async def fake_fetch(_url):
            return original
        monkeypatch.setattr(im.stash_client, "fetch_image_bytes", fake_fetch)

        out = await im._collapsed_stash_cover_fetcher("http://example/cover.gif?t=1")
        assert out is not None
        # Result decodes as a single static JPEG.
        img = Image.open(io.BytesIO(out))
        assert img.format == "JPEG"
        assert not getattr(img, "is_animated", False)

    async def test_fetch_failure_returns_none(self, monkeypatch):
        async def fake_fetch(_url):
            return None
        monkeypatch.setattr(im.stash_client, "fetch_image_bytes", fake_fetch)
        out = await im._collapsed_stash_cover_fetcher("http://example/missing.png")
        assert out is None


# --- extractor_image_hash with prefetched bytes ----------------------------


class TestExtractorPrefetched:
    async def test_prefetched_bytes_skip_fetcher(
        self, bridge_db, clean_settings, monkeypatch,
    ):
        """When prefetched_bytes is supplied, ex_client.fetch_asset must
        not be called. Featurization relies on this for animated assets:
        fetch + scene-detect once, compute per-frame without re-fetching.
        """
        called = [False]
        async def boom_fetch(_job, _ref):
            called[0] = True
            raise AssertionError("fetcher should not be called when prefetched_bytes is set")
        monkeypatch.setattr(im.ex_client, "fetch_asset", boom_fetch)

        clean_settings.bridge_legacy_dual_write_enabled = False
        h = await im.extractor_image_hash(
            "J", "x.png#frame_0", "phash", 16,
            prefetched_bytes=_gradient_bytes(7),
        )
        assert h is not None
        assert called[0] is False

        # The cached row's ref_id is the synthetic ref, fingerprint is the
        # parent's URL — verify by reading back via the same key.
        feat = await bridge_db.get_image_feature(
            "extractor_image", "J:x.png#frame_0",
            im._resolve_fingerprint_for("J", "x.png#frame_0"),
            "phash", "phash:16",
        )
        assert feat is not None


# --- _expanded_extractor_refs (DB-backed) ----------------------------------


class TestExpandedRefs:
    async def test_static_only_passes_through(self, bridge_db):
        # No image_features rows → every ref is treated as static.
        out = await im._expanded_extractor_refs("J", ["a.png", "b.jpg"])
        assert out == ["a.png", "b.jpg"]

    async def test_animated_ref_expands_in_index_order(
        self, bridge_db, clean_settings, monkeypatch,
    ):
        # Seed three synthetic rows under image_features. The expansion
        # helper should return them ordered by frame index, not by the
        # default ORDER BY ref_id (which would put frame_10 < frame_2).
        clean_settings.bridge_legacy_dual_write_enabled = False

        async def boom_fetch(*a, **kw):
            raise AssertionError("fetch should never run for prefetched featurize")
        monkeypatch.setattr(im.ex_client, "fetch_asset", boom_fetch)

        # Write 12 synthetic frames so the index sort matters.
        for i in range(12):
            await im.extractor_image_hash(
                "J", f"a.gif#frame_{i}", "phash", 16,
                prefetched_bytes=_gradient_bytes(i + 1),
            )

        out = await im._expanded_extractor_refs("J", ["a.gif"])
        assert out == [f"a.gif#frame_{i}" for i in range(12)]

    async def test_mixed_static_and_animated(
        self, bridge_db, clean_settings, monkeypatch,
    ):
        clean_settings.bridge_legacy_dual_write_enabled = False

        async def boom_fetch(*a, **kw):
            raise AssertionError("fetch should never run for prefetched featurize")
        monkeypatch.setattr(im.ex_client, "fetch_asset", boom_fetch)

        # Animated parent with 3 frames + a static sibling.
        for i in range(3):
            await im.extractor_image_hash(
                "J", f"a.gif#frame_{i}", "phash", 16,
                prefetched_bytes=_gradient_bytes(i + 1),
            )

        out = await im._expanded_extractor_refs("J", ["a.gif", "b.png"])
        assert out == [
            "a.gif#frame_0", "a.gif#frame_1", "a.gif#frame_2", "b.png",
        ]
