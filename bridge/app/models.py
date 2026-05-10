from typing import Literal, Optional
from pydantic import BaseModel, Field


ImageMode = Literal["cover", "sprite", "both"]
MatchMode = Literal["scrape", "search"]


class MatchRequest(BaseModel):
    """Common parameters for /match/* endpoints. Per CLAUDE.md §1, all
    scoring parameters live on the bridge — the scraper is a metadata
    transport. Every field below is Optional; if omitted, the bridge
    fills it from `settings.bridge_*` (see `bridge/app/settings.py`).
    """
    mode: MatchMode
    image_mode: Optional[ImageMode] = None
    threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    limit: Optional[int] = Field(default=None, ge=1, le=100)
    hash_algorithm: Optional[Literal["phash", "dhash", "ahash", "whash"]] = None
    hash_size: Optional[int] = Field(default=None, ge=8, le=32)
    sprite_sample_size: Optional[int] = Field(default=None, ge=0)
    image_gamma: Optional[float] = Field(default=None, ge=0.5, le=8.0)
    image_count_k: Optional[float] = Field(default=None, gt=0.0)
    image_channels: Optional[list[Literal["phash", "color_hist", "tone", "embedding"]]] = None
    image_min_contribution: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    image_bonus_per_extra: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    image_search_floor: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class FragmentMatchRequest(MatchRequest):
    scene_id: str


class UrlMatchRequest(MatchRequest):
    url: str


class NameMatchRequest(MatchRequest):
    name: str


class StashPerformerOut(BaseModel):
    Name: str
    Aliases: Optional[str] = None


class StashStudioOut(BaseModel):
    Name: str


class ScrapeResult(BaseModel):
    """Stash scraper output shape."""
    Title: Optional[str] = None
    Details: Optional[str] = None
    Date: Optional[str] = None
    URL: Optional[str] = None
    Code: Optional[str] = None
    Image: Optional[str] = None
    Studio: Optional[StashStudioOut] = None
    Performers: Optional[list[StashPerformerOut]] = None
    # Round-trip identifier per Stash's ScrapedScene GraphQL type. When
    # search returns multiple candidates and the user picks one, Stash
    # echoes this value back via sceneByQueryFragment so the scraper can
    # resolve to the exact picked record without re-matching. CLAUDE.md
    # §17 covers the wider read-only flow; §15.4 covers identity.
    remote_site_id: Optional[str] = None


class RecordLookupRequest(BaseModel):
    """Payload for /match/record — the sceneByQueryFragment landing
    endpoint. record_id is the canonical content-derived identifier
    (record_uuid; CLAUDE.md §15.4) the bridge stamped on the search
    result the user picked."""
    record_id: str


class SearchResult(ScrapeResult):
    match_score: float = 0.0
