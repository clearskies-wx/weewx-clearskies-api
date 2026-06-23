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
import math
import os
import time
from dataclasses import dataclass
from datetime import date as _date
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

# Minimum data span in days for a sensor to qualify for auto-bootstrap.
_MIN_DATA_SPAN_DAYS = 365


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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two points in km (Haversine formula)."""
    r = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.asin(math.sqrt(a))


def _parse_location_date(raw: object) -> _date | None:
    """Parse a datetimeFirst/datetimeLast value from an OpenAQ location record.

    OpenAQ v3 may return these as a dict with "utc"/"local" keys, or as a
    plain ISO-8601 string.  Returns None if unparseable.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        # {"utc": "2022-01-01T00:00:00Z", "local": "..."}
        raw_str = raw.get("utc") or raw.get("local") or ""
    else:
        raw_str = str(raw)
    raw_str = raw_str.strip()
    if not raw_str:
        return None
    try:
        # Take only the date portion.
        return _date.fromisoformat(raw_str[:10])
    except (ValueError, TypeError):
        return None


def _query_nearby_pm25_locations(lat: float, lon: float) -> list[dict]:
    """Fetch all reference PM2.5 locations within 25 km, with pagination.

    Queries GET /v3/locations with isMonitor=true to filter for reference /
    regulatory monitors only.  Paginates if meta.found > limit.

    Returns a flat list of raw location dicts from the OpenAQ response.
    Raises RuntimeError on API error.
    """
    limit = 100
    page = 1
    all_results: list[dict] = []

    while True:
        params = {
            "coordinates": f"{lat},{lon}",
            "radius": _MAX_RADIUS_M,
            "isMonitor": "true",
            "limit": limit,
            "page": page,
        }
        data = _api_get("/locations", params=params)
        results = data.get("results", [])
        meta = data.get("meta", {})

        all_results.extend(results)

        raw_found = str(meta.get("found", 0)).lstrip(">").strip()
        try:
            total_found = int(raw_found)
        except (TypeError, ValueError):
            total_found = 0

        if page == 1:
            logger.info(
                "OpenAQ /locations: found %d reference monitors within %d km of (%s, %s)",
                total_found,
                _MAX_RADIUS_M // 1000,
                lat,
                lon,
            )

        fetched_so_far = (page - 1) * limit + len(results)
        if not results or fetched_so_far >= total_found:
            break

        page += 1

    return all_results


def _is_reference_grade(location: dict) -> bool:
    """Return True if the location is a reference/regulatory monitor.

    OpenAQ's ``isMonitor=true`` only means "fixed location" (not mobile),
    so it includes low-cost sensors (AirGradient, Clarity, PurpleAir).
    Reference-grade stations are identified by their instrument metadata:
    government monitoring networks list instruments as "Government Monitor".
    """
    instruments = location.get("instruments", [])
    for inst in instruments:
        inst_name = str(inst.get("name", "")).lower()
        if "government" in inst_name:
            return True
    return False


def _location_to_sensor_dicts(
    location: dict, station_lat: float, station_lon: float
) -> list[dict]:
    """Extract PM2.5 sensor dicts from a single location record.

    Returns one dict per PM2.5 sensor found in the location.
    Skips sensors whose parameter is not PM2.5.
    Skips locations that are not reference-grade (government monitors).
    """
    if not _is_reference_grade(location):
        return []

    loc_name = (
        location.get("name")
        or location.get("locality")
        or "Unknown"
    )
    loc_id = location.get("id")
    coords = location.get("coordinates") or {}
    monitor_lat = float(coords.get("latitude") or station_lat)
    monitor_lon = float(coords.get("longitude") or station_lon)
    distance_km = _haversine_km(station_lat, station_lon, monitor_lat, monitor_lon)

    # Parse data span dates (at location level in OpenAQ v3).
    # Try both camelCase and snake_case field names defensively.
    raw_first = (
        location.get("datetimeFirst")
        or location.get("datetime_first")
    )
    raw_last = (
        location.get("datetimeLast")
        or location.get("datetime_last")
    )
    datetime_first = _parse_location_date(raw_first)
    datetime_last = _parse_location_date(raw_last)

    sensors = location.get("sensors", [])
    result = []
    for sensor in sensors:
        parameter = sensor.get("parameter", {})
        param_name = str(parameter.get("name", "")).lower()
        param_display = str(parameter.get("displayName", "")).lower()

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

        result.append({
            "sensor_id": int(sensor_id),
            "location_id": int(loc_id) if loc_id is not None else None,
            "name": str(loc_name),
            "lat": monitor_lat,
            "lon": monitor_lon,
            "distance_km": round(distance_km, 3),
            "datetime_first": datetime_first.isoformat() if datetime_first else None,
            "datetime_last": datetime_last.isoformat() if datetime_last else None,
            "is_monitor": True,  # we only query isMonitor=true
        })
    return result


