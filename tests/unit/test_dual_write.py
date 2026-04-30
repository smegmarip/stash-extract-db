"""Unit tests for the Phase 2 dual-write parity + Phase 7 soft-retirement
flag interaction.

Covers:
  - Hash compute writes to BOTH image_hashes (legacy) and image_features (new).
  - Reads prefer image_features; fall back to image_hashes when missing.
  - Fingerprint mismatch invalidates both paths.
  - BRIDGE_LEGACY_DUAL_WRITE_ENABLED=False stops legacy writes and skips
    the legacy fallback on read.

Synthetic image bytes only; the extractor/Stash HTTP clients are
short-circuited via the `fetcher` callback that _hash_or_compute takes.
"""
from typing import Optional

import pytest

from bridge.app.matching.image_match import _hash_or_compute


@pytest.fixture
def fake_fetcher(synth_image):
    """Returns a (fetcher_callable, called_flag) pair. The callable is
    awaitable and yields synthetic bytes; called_flag is a 1-element
    list so tests can observe whether the fetcher was invoked.
    """
    def _make(seed: int = 1) -> tuple:
        called = [False]
        async def fetcher() -> Optional[bytes]:
            called[0] = True
            return synth_image(seed)
        return fetcher, called
    return _make


# --- Dual-write parity ---------------------------------------------------

class TestDualWriteParity:
    async def test_hash_compute_populates_both_tables(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        h = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher,
        )
        assert h is not None

        legacy_hex = await bridge_db.get_image_hash(
            "extractor_image", "j:r0", "fp1", "phash", 8,
        )
        feat = await bridge_db.get_image_feature(
            "extractor_image", "j:r0", "fp1", "phash", "phash:8",
        )
        assert legacy_hex is not None
        assert feat is not None

        # Both encodings must reference the same hash bits.
        assert legacy_hex == feat[0].hex()
        assert 0.0 <= feat[1] <= 1.0  # quality is in range

    async def test_read_prefers_features_table(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        """Drop the legacy row after first compute; subsequent reads
        must hit image_features without re-fetching."""
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        await _hash_or_compute("extractor_image", "j:r0", "fp1", "phash", 8, fetcher)

        await bridge_db.db().execute(
            "DELETE FROM image_hashes WHERE source='extractor_image' AND ref_id='j:r0'"
        )
        await bridge_db.db().commit()

        fetcher2, called = fake_fetcher(1)
        h2 = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher2,
        )
        assert h2 is not None
        assert called[0] is False  # served from image_features

    async def test_legacy_fallback_when_features_missing(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        """Drop the features row but keep the legacy row; read should
        still succeed via fallback (when flag is on)."""
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        h = await _hash_or_compute("extractor_image", "j:r0", "fp1", "phash", 8, fetcher)
        h_str = str(h)

        await bridge_db.db().execute(
            "DELETE FROM image_features WHERE source='extractor_image' AND ref_id='j:r0'"
        )
        await bridge_db.db().commit()

        fetcher2, called = fake_fetcher(1)
        h2 = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher2,
        )
        assert str(h2) == h_str
        assert called[0] is False  # legacy fallback served the read

    async def test_fingerprint_mismatch_recomputes(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        await _hash_or_compute("extractor_image", "j:r0", "fp_a", "phash", 8, fetcher)

        # Different fingerprint → must re-fetch
        fetcher2, called = fake_fetcher(1)
        h2 = await _hash_or_compute(
            "extractor_image", "j:r0", "fp_b", "phash", 8, fetcher2,
        )
        assert h2 is not None
        assert called[0] is True

        # Both rows now exist for the new fingerprint
        legacy = await bridge_db.get_image_hash(
            "extractor_image", "j:r0", "fp_b", "phash", 8,
        )
        feat = await bridge_db.get_image_feature(
            "extractor_image", "j:r0", "fp_b", "phash", "phash:8",
        )
        assert legacy is not None
        assert feat is not None


# --- Phase 7 soft-retirement ---------------------------------------------

class TestLegacyDualWriteFlag:
    async def test_flag_off_skips_legacy_write(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        clean_settings.bridge_legacy_dual_write_enabled = False
        fetcher, _ = fake_fetcher(1)
        h = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher,
        )
        assert h is not None

        legacy = await bridge_db.get_image_hash(
            "extractor_image", "j:r0", "fp1", "phash", 8,
        )
        feat = await bridge_db.get_image_feature(
            "extractor_image", "j:r0", "fp1", "phash", "phash:8",
        )
        assert legacy is None  # legacy NOT written
        assert feat is not None  # features written

    async def test_flag_off_ignores_legacy_on_read(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        """Even if image_hashes has a row, the flag-off read path must
        re-fetch instead of falling back to it."""
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        h = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher,
        )
        h_str = str(h)

        # Now disable and remove the features row; legacy is still there
        clean_settings.bridge_legacy_dual_write_enabled = False
        await bridge_db.db().execute(
            "DELETE FROM image_features WHERE source='extractor_image' AND ref_id='j:r0'"
        )
        await bridge_db.db().commit()

        fetcher2, called = fake_fetcher(1)
        h2 = await _hash_or_compute(
            "extractor_image", "j:r0", "fp1", "phash", 8, fetcher2,
        )
        # Should re-fetch (didn't read legacy fallback) but produce the same hash
        assert called[0] is True
        assert str(h2) == h_str

    async def test_flag_on_writes_both(
        self, bridge_db, clean_settings, fake_fetcher,
    ):
        clean_settings.bridge_legacy_dual_write_enabled = True
        fetcher, _ = fake_fetcher(1)
        await _hash_or_compute("extractor_image", "j:r0", "fp1", "phash", 8, fetcher)
        legacy = await bridge_db.get_image_hash(
            "extractor_image", "j:r0", "fp1", "phash", 8,
        )
        feat = await bridge_db.get_image_feature(
            "extractor_image", "j:r0", "fp1", "phash", "phash:8",
        )
        assert legacy is not None
        assert feat is not None
