"""Shared pytest fixtures for the bridge's unit + integration suites.

Goals:
  - Each test gets an isolated SQLite database in a temp dir.
  - Each test that touches the FastAPI app gets a fresh `init_db` /
    `close_db` lifecycle (the bridge's cache.db module holds a global
    connection; tests must not share it).
  - Settings are restored after each test (we mutate them frequently —
    BRIDGE_LIFECYCLE_ENABLED, BRIDGE_NEW_SCORING_ENABLED, etc.).
  - Synthetic image bytes are deterministic per seed so failures are
    reproducible.

Conventions:
  - Use `synth_image` for any test that needs PNG bytes.
  - Use `bridge_db` for any test that touches the SQLite cache.
  - Use `clean_settings` for any test that mutates `settings.*`.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
import pytest_asyncio
from PIL import Image

# Make `bridge.app...` importable from anywhere in the suite.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- Synthetic image fixture ---------------------------------------------

def _make_synth_image(seed: int, size: int = 256) -> bytes:
    """Deterministic PNG bytes from `seed`. Gradient-plus-noise, mid-range
    luminance, color-mode RGB. Variance + entropy clear the bridge's
    LOW_VARIANCE_THRESHOLD by a wide margin, so the image always hashes.
    """
    rng = np.random.default_rng(seed)
    base = np.tile(
        np.linspace(20 + (seed * 7) % 50, 200 + (seed * 11) % 50, size, dtype=np.uint8),
        (size, 1),
    )
    noise = rng.integers(0, 30, size=(size, size), dtype=np.uint8)
    arr = np.clip(base.astype(np.int32) + noise.astype(np.int32), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def synth_image():
    """Returns a function `(seed: int, size: int = 256) -> bytes`."""
    return _make_synth_image


# --- Settings snapshot/restore -------------------------------------------

@pytest.fixture
def clean_settings():
    """Snapshot all bridge settings on entry; restore on exit. Lets tests
    freely mutate settings without bleeding into other tests.
    """
    from bridge.app.settings import settings
    snapshot = settings.model_dump()
    yield settings
    for k, v in snapshot.items():
        setattr(settings, k, v)


# --- DB lifecycle --------------------------------------------------------

@pytest_asyncio.fixture
async def bridge_db(tmp_path) -> Iterator:
    """Initialize a fresh SQLite database in a temp dir for the test, and
    close + clear the module-level connection on exit.
    """
    from bridge.app.cache import db as cdb
    cdb.settings.data_dir = str(tmp_path)
    await cdb.init_db()
    try:
        yield cdb
    finally:
        await cdb.close_db()


# --- Worker pool reset ---------------------------------------------------

@pytest_asyncio.fixture
async def reset_worker():
    """Clear the bridge worker pool's in-process state before/after the
    test. Required for any test that imports bridge.app.matching.worker —
    its `_inflight` dict and `_lru_task` are module-level globals.
    """
    from bridge.app.matching import worker as fw
    # Cancel any in-flight tasks from a previous test
    await fw.shutdown()
    # Reset the lazy semaphore so it's recreated against this loop
    fw._semaphore = None
    yield fw
    await fw.shutdown()
    fw._semaphore = None
