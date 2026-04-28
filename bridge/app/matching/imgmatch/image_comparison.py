"""Perceptual image hashing + comparison.

Lifted and adapted from stash-duplicate-scene-finder/python/image_comparison.py.
The bridge fetches images via httpx clients (stash/, extractor/) and passes
bytes here — this module is pure compute (no I/O).
"""
import io

import imagehash
import numpy as np
from PIL import Image


HASH_FUNCS = {
    "phash": imagehash.phash,
    "dhash": imagehash.dhash,
    "ahash": imagehash.average_hash,
    "whash": imagehash.whash,
}


def detect_letterbox(img: Image.Image, brightness_threshold: int = 20, dark_fraction: float = 0.85):
    gray = img.convert("L")
    arr = np.array(gray)
    h, w = arr.shape

    def is_bar(line):
        return np.mean(line < brightness_threshold) >= dark_fraction

    top = 0
    for i in range(h):
        if not is_bar(arr[i]):
            top = i; break
    bottom = h
    for i in range(h - 1, -1, -1):
        if not is_bar(arr[i]):
            bottom = i + 1; break
    left = 0
    for i in range(w):
        if not is_bar(arr[:, i]):
            left = i; break
    right = w
    for i in range(w - 1, -1, -1):
        if not is_bar(arr[:, i]):
            right = i + 1; break

    return (left, top, right, bottom)


def normalize_image(img: Image.Image, target_size=(256, 256)) -> Image.Image:
    crop_box = detect_letterbox(img)
    cropped = img.crop(crop_box)
    if cropped.size[0] < 10 or cropped.size[1] < 10:
        cropped = img
    return cropped.resize(target_size, Image.LANCZOS).convert("L")


def compute_hash(img: Image.Image, algorithm: str = "phash", hash_size: int = 16):
    fn = HASH_FUNCS.get(algorithm, imagehash.phash)
    return fn(img, hash_size=hash_size)


def hash_distance_to_similarity(distance: int, hash_size: int = 16) -> float:
    """0..1 similarity from Hamming distance."""
    max_distance = hash_size * hash_size
    return max(0.0, 1.0 - (distance / max_distance))


# Pixel-variance threshold for refusing to hash near-uniform images.
# Grayscale 0-255: real photos typically have variance > 1000; flat/blank
# images have variance < 30. Set at 30 (generous) so we err on the side of
# producing a hash and let the sim-time degeneracy check catch the rest.
LOW_VARIANCE_THRESHOLD = 30.0


def hash_image_bytes(data: bytes, algorithm: str = "phash", hash_size: int = 16):
    """Returns an imagehash, or None if the source is too low-variance to
    produce a reliable hash. Near-uniform images (all-black sprite frames
    at fade-in/out, blank placeholder thumbs, generic icons) yield
    degenerate pHashes that match each other at sim=1.0 — catastrophic
    false positives. We refuse to hash them. Callers must handle None."""
    img = Image.open(io.BytesIO(data))
    normalized = normalize_image(img)
    if np.asarray(normalized).var() < LOW_VARIANCE_THRESHOLD:
        return None
    return compute_hash(normalized, algorithm, hash_size)


def hex_to_hash(s: str):
    return imagehash.hex_to_hash(s)
