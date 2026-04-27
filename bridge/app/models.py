from typing import Literal, Optional
from pydantic import BaseModel, Field


ImageMode = Literal["cover", "sprite", "both"]
MatchMode = Literal["scrape", "search"]


class MatchRequest(BaseModel):
    """Common parameters for /match/* endpoints. Bridge has no fallbacks —
    all matching parameters originate in the scraper's config.py."""
    mode: MatchMode
    image_mode: ImageMode
    threshold: float = Field(ge=0.0, le=1.0)
    limit: int = Field(default=5, ge=1, le=100)
    hash_algorithm: Literal["phash", "dhash", "ahash", "whash"] = "phash"
    hash_size: int = Field(default=16, ge=8, le=32)
    sprite_sample_size: int = Field(default=8, ge=0)


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


class SearchResult(ScrapeResult):
    match_score: float = 0.0
