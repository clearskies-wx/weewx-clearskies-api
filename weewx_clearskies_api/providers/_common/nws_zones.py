"""Shared NWS marine zone discovery utility (ADR-083, PROVIDER-MANUAL §14.8).

**Not a dispatch-registered provider module.** No CAPABILITY symbol is
exported and this file is never passed to `wire_providers()`. It is a
shared building block consumed by four call sites:
  1. NWS marine zone text forecast provider (`providers/marine/nws_marine.py`,
     PROVIDER-MANUAL §14.4) — resolves the WFO that issues the CWF (Coastal
     Waters Forecast) text product covering a given `zone_id` via
     `get_wfo_for_zone()`, so the provider can fetch by zone_id alone
     without requiring lat/lon at call time.
  2. NWS Surf Zone Forecast provider (`providers/marine/nws_srf.py`,
     PROVIDER-MANUAL §14.5) — resolves the WFO (CWA) covering a spot via
     `get_cwa()`.
  3. Marine zone alerts extension (PROVIDER-MANUAL §8, ADR-089) — discovers
     zone IDs within the operator's configured alert radius at setup time.
  4. NWPS nearshore wave data provider (`providers/marine/nwps.py`,
     PROVIDER-MANUAL §14.6) — first attempt at WFO determination via
     `get_cwa()`, falling back to a bounding-box lookup.

`get_wfo_for_zone()` (2026-07-11, NWS marine zone text forecast fix):
  The NWS API has no `/zones/coastal/{zoneId}/forecast` endpoint (confirmed
  404 "Forecasts for marine areas are not yet supported by this API" against
  live api.weather.gov) — marine forecasts are only published as CWF free
  text products keyed by WFO, not by zone. `get_wfo_for_zone()` reuses the
  existing 24h-cached `type=coastal` zone list (`_fetch_coastal_zone_list`,
  originally added for `discover_marine_zones()`) and looks up the
  `cwa` array for the matching zone, returning its first entry. This keeps
  `nws_marine.fetch(zone_id=...)` a single-argument call (no lat/lon
  needed) and costs nothing extra after the first lookup in a given 24h
  cache window, since the same zone list is already fetched for zone
  discovery. See PROVIDER-MANUAL §14.4 for the updated wire format.

Algorithm (PROVIDER-MANUAL §14.8):
  1. GET /points/{lat},{lon}                      -> cwa (WFO office ID, e.g. "ILM")
  2. GET /zones?type=coastal                       -> full coastal zone list,
     filtered client-side by the `cwa` property (see "CWA filtering" below)
  3. GET /zones/coastal/{zoneId} per candidate zone -> polygon geometry
  4. haversine(lat, lon, nearest vertex) per zone
  5. Zones within `radius_miles`, sorted by distance ascending

CWA filtering (lead call, this round):
  The NWS `/zones` endpoint's `area` query parameter only accepts
  AreaCode values (states / marine areas, e.g. "NC", "AM") — NOT WFO/CWA
  codes.  `GET /zones?type=coastal&area=ILM` returns HTTP 400 (verified live
  against api.weather.gov).  There is no server-side CWA filter for `/zones`.
  Per the lead's explicit fallback instruction, this module fetches the full
  `type=coastal` zone list (~570 zones as of this writing, no pagination
  encountered in practice — see "Pagination" below) and filters client-side
  on each zone's `properties.cwa` array, which reliably contains the WFO
  office ID (e.g. `["ILM"]`) per a live fixture capture of
  `/zones/coastal/AMZ250`.  The full list is cached for 24h so this is a
  one-time cost per cache window, not a per-discovery cost.

Pagination:
  A live, unbounded `GET /zones?type=coastal` request returned all ~570
  features in a single response with no `pagination.next` key.  This module
  still checks for a `pagination.next` link defensively (NWS API pagination
  behavior is not contractually guaranteed to stay this way) and follows it
  if present.

Invocation context (PROVIDER-MANUAL §14.8):
  Called at setup/wizard time, not per-request. Results are stored in
  operator configuration by the caller.

NWS User-Agent (ADR-006):
  Same construction pattern as providers/alerts/nws.py and
  providers/forecast/nws.py: "(weewx-clearskies-api/<version>, <contact>)"
  when a contact is configured, "(weewx-clearskies-api/<version>)" +
  one-time WARN when not.  No project-level hardcoded fallback (ADR-006).

Rate limiter:
  Separate `RateLimiter` instance (name="nws-zones") rather than sharing the
  alerts/forecast limiters — same "don't couple domain quotas" rationale
  documented in providers/forecast/nws.py's rate-limiter section. 5 req/s
  "be polite" guard shared conceptually with all api.weather.gov traffic,
  enforced per-module per the established pattern in this codebase.

Caching (ADR-017):
  Zone discovery is a setup-time operation; even so, each of the three
  outbound call types is cached 86400s (24h) since NWS zone geometry and
  station-to-CWA mapping are effectively static:
    - /points -> cwa                         (get_cwa, keyed by lat4/lon4)
    - /zones?type=coastal -> zone list        (keyed globally, no params)
    - /zones/coastal/{zoneId} -> geometry     (keyed by zone_id)

Error handling:
  - /points 404 (non-US coordinates) -> get_cwa() raises
    GeographicallyUnsupported (same taxonomy as providers/forecast/nws.py);
    discover_marine_zones() catches this specifically and returns [].
  - Per-zone geometry fetch fails (any canonical provider error) -> log
    WARNING, skip that zone, continue with the remaining candidates.
  - Any other network/protocol error on /points or /zones list -> propagates
    via the canonical taxonomy (ProviderHTTPClient / RateLimiter already
    raise canonical exception types; never re-wrapped here).

Haversine accuracy (PROVIDER-MANUAL §14.8): ~0.1 miles is sufficient. This
module implements the standard haversine formula directly — no project-wide
haversine helper existed as of this writing (searched via grep before
writing; see rules/coding.md §3 DRY rule).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.errors import (
    GeographicallyUnsupported,
    ProviderError,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "nws"
DOMAIN = "zones"
NWS_BASE_URL = "https://api.weather.gov"
_ZONE_CACHE_TTL_SECONDS = 86400  # 24h — zone geometry/CWA mapping is static
_API_VERSION = "0.1.0"
_EARTH_RADIUS_MILES = 3959.0

# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarineZone:
    """One NWS coastal marine zone within the discovery radius.

    zone_id: NWS zone identifier, e.g. "AMZ250".
    name: Human-readable zone name, e.g.
        "Coastal waters from Surf City to Cape Fear NC out 20 NM".
    distance_miles: Haversine distance from the input coordinates to the
        nearest vertex of the zone's polygon geometry.
    """

    zone_id: str
    name: str
    distance_miles: float


# ---------------------------------------------------------------------------
# Rate limiter (separate instance — not shared with alerts/forecast limiters,
# same rationale as providers/forecast/nws.py's rate-limiter section)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="nws-zones",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (security-baseline §3.5)
# ---------------------------------------------------------------------------


class _NwsPointCwaProperties(BaseModel):
    """Minimal /points/{lat,lon} properties — only the cwa field is needed here."""

    model_config = ConfigDict(extra="ignore")

    cwa: str


class _NwsPointCwaResponse(BaseModel):
    """NWS /points/{lat,lon} GeoJSON Feature envelope (cwa-only wire shape)."""

    model_config = ConfigDict(extra="ignore")

    type: str
    properties: _NwsPointCwaProperties


class _NwsZoneListProperties(BaseModel):
    """Properties of one zone feature from the /zones list endpoint."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    cwa: list[str] = Field(default_factory=list)


