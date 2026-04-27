"""Multi-channel filename comparison.

Combines three independent comparators feeding a single score in [0, 1]:

  1. Naive normalize → RapidFuzz WRatio
        Robust on short clean names ("Horse_Walking.avi" vs "Horse Walking.avi")
        Collapses on release-style filenames (resolutions, codecs, group tags).
  2. Guessit-parsed title → RapidFuzz token_set_ratio
        Strips release/resolution/codec/group/ID noise, then fuzzy-matches the
        residual title. Underperforms on names without release patterns.
  3. Structured-field exact matches (year/season/episode/screen_size)
        Each contributes a small bonus when both sides parsed a non-null value
        AND they match.

Combination rule (per CLAUDE.md §13): final = min(1.0, max(naive, guessit) + bonus).
max() — not mean — so a strong signal from either fuzzy channel is never dragged
down by the other's miss. Bonus is additive on top because structured agreement
is independent corroborating evidence, not redundant with text similarity.
"""
from typing import Any
from urllib.parse import urlparse, unquote

from rapidfuzz import fuzz
from guessit import guessit

from .text import normalize_filename, basename_from_url


# Structured-field bonus weights. Tuned to feel proportional to confidence
# the field carries. Year+episode together can add 0.10; screen_size alone is
# weak signal (many files are 1080p).
STRUCTURED_BONUS_WEIGHTS: dict[str, float] = {
    "year": 0.05,
    "episode": 0.05,
    "season": 0.03,
    "screen_size": 0.02,
}


def _safe_guessit(s: str) -> dict[str, Any]:
    """guessit returns its own MatchesDict; coerce to plain dict and tolerate
    parse failures (return empty dict so callers can use .get safely)."""
    if not s:
        return {}
    try:
        m = guessit(s)
        return dict(m) if m else {}
    except Exception:
        return {}


def _basename_from_url(url: str) -> str:
    """Take the last path segment of a URL, decoded. Falls back to the input
    if it's already a bare filename."""
    if not url:
        return ""
    if url.startswith(("http://", "https://", "//")):
        path = urlparse(url).path or ""
        seg = path.rsplit("/", 1)[-1]
        return unquote(seg) if seg else ""
    return basename_from_url(url) or url


def _naive_channel(stash_basename: str, extractor_url: str) -> float:
    """The original normalize-then-WRatio path, kept verbatim semantics."""
    a = normalize_filename(stash_basename or "")
    b = normalize_filename(_basename_from_url(extractor_url or ""))
    if not a or not b:
        return 0.0
    return fuzz.WRatio(a, b) / 100.0


def _guessit_title_channel(g_s: dict[str, Any], g_e: dict[str, Any]) -> float:
    """token_set_ratio on the guessit-parsed titles. Empty titles → 0."""
    a = (g_s.get("title") or "").strip()
    b = (g_e.get("title") or "").strip()
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0


def _structured_bonus(g_s: dict[str, Any], g_e: dict[str, Any]) -> tuple[float, list[str]]:
    """Sum of STRUCTURED_BONUS_WEIGHTS for fields where both sides parsed a
    non-null value AND the values are equal. Returns (bonus, matched_fields)."""
    bonus = 0.0
    matched: list[str] = []
    for field, weight in STRUCTURED_BONUS_WEIGHTS.items():
        a, b = g_s.get(field), g_e.get(field)
        if a is None or b is None:
            continue
        if a == b:
            bonus += weight
            matched.append(field)
    return bonus, matched


def filename_score(stash_basename: str, extractor_url: str) -> float:
    """Composite score in [0, 1]. See module docstring for combination rule."""
    breakdown = filename_score_debug(stash_basename, extractor_url)
    return breakdown["score"]


def filename_score_debug(stash_basename: str, extractor_url: str) -> dict[str, Any]:
    """Same composite as filename_score, but returns the full channel breakdown
    for debug output and tuning."""
    naive = _naive_channel(stash_basename, extractor_url)

    extractor_basename = _basename_from_url(extractor_url or "")
    g_s = _safe_guessit(stash_basename or "")
    g_e = _safe_guessit(extractor_basename)

    guessit_title = _guessit_title_channel(g_s, g_e)
    bonus, matched = _structured_bonus(g_s, g_e)

    fuzzy = max(naive, guessit_title)
    score = min(1.0, fuzzy + bonus)

    return {
        "score": score,
        "naive": round(naive, 4),
        "guessit_title": round(guessit_title, 4),
        "structured_bonus": round(bonus, 4),
        "matched_fields": matched,
        "guessit_stash": {k: v for k, v in g_s.items() if isinstance(v, (str, int, float, bool))},
        "guessit_extractor": {k: v for k, v in g_e.items() if isinstance(v, (str, int, float, bool))},
    }
