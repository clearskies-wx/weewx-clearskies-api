"""NOAA CO-OPS tides & water levels provider module (ADR-038, ADR-083, PROVIDER-MANUAL §14.2).

Five responsibilities per ADR-038 §2 / PROVIDER-MANUAL §1:
  1. Outbound API call — CO-OPS Data API `/api/prod/datagetter` (predictions,
     water_level, water_temperature products) and the CO-OPS Metadata API
     `/mdapi/prod/webapi/stations.json` (station discovery).
  2. Response parsing — wire-shape Pydantic models for each product's response
     envelope.
  3. Translation to canonical `TidePrediction` / `WaterLevel` models (unit is
     already meters on the wire because every request pins `units=metric`).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy.

Mandatory `application` parameter (PROVIDER-MANUAL §14.2):
  CO-OPS requires every request to identify the calling application.  All
  outbound requests (data + metadata) include `application=clearskies`.

CO-OPS timestamp handling (ADR-020):
  Every datagetter request pins `time_zone=gmt`, so wire timestamps are
  *naive* `"YYYY-MM-DD HH:MM"` strings already expressed in GMT — no UTC
  offset is present.  The shared `to_utc_iso8601_from_offset` helper
  (providers/_common/datetime_utils.py) requires an explicit offset and
  raises `ProviderProtocolError` on a bare-naive value (by design, because
  a naive NWS timestamp would indicate a real NWS schema change).  CO-OPS's
  naive-but-known-GMT shape is a different, legitimate wire format, so this
  module defines its own local helper (`_parse_gmt_naive_to_iso8601`) that
  attaches UTC directly rather than reusing the NWS-shaped helper.

CO-OPS "200 OK for errors" quirk (PROVIDER-MANUAL §14.2, §10):
  CO-OPS returns HTTP 200 with `{"error": {"message": "..."}}` for both (a)
  legitimate "no data in this window" conditions and (b) malformed/invalid
  requests (bad station id, bad datum, etc).  The one documented, stable
  distinguishing signal is the message text: CO-OPS's own docs describe the
  no-data condition as "No data was found. This product may not be offered
  at this station at the requested time." — any other `error.message` is
  treated as a protocol-level failure.  Per the manual: no-data → empty list
  (not an error); anything else → ProviderProtocolError.

Cache layer (ADR-017, PROVIDER-MANUAL §14.2):
  Caches the post-normalization canonical value, not raw JSON.  Key: SHA-256
  of {"provider_id": "coops", "station_id": ..., "product": ...}.  Per-product
  TTL: predictions 21600s (6h, harmonic predictions are stable within a tidal
  epoch), water_level 600s (10min), water_temperature 1800s (30min), station
  metadata 86400s (24h).

Wire-shape Pydantic (security-baseline §3.5):
  extras="ignore" throughout so CO-OPS schema additions don't break us;
  missing required fields raise ValidationError -> ProviderProtocolError.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import TidePrediction, WaterLevel
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "coops"
DOMAIN = "tides"

COOPS_DATA_BASE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
COOPS_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"

# CO-OPS requires application identification on every request (PROVIDER-MANUAL §14.2).
_APPLICATION_ID = "clearskies"

# CO-OPS's documented no-data message (PROVIDER-MANUAL §14.2) -- substring match,
# case-insensitive, since CO-OPS has been observed to vary capitalization.
_NO_DATA_MESSAGE_SUBSTRING = "no data was found"

# Per-product cache TTLs (PROVIDER-MANUAL §14.2).
_PREDICTIONS_TTL = 21600  # 6h -- harmonic predictions are stable within a tidal epoch
_WATER_LEVEL_TTL = 600  # 10min
_WATER_TEMPERATURE_TTL = 1800  # 30min
_STATION_METADATA_TTL = 86400  # 24h

_DEFAULT_PRODUCTS: tuple[str, ...] = ("predictions", "water_level", "water_temperature")

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "time",
        "height",
        "type",  # TidePrediction
        "water_level_time",
        "water_level_height",
        "datum",
        "quality",  # WaterLevel
        "water_temperature",
    ),
    geographic_coverage="us_coastal",
    auth_required=(),
    default_poll_interval_seconds=600,  # 10 min for observations
    operator_notes=(
        "NOAA CO-OPS tides and water levels. No API key required. "
        "US coastal coverage. All requests must include application=clearskies."
    ),
    refresh_interval=600,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="NOAA CO-OPS",
        attribution_text=(
            "Data courtesy of NOAA Center for Operational Oceanographic Products and Services"
        ),
        text_prefix="Data courtesy of",
        text_provider_name="NOAA CO-OPS",
        url="https://tidesandcurrents.noaa.gov/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3) -- PROVIDER-MANUAL §14.2: no documented CO-OPS
# limit; use 2 req/s as a courtesy limit.
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="coops-tides",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# Source: PROVIDER-MANUAL §14.2 + https://api.tidesandcurrents.noaa.gov/api/prod/
# ---------------------------------------------------------------------------


class _CoopsErrorBody(BaseModel):
    """Inner `error` object of a CO-OPS logical-error response."""

    model_config = ConfigDict(extra="ignore")

    message: str | None = None


class _CoopsErrorResponse(BaseModel):
    """CO-OPS returns HTTP 200 with this envelope for logical errors."""

    model_config = ConfigDict(extra="ignore")

    error: _CoopsErrorBody


class _CoopsPredictionPoint(BaseModel):
    """Single `predictions[]` entry from the `product=predictions` response."""

    model_config = ConfigDict(extra="ignore")

    t: str  # "YYYY-MM-DD HH:MM", naive GMT (time_zone=gmt requested)
    v: str  # water level in meters, as a string


class _CoopsPredictionsResponse(BaseModel):
    """CO-OPS `product=predictions` response envelope."""

    model_config = ConfigDict(extra="ignore")

    predictions: list[_CoopsPredictionPoint] = Field(default_factory=list)


class _CoopsDataPoint(BaseModel):
    """Single `data[]` entry from `product=water_level` / `product=water_temperature`."""

    model_config = ConfigDict(extra="ignore")

    t: str  # "YYYY-MM-DD HH:MM", naive GMT
    v: str  # reading value as a string; may be "" for a data gap
    s: str | None = None  # sigma (standard deviation)
    f: str | None = None  # quality flags, comma-separated
    q: str | None = None  # quality assurance level, e.g. "v" verified, "p" preliminary


class _CoopsDataResponse(BaseModel):
    """CO-OPS `product=water_level` / `product=water_temperature` response envelope."""

    model_config = ConfigDict(extra="ignore")

    data: list[_CoopsDataPoint] = Field(default_factory=list)


class _CoopsStation(BaseModel):
    """Single station entry from the CO-OPS Metadata API `stations.json`.

    `products` is intentionally loosely typed: the exact nested shape
    returned by `expand=products` is not part of CO-OPS's stable public
    contract, so it's captured as opaque JSON and best-effort-decoded in
    `_extract_product_names` rather than pinned to a brittle schema.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    lat: float
    lng: float
    state: str | None = None
    products: Any = None