class _NwsZoneListFeature(BaseModel):
    """One zone feature from the /zones list endpoint (geometry omitted/null)."""

    model_config = ConfigDict(extra="ignore")

    properties: _NwsZoneListProperties


class _NwsZoneListResponse(BaseModel):
    """NWS /zones?type=coastal response envelope (GeoJSON FeatureCollection)."""

    model_config = ConfigDict(extra="ignore")

    features: list[_NwsZoneListFeature] = Field(default_factory=list)


class _NwsZoneDetailProperties(BaseModel):
    """Properties of a single-zone detail response (/zones/coastal/{zoneId})."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class _NwsZoneDetailResponse(BaseModel):
    """NWS /zones/coastal/{zoneId} GeoJSON Feature envelope."""

    model_config = ConfigDict(extra="ignore")

    properties: _NwsZoneDetailProperties
    geometry: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton — one per module, not per request)
# ---------------------------------------------------------------------------

_http_client: ProviderHTTPClient | None = None
_http_client_ua: str = ""


def _get_http_client(user_agent: str) -> ProviderHTTPClient:
    """Return the module-level HTTP client, (re-)constructing if UA changed."""
    global _http_client, _http_client_ua  # noqa: PLW0603
    if _http_client is None or _http_client_ua != user_agent:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=user_agent,
        )
        _http_client_ua = user_agent
    return _http_client


# ---------------------------------------------------------------------------
# User-Agent construction (ADR-006) — same pattern as alerts/forecast nws.py
# ---------------------------------------------------------------------------


def _build_user_agent(contact: str | None) -> str:
    """Build the NWS User-Agent string per ADR-006.

    NO project-level hardcoded fallback — would put the project on the hook
    for operator traffic patterns (ADR-006).
    """
    base = f"weewx-clearskies-api/{_API_VERSION}"
    if contact and contact.strip():
        return f"({base}, {contact.strip()})"
    return f"({base})"


_warned_missing_contact = False


def _warn_once_missing_contact() -> None:
    global _warned_missing_contact  # noqa: PLW0603
    if not _warned_missing_contact:
        logger.warning(
            "NWS zone discovery User-Agent contact is not set. "
            "Set the relevant nws_user_agent_contact in api.conf "
            "to reduce the risk of being blocked during NWS security events. "
            "See ADR-006 for the operator-managed compliance model."
        )
        _warned_missing_contact = True


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017 §Cache key)
# ---------------------------------------------------------------------------


def _build_points_cache_key(lat: float, lon: float) -> str:
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "points_cwa",
            "params": {"lat4": round(lat, 4), "lon4": round(lon, 4)},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_zones_list_cache_key() -> str:
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "endpoint": "zones_coastal_list", "params": {}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_zone_detail_cache_key(zone_id: str) -> str:
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "endpoint": "zone_detail",
            "params": {"zone_id": zone_id},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Haversine (PROVIDER-MANUAL §14.8: ~0.1 mile accuracy is sufficient)
# ---------------------------------------------------------------------------


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in statute miles."""
    lat1_r, lon1_r, lat2_r, lon2_r = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_MILES * c


