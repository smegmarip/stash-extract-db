#!/usr/bin/env python3
"""Stash Extract Scraper — metadata transport.

Stdlib-only. Reads scraper input from stdin, calls the bridge service,
writes the bridge's JSON response to stdout. The bridge holds all
matching logic and scoring configuration (CLAUDE.md §1). This script's
job is purely to bridge Stash's invocation contract to the bridge's
HTTP API — no scoring knobs flow through here.

argv[1] selects the Stash action mode: fragment | name | query | url.
"""
import json
import os
import re
import sys
import urllib.request
from urllib.error import HTTPError, URLError

# Allow `python3 scraper.py` to find config.py in the same directory
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import config  # noqa: E402


def _eprint(*args):
    print(*args, file=sys.stderr)


_WS_RUN = re.compile(r"\s+")
_SENTENCE_BOUNDARY = re.compile(r"([.!?])\s+([a-z])")


def _title_case(s):
    """Per-word capitalize first letter, lowercase the rest. Handles
    apostrophes correctly ('Sara's' stays 'Sara's', not 'Sara'S' as
    str.title produces). Numbers and non-letter leading chars pass
    through unchanged."""
    if not isinstance(s, str) or not s:
        return s
    return " ".join((w[:1].upper() + w[1:].lower()) if w else w for w in s.split())


def _sentence_case(s):
    """Lowercase, then capitalize the start of each sentence. Sentence
    boundaries are `.`, `!`, or `?` followed by whitespace. Whitespace
    is first collapsed (any run of space, tab, or line-break collapses
    to a single space)."""
    if not isinstance(s, str) or not s:
        return s
    # Normalize whitespace: tabs/newlines/multiple spaces → single space.
    s = _WS_RUN.sub(" ", s).strip()
    if not s:
        return s
    s = s.lower()
    # Capitalize first letter.
    s = s[:1].upper() + s[1:]
    # Capitalize letter after each sentence boundary.
    s = _SENTENCE_BOUNDARY.sub(lambda m: m.group(1) + " " + m.group(2).upper(), s)
    return s


def _normalize_result(obj):
    """Apply Title → title case, Details → sentence case, each
    Performers[].Name → title case. Mutates and returns `obj` for
    chaining. No-op when the object is missing those keys or they aren't
    of the expected types."""
    if not isinstance(obj, dict):
        return obj
    if isinstance(obj.get("Title"), str):
        obj["Title"] = _title_case(obj["Title"])
    if isinstance(obj.get("Details"), str):
        obj["Details"] = _sentence_case(obj["Details"])
    performers = obj.get("Performers")
    if isinstance(performers, list):
        for p in performers:
            if isinstance(p, dict) and isinstance(p.get("Name"), str):
                p["Name"] = _title_case(p["Name"])
    return obj


def _normalize_response(parsed):
    """Apply normalization to a bridge response — single dict (scrape
    mode) or list of dicts (search mode). Returns the normalized object."""
    if isinstance(parsed, list):
        return [_normalize_result(x) for x in parsed]
    return _normalize_result(parsed)


def _emit(obj):
    print(json.dumps(obj))


def _read_stdin_json():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        _eprint(f"scraper.py: invalid stdin JSON: {e}")
        return {}


def _post(endpoint: str, body: dict) -> str:
    url = config.BRIDGE_URL.rstrip("/") + endpoint
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        _eprint(f"scraper.py: bridge {url} returned HTTP {e.code} :: {err_body[:500]}")
        return "{}"
    except URLError as e:
        _eprint(f"scraper.py: bridge unreachable at {url} :: {e}")
        return "{}"


def _get_json(endpoint: str):
    """GET against the bridge, return parsed JSON or None on any failure
    (404, network, decode). Used for record-id resolution paths where a
    miss is not an error — the caller decides what to do."""
    url = config.BRIDGE_URL.rstrip("/") + endpoint
    req = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code != 404:
            _eprint(f"scraper.py: bridge {url} returned HTTP {e.code}")
        return None
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        _eprint(f"scraper.py: bridge GET {url} failed :: {e}")
        return None