class _CoopsStationsResponse(BaseModel):
    """CO-OPS Metadata API `stations.json` response envelope."""

    model_config = ConfigDict(extra="ignore")

    stations: list[_CoopsStation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton -- one per module, not per request)
# ---------------------------------------------------------------------------

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (tides/coops provider)"

_http_client: ProviderHTTPClient | None = None


def _get_http_client() -> ProviderHTTPClient:
    """Return the module-level HTTP client, constructing it on first use."""
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=_USER_AGENT,
        )
    return _http_client


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017 §Cache key, PROVIDER-MANUAL §14.2)
# ---------------------------------------------------------------------------


def _build_cache_key(station_id: str, product: str) -> str:
    """Build a deterministic cache key for (provider_id, station_id, product)."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "station_id": station_id, "product": product},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# Station metadata is fetched as a single full list (the mdapi endpoint takes
# no lat/lon filter) and then filtered client-side, so its cache key has no
# per-query parameters -- one entry serves every discover_stations() call.
_STATION_CACHE_KEY = hashlib.sha256(
    json.dumps({"provider_id": PROVIDER_ID, "endpoint": "stations"}, sort_keys=True).encode()
).hexdigest()


# ---------------------------------------------------------------------------
# Datetime normalization (ADR-020) -- see module docstring for why this is a
# local helper rather than a reuse of to_utc_iso8601_from_offset.
# ---------------------------------------------------------------------------


def _parse_gmt_naive_to_iso8601(s: str) -> str:
    """Convert a CO-OPS `time_zone=gmt` timestamp to UTC ISO-8601 with Z suffix.

    Args:
        s: CO-OPS timestamp, e.g. ``"2026-07-09 00:00"`` (naive, already GMT).

    Returns:
        UTC ISO-8601 string with Z suffix, e.g. ``"2026-07-09T00:00:00Z"``.

    Raises:
        ProviderProtocolError: The timestamp does not match CO-OPS's documented
            `"YYYY-MM-DD HH:MM"` shape (indicates a CO-OPS schema change).
    """
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProviderProtocolError(
            f"CO-OPS timestamp parse failed for {s!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_yyyymmdd() -> str:
    """Return today's UTC date in CO-OPS's `begin_date` format (`YYYYMMDD`)."""
    return datetime.now(UTC).strftime("%Y%m%d")


def _parse_reading(v: str, *, context: str) -> float:
    """Parse a CO-OPS string reading (`"v"` field) into a float.

    Raises:
        ProviderProtocolError: `v` is not a parseable number (CO-OPS schema
            change -- a genuine data gap is represented as `""` and handled
            by callers before reaching this function).
    """
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ProviderProtocolError(
            f"CO-OPS {context} reading {v!r} is not a parseable number: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


# ---------------------------------------------------------------------------
# High/low tide classification (PROVIDER-MANUAL §14.2)
# ---------------------------------------------------------------------------


def _classify_tide_predictions(points: list[_CoopsPredictionPoint]) -> list[TidePrediction]:
    """Classify each prediction point as high/low/interpolated by neighbor comparison.

    height > both neighbors -> "high"; height < both neighbors -> "low";
    otherwise (including the first/last point, which has no pair of
    neighbors) -> None (interpolated point).
    """
    heights = [_parse_reading(p.v, context="predictions") for p in points]
    n = len(points)
    results: list[TidePrediction] = []
    for i, point in enumerate(points):
        height = heights[i]
        tide_type: str | None = None
        if 0 < i < n - 1:
            prev_height = heights[i - 1]
            next_height = heights[i + 1]
            if height > prev_height and height > next_height:
                tide_type = "high"
            elif height < prev_height and height < next_height:
                tide_type = "low"
        results.append(
            TidePrediction(
                time=_parse_gmt_naive_to_iso8601(point.t),
                height=height,
                type=tide_type,
            )
        )
    return results


def _to_water_level(point: _CoopsDataPoint) -> WaterLevel:
    """Map a CO-OPS `product=water_level` data point to a canonical WaterLevel."""
    return WaterLevel(
        time=_parse_gmt_naive_to_iso8601(point.t),
        height=_parse_reading(point.v, context="water_level"),
        datum="MLLW",
        quality=point.q,
    )


# ---------------------------------------------------------------------------
# CO-OPS datagetter request + logical-error handling (PROVIDER-MANUAL §14.2, §10)
# ---------------------------------------------------------------------------


def _request_product(station_id: str, product: str, extra_params: dict[str, str]) -> dict[str, Any]:
    """Call the CO-OPS datagetter endpoint and return the parsed JSON body.

    CO-OPS returns HTTP 200 even for logical errors (see module docstring).
    A documented "no data in this window" condition is treated as empty
    (returns `{}`); any other `error.message` is a protocol-level failure.
    """
    _rate_limiter.acquire()

    params = {
        "product": product,
        "station": station_id,
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "application": _APPLICATION_ID,
        **extra_params,
    }
    response = _get_http_client().get(COOPS_DATA_BASE_URL, params=params)

    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ProviderProtocolError(
            f"CO-OPS response is not valid JSON: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    if isinstance(body, dict) and "error" in body:
        try:
            message = _CoopsErrorResponse.model_validate(body).error.message or ""
        except ValidationError:
            message = str(body.get("error"))

        if _NO_DATA_MESSAGE_SUBSTRING in message.lower():
            logger.info(
                "CO-OPS station=%s product=%s has no data for the requested window: %s",
                station_id,
                product,
                message,
            )
            return {}

        logger.error(
            "CO-OPS error station=%s product=%s: %s",
            station_id,
            product,
            message,
        )
        raise ProviderProtocolError(
            f"CO-OPS error for station {station_id} product {product}: {message}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    return body


# ---------------------------------------------------------------------------
# Per-product fetch + cache (PROVIDER-MANUAL §14.2)
# ---------------------------------------------------------------------------


def _fetch_predictions(station_id: str) -> list[TidePrediction]:
    cache_key = _build_cache_key(station_id, "predictions")
    cached = get_cache().get(cache_key)
    if cached is not None:
        return [TidePrediction.model_validate(d) for d in cached]

    body = _request_product(
        station_id,
        "predictions",
        {
            "datum": "MLLW",
            "begin_date": _today_yyyymmdd(),
            "range": "72",
        },
    )
    if not body:
        predictions: list[TidePrediction] = []
    else:
        try:
            wire = _CoopsPredictionsResponse.model_validate(body)
        except ValidationError as exc:
            raise ProviderProtocolError(
                f"CO-OPS predictions response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        predictions = _classify_tide_predictions(wire.predictions)

    get_cache().set(
        cache_key,
        [p.model_dump() for p in predictions],
        ttl_seconds=_PREDICTIONS_TTL,
    )
    return predictions


def _fetch_water_level(station_id: str) -> list[WaterLevel]:
    cache_key = _build_cache_key(station_id, "water_level")
    cached = get_cache().get(cache_key)
    if cached is not None:
        return [WaterLevel.model_validate(d) for d in cached]

    body = _request_product(
        station_id,
        "water_level",
        {
            "datum": "MLLW",
            "range": "24",
        },
    )
    if not body:
        water_levels: list[WaterLevel] = []
    else:
        try:
            wire = _CoopsDataResponse.model_validate(body)
        except ValidationError as exc:
            raise ProviderProtocolError(
                f"CO-OPS water_level response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        # Skip data-gap points (empty "v") rather than raising -- gaps are a
        # normal occurrence in observed gauge data, not a protocol error.
        water_levels = [_to_water_level(p) for p in wire.data if p.v not in (None, "")]

    get_cache().set(
        cache_key,
        [w.model_dump() for w in water_levels],
        ttl_seconds=_WATER_LEVEL_TTL,
    )
    return water_levels


def _fetch_water_temperature(station_id: str) -> float | None:
    """Return the most recent water temperature reading, or None.

    Some CO-OPS stations don't report water temperature at all (no sensor);
    that surfaces as the documented no-data condition and is handled as
    "no reading" rather than an error, per PROVIDER-MANUAL §14.2.

    Cache stores `{"value": ...}` rather than the bare float/None so a cache
    hit of "no reading" (value=None) is distinguishable from a cache miss
    (get_cache().get() returning None).
    """
    cache_key = _build_cache_key(station_id, "water_temperature")
    cached = get_cache().get(cache_key)
    if cached is not None:
        cached_value: float | None = cached["value"]
        return cached_value

    body = _request_product(station_id, "water_temperature", {"range": "24"})
    value: float | None = None
    if body:
        try:
            wire = _CoopsDataResponse.model_validate(body)
        except ValidationError as exc:
            raise ProviderProtocolError(
                f"CO-OPS water_temperature response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        # Data is chronological ascending; walk backward for the most recent
        # non-gap reading.
        for point in reversed(wire.data):
            if point.v in (None, ""):
                continue
            value = _parse_reading(point.v, context="water_temperature")
            break

    get_cache().set(cache_key, {"value": value}, ttl_seconds=_WATER_TEMPERATURE_TTL)
    return value


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(
    *,
    station_id: str,
    products: tuple[str, ...] = _DEFAULT_PRODUCTS,
) -> dict[str, Any]:
    """Fetch CO-OPS tide predictions and water level observations.

    Args:
        station_id: CO-OPS station identifier (e.g. `"8443970"`).
        products: Which products to fetch. Subset of
            `("predictions", "water_level", "water_temperature")`.

    Returns:
        dict with:
          - "predictions": list[TidePrediction]
          - "water_levels": list[WaterLevel]
          - "water_temperature": float | None (most recent reading)
          - "station_id": str

    Raises:
        QuotaExhausted: Local rate limiter tripped (2 req/s courtesy limit).
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed, or CO-OPS returned
            a logical error other than the documented no-data condition
            (e.g. an invalid station id).
    """
    result: dict[str, Any] = {
        "station_id": station_id,
        "predictions": [],
        "water_levels": [],
        "water_temperature": None,
    }

    if "predictions" in products:
        result["predictions"] = _fetch_predictions(station_id)
    if "water_level" in products:
        result["water_levels"] = _fetch_water_level(station_id)
    if "water_temperature" in products:
        result["water_temperature"] = _fetch_water_temperature(station_id)

    logger.info(
        "CO-OPS fetch complete station_id=%s predictions=%d water_levels=%d water_temperature=%s",
        station_id,
        len(result["predictions"]),
        len(result["water_levels"]),
        result["water_temperature"],
    )
    return result


# ---------------------------------------------------------------------------
# Station discovery (PROVIDER-MANUAL §14.2)
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two WGS84 points."""
    earth_radius_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _extract_product_names(raw: Any) -> list[str]:
    """Best-effort extraction of product names from the mdapi `expand=products` payload.

    The nested shape of `expand=products` is not part of CO-OPS's stable
    public contract (unlike the datagetter product responses documented in
    PROVIDER-MANUAL §14.2), so this makes no assumption stronger than
    "a list of dicts with a name-ish key, possibly nested under a `products`
    key" and returns `[]` for anything else rather than raising -- a missing
    product list is a degraded-but-usable discovery result, not a protocol
    failure.
    """
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("parameter")
            if isinstance(name, str):
                names.append(name)
    return names


