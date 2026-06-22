"""OpenAQ API v3 client for bootstrap-only historical PM2.5 retrieval (ADR-068 T8.1).

This module is bootstrap-only — it is NOT a real-time AQI provider.
Latency from OpenAQ is ~1-2 hours, making it unsuitable for real-time haze
detection.  Use only during the one-time calibration bootstrap CLI run.

Auth:   X-API-Key header.  Key from env var WEEWX_CLEARSKIES_OPENAQ_API_KEY.
Rate:   60 req/min, 2,000 req/hr (free tier).  We sleep 1 second between
        requests to stay well within limits.
Source: https://docs.openaq.org/docs/introduction (v3 API)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openaq.org/v3"
_MAX_RADIUS_M = 25000  # 25 km max per OpenAQ docs
_REQUEST_TIMEOUT = 30  # seconds
_RATE_LIMIT_SLEEP = 1.0  # 1 second between requests (safe at 60/min)

# PM value sanity bounds.
_PM_MIN = 0.0
_PM_MAX = 999.0


@dataclass(slots=True)
class PMRecord:
    """A single hourly PM2.5 measurement from OpenAQ."""

    timestamp_utc: float  # unix timestamp (seconds since epoch)
    pm25: float           # PM2.5 concentration in µg/m³


def _get_api_key() -> str:
    """Read the OpenAQ API key from the environment.

    Raises:
        RuntimeError: Key is absent or empty.
    """
    key = os.environ.get("WEEWX_CLEARSKIES_OPENAQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "WEEWX_CLEARSKIES_OPENAQ_API_KEY environment variable not set. "
            "Register for a free API key at https://explore.openaq.org/register"
        )
    return key


def _api_get(path: str, params: dict | None = None) -> dict:
    """Make a GET request to the OpenAQ v3 API.

    Uses urllib (stdlib only — no requests dependency).
    Includes the X-API-Key header on every request.
    Sleeps 1 second after each request to stay within the 60 req/min limit.

    Args:
        path:   URL path relative to _BASE_URL, e.g. "/locations".
        params: Optional dict of query parameters.

    Returns:
        Parsed JSON response body as a dict.

    Raises:
        RuntimeError: HTTP error, network error, or JSON decode failure.
    """
    url = f"{_BASE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    api_key = _get_api_key()
    req = Request(url, headers={"X-API-Key": api_key, "Accept": "application/json"})

    logger.debug("OpenAQ GET %s", url)
    try:
        with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(
                f"OpenAQ rate limit exceeded (HTTP 429). "
                f"Wait a minute and retry, or increase _RATE_LIMIT_SLEEP."
            ) from exc
        if exc.code == 410:
            raise RuntimeError(
                "OpenAQ API v1/v2 endpoints are retired (HTTP 410). "
                "This client uses v3 only — check _BASE_URL."
            ) from exc
        raise RuntimeError(
            f"OpenAQ HTTP error {exc.code} for {url}: {exc.reason}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"OpenAQ network error for {url}: {exc.reason}"
        ) from exc

    # Respect rate limits: 1-second sleep after every call.
    time.sleep(_RATE_LIMIT_SLEEP)

    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"OpenAQ returned non-JSON response for {url}: {exc}"
        ) from exc


def find_nearest_pm25_sensor(
    lat: float, lon: float
) -> tuple[int, float, float, str]:
    """Find the nearest PM2.5 sensor within 25 km of the given coordinates.

    Queries GET /v3/locations?coordinates={lat},{lon}&radius=25000.
    Filters results for PM2.5 sensors (parameter name contains "pm25" or
    "pm2.5", case-insensitive).

    Iterates all location results and all sensors within each location,
    selecting the first sensor whose parameter looks like PM2.5.

    Args:
        lat: Station latitude in decimal degrees.
        lon: Station longitude in decimal degrees.

    Returns:
        Tuple of (sensor_id, monitor_lat, monitor_lon, location_name).

    Raises:
        RuntimeError: No PM2.5 monitor found within 25 km, or API error.
    """
    params = {
        "coordinates": f"{lat},{lon}",
        "radius": _MAX_RADIUS_M,
        "limit": 100,
        "page": 1,
    }

    data = _api_get("/locations", params=params)
    results = data.get("results", [])
    meta = data.get("meta", {})

    if not results:
        raise RuntimeError(
            f"No air quality monitors found within {_MAX_RADIUS_M // 1000} km "
            f"of coordinates ({lat}, {lon}). "
            "Try a location with an OpenAQ-listed monitor nearby, or check "
            "https://explore.openaq.org/ to find the nearest monitor."
        )

    total_found = meta.get("found", len(results))
    logger.info(
        "OpenAQ /locations: found %d monitors within %d km of (%s, %s)",
        total_found,
        _MAX_RADIUS_M // 1000,
        lat,
        lon,
    )

    # Iterate locations sorted by distance (OpenAQ returns nearest first).
    for location in results:
        loc_name = location.get("name", "") or location.get("locality", "") or "Unknown"
        loc_id = location.get("id")

        # Each location has a list of sensors.
        sensors = location.get("sensors", [])
        for sensor in sensors:
            parameter = sensor.get("parameter", {})
            param_name = str(parameter.get("name", "")).lower()
            param_display = str(parameter.get("displayName", "")).lower()

            # Accept "pm25", "pm2.5", or display names containing those substrings.
            is_pm25 = (
                "pm25" in param_name
                or "pm2.5" in param_name
                or "pm25" in param_display
                or "pm2.5" in param_display
            )
            if not is_pm25:
                continue

            sensor_id = sensor.get("id")
            if sensor_id is None:
                continue

            # Extract monitor coordinates (may be at location level).
            coords = location.get("coordinates") or {}
            monitor_lat = float(coords.get("latitude", lat))
            monitor_lon = float(coords.get("longitude", lon))

            logger.info(
                "OpenAQ: selected PM2.5 sensor id=%d at location %r (%s, %s)",
                sensor_id,
                loc_name,
                monitor_lat,
                monitor_lon,
            )
            return (int(sensor_id), monitor_lat, monitor_lon, str(loc_name))

    raise RuntimeError(
        f"No PM2.5 sensors found within {_MAX_RADIUS_M // 1000} km of "
        f"({lat}, {lon}). "
        f"Found {len(results)} monitor(s) but none measured PM2.5. "
        "Check https://explore.openaq.org/ for the nearest PM2.5 monitor."
    )


def fetch_historical_pm25(
    sensor_id: int,
    date_from: str,
    date_to: str,
) -> list[PMRecord]:
    """Fetch historical hourly PM2.5 measurements for a sensor.

    Queries GET /v3/sensors/{sensor_id}/measurements with full pagination.
    Chunks requests by year to avoid timeouts (per OpenAQ docs recommendation
    to narrow date ranges to <= 1 year for performance).

    Args:
        sensor_id: OpenAQ sensor ID (from find_nearest_pm25_sensor).
        date_from: ISO-8601 date string (e.g. "2024-01-01").
        date_to:   ISO-8601 date string (e.g. "2025-12-31").

    Returns:
        List of PMRecord sorted by timestamp ascending.
        Records with invalid/missing values are silently skipped.
        PM values outside [0, 999] µg/m³ are skipped.

    Raises:
        RuntimeError: API error or network failure.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    # Parse date_from and date_to as dates so we can chunk by year.
    try:
        dt_from = date.fromisoformat(date_from[:10])
        dt_to = date.fromisoformat(date_to[:10])
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid date format in fetch_historical_pm25: {exc}. "
            "Use ISO-8601 format (e.g. '2024-01-01')."
        ) from exc

    if dt_from >= dt_to:
        raise RuntimeError(
            f"date_from ({date_from}) must be before date_to ({date_to})."
        )

    all_records: list[PMRecord] = []

    # Build year-long chunks to avoid API timeouts on large date ranges.
    chunk_start = dt_from
    while chunk_start < dt_to:
        # End of this chunk: 1 year later, or date_to, whichever is earlier.
        chunk_end_candidate = date(chunk_start.year + 1, chunk_start.month, chunk_start.day)
        chunk_end = min(chunk_end_candidate, dt_to)

        chunk_from_str = chunk_start.isoformat() + "T00:00:00Z"
        chunk_to_str = chunk_end.isoformat() + "T00:00:00Z"

        logger.info(
            "OpenAQ: fetching sensor %d measurements %s -> %s",
            sensor_id,
            chunk_start.isoformat(),
            chunk_end.isoformat(),
        )

        chunk_records = _fetch_sensor_measurements_paginated(
            sensor_id=sensor_id,
            date_from=chunk_from_str,
            date_to=chunk_to_str,
        )
        all_records.extend(chunk_records)

        chunk_start = chunk_end

    # Sort ascending by timestamp.
    all_records.sort(key=lambda r: r.timestamp_utc)

    logger.info(
        "OpenAQ: fetched %d total PM2.5 records for sensor %d (%s to %s)",
        len(all_records),
        sensor_id,
        date_from,
        date_to,
    )
    return all_records


