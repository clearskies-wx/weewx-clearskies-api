"""Feed AQI provider PM2.5/PM10 into the input smoother (ADR-066, ADR-067).

AQI providers return PM data at multi-minute intervals (Aeris: ~5 min,
others: 30-60 min).  This module bridges that data into the input_smoother
ring buffers so compose_weather_text() can access smoothed PM values for
haze detection (Phase 4) and fog disambiguation (Phase 5).

Data flow:
  AQI endpoint fetch() → set_latest_pm()  (stores latest reading)
  Loop packet cycle    → feed_to_smoother() (registered as packet_tap)
                       → input_smoother.add_sample("pollutantPM25", ...)

Staleness: PM readings older than 2 hours are not fed.  The smoother
ring buffer naturally ages out old values, so stale data eventually
clears even without explicit purging.
"""

from __future__ import annotations

import logging
import time

from weewx_clearskies_api.sse.enrichment import input_smoother

logger = logging.getLogger(__name__)

_latest_pm25: float | None = None
_latest_pm10: float | None = None
_latest_timestamp: float = 0.0
_is_observed: bool = False

_STALE_SECONDS: float = 7200.0  # 2 hours per API-MANUAL §8 haze detection rule 4


def set_latest_pm(
    *,
    pm25: float | None,
    pm10: float | None,
    timestamp: float,
    is_observed: bool,
) -> None:
    """Store the latest PM reading from an AQI provider fetch.

    Called by the /aqi/current endpoint after a successful provider dispatch.
    Only readings from observed-data providers (is_observed=True) are eligible
    for haze confirmation per ADR-066.
    """
    global _latest_pm25, _latest_pm10, _latest_timestamp, _is_observed  # noqa: PLW0603
    _latest_pm25 = pm25
    _latest_pm10 = pm10
    _latest_timestamp = timestamp
    _is_observed = is_observed


def feed_to_smoother(packet: dict) -> None:  # type: ignore[type-arg]
    """Packet-tap processor: inject latest PM into input_smoother buffers.

    Registered via packet_tap.register_processor() at startup.  Runs on
    every loop-packet cycle (~5 seconds).  The packet argument is ignored —
    PM data comes from the module-level store, not from loop packets.

    Guards:
      - Only feeds observed-source PM (ADR-066 is_observed_source check).
      - Suppresses stale data (> 2 hours since last AQI fetch).
    """
    if not _is_observed:
        return
    if _latest_timestamp == 0.0:
        return
    elapsed = time.time() - _latest_timestamp
    if elapsed > _STALE_SECONDS:
        return
    if _latest_pm25 is not None:
        input_smoother.add_sample("pollutantPM25", _latest_pm25)
    if _latest_pm10 is not None:
        input_smoother.add_sample("pollutantPM10", _latest_pm10)


def get_pm_staleness() -> float | None:
    """Return seconds since the last PM reading, or None if never received."""
    if _latest_timestamp == 0.0:
        return None
    return time.time() - _latest_timestamp


def reset() -> None:
    """Clear module state.  For test isolation only."""
    global _latest_pm25, _latest_pm10, _latest_timestamp, _is_observed  # noqa: PLW0603
    _latest_pm25 = None
    _latest_pm10 = None
    _latest_timestamp = 0.0
    _is_observed = False
