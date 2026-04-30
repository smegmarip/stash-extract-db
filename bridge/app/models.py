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

    # Phase 4 new-scoring fields. Optional at the Pydantic layer so old
    # scraper configs still work when BRIDGE_NEW_SCORING_ENABLED=false.
    # Validated at the scoring entry point: when the new formula is
    # engaged, missing fields → 400 (per CLAUDE.md §1).
    image_gamma: Optional[float] = Field(default=None, ge=0.5, le=8.0)
    image_count_k: Optional[float] = Field(default=None, gt=0.0)
    image_uniqueness_alpha: Optional[float] = Field(default=None, ge=0.0)

    # Phase 5 multi-channel composition fields. Same Optional rationale as
    # the Phase 4 fields — old scraper configs still work behind the
    # BRIDGE_NEW_SCORING_ENABLED flag.
    image_channels: Optional[list[Literal["phash", "color_hist", "tone"]]] = None
    image_min_contribution: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    image_bonus_per_extra: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Phase 6 search-mode confidence floor. Drops candidates whose image
    # composite is below the floor *unless* a definitive signal (Studio+Code
    # or Exact Title) fired. Scrape mode is unaffected — it has its own
    # `threshold` gate at the composite level. None = no floor (legacy
    # behavior). See CALIBRATION_RESULTS.md Run 5 for why this is needed:
    # weak matches at composite ~0.11–0.13 leak into search results for
    # scenes with no real extractor counterpart.
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


class SearchResult(ScrapeResult):
    match_score: float = 0.0