def _iter_coordinate_pairs(node: Any) -> Iterator[tuple[float, float]]:
    """Yield (lon, lat) leaf pairs from an arbitrarily-nested GeoJSON coordinates
    array (handles Polygon and MultiPolygon geometry without distinguishing
    between them — we only need the full set of vertices).
    """
    if not isinstance(node, list) or not node:
        return
    if all(isinstance(x, int | float) for x in node) and 2 <= len(node) <= 3:
        yield (float(node[0]), float(node[1]))
        return
    for child in node:
        yield from _iter_coordinate_pairs(child)


def _min_distance_to_geometry(
    lat: float, lon: float, geometry: dict[str, Any] | None
) -> float | None:
    """Minimum haversine distance from (lat, lon) to any vertex of a GeoJSON
    Polygon/MultiPolygon geometry. Returns None if geometry is missing/empty.
    """
    if not geometry:
        return None
    coordinates = geometry.get("coordinates")
    if not coordinates:
        return None
    best: float | None = None
    for lon_v, lat_v in _iter_coordinate_pairs(coordinates):
        d = haversine_miles(lat, lon, lat_v, lon_v)
        if best is None or d < best:
            best = d
    return best


# ---------------------------------------------------------------------------
# get_cwa — /points -> cwa (shared by nws_marine.py and nws_srf.py)
# ---------------------------------------------------------------------------


