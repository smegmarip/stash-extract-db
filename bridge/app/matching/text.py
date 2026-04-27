"""Text + filename + performer + date scoring helpers."""
import os
import re
import urllib.parse
from typing import Any, Optional


_SEPARATOR_RE = re.compile(r"[_\-.]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_filename(s: str) -> str:
    """Lowercase, URL-decode, strip extension, collapse separators / whitespace.
    Used for filename similarity only — not for exact-title matching."""
    if not s:
        return ""
    s = urllib.parse.unquote(s)
    # Strip query / fragment if it's a URL fragment
    s = s.split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("/", 1)[-1]
    s, _ = os.path.splitext(s)
    s = _SEPARATOR_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip().casefold()
    return s


def basename_from_url(url: str) -> str:
    if not url:
        return ""
    return urllib.parse.unquote(url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1])


def parse_partial_date(s: str) -> Optional[tuple[int, Optional[int], Optional[int]]]:
    """Parses YYYY, YYYY-MM, YYYY-MM-DD. Returns (year, month?, day?) or None."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?$", s)
    if not m:
        # Try ISO datetime
        m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if m2:
            return int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return None
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else None
    d = int(m.group(3)) if m.group(3) else None
    return y, mo, d


def date_score(stash_date: Optional[str], extractor_date: Optional[str]) -> float:
    """1.0 same day · 0.5 same year+month · 0.2 same year · 0 else."""
    a = parse_partial_date(stash_date or "")
    b = parse_partial_date(extractor_date or "")
    if not a or not b:
        return 0.0
    if a == b and a[1] is not None and a[2] is not None and b[1] is not None and b[2] is not None:
        return 1.0
    if a[0] == b[0] and a[1] is not None and b[1] is not None and a[1] == b[1]:
        return 0.5
    if a[0] == b[0]:
        return 0.2
    return 0.0


def studio_and_code_fires(scene: dict[str, Any], record: dict[str, Any], used_studio_filter: bool) -> bool:
    """Studio+Code definitive signal.
    Per CLAUDE.md §5: studio is the job-level filter, applied case-insensitively
    against job names. If we're operating with a studio filter (studio applied),
    the studio side is implicitly satisfied. Then we test code equality.
    """
    if not used_studio_filter:
        return False
    code = (scene.get("code") or "").strip()
    if not code:
        return False
    rec_id = (record.get("id") or "").strip() if record.get("id") is not None else ""
    return rec_id == code  # case-sensitive


def exact_title_fires(scene: dict[str, Any], record: dict[str, Any]) -> bool:
    title = (scene.get("title") or "").strip()
    if not title:
        return False
    rec_title = record.get("title") or ""
    return title == rec_title  # strict, no normalization


async def performer_score(
    scene: dict[str, Any],
    record: dict[str, Any],
    alias_resolver,
) -> float:
    """|matched ∩| / |stash_performers|, alias-resolved.
    Returns 0.0 when scene has no performers (neutral, not penalty)."""
    stash_perfs = scene.get("performers") or []
    if not stash_perfs:
        return 0.0
    extractor_names = record.get("performers") or []
    if not extractor_names:
        return 0.0
    stash_ids = {p["id"] for p in stash_perfs if p.get("id")}
    matched_ids: set[str] = set()
    for n in extractor_names:
        ids = await alias_resolver.resolve(str(n))
        matched_ids |= (ids & stash_ids)
    return len(matched_ids) / max(len(stash_ids), 1)
