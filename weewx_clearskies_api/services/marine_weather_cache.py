"""On-demand cache for general weather data at marine locations (T1.2).

General weather data (air temp, wind, precip, sky conditions) for marine
locations is fetched on demand and cached with spatial deduplication.  Two
locations within the configured dedup radius share a single cache entry,
so one fetch serves both.

Architecture:
  Dict-based cache keyed by grid group string keys sourced from
  marine_location_resolver's distance-based clustering (resolve_grid_groups).
  Before the resolver has run (e.g. during startup), falls back to rounding
  coordinates to two decimal places so the cache still functions.

Thread safety:
  threading.Lock around the cache dict.  Unlike the provider-layer
  MemoryCache (which relies on CPython's GIL for the single-writer
  pattern), this cache may be written from multiple request threads
  concurrently, so an explicit Lock is required.

This cache does NOT call providers directly, to avoid circular imports
with the provider layer.  The caller is responsible for fetching fresh
data and calling put_weather() to populate the cache.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class MarineWeatherCache:
    """On-demand cache for general weather data at marine locations."""

    def __init__(
        self,
        forecast_ttl_hours: int,
        observation_ttl_minutes: int,
        dedup_radius_km: float,
    ) -> None:
        self._forecast_ttl_seconds = forecast_ttl_hours * 3600
        self._observation_ttl_seconds = observation_ttl_minutes * 60
        self._dedup_radius_km = dedup_radius_km
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _grid_key(self, lat: float, lon: float) -> str:
        """Look up grid group key for these coordinates.

        Falls back to rounding if the resolver hasn't been configured yet.
        """
        from weewx_clearskies_api.services.marine_location_resolver import (
            get_grid_group_by_coords,
        )

        group = get_grid_group_by_coords(lat, lon)
        if group is not None:
            return group
        # Fallback: round to nearest degree fraction (only during startup
        # before resolve_grid_groups() has run).
        return f"{round(lat, 2)}_{round(lon, 2)}"

    def get_weather(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Return cached weather data if present and not expired, else None."""
        key = self._grid_key(lat, lon)
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        age = time.monotonic() - entry.get("forecast_fetched_at", 0.0)
        if age >= self._observation_ttl_seconds:
            return None
        return entry

    def put_weather(self, lat: float, lon: float, data: dict[str, Any]) -> None:
        """Store weather data under the grid key for this coordinate."""
        key = self._grid_key(lat, lon)
        data["forecast_fetched_at"] = time.monotonic()
        with self._lock:
            self._data[key] = data

    def get_current_conditions(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Extract current conditions from cached forecast data.

        Returns a dict with airTemp, windSpeed, windDirection, weatherCode,
        isDay, and skyCondition if cached data is available; None otherwise.
        """
        weather = self.get_weather(lat, lon)
        if weather is None:
            return None
        return {
            "airTemp": weather.get("airTemp"),
            "windSpeed": weather.get("windSpeed"),
            "windDirection": weather.get("windDirection"),
            "weatherCode": weather.get("weatherCode"),
            "isDay": weather.get("isDay"),
            "skyCondition": weather.get("skyCondition"),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache: MarineWeatherCache | None = None


def configure(
    forecast_ttl_hours: int,
    observation_ttl_minutes: int,
    dedup_radius_km: float,
) -> None:
    """Create the module-level MarineWeatherCache singleton."""
    global _cache  # noqa: PLW0603
    _cache = MarineWeatherCache(
        forecast_ttl_hours=forecast_ttl_hours,
        observation_ttl_minutes=observation_ttl_minutes,
        dedup_radius_km=dedup_radius_km,
    )
    logger.info(
        "Marine weather cache configured: forecast_ttl=%dh, observation_ttl=%dm, dedup_radius=%.1fkm",
        forecast_ttl_hours,
        observation_ttl_minutes,
        dedup_radius_km,
    )


def get_marine_weather_cache() -> MarineWeatherCache | None:
    """Return the module-level MarineWeatherCache singleton, or None if not configured."""
    return _cache
