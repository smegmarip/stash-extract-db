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