# `#sx=<hex>` is the bridge's URL-fragment workaround for Stash dropping
# `remote_site_id` on the sceneByQueryFragment payload (see
# bridge match.py::_stamp_record_url, CLAUDE.md §15.4). Recoverable from
# any URL Stash hands us, regardless of action.
_SX_FRAGMENT = re.compile(r"#sx=([0-9a-f]+)")

# Viewer deep-link path: `/records/<job_id>/<result_index>`. The viewer
# (CLAUDE.md §17) renders one record per page at this route; pasting one
# of these into Stash's "Scrape from URL" should resolve to the same
# record without going through the matching engine.
_VIEWER_RECORD_PATH = re.compile(r"/records/([^/?#]+)/(\d+)")


def _record_id_from_url(url_in: str):
    """Return a bridge record_id (uuid) when `url_in` carries one, else
    None. Two carriers are recognized:

    - `#sx=<hex>` URL fragment — stamped by the bridge on search results
      so sceneByQueryFragment can round-trip through `/match/record`.
    - viewer deep-link `/records/<job_id>/<result_index>` — manually
      copied from the viewer UI; resolved via the bridge's admin lookup
      to a record_id.

    The viewer-URL form requires a bridge round-trip (admin GET); the
    `#sx=` form is purely string parsing.
    """
    if not url_in:
        return None
    m = _SX_FRAGMENT.search(url_in)
    if m:
        return m.group(1)
    m = _VIEWER_RECORD_PATH.search(url_in)
    if m:
        job_id, idx = m.group(1), m.group(2)
        rec = _get_json(f"/api/admin/records/{job_id}/{idx}")
        if isinstance(rec, dict):
            rid = rec.get("record_id")
            if isinstance(rid, str) and rid:
                return rid
    return None


