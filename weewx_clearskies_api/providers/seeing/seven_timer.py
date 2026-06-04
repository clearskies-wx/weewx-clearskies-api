"""7Timer astronomical seeing forecast provider.

Fetches 72-hour seeing forecasts from the free 7Timer ASTRO product.
No API key or registration required.

Endpoint:
    GET http://www.7timer.info/bin/api.pl?lon={lon}&lat={lat}&product=astro&output=json

7Timer returns a 72-hour forecast in 3-hour intervals (timepoints 3, 6, ..., 72).
All index fields use -9999 to indicate undefined/unavailable data; entries with
-9999 in seeing, transparency, or cloudcover are skipped.

Error handling strategy:
    All errors return an empty list rather than raising.  7Timer is a free
    best-effort service with no SLA.  A missing seeing forecast degrades
    gracefully — the endpoint that consumes this provider can return a
    partial or empty response rather than a 5xx.

This provider uses httpx.Client directly (not ProviderHTTPClient) because
7Timer is a simple, keyless, no-auth provider.  Coupling to the keyed-provider
error taxonomy would add unnecessary complexity and re-wrapping overhead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical output model
# ---------------------------------------------------------------------------


class SeeingForecastPoint(BaseModel):
    """One 3-hour forecast point from the 7Timer ASTRO product.

    Field ranges per 7Timer documentation:
      seeing_index:      1-8 (1 = perfect <0.5", 8 = severe >2.5")
      transparency_index:1-8 (1 = best)
      cloud_cover_octet: 1-9 (1 = clear 0-6%, 9 = overcast 94-100%)
      lifted_index:      -10 to 15 (positive = stable atmosphere)
      wind_speed_class:  1-8 (Beaufort-derived scale)
      wind_direction:    N/NE/E/SE/S/SW/W/NW
      temp_2m_c:         Celsius (-76 to +60)
      humidity_class:    -4 to 16 (7Timer proprietary scale)
      prec_type:         none/rain/snow/frzr/icep
    """

    model_config = ConfigDict(extra="forbid")

    valid_time: datetime        # init + timepoint hours (UTC, timezone-aware)
    seeing_index: int           # 1-8
    transparency_index: int     # 1-8
    cloud_cover_octet: int      # 1-9
    lifted_index: int           # atmospheric stability
    wind_speed_class: int       # 1-8
    wind_direction: str         # N/NE/E/SE/S/SW/W/NW
    temp_2m_c: int              # Celsius
    humidity_class: int         # -4 to 16
    prec_type: str              # none/rain/snow/frzr/icep


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------

_UNDEFINED = -9999


class SevenTimerProvider:
    """Fetches and parses 7Timer ASTRO seeing forecasts.

    Usage:
        provider = SevenTimerProvider(
            base_url="http://www.7timer.info/bin/api.pl",
            timeout_seconds=10,
        )
        points = provider.fetch_forecast(lat=47.6, lon=-122.3)
        provider.close()

    Or use as a context manager:
        with SevenTimerProvider(...) as p:
            points = p.fetch_forecast(...)
    """

    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._client = httpx.Client(timeout=httpx.Timeout(float(timeout_seconds)))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_forecast(self, lat: float, lon: float) -> list[SeeingForecastPoint]:
        """Fetch and parse the 7Timer ASTRO forecast for the given location.

        Args:
            lat: Latitude in decimal degrees (-90 to +90).
            lon: Longitude in decimal degrees (-180 to +180).

        Returns:
            Ordered list of SeeingForecastPoint (3-hour intervals, up to 72 h).
            Returns an empty list on any error (network, parse, or missing data).
        """
        url = self._build_url(lat, lon)

        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException:
            logger.warning(
                "7Timer request timed out after %s s (lat=%s, lon=%s)",
                self._timeout_seconds, lat, lon,
            )
            return []
        except httpx.ConnectError:
            logger.warning(
                "7Timer connection error for lat=%s, lon=%s", lat, lon
            )
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "7Timer returned HTTP %s for lat=%s, lon=%s",
                exc.response.status_code, lat, lon,
            )
            return []

        try:
            payload: dict[str, Any] = response.json()
        except Exception:  # noqa: BLE001  (json.JSONDecodeError or similar)
            logger.warning(
                "7Timer returned non-JSON response for lat=%s, lon=%s", lat, lon
            )
            return []

        return self._parse_payload(payload, lat, lon)

    def close(self) -> None:
        """Release the underlying httpx.Client."""
        self._client.close()

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "SevenTimerProvider":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_url(self, lat: float, lon: float) -> str:
        """Build the 7Timer ASTRO URL with query parameters."""
        return (
            f"{self._base_url}"
            f"?lon={lon}&lat={lat}&product=astro&output=json"
        )

    def _parse_payload(
        self,
        payload: dict[str, Any],
        lat: float,
        lon: float,
    ) -> list[SeeingForecastPoint]:
        """Parse the 7Timer JSON payload into a list of SeeingForecastPoint.

        Skips entries with undefined fields (-9999) in seeing, transparency,
        or cloudcover.  Skips individual entries that fail to parse rather
        than aborting the whole response.
        """
        init_str = payload.get("init")
        if not init_str:
            logger.warning(
                "7Timer response missing 'init' field for lat=%s, lon=%s", lat, lon
            )
            return []

        try:
            init_time = datetime.strptime(str(init_str), "%Y%m%d%H").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.warning(
                "7Timer 'init' value %r is not in expected YYYYMMDDHH format "
                "(lat=%s, lon=%s)",
                init_str, lat, lon,
            )
            return []

        dataseries = payload.get("dataseries")
        if dataseries is None:
            logger.warning(
                "7Timer response missing 'dataseries' key for lat=%s, lon=%s",
                lat, lon,
            )
            return []

        if not isinstance(dataseries, list):
            logger.warning(
                "7Timer 'dataseries' is not a list for lat=%s, lon=%s", lat, lon
            )
            return []

        points: list[SeeingForecastPoint] = []
        for entry in dataseries:
            point = self._parse_entry(entry, init_time, lat, lon)
            if point is not None:
                points.append(point)

        return points

    def _parse_entry(
        self,
        entry: Any,
        init_time: datetime,
        lat: float,
        lon: float,
    ) -> SeeingForecastPoint | None:
        """Parse a single dataseries entry.

        Returns None (and logs a warning) if the entry is missing required
        fields, contains undefined sentinel values in core astronomy fields,
        or fails Pydantic validation.
        """
        try:
            timepoint = int(entry["timepoint"])
            seeing = int(entry["seeing"])
            transparency = int(entry["transparency"])
            cloudcover = int(entry["cloudcover"])

            # Skip entries where core astronomy fields are undefined.
            if seeing == _UNDEFINED or transparency == _UNDEFINED or cloudcover == _UNDEFINED:
                return None

            lifted_index = int(entry["lifted_index"])
            rh2m = int(entry["rh2m"])
            temp_2m_c = int(entry["temp2m"])
            prec_type = str(entry["prec_type"])

            wind10m = entry["wind10m"]
            wind_direction = str(wind10m["direction"])
            wind_speed_class = int(wind10m["speed"])

            valid_time = init_time + timedelta(hours=timepoint)

            return SeeingForecastPoint(
                valid_time=valid_time,
                seeing_index=seeing,
                transparency_index=transparency,
                cloud_cover_octet=cloudcover,
                lifted_index=lifted_index,
                wind_speed_class=wind_speed_class,
                wind_direction=wind_direction,
                temp_2m_c=temp_2m_c,
                humidity_class=rh2m,
                prec_type=prec_type,
            )

        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping 7Timer dataseries entry due to parse error "
                "(lat=%s, lon=%s): %s",
                lat, lon, exc,
            )
            return None
