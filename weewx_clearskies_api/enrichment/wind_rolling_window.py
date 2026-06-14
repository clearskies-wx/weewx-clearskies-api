# Shim — re-exports from the canonical sse.enrichment module.
# transformer.py lazy-imports get_wind_avg and get_gust_max from this path.
from weewx_clearskies_api.sse.enrichment.wind_rolling_window import (  # noqa: F401
    get_gust_max,
    get_wind_avg,
)
