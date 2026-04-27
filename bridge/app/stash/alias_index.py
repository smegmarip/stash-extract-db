"""Lazy alias resolver. For each extractor performer name we look up
matching Stash performers (by name or by alias_list) and return their ids.
Results are memoized in-process for the lifetime of the request batch.
"""
import logging
from typing import Optional

from .client import find_performers_by_name_or_alias

logger = logging.getLogger(__name__)


class AliasResolver:
    def __init__(self) -> None:
        self._cache: dict[str, set[str]] = {}

    async def resolve(self, name: str) -> set[str]:
        """Returns the set of stash performer ids that match `name` (by
        equality or by being in alias_list)."""
        if not name:
            return set()
        key = name.strip().casefold()
        if key in self._cache:
            return self._cache[key]
        try:
            performers = await find_performers_by_name_or_alias(name.strip())
        except Exception as e:
            logger.warning("alias resolve failed for %r: %s", name, e)
            performers = []
        ids = {p["id"] for p in performers if p.get("id")}
        self._cache[key] = ids
        return ids