def get_cwa(lat: float, lon: float, *, user_agent_contact: str | None = None) -> str:
    """Return the NWS County Warning Area (WFO office ID) for coordinates.

    Calls GET /points/{lat},{lon} and extracts the 'cwa' field. Cached 24h,
    keyed by rounded (lat, lon).

    Raises:
        GeographicallyUnsupported: /points returned 404 (non-US lat/lon),
            mirroring providers/forecast/nws.py's established pattern.
        QuotaExhausted: NWS returned 429.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
        ProviderProtocolError: Response validation failed.
    """
    if not user_agent_contact:
        _warn_once_missing_contact()

    cache_key = _build_points_cache_key(lat, lon)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(str, cached)

    _rate_limiter.acquire()

    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    lat4 = round(lat, 4)
    lon4 = round(lon, 4)
    url = f"{NWS_BASE_URL}/points/{lat4},{lon4}"

    try:
        response = client.get(url, headers={"Accept": "application/geo+json"})
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            raise GeographicallyUnsupported(
                f"NWS /points returned 404 for lat={lat4},lon={lon4} — "
                "location is outside NWS coverage (USA + territories only).",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        raise

    try:
        wire = _NwsPointCwaResponse.model_validate(response.json())
    except (ValidationError, ValueError) as exc:
        logger.error(
            "NWS /points response validation failed (zone discovery): %s. "
            "Response body (first 2000 chars): %.2000s",
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS /points response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    cwa = wire.properties.cwa
    get_cache().set(cache_key, cwa, ttl_seconds=_ZONE_CACHE_TTL_SECONDS)
    return cwa


# ---------------------------------------------------------------------------
# Coastal zone list fetch (cached, paginated defensively)
# ---------------------------------------------------------------------------


def _fetch_coastal_zone_list(client: ProviderHTTPClient) -> list[dict[str, Any]]:
    """Return all NWS type=coastal zones as [{"id", "name", "cwa": [...]}], cached 24h.

    CWA filtering happens client-side by the caller (discover_marine_zones) —
    the /zones `area` query parameter does not accept WFO/CWA codes (see
    module docstring "CWA filtering" section).

    Handles a `pagination.next` link defensively, though a live unbounded
    request returned all ~570 coastal zones in a single page with no such key.
    """
    cache_key = _build_zones_list_cache_key()
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(list[dict[str, Any]], cached)

    all_features: list[dict[str, Any]] = []
    url: str | None = f"{NWS_BASE_URL}/zones"
    params: dict[str, str] | None = {"type": "coastal"}

    while url:
        _rate_limiter.acquire()
        response = client.get(url, params=params, headers={"Accept": "application/geo+json"})
        try:
            raw = response.json()
        except ValueError as exc:
            raise ProviderProtocolError(
                f"NWS /zones response was not valid JSON: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc
        try:
            wire = _NwsZoneListResponse.model_validate(raw)
        except ValidationError as exc:
            logger.error(
                "NWS /zones response validation failed: %s. "
                "Response body (first 2000 chars): %.2000s",
                exc,
                response.text,
            )
            raise ProviderProtocolError(
                f"NWS /zones response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        for feature in wire.features:
            all_features.append(
                {
                    "id": feature.properties.id,
                    "name": feature.properties.name,
                    "cwa": feature.properties.cwa,
                }
            )

        # Defensive pagination follow — not observed live, but the NWS API
        # documents pagination.next on other collection endpoints.
        next_url = raw.get("pagination", {}).get("next") if isinstance(raw, dict) else None
        url = next_url
        params = None  # next_url already carries its own query string

    get_cache().set(cache_key, all_features, ttl_seconds=_ZONE_CACHE_TTL_SECONDS)
    return all_features


def _fetch_zone_detail(client: ProviderHTTPClient, zone_id: str) -> dict[str, Any]:
    """Return {"id", "name", "geometry"} for one zone, cached 24h by zone_id."""
    cache_key = _build_zone_detail_cache_key(zone_id)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return cast(dict[str, Any], cached)

    _rate_limiter.acquire()
    url = f"{NWS_BASE_URL}/zones/coastal/{zone_id}"
    response = client.get(url, headers={"Accept": "application/geo+json"})
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderProtocolError(
            f"NWS zone detail response for {zone_id!r} was not valid JSON: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc
    try:
        wire = _NwsZoneDetailResponse.model_validate(raw)
    except ValidationError as exc:
        logger.error(
            "NWS zone detail response validation failed for %s: %s. "
            "Response body (first 2000 chars): %.2000s",
            zone_id,
            exc,
            response.text,
        )
        raise ProviderProtocolError(
            f"NWS zone detail response validation failed for {zone_id!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    result = {
        "id": wire.properties.id,
        "name": wire.properties.name,
        "geometry": wire.geometry,
    }
    get_cache().set(cache_key, result, ttl_seconds=_ZONE_CACHE_TTL_SECONDS)
    return result


# ---------------------------------------------------------------------------
# get_wfo_for_zone — zone_id -> WFO via the cached coastal zone list
# (shared by nws_marine.py; see module docstring "get_wfo_for_zone()")
# ---------------------------------------------------------------------------


def get_wfo_for_zone(zone_id: str, *, user_agent_contact: str | None = None) -> str:
    """Return the NWS WFO (CWA) office ID whose CWF text product covers `zone_id`.

    Looks up `zone_id` in the 24h-cached `type=coastal` zone list and
    returns the first entry of its `cwa` array (the same field
    `discover_marine_zones()` filters candidates by). This is the WFO to
    use with `GET /products/types/CWF/locations/{wfo}`.

    Raises:
        ProviderProtocolError: `zone_id` is not present in the NWS coastal
            zone list, or its `cwa` array is empty — either an
            operator-configured zone_id that isn't a valid NWS coastal
            zone, or an unexpected NWS schema change.
        QuotaExhausted: NWS returned 429 (rate limit), propagated from the
            underlying `/zones` list fetch.
        TransientNetworkError: Network/DNS failure or 5xx after retries.
    """
    if not zone_id or not zone_id.strip():
        raise ValueError("zone_id must not be empty")
    zone_id = zone_id.strip()

    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)
    zone_list = _fetch_coastal_zone_list(client)

    for entry in zone_list:
        if entry.get("id") != zone_id:
            continue
        cwa_list = entry.get("cwa") or []
        if not cwa_list:
            raise ProviderProtocolError(
                f"NWS coastal zone {zone_id!r} has no cwa (WFO) mapping in "
                "the /zones?type=coastal response.",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            )
        return cast(str, cwa_list[0])

    raise ProviderProtocolError(
        f"Zone id {zone_id!r} not found in the NWS type=coastal zone list; "
        "cannot determine the covering WFO for its CWF text product.",
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    )


# ---------------------------------------------------------------------------
# Public entrypoint (PROVIDER-MANUAL §14.8)
# ---------------------------------------------------------------------------


def discover_marine_zones(
    lat: float,
    lon: float,
    radius_miles: float,
    *,
    user_agent_contact: str | None = None,
) -> list[MarineZone]:
    """Discover NWS coastal marine zones within radius of given coordinates.

    Algorithm (PROVIDER-MANUAL §14.8):
      1. GET /points/{lat},{lon} -> cwa
      2. GET /zones?type=coastal -> coastal zone list, filtered client-side
         by cwa (see module docstring "CWA filtering")
      3. GET /zones/coastal/{zoneId} per candidate -> polygon geometry
      4. haversine(lat, lon, nearest vertex) per zone
      5. Zones within radius_miles, sorted by distance ascending

    Returns an empty list for inland points (no CWA — /points 404) and for
    coastal points with no zones within radius_miles.

    Per-zone geometry fetch failures are logged at WARNING and skipped;
    the remaining candidates are still evaluated (module docstring
    "Error handling").
    """
    try:
        cwa = get_cwa(lat, lon, user_agent_contact=user_agent_contact)
    except GeographicallyUnsupported:
        logger.info(
            "No NWS CWA for lat=%s,lon=%s (outside NWS coverage); "
            "returning zero marine zones.",
            lat,
            lon,
        )
        return []

    user_agent = _build_user_agent(user_agent_contact)
    client = _get_http_client(user_agent)

    zone_list = _fetch_coastal_zone_list(client)
    candidates = [z for z in zone_list if cwa in z.get("cwa", [])]

    results: list[MarineZone] = []
    for candidate in candidates:
        zone_id = candidate["id"]
        try:
            detail = _fetch_zone_detail(client, zone_id)
        except ProviderError as exc:
            logger.warning(
                "Failed to fetch geometry for marine zone %s: %s; skipping.",
                zone_id,
                exc,
            )
            continue

        distance = _min_distance_to_geometry(lat, lon, detail.get("geometry"))
        if distance is None:
            logger.warning(
                "Marine zone %s has no usable geometry; skipping.", zone_id
            )
            continue

        if distance <= radius_miles:
            results.append(
                MarineZone(
                    zone_id=zone_id,
                    name=detail.get("name") or candidate.get("name") or zone_id,
                    distance_miles=round(distance, 2),
                )
            )

    results.sort(key=lambda z: z.distance_miles)
    return results


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client, _http_client_ua, _warned_missing_contact  # noqa: PLW0603
    _http_client = None
    _http_client_ua = ""
    _warned_missing_contact = False
