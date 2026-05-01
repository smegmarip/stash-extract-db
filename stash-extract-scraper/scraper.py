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
import sys
import urllib.request
from urllib.error import HTTPError, URLError

# Allow `python3 scraper.py` to find config.py in the same directory
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import config  # noqa: E402


def _eprint(*args):
    print(*args, file=sys.stderr)


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
        # sceneByName — Stash passes a search query
        name = str(payload.get("name") or "").strip()
        if not name:
            print("[]")
            return
        body_text = _post("/match/name", {**base, "name": name, "mode": "search"})

    elif mode_arg == "query":
        # sceneByQueryFragment — user picked a search result; scrape it
        if payload.get("id"):
            body_text = _post("/match/fragment", {**base, "scene_id": str(payload["id"]), "mode": "scrape"})
        elif payload.get("url"):
            body_text = _post("/match/url", {**base, "url": str(payload["url"]), "mode": "scrape"})
        else:
            _emit({})
            return

    elif mode_arg == "url":
        # sceneByURL — Stash passes a URL
        url_in = str(payload.get("url") or "").strip()
        if not url_in:
            _emit({})
            return
        body_text = _post("/match/url", {**base, "url": url_in, "mode": "scrape"})

    else:
        _eprint(f"scraper.py: unknown mode {mode_arg!r}")
        _emit({})
        return

    # Pass bridge response through verbatim — bridge already shaped it.
    sys.stdout.write(body_text)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
