# Shim — re-exports from the canonical sse.enrichment module.
# transformer.py lazy-imports compose_weather_text from this path.
from weewx_clearskies_api.sse.enrichment.weather_text import compose_weather_text  # noqa: F401
