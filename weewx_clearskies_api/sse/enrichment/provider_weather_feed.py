"""Store provider current-conditions weather text for nighttime haze deferral.

At night (solar elevation ≤ 10°), the pyranometer-based local haze detector
is inactive.  This module bridges the forecast provider's current-conditions
present-weather field into the enrichment pipeline so compose_weather_text()
can detect haze/smoke during nighttime hours (ADR-071, API-MANUAL §8 nighttime
mode).

Data flow:
  /current endpoint fetch  → set_latest_weather_text()  (stores latest reading)
  compose_weather_text()   → get_provider_weather_text() (reads, checks staleness)

Staleness: provider weather text older than 2 hours is treated as unavailable
(API-MANUAL §8 nighttime mode: "Provider data freshness: > 2 hours old =
unavailable").  Absence of fresh data is not evidence of absence — no haze
label is emitted when data is stale.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_latest_weather_text: str | None = None
_latest_timestamp: float = 0.0
_STALE_SECONDS: float = 7200.0  # 2 hours per API-MANUAL §8 nighttime mode


def set_latest_weather_text(*, weather_text: str | None, timestamp: float) -> None:
    """Store the latest provider weather text.

    Called from _fill_cloudcover_from_provider() in endpoints/observations.py
    after a successful provider conditions fetch.  Stores even when
    weather_text is None so the timestamp reflects the last fetch attempt.

    Args:
        weather_text: Provider current-conditions weather text string, or None
                      when the provider returned no present-weather description.
        timestamp:    Unix epoch seconds at the time of the fetch.
    """
    global _latest_weather_text, _latest_timestamp  # noqa: PLW0603
    _latest_weather_text = weather_text
    _latest_timestamp = timestamp


def get_provider_weather_text() -> tuple[str | None, float | None]:
    """Return (weather_text, age_seconds) or (None, None).

    Returns (None, None) in three cases:
      - Never set (no provider fetch has occurred).
      - Data is stale (> 2 hours since last fetch).
      - weather_text stored is None (provider returned no present-weather field).

    When data is fresh and non-None, returns (weather_text, age_seconds) so
    callers can inspect freshness if needed (age_seconds is always < 7200.0
    when the first element is not None).
    """
    if _latest_timestamp == 0.0:
        return (None, None)
    age = time.time() - _latest_timestamp
    if age > _STALE_SECONDS:
        return (None, None)
    if _latest_weather_text is None:
        return (None, None)
    return (_latest_weather_text, age)


def reset() -> None:
    """Clear module state.  For test isolation only."""
    global _latest_weather_text, _latest_timestamp  # noqa: PLW0603
    _latest_weather_text = None
    _latest_timestamp = 0.0
