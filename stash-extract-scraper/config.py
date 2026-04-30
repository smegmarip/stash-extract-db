"""User-edited configuration for the Stash Extract DB scraper.

All matching parameters originate here and are sent in every request to
the bridge. The bridge has no fallbacks (per CLAUDE.md §1).
"""

# Where the bridge service is reachable from inside the Stash environment.
# - Stash in Docker (typical):   "http://host.docker.internal:13000"
# - Stash on host (no Docker):   "http://localhost:13000"
BRIDGE_URL = "http://host.docker.internal:13000"

# Image-matching mode:
#   "cover"  — Stash cover vs extractor images[]                  (1:N, fast)
#   "sprite" — Stash sprite frames vs extractor images[]          (M:N, slow)
#   "both"   — run cover + sprite, take max similarity            (slowest)
IMAGE_MODE = "cover"

# Per-pair similarity threshold (0..1).
# In scrape mode: an image firing as definitive requires >= threshold.
# In search mode: similarities >= threshold contribute their raw score;
# similarities below contribute 0.5 * raw (still useful as ranking signal).
IMAGE_THRESHOLD = 0.7

# Number of ranked candidates to return in search mode.
SEARCH_LIMIT = 5

# Perceptual hash algorithm: "phash" | "dhash" | "ahash" | "whash".
HASH_ALGORITHM = "phash"

# Hash resolution in bits. Higher → more discriminating but slower compare.
HASH_SIZE = 16

# Number of sprite frames to sample per Stash scene in sprite mode (0 = all).
SPRITE_SAMPLE_SIZE = 8

# How long to wait for the bridge to respond, in seconds.
REQUEST_TIMEOUT_S = 90

# --- Multi-channel scoring (used when bridge has BRIDGE_NEW_SCORING_ENABLED=true)
# See MULTI_CHANNEL_SCORING.md §3 for the formulas these tune.

# Sharpening exponent: higher → stronger suppression of noise-floor
# similarities. 2 is the calibrated default; raise to 3 for stricter
# false-positive rejection if your corpus has many spurious near-matches.
IMAGE_GAMMA = 2.0

# Count-saturation k: lower → records with few images are penalized more
# heavily relative to records with many. 2.0 is calibrated for typical
# extractor records (≤5 images each).
IMAGE_COUNT_K = 2.0

# Uniqueness smoothing α: c_i = 1 / (1 + α * matches). Higher → reused
# images (logos, title cards) get penalized more sharply.
IMAGE_UNIQUENESS_ALPHA = 1.0

# Channels to evaluate. Order doesn't matter for scoring (composition is
# `max + bonus`). Drop a channel to disable it (e.g. ["phash"] to revert
# to single-channel behavior with the new scoring formula).
IMAGE_CHANNELS = ["phash", "color_hist", "tone"]

# A channel "fires" if its S >= IMAGE_MIN_CONTRIBUTION; only firing
# channels participate in cross-channel composition. Lower → more
# channels qualify for the bonus; raise to gate out weak channels.
IMAGE_MIN_CONTRIBUTION = 0.3

# Cross-channel bonus per extra firing channel (composition is
# `max(fired) + bonus * (n_fired - 1)`, capped at 1.0). Tune higher to
# reward broad agreement, lower to make the strongest channel dominate.
IMAGE_BONUS_PER_EXTRA = 0.1

# Search-mode confidence floor on the image composite. When set,
# candidates whose image composite is below this AND have no definitive
# signal (Studio+Code or Exact Title) are dropped from search results.
# Scrape mode is unaffected — it has its own `IMAGE_THRESHOLD` gate.
IMAGE_SEARCH_FLOOR = None
