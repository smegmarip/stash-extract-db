"""User-edited configuration for the Stash Extract DB scraper.

The scraper is a thin metadata transport: it reads a scene fragment
from stdin and forwards it to the bridge, which holds all matching
logic and scoring configuration. The scraper does NOT carry threshold,
gamma, channel weights, or any other scoring knob — those live in the
bridge's environment (`.env` / `docker-compose.yml`). See
`docs/HOW_TO_USE.md` §9 for the full bridge env reference.

The only things you might want to change here are connection-side.
"""

# Where the bridge service is reachable from inside the Stash environment.
# - Stash in Docker (typical):   "http://host.docker.internal:13000"
# - Stash on host (no Docker):   "http://localhost:13000"
BRIDGE_URL = "http://host.docker.internal:13000"

# How long to wait for the bridge to respond, in seconds. The bridge can
# return 503 + Retry-After during cold-start featurization; Stash retries
# automatically, so this only needs to cover a single round-trip.
REQUEST_TIMEOUT_S = 90
