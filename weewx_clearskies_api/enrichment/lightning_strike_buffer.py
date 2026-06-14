# Shim — re-exports from the canonical sse.enrichment module.
# transformer.py lazy-imports get_strike_history from this path.
from weewx_clearskies_api.sse.enrichment.lightning_strike_buffer import (  # noqa: F401
    get_strike_history,
)
