"""User-edited configuration for the Stash Extract DB scraper.

The scraper is a thin metadata transport: it reads a scene fragment
from stdin and forwards it to the bridge. All scoring math (γ, k,
thresholds for the calibrated multi-channel formula, channel weights,
uniqueness smoothing, etc.) is bridge-internal and lives in the
bridge's environment per CLAUDE.md §1.

The settings below are operational overrides — values the scraper can
choose to send per-request. When set, they override the bridge's
defaults. When set to `None`, the bridge falls back to its own
configured value (bridge env / settings.py).
"""

# Where the bridge service is reachable from inside the Stash environment.
# - Stash in Docker (typical):   "http://host.docker.internal:13000"
# - Stash on host (no Docker):   "http://localhost:13000"
BRIDGE_URL = "http://host.docker.internal:13000"

# How long to wait for the bridge to respond, in seconds. The bridge can
# return 503 + Retry-After during cold-start featurization; Stash retries
# automatically, so this only needs to cover a single round-trip.
REQUEST_TIMEOUT_S = 90

# --- Per-request operational overrides (None → bridge default) ---

# Image-matching mode. None = use bridge default (cover).
#   "cover"  — Stash cover only (1 Stash image per request)         fastest
#   "sprite" — Stash sprite frames only (≈ 8 Stash images)
#   "both"   — cover + sprite frames (≈ 9 Stash images)              slowest, most accurate
IMAGE_MODE = None

# Composite-score threshold for the scrape-mode image tier. None = use
# bridge default (0.7). Lower → more permissive scrape matches.
IMAGE_THRESHOLD = None

# Top-N candidates to return in search mode. None = use bridge default (5).
SEARCH_LIMIT = None

# Number of sprite frames the bridge samples per Stash scene when
# IMAGE_MODE is "sprite" or "both". None = use bridge default (8).
SPRITE_SAMPLE_SIZE = None