def _get_stations_metadata() -> list[_CoopsStation]:
    """Return the full CO-OPS water-level station list, cached 24h."""
    cached = get_cache().get(_STATION_CACHE_KEY)
    if cached is not None:
        return [_CoopsStation.model_validate(d) for d in cached]

    _rate_limiter.acquire()
    response = _get_http_client().get(
        COOPS_STATIONS_URL,
        params={"type": "waterlevels", "units": "metric", "expand": "products"},
    )
    try:
        wire = _CoopsStationsResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        raise ProviderProtocolError(
            f"CO-OPS station metadata validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    get_cache().set(
        _STATION_CACHE_KEY,
        [s.model_dump() for s in wire.stations],
        ttl_seconds=_STATION_METADATA_TTL,
    )
    return wire.stations


def discover_stations(lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
    """Discover CO-OPS water-level stations within `radius_km` of (lat, lon).

    Returns:
        List of dicts sorted by distance ascending:
          {"id": str, "name": str | None, "lat": float, "lon": float,
           "distance_km": float, "products": list[str]}
    """
    stations = _get_stations_metadata()
    results: list[dict[str, Any]] = []
    for station in stations:
        distance_km = _haversine_km(lat, lon, station.lat, station.lng)
        if distance_km <= radius_km:
            results.append(
                {
                    "id": station.id,
                    "name": station.name,
                    "lat": station.lat,
                    "lon": station.lng,
                    "distance_km": round(distance_km, 2),
                    "products": _extract_product_names(station.products),
                }
            )
    results.sort(key=lambda s: s["distance_km"])
    return results


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None
