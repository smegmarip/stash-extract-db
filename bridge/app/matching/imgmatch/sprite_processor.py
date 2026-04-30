"""Sprite + VTT parsing.

Lifted from stash-duplicate-scene-finder/python/sprite_processor.py — pure
compute (no I/O); the Stash client supplies sprite + VTT bytes.
"""
import base64
import io

from PIL import Image

from .image_comparison import normalize_image, compute_hash, compute_quality


def parse_vtt(vtt_text: str) -> list[dict]:
    frames: list[dict] = []
    time_seconds = None
    for line in vtt_text.split("\n"):
        line = line.strip()
        if "-->" in line:
            start = line.split("-->")[0].strip().split(":")
            try:
                time_seconds = (
                    int(start[0]) * 3600 + int(start[1]) * 60 + float(start[2])
                )
            except (ValueError, IndexError):
                time_seconds = None
        elif "xywh=" in line:
            try:
                coords = line.split("xywh=")[1].split(",")
                left, top, width, height = [int(c) for c in coords]
                if time_seconds is not None:
                    frames.append(
                        {"time_seconds": time_seconds, "left": left, "top": top,
                         "width": width, "height": height}
                    )
            except (ValueError, IndexError):
                continue
    return frames


def decode_vtt_text(text: str) -> str:
    if text.startswith("data:text/vtt;base64,"):
        encoded = text.replace("data:text/vtt;base64,", "")
        return base64.b64decode(encoded).decode("utf-8")
    return text


def extract_sprite_frames(sprite_img: Image.Image, vtt_frames: list[dict]) -> list[dict]:
    extracted = []
    for f in vtt_frames:
        box = (f["left"], f["top"], f["left"] + f["width"], f["top"] + f["height"])
        cropped = sprite_img.crop(box)
        extracted.append({"image": cropped, "time_seconds": f["time_seconds"], "index": len(extracted)})
    return extracted


def sample_frames(frames: list[dict], sample_size: int) -> list[dict]:
    if sample_size <= 0 or sample_size >= len(frames):
        return frames
    step = len(frames) / sample_size
    indices = [int(i * step) for i in range(sample_size)]
    return [frames[i] for i in indices]


def hash_sprite_frames(
    sprite_bytes: bytes,
    vtt_text: str,
    sample_size: int,
    algorithm: str = "phash",
    hash_size: int = 16,
) -> list:
    """Returns a list of `(imagehash, quality)` tuples — one per sampled frame.

    Quality is per-frame q_i for grayscale-derived channels (pHash and tone
    share the same formula); see CLAUDE.md §13.4.
    """
    sprite_img = Image.open(io.BytesIO(sprite_bytes))
    vtt_frames = parse_vtt(decode_vtt_text(vtt_text))
    if not vtt_frames:
        return []
    extracted = extract_sprite_frames(sprite_img, vtt_frames)
    sampled = sample_frames(extracted, sample_size)
    out: list = []
    for frame in sampled:
        normalized = normalize_image(frame["image"])
        h = compute_hash(normalized, algorithm, hash_size)
        q = compute_quality(normalized)
        out.append((h, q))
    return out
