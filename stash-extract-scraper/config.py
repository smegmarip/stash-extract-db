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
# See CLAUDE.md §13 for the formulas these tune.

# Sharpening exponent: higher → stronger suppression of noise-floor
# similarities. 3.5 is the calibrated peak on a diverse 491-video Pexels
# corpus (CALIBRATION_RESULTS.md Run 3a). Lower (2.0) keeps weak signals;
# higher (4.0+) plateaus then drifts as legitimate marginal sims also
# get suppressed.
IMAGE_GAMMA = 3.5

# Count-saturation k: count_conf = 1 - exp(-Σw / k). Lower → sparse-N
# records (a single perfect match) compete more equally with broad-shallow
# records (many mediocre matches). 0.25 is the calibrated peak; the prior
# default of 2.0 was biased against records with 1-2 strong images.
# (CALIBRATION_RESULTS.md Run 3c.)
IMAGE_COUNT_K = 0.25

# Uniqueness smoothing α: c_i = 1 / (1 + α * matches). Higher → reused
# images (logos, title cards) get penalized more sharply. 1.0 confirmed
# at the empirical peak (CALIBRATION_RESULTS.md Run 5b).
IMAGE_UNIQUENESS_ALPHA = 1.0

# Channels to evaluate. Order doesn't matter for scoring (composition is
# `max + bonus`). Drop a channel to disable it (e.g. ["phash"] to revert
# to single-channel behavior with the new scoring formula). On
# Pexels-style mixed-content corpora, channel C (tone) is effectively
# silent because the global uniqueness threshold collapses tone c_i;
# you can drop "tone" from this list for a ~33% per-query speedup with
# no precision change. (CALIBRATION_RESULTS.md Run 7.)
IMAGE_CHANNELS = ["phash", "color_hist", "tone"]

# A channel "fires" if its S >= IMAGE_MIN_CONTRIBUTION; only firing
# channels participate in cross-channel composition. 0.05 is the
# calibrated peak — higher values exclude weak-but-correct contributions
# from the bonus more aggressively than they exclude weak-but-incorrect
# ones, so precision drops. (CALIBRATION_RESULTS.md Run 3b.)
IMAGE_MIN_CONTRIBUTION = 0.05

# Cross-channel bonus per extra firing channel (composition is
# `max(fired) + bonus * (n_fired - 1)`, capped at 1.0). Tune higher to
# reward broad agreement, lower to make the strongest channel dominate.
IMAGE_BONUS_PER_EXTRA = 0.1

# Search-mode confidence floor on the image composite. When set,
# candidates whose image composite is below this AND have no definitive
# signal (Studio+Code or Exact Title) are dropped from search results.
# Scrape mode is unaffected — it has its own `IMAGE_THRESHOLD` gate.
#
# Default `None` (legacy behavior) — verified empirically on the Pexels
# calibration corpus that any positive non-trivial value drops too many
# weak-but-correct positive matches whose composite distribution
# overlaps with weak-but-incorrect negative-control returns. See
# `tests/calibration/CALIBRATION_RESULTS.md` Run 6 for the experiment.
# Users with sharper corpus characteristics (clearer separation between
# correct and incorrect composites) may benefit from a floor at 0.10–0.20.
IMAGE_SEARCH_FLOOR = None
