import logging
from typing import Any, Optional

import httpx

from ..settings import settings

logger = logging.getLogger(__name__)


SCENE_FRAGMENT = """
fragment SceneForMatch on Scene {
  id title details date code urls
  files {
    path basename duration width height frame_rate
    fingerprints { type value }
  }
  paths { screenshot preview sprite vtt }
  studio { id name url }
  performers { id name alias_list }
  tags { id name }
  stash_ids { endpoint stash_id }
}
"""

FIND_SCENE_QUERY = SCENE_FRAGMENT + """
query FindScene($id: ID!) {
  findScene(id: $id) { ...SceneForMatch }
}
"""

FIND_PERFORMERS_QUERY = """
query FindPerformers($filter: FindFilterType, $performer_filter: PerformerFilterType) {
  findPerformers(filter: $filter, performer_filter: $performer_filter) {
    count
    performers { id name alias_list }
  }
}
"""


def _auth_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.stash_api_key:
        h["ApiKey"] = settings.stash_api_key
    return h


def _auth_cookies() -> dict[str, str]:
    if settings.stash_session_cookie:
        return {"session": settings.stash_session_cookie}
    return {}


async def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    url = settings.stash_url.rstrip("/") + "/graphql"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            json={"query": query, "variables": variables},
            headers=_auth_headers(),
            cookies=_auth_cookies(),
        )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        logger.warning("Stash GraphQL errors: %s", data["errors"])
    return data.get("data") or {}


async def find_scene(scene_id: str) -> Optional[dict[str, Any]]:
    data = await _gql(FIND_SCENE_QUERY, {"id": scene_id})
    return data.get("findScene")


async def find_performers_by_name_or_alias(name: str) -> list[dict[str, Any]]:
    """Returns performers matching name OR having `name` in alias_list."""
    pf = {
        "OR": {
            "aliases": {"value": name, "modifier": "INCLUDES"},
            "name": {"value": name, "modifier": "EQUALS"},
        },
    }
    data = await _gql(
        FIND_PERFORMERS_QUERY,
        {
            "filter": {"per_page": 25},
            "performer_filter": pf,
        },
    )
    fp = data.get("findPerformers") or {}
    return fp.get("performers") or []


async def fetch_image_bytes(url: str) -> Optional[bytes]:
    """Fetch a binary asset from Stash with auth headers."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=_auth_headers(), cookies=_auth_cookies())
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.warning("Stash image fetch failed: %s :: %s", url, e)
            return None


async def fetch_text(url: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=_auth_headers(), cookies=_auth_cookies())
            r.raise_for_status()
            return r.text
        except Exception as e:
            logger.warning("Stash text fetch failed: %s :: %s", url, e)
            return None