def _resolve_scene_id_by_title(title: str):
    """Stash's sceneByName payload is just `{name}` — no scene context,
    so the bridge can't apply its CLAUDE.md §5 studio filter. Bridge that
    gap here: ask Stash for scenes whose title equals the query string;
    when exactly one matches, return its id so we can re-route through
    /match/fragment (which carries studio narrowing). Returns None when
    the lookup is ambiguous (0 or >1 hits), Stash is unreachable, or
    auth fails — caller falls back to /match/name (unfiltered search).

    Stdlib only, like the rest of the scraper. The community py_common
    package is a richer alternative but introduces a `requests` and
    config.ini dependency that breaks the §1 transport contract.
    """
    stash_url = (getattr(config, "STASH_URL", "") or "").rstrip("/")
    if not stash_url:
        return None
    api_key = getattr(config, "STASH_API_KEY", "") or ""
    query = (
        "query FindScenesByTitle($filter: FindFilterType, $scene_filter: SceneFilterType) {"
        "  findScenes(filter: $filter, scene_filter: $scene_filter) {"
        "    count scenes { id }"
        "  }"
        "}"
    )
    payload = json.dumps({
        "query": query,
        "variables": {
            "filter": {"per_page": 2},
            "scene_filter": {"title": {"value": title, "modifier": "EQUALS"}},
        },
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key
    req = urllib.request.Request(stash_url + "/graphql", data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
        _eprint(f"scraper.py: stash title lookup failed :: {e}")
        return None
    fs = ((body.get("data") or {}).get("findScenes") or {})
    scenes = fs.get("scenes") or []
    if len(scenes) != 1:
        return None
    sid = scenes[0].get("id")
    return str(sid) if sid is not None else None


def _overrides() -> dict:
    """Operational overrides from config.py. None entries are dropped so
    the bridge can apply its own default. Anything not surfaced here is
    bridge-internal calibration (CLAUDE.md §1)."""
    out: dict = {}
    for key, attr in (
        ("image_mode",         "IMAGE_MODE"),
        ("threshold",          "IMAGE_THRESHOLD"),
        ("limit",              "SEARCH_LIMIT"),
        ("sprite_sample_size", "SPRITE_SAMPLE_SIZE"),
    ):
        v = getattr(config, attr, None)
        if v is not None:
            out[key] = v
    return out


def main():
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "fragment"
    payload = _read_stdin_json()
    base = _overrides()

    if mode_arg == "fragment":
        # sceneByFragment — full scene fragment in stdin, look up by id
        scene_id = str(payload.get("id") or "").strip()
        if not scene_id:
            _emit({})
            return
        body_text = _post("/match/fragment", {**base, "scene_id": scene_id, "mode": "scrape"})

    elif mode_arg == "name":
        # sceneByName — Stash passes a search query and nothing else.
        # Try to resolve the title to a single Stash scene id first; if
        # exactly one matches, route through /match/fragment so the
        # bridge applies the §5 studio filter. Otherwise fall back to
        # /match/name (unfiltered search across all scene-shaped jobs).
        name = str(payload.get("name") or "").strip()
        if not name:
            print("[]")
            return
        scene_id = _resolve_scene_id_by_title(name)
        if scene_id:
            body_text = _post("/match/fragment", {**base, "scene_id": scene_id, "mode": "search"})
        else:
            body_text = _post("/match/name", {**base, "name": name, "mode": "search"})

    elif mode_arg == "query":
        # sceneByQueryFragment — Stash echoes the picked search result
        # back here. Prefer the round-trip identifier the bridge stamped
        # on the result over fuzzier hints (URL exact-match, scene-id
        # rescrape) — those collapse multiple candidates onto the first
        # hit when records share a URL or no URL came through.
        rid = str(
            payload.get("remote_site_id")
            or payload.get("RemoteSiteID")
            or ""
        ).strip()
        if not rid:
            # Stash's `sceneInput` Go struct (the one marshalled to our
            # stdin for sceneByQueryFragment) drops `remote_site_id`
            # even though ScrapedSceneInput defines it. The bridge
            # stamps the uuid into the URL fragment as a workaround
            # (see match.py::_stamp_record_url); recover it here.
            for u in (payload.get("url"), *(payload.get("urls") or [])):
                resolved = _record_id_from_url(str(u or ""))
                if resolved:
                    rid = resolved
                    break
        if rid:
            body_text = _post("/match/record", {"record_id": rid})
        elif payload.get("id"):
            body_text = _post("/match/fragment", {**base, "scene_id": str(payload["id"]), "mode": "scrape"})
        elif payload.get("url"):
            body_text = _post("/match/url", {**base, "url": str(payload["url"]), "mode": "scrape"})
        else:
            _emit({})
            return

    elif mode_arg == "url":
        # sceneByURL — Stash passes a URL. Two manual-matching shortcuts
        # ride on this entrypoint before the generic /match/url path:
        # a `#sx=<uuid>` fragment carries a bridge-stamped record_id
        # (same recovery as the `query` mode workaround), and a viewer
        # deep-link `/records/<job_id>/<result_index>` resolves through
        # the bridge's admin lookup. Either form short-circuits to
        # /match/record for an exact, content-derived hit.
        url_in = str(payload.get("url") or "").strip()
        if not url_in:
            _emit({})
            return
        rid = _record_id_from_url(url_in)
        if rid:
            body_text = _post("/match/record", {"record_id": rid})
        else:
            body_text = _post("/match/url", {**base, "url": url_in, "mode": "scrape"})

    else:
        _eprint(f"scraper.py: unknown mode {mode_arg!r}")
        _emit({})
        return

    # Bridge has already shaped the JSON; the scraper applies presentation
    # normalization (Title → title case, Details → sentence case, Performer
    # Names → title case) before handing off to Stash. Bridge responses are
    # well-formed JSON; if parsing fails, fall through to verbatim passthrough
    # so a deformed payload doesn't lose data.
    try:
        parsed = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        sys.stdout.write(body_text)
        sys.stdout.write("\n")
        return
    normalized = _normalize_response(parsed)
    sys.stdout.write(json.dumps(normalized))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
