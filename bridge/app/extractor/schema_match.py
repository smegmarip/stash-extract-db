"""Determine whether an extractor schema is "scene-shaped" (superset of
the canonical Video Scene fields). User can clone or rebuild the schema —
match by field-set, not by template id or name."""
from typing import Any

CANONICAL_SCENE_FIELDS = {
    "title", "url", "cover_image", "images", "performers", "date", "details", "id"
}


def is_scene_shaped(schema: dict[str, Any]) -> bool:
    fields = schema.get("fields") or []
    names = {f.get("name") for f in fields if f.get("name")}
    return CANONICAL_SCENE_FIELDS.issubset(names)