def _fetch_sensor_measurements_paginated(
    sensor_id: int,
    date_from: str,
    date_to: str,
) -> list[PMRecord]:
    """Fetch all pages of measurements for a sensor over a date range.

    Internal helper — called by fetch_historical_pm25 once per year-chunk.

    Args:
        sensor_id: OpenAQ sensor ID.
        date_from: ISO-8601 datetime string with Z suffix.
        date_to:   ISO-8601 datetime string with Z suffix.

    Returns:
        List of PMRecord (unsorted).
    """
    records: list[PMRecord] = []
    page = 1
    limit = 1000  # OpenAQ max per page

    while True:
        params = {
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "page": page,
        }
        path = f"/sensors/{sensor_id}/measurements"

        data = _api_get(path, params=params)
        meta = data.get("meta", {})
        results = data.get("results", [])
        total_found = meta.get("found", 0)

        # Parse each measurement record.
        for item in results:
            record = _parse_measurement(item)
            if record is not None:
                records.append(record)

        logger.debug(
            "OpenAQ sensor %d: page %d/%d — got %d results (%d valid so far)",
            sensor_id,
            page,
            -(-total_found // limit) if total_found else "?",  # ceil division
            len(results),
            len(records),
        )

        # Check if we need another page.
        fetched_so_far = (page - 1) * limit + len(results)
        if not results or fetched_so_far >= total_found:
            break

        page += 1

    return records


def _parse_measurement(item: dict) -> PMRecord | None:
    """Parse a single measurement dict from the OpenAQ v3 response.

    Returns None for records with missing, null, or out-of-range values.

    OpenAQ v3 measurement format:
      {
        "value": 12.5,
        "parameter": {"id": 2, "name": "pm25", "units": "µg/m³", ...},
        "period": {
          "datetimeFrom": {"utc": "2024-01-01T00:00:00Z", "local": "..."},
          "datetimeTo":   {"utc": "2024-01-01T01:00:00Z", "local": "..."},
          ...
        },
        ...
      }
    """
    # Extract PM2.5 value.
    raw_value = item.get("value")
    if raw_value is None:
        return None
    try:
        pm25 = float(raw_value)
    except (TypeError, ValueError):
        return None

    # Validate PM2.5 range.
    if not (_PM_MIN <= pm25 <= _PM_MAX):
        logger.debug("OpenAQ: skipping out-of-range PM2.5 value %.1f", pm25)
        return None

    # Extract timestamp.  Use the end of the measurement period (datetimeTo)
    # as the record timestamp — OpenAQ uses time-ending convention.
    period = item.get("period") or {}
    datetime_to = period.get("datetimeTo") or {}
    utc_str = datetime_to.get("utc") or ""

    if not utc_str:
        # Fall back to datetimeFrom if datetimeTo is missing.
        datetime_from = period.get("datetimeFrom") or {}
        utc_str = datetime_from.get("utc") or ""

    if not utc_str:
        logger.debug("OpenAQ: skipping record with missing timestamp")
        return None

    try:
        timestamp_utc = _iso8601z_to_unix(utc_str)
    except (ValueError, OverflowError):
        logger.debug("OpenAQ: skipping record with unparseable timestamp %r", utc_str)
        return None

    return PMRecord(timestamp_utc=timestamp_utc, pm25=pm25)


def _iso8601z_to_unix(iso_str: str) -> float:
    """Convert an ISO-8601 UTC string (ending in Z) to a Unix timestamp.

    Handles the format returned by OpenAQ: "2024-01-01T00:00:00Z" or
    "2024-01-01T00:00:00.000000Z".

    Args:
        iso_str: ISO-8601 datetime string with Z suffix.

    Returns:
        Unix timestamp as a float.

    Raises:
        ValueError: String cannot be parsed.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    # Normalise: strip trailing Z, replace with +00:00 for fromisoformat.
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.timestamp()