def find_best_pm25_sensor(lat: float, lon: float) -> list[dict]:
    """Find ranked reference PM2.5 sensors within 25 km of the given coordinates.

    Queries GET /v3/locations with isMonitor=true, filters for sensors with
    at least 365 days of data span, and returns them sorted by distance
    ascending.

    This replaces the old find_nearest_pm25_sensor() which returned a single
    tuple and raised RuntimeError on no results.  This function returns an
    empty list when no qualifying sensors are found — the caller handles that.

    Args:
        lat: Station latitude in decimal degrees.
        lon: Station longitude in decimal degrees.

    Returns:
        List of sensor dicts sorted by distance_km ascending.  Each dict has:
          sensor_id (int), location_id (int|None), name (str), lat (float),
          lon (float), distance_km (float), datetime_first (str|None),
          datetime_last (str|None), is_monitor (bool).
        Empty list if no qualifying sensors found.

    Raises:
        RuntimeError: API error or network failure.
    """
    raw_locations = _query_nearby_pm25_locations(lat, lon)
    if not raw_locations:
        logger.info(
            "OpenAQ: no reference monitors found within %d km of (%s, %s)",
            _MAX_RADIUS_M // 1000,
            lat,
            lon,
        )
        return []

    candidates: list[dict] = []
    for location in raw_locations:
        for sensor_dict in _location_to_sensor_dicts(location, lat, lon):
            # Apply 12-month data span filter.
            dt_first_str = sensor_dict.get("datetime_first")
            dt_last_str = sensor_dict.get("datetime_last")
            if dt_first_str and dt_last_str:
                try:
                    dt_first = _date.fromisoformat(dt_first_str)
                    dt_last = _date.fromisoformat(dt_last_str)
                    span_days = (dt_last - dt_first).days
                    if span_days < _MIN_DATA_SPAN_DAYS:
                        logger.debug(
                            "OpenAQ: skipping sensor %d '%s' — data span %d days < %d",
                            sensor_dict["sensor_id"],
                            sensor_dict["name"],
                            span_days,
                            _MIN_DATA_SPAN_DAYS,
                        )
                        continue
                except (ValueError, TypeError):
                    pass  # dates unparseable — include sensor anyway
            # else: dates absent — include sensor (no filter applied)

            candidates.append(sensor_dict)

    # Sort by distance ascending (OpenAQ returns nearest first, but sort
    # explicitly after filtering to guarantee order).
    candidates.sort(key=lambda d: d["distance_km"])

    logger.info(
        "OpenAQ: %d qualifying reference PM2.5 sensor(s) within %d km of (%s, %s)",
        len(candidates),
        _MAX_RADIUS_M // 1000,
        lat,
        lon,
    )
    return candidates


def get_nearby_sensors(lat: float, lon: float) -> list[dict]:
    """List all reference PM2.5 sensors within 25 km for the admin UI dropdown.

    Same query as find_best_pm25_sensor but WITHOUT the 12-month data age
    filter, so the operator can see all available sensors.

    Args:
        lat: Station latitude in decimal degrees.
        lon: Station longitude in decimal degrees.

    Returns:
        List of sensor dicts sorted by distance_km ascending (same shape as
        find_best_pm25_sensor).  Empty list if none found.

    Raises:
        RuntimeError: API error or network failure.
    """
    raw_locations = _query_nearby_pm25_locations(lat, lon)
    if not raw_locations:
        return []

    all_sensors: list[dict] = []
    for location in raw_locations:
        all_sensors.extend(_location_to_sensor_dicts(location, lat, lon))

    all_sensors.sort(key=lambda d: d["distance_km"])
    return all_sensors


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
        sensor_id: OpenAQ sensor ID (from find_best_pm25_sensor).
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
            "datetime_from": date_from,
            "datetime_to": date_to,
            "limit": limit,
            "page": page,
        }
        path = f"/sensors/{sensor_id}/measurements"

        data = _api_get(path, params=params)
        meta = data.get("meta", {})
        results = data.get("results", [])
        # OpenAQ v3 returns meta.found as ">1000" (string with > prefix)
        # when the exact count exceeds the limit. Strip the prefix.
        raw_found = str(meta.get("found", 0)).lstrip(">").strip()
        try:
            total_found = int(raw_found)
        except (TypeError, ValueError):
            total_found = 0

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
