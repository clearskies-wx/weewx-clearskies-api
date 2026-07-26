"""NOAA CUDEM bathymetry utility (Phase 3, T3.1; PROVIDER-MANUAL §14.7).

**Not a dispatch-registered provider module.** This is a data-access
utility for surf-spot configuration and runtime SWAN bidirectional profiles.

``download_bathymetric_profile`` produces a 1-D depth transect (list of
``BathymetryPoint``) available to operator-configured setups (the fishing
habitat-annotation consumer, ``enrichment/fishing_scorer.py``, was deleted
as orphaned dead code — audit finding F2, 2026-07-25; nothing in this repo
calls this function today, but it is kept as it is still exercised by
``services/swan_domain.py``'s import of this module).
``download_bidirectional_profile`` produces a bidirectional
coastline-anchored CUDEM transect cached at
``/etc/weewx-clearskies/spot_profiles/`` and consumed by SWAN runs.

Data source:
    This module queries the NCEI ArcGIS ImageServer ``identify``
    endpoint directly:
    ``https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/identify``.
    This is a free, keyless, government-hosted REST point-query service that
    serves NOAA's Continuously Updated Digital Elevation Model (CUDEM) at
    1/9 arc-second resolution (~3.4m).

    The NCEI ImageServer ``identify`` endpoint is a single-point query API:
    one HTTP request per (lat, lon) sample. See ``_query_depths_m`` below.

Elevation sign convention: NCEI/CUDEM elevation is relative to Mean Sea
Level (MSL). Negative elevation = underwater depth. Depth values returned
by this module are always non-negative meters (``depth_m = max(0,
-elevation)``); points that come back with positive elevation (land) are
treated as zero depth. A ``value`` of ``"NoData"`` (or a missing/null
value) means the queried point falls outside CUDEM's data coverage and is
treated as an unknown reading (``None``), not zero depth.

Regional deep-water thresholds and hardcoded fallback profiles encode
rough, non-authoritative continental-shelf characteristics (steep Pacific
shelf vs. gradual Gulf shelf, etc.) — see ``REGION_DEEP_WATER_THRESHOLDS_M``
and ``FALLBACK_DEPTH_PROFILES_M`` below.

Units: this module works exclusively in meters (depth, distance) throughout.
No unit conversion is performed or required here — CUDEM/NCEI elevations
are always meters, and stored profile distances are always meters; the
project's ``UnitTransformer`` only enters the picture at API response time
when a request is scoped to a non-metric unit system, which is out of scope
for this setup-time module.

Attribution (PROVIDER-MANUAL §14.7): NOAA CUDEM data requires attribution
and a navigation disclaimer wherever bathymetric data is displayed — see
``ATTRIBUTION`` / ``DISCLAIMER`` below.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scipy.interpolate import PchipInterpolator

from weewx_clearskies_api.providers._common.errors import (
    ProviderError,
    ProviderProtocolError,
    QuotaExhausted,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class BathymetryPoint:
    """One point in a CUDEM bathymetric depth profile.

    Moved here from config/marine_config.py — the wizard-time api.conf
    storage path that originally motivated its home there has been removed;
    this class is now only used by the bathymetry download and analysis
    pipeline in this module and enrichment/fishing_scorer.py.
    """

    distance_m: float
    depth_m: float

    def __init__(self, section: dict[str, Any]) -> None:
        self.distance_m = float(section.get("distance_m", 0.0))
        self.depth_m = float(section.get("depth_m", 0.0))

# ---------------------------------------------------------------------------
# Attribution (PROVIDER-MANUAL §14.7) — display wherever bathymetric data
# (depth profiles, habitat annotations) is shown to end users.
# ---------------------------------------------------------------------------

ATTRIBUTION = "NOAA National Centers for Environmental Information"
DISCLAIMER = "Not for navigation"

# ---------------------------------------------------------------------------
# NCEI ArcGIS ImageServer CUDEM endpoints (T1.2 — see module docstring for why
# this replaced the nonexistent OpenTopoData ``/v1/cudem`` endpoint).
# ---------------------------------------------------------------------------

PROVIDER_ID = "cudem"
DOMAIN = "bathymetry"
_NCEI_IMAGESERVER_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/identify"
)
_NCEI_GETSAMPLES_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/getSamples"
)
_USER_AGENT = "weewx-clearskies-api-bathymetry/0.1 (NOAA CUDEM via NCEI ArcGIS)"

# 2 calls/sec courtesy limit. The NCEI ImageServer identify endpoint has no
# documented rate limit (it's a free, keyless government REST service), but
# it is a single-point query API (no batching — see module docstring), so a
# single profile download issues many sequential requests. 2 req/s is a
# conservative, courteous budget for a service with no published limit.
_rate_limiter = RateLimiter(
    name="ncei-cudem",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
    window_seconds=1,
)


def _acquire_rate_limit_slot() -> None:
    """Block until the local ``ncei-cudem`` rate-limit budget allows another call.

    ``RateLimiter.acquire()`` is non-blocking by design (ADR-038 §3): it
    raises ``QuotaExhausted`` immediately when the in-process budget is
    exhausted rather than sleeping, so that request-path callers can decide
    how to react. This module is different: because the NCEI ImageServer has
    no batch query shape, a single ``download_bathymetric_profile`` call
    issues dozens of sequential per-point requests, and without pacing here
    almost every download would immediately exceed the 2 req/s courtesy
    budget on its second or third request. This helper turns the local,
    self-imposed limiter into a blocking pacer by sleeping and retrying on
    ``QuotaExhausted`` — this is *not* a canonical ``ProviderHTTPClient``
    exception being re-wrapped (the hard constraint against that concerns
    exceptions raised at the HTTP boundary); ``QuotaExhausted`` here is
    raised by our own in-process ``RateLimiter`` *before* any HTTP request is
    made, purely to pace our own outbound call rate.
    """
    while True:
        try:
            _rate_limiter.acquire()
            return
        except QuotaExhausted as exc:
            time.sleep(max(0.1, float(exc.retry_after_seconds or 1)))


_http_client: ProviderHTTPClient | None = None


def _get_http_client() -> ProviderHTTPClient:
    """Return the module-level HTTP client singleton (constructed lazily)."""
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=_USER_AGENT,
        )
    return _http_client


def _reset_http_client_for_tests() -> None:
    """Reset the module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None


# ---------------------------------------------------------------------------
# Wire-shape validation (security-baseline §3.5 — validate at trust boundary)
# ---------------------------------------------------------------------------


class _NceiCatalogItemAttributes(BaseModel):
    """``catalogItems.features[].attributes`` — identifies the source CUDEM
    tile (e.g. ``"ncei19_n34x25_w078x00_2019v1"``), used only for logging/
    verification that a 1/9 arc-second CUDEM tile actually backed the reading."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, alias="Name")


class _NceiCatalogItemFeature(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attributes: _NceiCatalogItemAttributes | None = None


class _NceiCatalogItems(BaseModel):
    model_config = ConfigDict(extra="ignore")

    features: list[_NceiCatalogItemFeature] = Field(default_factory=list)


class _NceiIdentifyResponse(BaseModel):
    """NCEI ArcGIS ImageServer ``/identify`` response envelope.

    Example live response::

        {"value": "-3.30559", "objectId": 0, "name": "Pixel",
         "location": {...}, "properties": null,
         "catalogItems": {"features": [{"attributes":
             {"Name": "ncei19_n34x25_w078x00_2019v1"}}]}}

    ``value`` is a *string* representation of the elevation in meters
    (negative = underwater depth); it is also the sentinel string
    ``"NoData"`` for points outside CUDEM's data coverage.
    """

    model_config = ConfigDict(extra="ignore")

    value: str | None = None
    catalog_items: _NceiCatalogItems | None = Field(default=None, alias="catalogItems")


# ---------------------------------------------------------------------------
# Regional adaptations (deep-water thresholds + hardcoded fallback profiles)
# ---------------------------------------------------------------------------

REGION_PACIFIC = "pacific coast"
REGION_ATLANTIC = "atlantic coast"
REGION_GULF = "gulf coast"
REGION_HAWAII = "hawaii"
REGION_GREAT_LAKES = "great lakes"

# Deep-water thresholds (meters) — rough continental-shelf characteristics.
REGION_DEEP_WATER_THRESHOLDS_M: dict[str, float] = {
    REGION_PACIFIC: 50.0,  # steep continental shelf
    REGION_ATLANTIC: 35.0,
    REGION_GULF: 25.0,  # gradual shelf
    REGION_HAWAII: 60.0,  # volcanic shelf drops off fast
    REGION_GREAT_LAKES: 15.0,
}
_DEFAULT_DEEP_WATER_THRESHOLD_M = REGION_DEEP_WATER_THRESHOLDS_M[REGION_ATLANTIC]

# Hardcoded fallback depth profiles (meters), ordered offshore -> nearshore,
# used when CUDEM/NCEI is unavailable (network failure, quota, etc.).
FALLBACK_DEPTH_PROFILES_M: dict[str, list[float]] = {
    REGION_PACIFIC: [50.0, 40.0, 30.0, 20.0, 12.0, 6.0, 3.0],
    REGION_ATLANTIC: [35.0, 28.0, 22.0, 16.0, 10.0, 5.0, 2.5],
    REGION_GULF: [25.0, 20.0, 15.0, 12.0, 8.0, 4.0, 2.0],
    REGION_HAWAII: [60.0, 45.0, 30.0, 18.0, 10.0, 5.0, 3.5],
    # Not specified by the research brief; scaled from the Great Lakes
    # deep-water threshold (15m) using the same offshore->nearshore shape
    # as the other regions.
    REGION_GREAT_LAKES: [15.0, 12.0, 9.0, 6.0, 4.0, 2.0, 1.0],
}
# Distance spacing (meters) shared by all fallback profiles: offshore (20km)
# down to near-shore, with denser spacing close to shore (consistent with
# the adaptive-refinement "denser near shore" rule applied to live data).
FALLBACK_DISTANCES_M: list[float] = [20000.0, 15000.0, 10000.0, 6000.0, 3000.0, 1000.0, 300.0]


# Approximate bounding boxes, checked in this order (most specific first).
# Real coastline geography does not tile into non-overlapping rectangles;
# these are deliberately coarse per PROVIDER-MANUAL §14 "v1 marine providers"
# scope (US coastal regions only).
def classify_region(lat: float, lon: float) -> str:
    """Classify (lat, lon) into a coarse US coastal region.

    Returns one of ``REGION_PACIFIC``, ``REGION_ATLANTIC``, ``REGION_GULF``,
    ``REGION_HAWAII``, ``REGION_GREAT_LAKES``. Falls back to
    ``REGION_ATLANTIC`` (logged at WARNING) for coordinates outside all
    known bounding boxes — a safe default since the Atlantic profile sits
    in the middle of the other regions' deep-water thresholds.
    """
    if 18.0 <= lat <= 23.0 and -161.0 <= lon <= -154.0:
        return REGION_HAWAII
    if 41.0 <= lat <= 49.0 and -93.0 <= lon <= -76.0:
        return REGION_GREAT_LAKES
    if 32.0 <= lat <= 49.0 and -125.0 <= lon <= -117.0:
        return REGION_PACIFIC
    if 24.0 <= lat <= 31.0 and -98.0 <= lon <= -80.0:
        return REGION_GULF
    if 24.0 <= lat <= 45.0 and -82.0 <= lon <= -65.0:
        return REGION_ATLANTIC

    logger.warning(
        "classify_region: lat=%s, lon=%s did not match any known US coastal "
        "region bounding box; defaulting to %r.",
        lat,
        lon,
        REGION_ATLANTIC,
    )
    return REGION_ATLANTIC


def _fallback_profile(region: str) -> list[BathymetryPoint]:
    """Return the hardcoded fallback profile for *region*, logged at WARNING."""
    depths = FALLBACK_DEPTH_PROFILES_M.get(region, FALLBACK_DEPTH_PROFILES_M[REGION_ATLANTIC])
    logger.warning(
        "Bathymetry: CUDEM/NCEI unavailable for region=%r; "
        "using hardcoded fallback depth profile.",
        region,
    )
    return [
        BathymetryPoint({"distance_m": d, "depth_m": z})
        for d, z in zip(FALLBACK_DISTANCES_M, depths, strict=True)
    ]


# ---------------------------------------------------------------------------
# Geodesic helper
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0


def point_along_bearing(
    lat: float, lon: float, bearing_degrees: float, distance_m: float
) -> tuple[float, float]:
    """Return the (lat, lon) reached from (lat, lon) travelling *distance_m*
    meters along *bearing_degrees* (0 = north, 90 = east), using the
    standard spherical-earth destination-point formula.
    """
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    brng = math.radians(bearing_degrees)
    ang_dist = distance_m / _EARTH_RADIUS_M

    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist) + math.cos(lat1) * math.sin(ang_dist) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0


def _linspace(start: float, stop: float, num: int) -> list[float]:
    """Pure-python evenly-spaced sample points (inclusive of both ends)."""
    if num <= 1:
        return [start]
    step = (stop - start) / (num - 1)
    return [start + step * i for i in range(num)]


# ---------------------------------------------------------------------------
# Depth query (per-point NCEI ImageServer identify calls)
# ---------------------------------------------------------------------------


def _query_depths_m(points: list[tuple[float, float]]) -> list[float | None]:
    """Query depth (meters, non-negative) at each (lat, lon) in *points*.

    The NCEI ImageServer ``identify`` endpoint has no batch query shape
    (unlike the originally-planned OpenTopoData endpoint, which never
    actually existed) — one HTTP GET per point, paced by
    ``_acquire_rate_limit_slot()`` (2 req/s courtesy budget). Returns
    ``None`` for any point CUDEM has no coverage for (``value == "NoData"``)
    or where the identify call returned no ``value`` at all.

    Raises the canonical ``ProviderError`` taxonomy on any network/protocol
    failure — never re-wrapped, propagates as-is per project convention.
    Callers that want graceful fallback must catch ``ProviderError``
    themselves (see ``download_bathymetric_profile``).
    """
    client = _get_http_client()
    depths: list[float | None] = []

    for lat, lon in points:
        _acquire_rate_limit_slot()
        params = {
            "geometry": f"{lon:.6f},{lat:.6f}",
            "geometryType": "esriGeometryPoint",
            "returnGeometry": "false",
            "f": "json",
        }
        response = client.get(_NCEI_IMAGESERVER_URL, params=params)

        try:
            wire = _NceiIdentifyResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            logger.error(
                "NCEI ImageServer identify response validation failed for "
                "lat=%.6f,lon=%.6f: %s. Response body (first 2000 chars): %.2000s",
                lat,
                lon,
                exc,
                response.text,
            )
            raise ProviderProtocolError(
                f"NCEI ImageServer identify response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        raw_value = wire.value
        if raw_value is None or raw_value.strip().lower() == "nodata":
            depths.append(None)
            continue

        try:
            elevation_m = float(raw_value)
        except ValueError as exc:
            logger.error(
                "NCEI ImageServer identify returned non-numeric value %r for "
                "lat=%.6f,lon=%.6f",
                raw_value,
                lat,
                lon,
            )
            raise ProviderProtocolError(
                f"NCEI ImageServer identify returned non-numeric value {raw_value!r}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        # Negative elevation = underwater depth (MSL-relative). Positive
        # elevation (land) clamps to zero depth.
        depths.append(max(0.0, -elevation_m))

    return depths


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

_DEFAULT_SEARCH_STEP_KM = 1.0
_DEFAULT_MAX_SEARCH_KM = 75.0
_DEFAULT_PROFILE_POINTS = 16
_GRADIENT_REFINEMENT_THRESHOLD = 0.15  # m depth change per m horizontal distance
_MAX_REFINEMENT_ITERATIONS = 3


def download_bathymetric_profile(
    lat: float,
    lon: float,
    bearing_degrees: float,
    *,
    max_search_km: float = _DEFAULT_MAX_SEARCH_KM,
    search_step_km: float = _DEFAULT_SEARCH_STEP_KM,
    num_profile_points: int = _DEFAULT_PROFILE_POINTS,
) -> list[BathymetryPoint]:
    """Download a bathymetric depth profile for a surf/fishing spot.

    Algorithm (PROVIDER-MANUAL §14.7):
      1. Classify the region from (lat, lon) and look up its deep-water
         threshold.
      2. Search outward from (lat, lon) along ``bearing_degrees`` in
         ``search_step_km`` increments (default 1km) up to ``max_search_km``
         (default 75km) for the first point whose depth meets the
         threshold — this becomes the deep-water endpoint.
      3. Build a ``num_profile_points``-point (default 16) linearly
         interpolated transect between the break point (distance 0, the
         spot's own coordinates) and the deep-water endpoint.
      4. Adaptively refine: where the depth gradient between adjacent
         points exceeds ``_GRADIENT_REFINEMENT_THRESHOLD``, insert a
         midpoint sample. Up to ``_MAX_REFINEMENT_ITERATIONS`` rounds.
      5. Drop any missing readings and return the profile sorted by
         distance ascending.

    On any provider failure (network error, quota, protocol error), logs a
    WARNING and returns the hardcoded regional fallback profile instead of
    raising — this is a best-effort setup-time convenience, not a
    request-path operation that must surface errors to a caller.
    """
    region = classify_region(lat, lon)
    threshold_m = REGION_DEEP_WATER_THRESHOLDS_M.get(region, _DEFAULT_DEEP_WATER_THRESHOLD_M)

    try:
        return _download_bathymetric_profile_impl(
            lat,
            lon,
            bearing_degrees,
            region=region,
            threshold_m=threshold_m,
            max_search_km=max_search_km,
            search_step_km=search_step_km,
            num_profile_points=num_profile_points,
        )
    except ProviderError as exc:
        logger.warning(
            "Bathymetry: download_bathymetric_profile failed for "
            "lat=%s,lon=%s,bearing=%s (region=%r): %s. Using fallback profile.",
            lat,
            lon,
            bearing_degrees,
            region,
            exc,
        )
        return _fallback_profile(region)


def _download_bathymetric_profile_impl(
    lat: float,
    lon: float,
    bearing_degrees: float,
    *,
    region: str,
    threshold_m: float,
    max_search_km: float,
    search_step_km: float,
    num_profile_points: int,
) -> list[BathymetryPoint]:
    # --- Step 1/2: search outward for the deep-water endpoint ---
    num_steps = int(max_search_km / search_step_km)
    search_distances_m = [search_step_km * 1000.0 * i for i in range(1, num_steps + 1)]
    search_points = [point_along_bearing(lat, lon, bearing_degrees, d) for d in search_distances_m]
    search_depths = _query_depths_m(search_points)

    deep_water_distance_m = search_distances_m[-1]  # default: farthest searched point
    found_threshold = False
    for distance_m, depth_m in zip(search_distances_m, search_depths, strict=True):
        if depth_m is not None and depth_m >= threshold_m:
            deep_water_distance_m = distance_m
            found_threshold = True
            break

    if not found_threshold:
        logger.warning(
            "Bathymetry: no point reached deep-water threshold %.1fm within "
            "%.1fkm for lat=%s,lon=%s,bearing=%s (region=%r); using farthest "
            "searched point as the deep-water endpoint.",
            threshold_m,
            max_search_km,
            lat,
            lon,
            bearing_degrees,
            region,
        )

    # --- Step 3: initial linearly-interpolated transect ---
    distances_m = _linspace(0.0, deep_water_distance_m, num_profile_points)
    profile_points = [point_along_bearing(lat, lon, bearing_degrees, d) for d in distances_m]
    depths_m = _query_depths_m(profile_points)

    # --- Step 4: adaptive refinement ---
    for _iteration in range(_MAX_REFINEMENT_ITERATIONS):
        insertions: list[tuple[float, tuple[float, float]]] = []
        for i in range(len(distances_m) - 1):
            d0, d1 = depths_m[i], depths_m[i + 1]
            x0, x1 = distances_m[i], distances_m[i + 1]
            if d0 is None or d1 is None or x1 <= x0:
                continue
            gradient = abs(d1 - d0) / (x1 - x0)
            if gradient > _GRADIENT_REFINEMENT_THRESHOLD:
                mid_distance = (x0 + x1) / 2.0
                insertions.append(
                    (mid_distance, point_along_bearing(lat, lon, bearing_degrees, mid_distance))
                )

        if not insertions:
            break

        new_depths = _query_depths_m([pt for _, pt in insertions])
        for (mid_distance, _pt), mid_depth in zip(insertions, new_depths, strict=True):
            distances_m.append(mid_distance)
            depths_m.append(mid_depth)

        # Re-sort by distance ascending after each refinement round.
        paired = sorted(zip(distances_m, depths_m, strict=True), key=lambda p: p[0])
        distances_m = [p[0] for p in paired]
        depths_m = [p[1] for p in paired]

    # --- Step 5: drop any still-missing readings ---
    clean_pairs = [(d, z) for d, z in zip(distances_m, depths_m, strict=True) if z is not None]
    clean_pairs.sort(key=lambda p: p[0])

    return [
        BathymetryPoint({"distance_m": d, "depth_m": z})
        for d, z in clean_pairs
    ]


# ---------------------------------------------------------------------------
# Bidirectional runtime profile (SWAN bidirectional transect)
# ---------------------------------------------------------------------------

_BIDIR_PROFILE_STEP_M = 50.0  # interval between profile sample points


def _try_opendap_bidirectional(
    lat: float,
    lon: float,
    bearing_degrees: float,
    target_deep_m: float,
    max_search_m: float,
    step_m: float,
) -> dict[str, Any] | None:
    """Try to build a bidirectional profile from OPeNDAP regional DEM data.

    Returns a profile dict on success, None if no regional DEM covers the spot
    or on any OPeNDAP failure (caller falls back to CRM point queries).
    """
    try:
        from weewx_clearskies_api.services.bathymetry_resolver import (
            find_best_dem,
            fetch_opendap_grid,
        )
    except ImportError:
        return None

    # Build a small bbox around the spot covering max_search_m in all directions
    deg_per_m = 1.0 / 111_320.0
    margin_deg = max_search_m * deg_per_m * 1.5
    spot_bbox = (lon - margin_deg, lat - margin_deg, lon + margin_deg, lat + margin_deg)

    dem = find_best_dem(spot_bbox)
    if dem is None:
        return None

    try:
        grid = fetch_opendap_grid(
            dem["filename"], spot_bbox, step_m, dem["elevation_var"]
        )
        grid["vertical_datum"] = dem["vertical_datum"]
    except Exception:
        logger.warning(
            "OPeNDAP bidirectional profile failed for (%.4f, %.4f); "
            "falling back to CRM point queries",
            lat, lon, exc_info=True,
        )
        return None

    # Extract 1D profile along the bearing from the 2D grid
    depths_2d = grid["depths"]
    nj = grid["nj"]
    ni = grid["ni"]
    lat_first = grid["lat_first"]
    lon_first = grid["lon_first"]
    lat_last = grid["lat_last"]
    lon_last = grid["lon_last"]

    if nj < 2 or ni < 2 or not depths_2d:
        return None

    shoreward_bearing = (bearing_degrees + 180.0) % 360.0

    def _interp_depth(plat: float, plon: float) -> float | None:
        """Bilinear interpolation from the 2D grid."""
        fj = (plat - lat_first) / (lat_last - lat_first) * (nj - 1)
        fi = (plon - lon_first) / (lon_last - lon_first) * (ni - 1)
        if fj < 0 or fj >= nj - 1 or fi < 0 or fi >= ni - 1:
            return None
        j0 = int(fj)
        i0 = int(fi)
        dj = fj - j0
        di = fi - i0
        v00 = depths_2d[j0][i0]
        v01 = depths_2d[j0][i0 + 1]
        v10 = depths_2d[j0 + 1][i0]
        v11 = depths_2d[j0 + 1][i0 + 1]
        val = v00 * (1 - dj) * (1 - di) + v01 * (1 - dj) * di + v10 * dj * (1 - di) + v11 * dj * di
        return float(val)

    # Step 1: find coastline (walk shoreward from pin until depth crosses 0)
    pin_elev = _interp_depth(lat, lon)
    coastline_lat = lat
    coastline_lon = lon

    if pin_elev is None or pin_elev >= 0.0:
        pass  # pin is on land or outside grid — treat as shore
    else:
        dist = step_m
        while dist <= max_search_m:
            pt_lat, pt_lon = point_along_bearing(lat, lon, shoreward_bearing, dist)
            elev = _interp_depth(pt_lat, pt_lon)
            if elev is not None and elev >= 0.0:
                coastline_lat = pt_lat
                coastline_lon = pt_lon
                break
            dist += step_m

    # Step 2: find deep endpoint (walk offshore from coastline)
    deep_end_dist_m = max_search_m
    dist = step_m
    while dist <= max_search_m:
        pt_lat, pt_lon = point_along_bearing(
            coastline_lat, coastline_lon, bearing_degrees, dist
        )
        elev = _interp_depth(pt_lat, pt_lon)
        if elev is not None and -elev >= target_deep_m:
            deep_end_dist_m = dist
            break
        dist += step_m

    # Step 3: sample at ~50m intervals
    num_pts = max(2, int(round(deep_end_dist_m / _BIDIR_PROFILE_STEP_M)) + 1)
    distances_m = _linspace(0.0, deep_end_dist_m, num_pts)

    profile: list[dict[str, float]] = []
    for d in distances_m:
        pt_lat, pt_lon = point_along_bearing(
            coastline_lat, coastline_lon, bearing_degrees, d
        )
        elev = _interp_depth(pt_lat, pt_lon)
        if elev is not None:
            depth_m = max(0.0, -elev)  # CUDEM convention: neg = ocean
            profile.append({
                "distance_m": round(d, 1),
                "depth_m": round(depth_m, 2),
            })

    if len(profile) < 2:
        logger.warning(
            "OPeNDAP bidirectional profile: only %d valid points; "
            "falling back to CRM", len(profile),
        )
        return None

    unique_depths = len(set(p["depth_m"] for p in profile))
    logger.info(
        "OPeNDAP bidirectional profile: %d points, %d unique depths, "
        "source=%s (%.0fm), coastline=(%.4f, %.4f)",
        len(profile), unique_depths, dem["filename"],
        dem["resolution_m"], coastline_lat, coastline_lon,
    )

    return {
        "coastline_lat": round(coastline_lat, 6),
        "coastline_lon": round(coastline_lon, 6),
        "profile": profile,
        "source": "ncei_regional",
    }


def download_bidirectional_profile(
    lat: float,
    lon: float,
    bearing_degrees: float,
    *,
    target_deep_m: float = 15.0,
    target_shallow_m: float = 0.5,  # reserved; not used in search
    step_m: float = 10.0,
    max_search_m: float = 5000.0,
) -> dict[str, Any]:
    """Download a bidirectional CUDEM depth profile for a surf spot at runtime.

    Unlike ``download_bathymetric_profile`` (which goes only offshore from the
    pin), this function builds a profile that spans from the actual coastline
    (depth ≈ 0 m) to deep water (~``target_deep_m``).  The operator's pin is
    treated as a general indicator; the true shoreline is located by sampling
    SHOREWARD from the pin in ``step_m`` increments until the CUDEM elevation
    crosses MSL (depth == 0).

    Algorithm:
      1. **Find the coastline.** Query depth at the pin.  If it is already 0
         (land/MSL) or has no CUDEM coverage, the pin is treated as the
         shoreline.  Otherwise, walk shoreward at ``step_m`` intervals until
         depth == 0 or ``max_search_m`` is exhausted.
      2. **Find the deep endpoint.** From the coastline, walk offshore at
         ``step_m`` intervals until depth ≥ ``target_deep_m`` or
         ``max_search_m`` is exhausted.
      3. **Sample the profile.** Query CUDEM at ``~50 m`` intervals from the
         coastline to the deep endpoint and return.

    All NCEI ImageServer queries honour the 2 req/s courtesy rate limit via
    ``_acquire_rate_limit_slot()``.  On any ``ProviderError``, re-raises —
    the caller is responsible for wrapping in a try/except and falling back
    to the wizard-configured profile.

    Args:
        lat: Latitude of the surf spot pin (degrees).
        lon: Longitude of the surf spot pin (degrees).
        bearing_degrees: Compass bearing pointing FROM shore TOWARD the ocean
            (the offshore direction, 0 = north, 90 = east).
        target_deep_m: Depth threshold for the offshore search endpoint (m).
        target_shallow_m: Reserved for future profile trimming; not used here.
        step_m: Step size (m) for the shoreward and offshore search walks.
        max_search_m: Maximum search distance in each direction (m).

    Returns:
        dict with keys:
          ``"coastline_lat"`` (float): latitude of the located shoreline.
          ``"coastline_lon"`` (float): longitude of the located shoreline.
          ``"profile"`` (list[dict]): each entry has ``"distance_m"`` (float,
              from the coastline, increasing offshore) and ``"depth_m"``
              (float, non-negative, positive = below MSL).  Sorted by
              ``distance_m`` ascending.
    """
    shoreward_bearing = (bearing_degrees + 180.0) % 360.0

    # ---- Try OPeNDAP path first (Phase 20, T20.4) ----
    # If a regional DEM covers the spot, extract the 1D profile from a 2D
    # OPeNDAP strip instead of making ~48 individual CRM point queries.
    opendap_profile = _try_opendap_bidirectional(
        lat, lon, bearing_degrees, target_deep_m, max_search_m, step_m,
    )
    if opendap_profile is not None:
        return opendap_profile

    # ---- Fallback: CRM single-point queries ----

    # ---- Step 1: find the coastline ----
    pin_depth = _query_depths_m([(lat, lon)])[0]

    coastline_lat = lat
    coastline_lon = lon

    if pin_depth is None or pin_depth <= 0.0:
        # Pin is on land (depth <= 0) or outside CUDEM coverage — treat as shore.
        logger.debug(
            "Bidirectional profile: pin at lat=%.6f,lon=%.6f depth=%s "
            "(land/no-coverage); treating pin as coastline.",
            lat, lon, pin_depth,
        )
    else:
        found_coast = False
        dist = step_m
        while dist <= max_search_m:
            pt_lat, pt_lon = point_along_bearing(lat, lon, shoreward_bearing, dist)
            depth = _query_depths_m([(pt_lat, pt_lon)])[0]
            if depth is not None and depth <= 0.0:
                coastline_lat = pt_lat
                coastline_lon = pt_lon
                found_coast = True
                logger.debug(
                    "Bidirectional profile: coastline at %.0f m shoreward of pin "
                    "(lat=%.6f, lon=%.6f).",
                    dist, coastline_lat, coastline_lon,
                )
                break
            dist += step_m

        if not found_coast:
            logger.warning(
                "Bidirectional profile: no land (depth==0) found within %.0f m "
                "shoreward of lat=%.6f,lon=%.6f (shoreward bearing=%.1f°); "
                "using pin as coastline.",
                max_search_m, lat, lon, shoreward_bearing,
            )

    # ---- Step 2: find the deep-water endpoint from the coastline ----
    deep_end_dist_m = max_search_m
    found_deep = False
    dist = step_m
    while dist <= max_search_m:
        pt_lat, pt_lon = point_along_bearing(
            coastline_lat, coastline_lon, bearing_degrees, dist
        )
        depth = _query_depths_m([(pt_lat, pt_lon)])[0]
        if depth is not None and depth >= target_deep_m:
            deep_end_dist_m = dist
            found_deep = True
            logger.debug(
                "Bidirectional profile: %.1f m depth reached at %.0f m offshore "
                "of coastline.",
                target_deep_m, dist,
            )
            break
        dist += step_m

    if not found_deep:
        logger.warning(
            "Bidirectional profile: depth %.1f m not reached within %.0f m "
            "offshore of coastline (lat=%.6f,lon=%.6f); using farthest "
            "searched point (%.0f m) as the deep endpoint.",
            target_deep_m, max_search_m,
            coastline_lat, coastline_lon, deep_end_dist_m,
        )

    # ---- Step 3: sample the profile at ~50 m intervals ----
    num_pts = max(2, int(round(deep_end_dist_m / _BIDIR_PROFILE_STEP_M)) + 1)
    distances_m = _linspace(0.0, deep_end_dist_m, num_pts)
    profile_pts = [
        point_along_bearing(coastline_lat, coastline_lon, bearing_degrees, d)
        for d in distances_m
    ]
    raw_depths = _query_depths_m(profile_pts)

    # Drop None readings, smooth outliers.
    clean_pairs = [
        (d, z)
        for d, z in zip(distances_m, raw_depths, strict=True)
        if z is not None
    ]
    clean_pairs.sort(key=lambda p: p[0])

    profile = [
        {"distance_m": round(d, 1), "depth_m": round(z, 2)}
        for d, z in clean_pairs
    ]

    return {
        "coastline_lat": round(coastline_lat, 6),
        "coastline_lon": round(coastline_lon, 6),
        "profile": profile,
        "source": "crm_point_query",
    }


# ---------------------------------------------------------------------------
# PCHIP variable-resolution profile generation (Phase 4A, T4A.2;
# MARINE-SERVICE-SEPARATION-PLAN.md §T4A.2; SURF-ZONE-MODEL-BRIEF §6.1).
#
# Problem this fixes: ``download_bidirectional_profile`` samples at a fixed
# ``_BIDIR_PROFILE_STEP_M`` (50m) interval regardless of the underlying
# native DEM resolution.  At 50m dx the Battjes-Janssen breaking dissipation
# in ``services/surf_1d_analytical.py`` over-attenuates wave energy and
# SwellTrack finds zero break points.  ``interpolate_profile_pchip`` takes a
# raw depth transect (ideally sampled at native CUDEM resolution — T4A.2
# step 3, wired by T4A.3) and produces a depth-zone variable-resolution grid:
# 1-2m dx through the breaking/structure zone, 3-5m dx through the shoaling
# zone, and native resolution beyond ~15m depth where wave transformation is
# minimal.
# ---------------------------------------------------------------------------

# De-duplication tolerance for near-duplicate ``distance_m`` values that the
# adaptive-refinement path above can emit (T4A.2 step 2). Comfortably below
# the coarsest legitimate native DEM spacing (~10m, see
# ``_NATIVE_SPACING_RAISE_THRESHOLD_M`` below) so genuine distinct samples
# are never merged.
_DEDUP_TOLERANCE_M = 0.5

# Minimum raw profile points required to fit a meaningful variable-resolution
# grid. Below this the profile is too sparse to define fine/shoaling/approach
# zones at all.
_MIN_RAW_PROFILE_POINTS = 4

# LC-4 (P4A-SWELLTRACK-PHYSICS-BRIEF round brief): plausible native DEM
# resolution for the per-spot FINE-zone raw profile this function consumes.
# Sourced from PROVIDER-MANUAL §14.7's data-source priority chain and this
# module's own docstring:
#   - NCEI regional coastal DEMs (priority 2): ~10m (1/3 arc-second)
#   - USGS Great Lakes DEMs (priority 3): ~3-5m
#   - This module's own CUDEM identify endpoint: 1/9 arc-second, ~3.4m
#   - CRM/DEM_all (priority 4, ~90m/3 arc-second) is NOT a legitimate source
#     for this function's input: plan T4A.3 step 6/8 (the FINE download that
#     feeds per-spot raw profiles) targets ~3-10m only; CRM is reserved for
#     coarse L1 shelf-scale sizing, never the per-spot fine-zone profile.
# Coordinator's live data check (c:\tmp\marine-sep-P4A-scratch.md, "DEM
# coverage at HB pier", 2026-07-24) confirms HB's actual finest available
# tile is 1/3 arc-second = 0.3333" ~ 10.28m (NOT exactly 10.0m) — 1/9
# arc-second does not exist south of 36°N on the Pacific coast. The clean
# threshold must accept genuine 10.3m native data, not just <=10.0m exactly,
# or this function would WARN on the real HB spot's correct, legitimate
# native resolution every time. 11m gives a small margin above the documented
# 10.28m worst-legitimate-case; 15m (unchanged) still unambiguously catches
# both the current 49.8m production bug and any accidental CRM-native
# (~90m) profile misrouted here.
_NATIVE_SPACING_WARN_THRESHOLD_M = 11.0
_NATIVE_SPACING_RAISE_THRESHOLD_M = 15.0

# Shoaling amplification margin (LC-1, P4A-SWELLTRACK-PHYSICS-BRIEF): the
# plan's own worked examples and Accept bullet use max(), not +; a 4m
# offshore swell shoals to ~7.1m before breaking, not 5.5m + a separate
# structure-depth addend.
_FINE_ZONE_SHOALING_MARGIN = 1.3

# Per-zone target dx (T4A.2 step 1 / API-MANUAL "1D grid resolution" table).
_FINE_ZONE_DX_M = 1.5  # within the 1-2m fine-zone spec
_SHOALING_ZONE_DX_M = 4.0  # within the 3-5m shoaling-zone spec
_SHOALING_ZONE_MAX_DEPTH_M = 15.0  # shoaling zone upper bound; beyond is "approach"


def compute_fine_zone_max_depth(
    max_hs_m: float, gamma: float, structure_zone_depth: float = 0.0
) -> float:
    """Companion helper (LC-3): the fine-zone depth threshold, exposed
    separately from ``interpolate_profile_pchip``'s fixed signature so T4A.3
    can persist it (per-spot profile cache metadata) without this function
    needing to return anything beyond the point list.

    ``fine_zone_max_depth = max(1.3 * max_hs_m / gamma, structure_zone_depth)``
    — LC-1: the two terms are independently-derived depths for the same
    zone (max breaking depth with shoaling margin vs. structure interaction
    depth), not additive contributions. See MARINE-SERVICE-SEPARATION-PLAN.md
    §T4A.2 step 4 and its two worked examples (Newport:
    ``max(7.1, 10.0) = 10.0``, not 17.1).
    """
    if max_hs_m <= 0.0:
        raise ValueError(f"max_hs_m must be positive, got {max_hs_m!r}")
    if gamma <= 0.0:
        raise ValueError(f"gamma must be positive, got {gamma!r}")
    if structure_zone_depth < 0.0:
        raise ValueError(
            f"structure_zone_depth must be non-negative, got {structure_zone_depth!r}"
        )
    return max(_FINE_ZONE_SHOALING_MARGIN * max_hs_m / gamma, structure_zone_depth)


def _dedup_profile_by_distance(
    distances: list[float], depths: list[float], tolerance_m: float
) -> tuple[list[float], list[float]]:
    """Merge near-duplicate ``distance_m`` values within *tolerance_m*.

    Keeps the shallower depth of the pair; ties keep the first-encountered
    (lower-distance) occurrence — T4A.2 step 2's "keep the shallower/first
    occurrence deterministically" rule. Input need not be pre-sorted; output
    is sorted ascending by distance.
    """
    paired = sorted(zip(distances, depths, strict=True), key=lambda p: p[0])
    out: list[tuple[float, float]] = []
    for d, z in paired:
        if out and (d - out[-1][0]) < tolerance_m:
            if z < out[-1][1]:
                out[-1] = (d, z)
            # else: tie or new point deeper — keep the first-encountered pair.
        else:
            out.append((d, z))
    return [p[0] for p in out], [p[1] for p in out]


def _dedup_sorted_floats(values: list[float], tolerance_m: float) -> list[float]:
    """Drop near-duplicate values (within *tolerance_m*) from a sorted-ascending
    build-up of grid boundary points (zone-boundary coincidences), keeping the
    first occurrence."""
    out: list[float] = []
    for v in sorted(values):
        if out and (v - out[-1]) < tolerance_m:
            continue
        out.append(v)
    return out


def _find_depth_crossing_distance(
    distances: list[float], depths: list[float], target_depth_m: float
) -> float:
    """Walk shore->offshore (ascending distance) and return the (linearly
    interpolated) distance at which *depths* first reaches *target_depth_m*.

    Same "first point that meets the threshold" technique already used by
    ``download_bathymetric_profile``'s deep-water search (DRY — rules/coding.md
    §3) — a soft guide for zone-grid density, not itself a physical result, so
    local sandbar/trough non-monotonicity is an acceptable simplification.
    Clamps to the profile's own extent when the target is never reached
    (too-large fine zone) or already exceeded at the first point.
    """
    if depths[0] >= target_depth_m:
        return distances[0]
    for i in range(1, len(distances)):
        if depths[i] >= target_depth_m:
            d0, d1 = depths[i - 1], depths[i]
            x0, x1 = distances[i - 1], distances[i]
            if d1 == d0:
                return x1
            frac = (target_depth_m - d0) / (d1 - d0)
            return x0 + frac * (x1 - x0)
    return distances[-1]


def interpolate_profile_pchip(
    raw_profile: list[dict[str, float]],
    max_hs_m: float,
    gamma: float,
    structure_zone_depth: float = 0.0,
) -> list[dict[str, float]]:
    """Interpolate a raw CUDEM depth transect to a depth-zone variable-
    resolution grid using PCHIP (Piecewise Cubic Hermite Interpolating
    Polynomial).

    Pure function (LC-3): raw profile in, variable-resolution profile out.
    Does NOT download, does NOT write files, does NOT read config — T4A.3's
    caller owns I/O; this function is designed to lift into the marine
    service unchanged at Phase 5.

    Args:
        raw_profile: list of ``{"distance_m": float, "depth_m": float}``,
            distance measured from the coastline (0) increasing offshore.
            **Input contract (T4A.2 step 3):** must be sampled at native
            CUDEM DEM resolution (~3-10m) — NOT the 50m
            ``download_bidirectional_profile`` stepper output, and NOT a
            resampled/cached L3 grid. Wiring the native-resolution
            extraction is T4A.3's job; this function validates the spacing
            it's actually given (see Raises).
        max_hs_m: spot's configured maximum expected significant wave height
            (m). Must be positive.
        gamma: breaking parameter (H/d at breaking, e.g. 0.73).
        structure_zone_depth: deepest configured structure's depth + margin
            for this spot, or 0.0 when no structures are configured. Must be
            non-negative. Required (no silently-extending default beyond
            0.0) — LC-3 / T4A.2 step 6.

    Returns:
        Variable-resolution profile: list of ``{"distance_m": float,
        "depth_m": float}`` dicts (same keys ``download_bidirectional_profile``
        already uses — the on-disk spot-profile cache format is unchanged),
        sorted ascending by distance_m:
          - Fine zone (0 -> ``compute_fine_zone_max_depth(...)``): ~1.5m dx.
          - Shoaling zone (fine max -> ~15m depth): ~4m dx.
          - Approach zone (>~15m depth): the raw profile's own native points,
            unmodified — no benefit from finer interpolation there.

    Raises:
        ValueError: fewer than 4 raw points (before or after de-duplication),
            non-positive ``max_hs_m``/``gamma``, negative
            ``structure_zone_depth``, a negative ``depth_m`` in the raw
            profile, non-monotonic ``distance_m`` remaining after
            de-duplication and sorting (PCHIP requires strictly increasing
            x — never silently caught, per LC-3: "a silent try/except around
            it is a defect, not a fix"), or a raw profile whose median native
            spacing exceeds ``_NATIVE_SPACING_RAISE_THRESHOLD_M`` (15m) — LC-4:
            there is no legitimate caller that should feed a profile coarser
            than plausible native DEM resolution into this function. Logs a
            WARNING (does not raise) for the grey band between 10m and 15m.
    """
    if len(raw_profile) < _MIN_RAW_PROFILE_POINTS:
        raise ValueError(
            f"interpolate_profile_pchip requires at least {_MIN_RAW_PROFILE_POINTS} "
            f"raw profile points, got {len(raw_profile)}"
        )
    if max_hs_m <= 0.0:
        raise ValueError(f"max_hs_m must be positive, got {max_hs_m!r}")
    if gamma <= 0.0:
        raise ValueError(f"gamma must be positive, got {gamma!r}")
    if structure_zone_depth < 0.0:
        raise ValueError(
            f"structure_zone_depth must be non-negative, got {structure_zone_depth!r}"
        )

    raw_distances = [float(p["distance_m"]) for p in raw_profile]
    raw_depths = [float(p["depth_m"]) for p in raw_profile]
    if any(z < 0.0 for z in raw_depths):
        raise ValueError("interpolate_profile_pchip: raw profile contains negative depth_m")

    distances, depths = _dedup_profile_by_distance(raw_distances, raw_depths, _DEDUP_TOLERANCE_M)

    if len(distances) < _MIN_RAW_PROFILE_POINTS:
        raise ValueError(
            f"interpolate_profile_pchip: only {len(distances)} distinct distance_m "
            f"values remain after de-duplication (need >= {_MIN_RAW_PROFILE_POINTS}); "
            "raw profile is degenerate."
        )
    for i in range(1, len(distances)):
        if distances[i] <= distances[i - 1]:
            raise ValueError(
                "interpolate_profile_pchip: non-monotonic distance_m after "
                f"de-duplication and sorting (index {i}: "
                f"{distances[i - 1]} -> {distances[i]}); PCHIP requires "
                "strictly increasing x."
            )

    # LC-4: validate the raw profile actually looks like native-DEM-resolution
    # data, not a resampled/stepped substitute.
    diffs = sorted(distances[i] - distances[i - 1] for i in range(1, len(distances)))
    median_dx = diffs[len(diffs) // 2]
    if median_dx > _NATIVE_SPACING_RAISE_THRESHOLD_M:
        raise ValueError(
            f"interpolate_profile_pchip: raw profile median native spacing "
            f"{median_dx:.1f}m exceeds the plausible native CUDEM/DEM "
            f"resolution ({_NATIVE_SPACING_RAISE_THRESHOLD_M:.0f}m — "
            "PROVIDER-MANUAL §14.7). This function's input contract (T4A.2 "
            "step 3) requires a profile sampled at native DEM resolution "
            "(~3-10m); a 50m-stepped `download_bidirectional_profile` "
            "output (or an accidental CRM-native ~90m profile) is a known "
            "contract violation that T4A.3 fixes by extracting raw profiles "
            "from the FINE CUDEM download instead. Refusing to interpolate "
            "garbage."
        )
    if median_dx > _NATIVE_SPACING_WARN_THRESHOLD_M:
        logger.warning(
            "interpolate_profile_pchip: raw profile median native spacing is "
            "%.1fm — coarser than the finest documented native CUDEM "
            "resolution (~10m, PROVIDER-MANUAL §14.7) but within the "
            "%.0fm raise threshold. Proceeding, but this profile may not "
            "have been extracted at true native DEM resolution (T4A.2 step 3).",
            median_dx,
            _NATIVE_SPACING_RAISE_THRESHOLD_M,
        )

    fine_zone_max_depth = compute_fine_zone_max_depth(max_hs_m, gamma, structure_zone_depth)

    shore_distance = distances[0]
    dist_fine = _find_depth_crossing_distance(distances, depths, fine_zone_max_depth)
    dist_shoal = _find_depth_crossing_distance(distances, depths, _SHOALING_ZONE_MAX_DEPTH_M)
    dist_shoal = max(dist_shoal, dist_fine)

    grid_distances: list[float] = []

    # Fine zone: shore -> dist_fine, dx ~= _FINE_ZONE_DX_M (<= 2m spec).
    n_fine = max(2, round((dist_fine - shore_distance) / _FINE_ZONE_DX_M) + 1)
    grid_distances.extend(_linspace(shore_distance, dist_fine, n_fine))

    # Shoaling zone: dist_fine -> dist_shoal, dx ~= _SHOALING_ZONE_DX_M (3-5m spec).
    if dist_shoal > dist_fine:
        n_shoal = max(2, round((dist_shoal - dist_fine) / _SHOALING_ZONE_DX_M) + 1)
        grid_distances.extend(_linspace(dist_fine, dist_shoal, n_shoal)[1:])

    # Approach zone: the raw profile's own native points beyond dist_shoal —
    # no interpolation added (T4A.2: "no benefit from finer grid" there).
    grid_distances.extend(d for d in distances if d > dist_shoal)

    grid_distances = _dedup_sorted_floats(grid_distances, _DEDUP_TOLERANCE_M)

    pchip = PchipInterpolator(np.array(distances), np.array(depths))
    min_x, max_x = distances[0], distances[-1]
    clipped = np.array([min(max(d, min_x), max_x) for d in grid_distances])
    grid_depths = pchip(clipped)

    return [
        {"distance_m": float(round(d, 3)), "depth_m": float(round(max(z, 0.0), 3))}
        for d, z in zip(grid_distances, grid_depths, strict=True)
    ]


# ---------------------------------------------------------------------------
# Downloaded-grid contour search + native-resolution profile extraction
# (Phase 4A, T4A.3; MARINE-SERVICE-SEPARATION-PLAN.md §T4A.3 Do steps 3-4,
# 7-8; lead calls LC-10, LC-11).
#
# These functions operate on an already-downloaded 2-D bathymetry grid dict
# — the same ``{"lat_first", "lon_first", "lat_last", "lon_last", "ni", "nj",
# "depths"}`` shape produced by ``bathymetry_resolver.fetch_opendap_grid()``/
# ``fetch_great_lake_grid()``/``get_operator_grid()`` and by
# ``download_swan_depth_grid()``.  They do NOT make any network request —
# the apply-time chain (``services/marine_setup.py``) downloads the COARSE/
# MEDIUM/FINE grids first, then calls these to locate depth contours and
# extract raw transects from that already-downloaded data.  Per-spot bearing
# (never an averaged bearing — LC-10) so a single transect through a
# submarine canyon cannot mislocate the contour for every other spot.
# ---------------------------------------------------------------------------


def _bilinear_grid_depth(grid: dict[str, Any], lat: float, lon: float) -> float | None:
    """Bilinearly interpolate depth at *(lat, lon)* from a downloaded 2-D grid.

    Returns ``None`` when the point falls outside the grid's coverage.
    Sign convention matches the grid's own (CUDEM: negative = ocean); this
    helper returns the RAW cell value(s), not a depth-in-meters conversion —
    callers that need non-negative "depth below MSL" must negate/clamp
    themselves (see ``_grid_depth_below_msl`` below).
    """
    lat_first = grid["lat_first"]
    lon_first = grid["lon_first"]
    lat_last = grid["lat_last"]
    lon_last = grid["lon_last"]
    nj = grid["nj"]
    ni = grid["ni"]
    depths_2d = grid["depths"]

    if nj < 2 or ni < 2 or not depths_2d:
        return None
    if lat_last == lat_first or lon_last == lon_first:
        return None

    fj = (lat - lat_first) / (lat_last - lat_first) * (nj - 1)
    fi = (lon - lon_first) / (lon_last - lon_first) * (ni - 1)
    if fj < 0 or fj >= nj - 1 or fi < 0 or fi >= ni - 1:
        return None

    j0 = int(fj)
    i0 = int(fi)
    dj = fj - j0
    di = fi - i0
    v00 = depths_2d[j0][i0]
    v01 = depths_2d[j0][i0 + 1]
    v10 = depths_2d[j0 + 1][i0]
    v11 = depths_2d[j0 + 1][i0 + 1]
    return float(
        v00 * (1 - dj) * (1 - di)
        + v01 * (1 - dj) * di
        + v10 * dj * (1 - di)
        + v11 * dj * di
    )


def _grid_depth_below_msl(grid: dict[str, Any], lat: float, lon: float) -> float | None:
    """Non-negative depth below MSL at *(lat, lon)*, or ``None`` off-grid.

    CUDEM/regional-DEM convention: negative elevation = underwater.  Positive
    (land) clamps to 0.0, matching ``_query_depths_m``'s convention elsewhere
    in this module.
    """
    elev = _bilinear_grid_depth(grid, lat, lon)
    if elev is None:
        return None
    return max(0.0, -elev)


def find_shoreline_from_grid(
    grid: dict[str, Any],
    pin_lat: float,
    pin_lon: float,
    bearing_degrees: float,
    *,
    step_m: float = 10.0,
    max_search_m: float = 2000.0,
) -> tuple[float, float]:
    """Locate the coastline (depth crossing 0) shoreward of *(pin_lat, pin_lon)*.

    Walks shoreward (``bearing_degrees + 180``) at ``step_m`` intervals through
    the downloaded *grid*.  If the pin itself is already on land / outside
    grid coverage, the pin is treated as the shoreline.  If no crossing is
    found within ``max_search_m``, logs a WARNING and returns the pin
    unchanged — this is a best-effort geometric anchor, not itself a depth
    contour subject to LC-10's "no fallback" rule (that rule applies to the
    30 m/15 m sizing contours in ``find_depth_contour_distance``, not to
    shoreline location).

    Returns:
        ``(coastline_lat, coastline_lon)``.
    """
    shoreward_bearing = (bearing_degrees + 180.0) % 360.0
    pin_depth = _grid_depth_below_msl(grid, pin_lat, pin_lon)

    if pin_depth is None or pin_depth <= 0.0:
        return pin_lat, pin_lon

    dist = step_m
    while dist <= max_search_m:
        pt_lat, pt_lon = point_along_bearing(pin_lat, pin_lon, shoreward_bearing, dist)
        depth = _grid_depth_below_msl(grid, pt_lat, pt_lon)
        if depth is not None and depth <= 0.0:
            return pt_lat, pt_lon
        dist += step_m

    logger.warning(
        "find_shoreline_from_grid: no land found within %.0f m shoreward of "
        "pin (%.6f, %.6f) at bearing=%.1f°; using pin as coastline.",
        max_search_m, pin_lat, pin_lon, shoreward_bearing,
    )
    return pin_lat, pin_lon


def find_depth_contour_distance(
    grid: dict[str, Any],
    coastline_lat: float,
    coastline_lon: float,
    bearing_degrees: float,
    target_depth_m: float,
    *,
    spot_id: str = "",
    step_m: float = 50.0,
    max_search_m: float = 60_000.0,
) -> float:
    """Distance (m) from the coastline to *target_depth_m* along *bearing_degrees*.

    Walks OFFSHORE from ``(coastline_lat, coastline_lon)`` through the
    downloaded *grid*, sampling every ``step_m``, and linearly interpolates
    the distance at which depth first reaches ``target_depth_m``.

    Per LC-10: a contour not found within ``max_search_m`` (either because
    the grid coverage ends first, or the target depth is never reached) is
    an ERROR, not a fallback — callers must NOT substitute a hardcoded
    distance.  Raises ``ValueError`` naming the spot, bearing, target depth,
    and the maximum depth actually reached so the operator can diagnose
    (grid too small, wrong DEM, target depth unreasonable for this coast).

    Args:
        grid: Downloaded 2-D bathymetry grid (COARSE for the 30 m contour,
            MEDIUM for the 15 m contour — Do steps 3-4/6).
        coastline_lat, coastline_lon: Shoreline anchor (from
            ``find_shoreline_from_grid`` or the spot's own pin).
        bearing_degrees: THIS SPOT's own offshore bearing — never an
            averaged bearing across spots (LC-10).
        target_depth_m: Target contour depth (30.0 for L2, 15.0 for L3).
        spot_id: Included in the raised error message for diagnostics.
        step_m: Search step size (m).
        max_search_m: Maximum search distance (m) before raising.

    Returns:
        Distance in meters from the coastline to the target depth contour.

    Raises:
        ValueError: The contour was not reached within ``max_search_m``.
    """
    max_depth_reached = 0.0
    dist = step_m
    while dist <= max_search_m:
        pt_lat, pt_lon = point_along_bearing(
            coastline_lat, coastline_lon, bearing_degrees, dist
        )
        depth = _grid_depth_below_msl(grid, pt_lat, pt_lon)
        if depth is not None:
            if depth >= target_depth_m:
                # Linear back-interpolation between this sample and the
                # previous one for a smoother distance estimate.
                prev_dist = dist - step_m
                prev_lat, prev_lon = point_along_bearing(
                    coastline_lat, coastline_lon, bearing_degrees, max(prev_dist, 0.0)
                )
                prev_depth = _grid_depth_below_msl(grid, prev_lat, prev_lon) or 0.0
                if depth == prev_depth or prev_dist < 0.0:
                    result_dist = dist
                else:
                    frac = (target_depth_m - prev_depth) / (depth - prev_depth)
                    result_dist = prev_dist + frac * step_m
                logger.info(
                    "find_depth_contour_distance: spot=%r bearing=%.1f° "
                    "target=%.1fm reached at %.0fm offshore of coastline "
                    "(%.6f, %.6f)",
                    spot_id, bearing_degrees, target_depth_m, result_dist,
                    coastline_lat, coastline_lon,
                )
                return result_dist
            max_depth_reached = max(max_depth_reached, depth)
        dist += step_m

    raise ValueError(
        f"find_depth_contour_distance: spot={spot_id!r} bearing="
        f"{bearing_degrees:.1f}° target_depth_m={target_depth_m:.1f} was "
        f"never reached within max_search_m={max_search_m:.0f} from coastline "
        f"({coastline_lat:.6f}, {coastline_lon:.6f}) — deepest depth actually "
        f"reached in the search was {max_depth_reached:.1f}m. This is an error, "
        "not a fallback (T4A.3 LC-10): the caller must NOT substitute a "
        "hardcoded distance. Check the downloaded grid's coverage/resolution "
        "for this location and bearing."
    )


def extract_native_profile_from_grid(
    grid: dict[str, Any],
    coastline_lat: float,
    coastline_lon: float,
    bearing_degrees: float,
    max_distance_m: float,
) -> list[dict[str, float]]:
    """Extract a raw depth transect from *grid* at the grid's OWN native spacing.

    T4A.3 Do step 8: the raw profile fed to ``interpolate_profile_pchip``
    must be sampled at native DEM resolution (~3-10 m) — NOT the 50 m
    ``download_bidirectional_profile`` stepper.  This function derives the
    native cell spacing directly from *grid*'s own lat/lon extent and cell
    count, so the returned profile's spacing always matches whatever DEM
    actually backed the download (T4A.2's ``interpolate_profile_pchip``
    itself re-validates this — a grid degraded to CRM (~90 m) would raise
    there, not here).

    Args:
        grid: Downloaded FINE-tier 2-D bathymetry grid for this spot's L3
            cluster (Do step 7).
        coastline_lat, coastline_lon: Shoreline anchor.
        bearing_degrees: this spot's own offshore bearing.
        max_distance_m: Extent of the returned profile (typically the L3
            grid's own offshore boundary distance for this spot/cluster).

    Returns:
        List of ``{"distance_m": float, "depth_m": float}`` dicts, sorted
        ascending by distance, sampled at the grid's native cell spacing.
        Points outside the grid's coverage are omitted (never a gap-filled
        guess).
    """
    lat_first = grid["lat_first"]
    lon_first = grid["lon_first"]
    lat_last = grid["lat_last"]
    lon_last = grid["lon_last"]
    nj = grid["nj"]
    ni = grid["ni"]

    mean_lat = (lat_first + lat_last) / 2.0
    dlat_deg = abs(lat_last - lat_first) / max(nj - 1, 1)
    dlon_deg = abs(lon_last - lon_first) / max(ni - 1, 1)
    dlat_m = dlat_deg * 111_320.0
    dlon_m = dlon_deg * 111_320.0 * math.cos(math.radians(mean_lat))
    native_spacing_m = max(min(v for v in (dlat_m, dlon_m) if v > 0.0), 0.5)

    num_pts = max(2, int(round(max_distance_m / native_spacing_m)) + 1)
    distances_m = _linspace(0.0, max_distance_m, num_pts)

    profile: list[dict[str, float]] = []
    for d in distances_m:
        pt_lat, pt_lon = point_along_bearing(coastline_lat, coastline_lon, bearing_degrees, d)
        depth = _grid_depth_below_msl(grid, pt_lat, pt_lon)
        if depth is not None:
            profile.append({"distance_m": round(d, 2), "depth_m": round(depth, 3)})

    return profile


def compute_structure_zone_depth(
    structures: list[dict[str, Any]] | None,
    grid: dict[str, Any],
    *,
    margin_m: float = 2.0,
    spot_id: str = "",
) -> float:
    """``structure_zone_depth`` — T4A.2/T4A.3 Do step 5.

    **LC-R2-8 (P4A Round 2): two conflicting definitions exist in the
    governing documents.** Plan T4A.2 and API-MANUAL §17 both say "the depth
    of the deepest structure affecting this spot, plus margin" — that is
    what this function implements. ``L3-1D-BOUNDARY-DECISIONS-BRIEF.md`` §4
    quotes SURF-ZONE-MODEL-BRIEF §6.1 as "maximum depth at the L3 SWAN grid
    boundary for this spot's cluster" — a DIFFERENT quantity (a property of
    the sized grid, not of the structures themselves) that is NOT
    implemented here. T4A.3 Do step 5's own instruction and T4A.2's two
    worked examples (HB: no structures → 0.0; Newport: breakwater at 8m →
    ``10.0`` = 8 + 2 margin) both support the deepest-structure reading, so
    that is the one built. Flagged, not silently resolved — see the P4A
    Round 2 closeout for B2's explicit answer on this.

    Queries *grid* (a downloaded bathymetry grid — the same MEDIUM or FINE
    tier the caller already has from the apply-time chain) at every
    structure coordinate and returns the deepest depth found, plus
    *margin_m*. Structures whose coordinates fall outside *grid*'s coverage
    are skipped with a WARNING (per "Silent skipping of configured inputs
    is a bug pattern") rather than silently ignored.

    Returns ``0.0`` when *structures* is empty/``None``, or when none of
    the supplied structures have coordinate data reachable in *grid* — this
    matches T4A.2's documented default (fine zone covers only the breaking
    zone, no structure interaction).
    """
    if not structures:
        return 0.0

    deepest_m = 0.0
    any_reachable = False
    for struct in structures:
        coords = struct.get("coordinates") or []
        for coord in coords:
            if len(coord) < 2:
                continue
            lon, lat = float(coord[0]), float(coord[1])
            depth = _grid_depth_below_msl(grid, lat, lon)
            if depth is None:
                continue
            any_reachable = True
            deepest_m = max(deepest_m, depth)

    if not any_reachable:
        if structures:
            logger.warning(
                "compute_structure_zone_depth: spot=%r has %d structure(s) "
                "configured but none has a coordinate reachable in the "
                "supplied bathymetry grid — structure_zone_depth defaults "
                "to 0.0 (fine zone will NOT be extended for these "
                "structures). Check structure coordinates against the grid "
                "bbox.",
                spot_id, len(structures),
            )
        return 0.0

    return round(deepest_m + margin_m, 3)


# ---------------------------------------------------------------------------
# Beach slope computation (used by wave_transform.py's Battjes gamma formula)
# ---------------------------------------------------------------------------

_NEARSHORE_RANGE_M = 500.0


def compute_beach_slope(profile: list[BathymetryPoint]) -> float:
    """Compute the average nearshore bottom slope tan(alpha) from *profile*.

    Linear regression (least squares) of depth_m over distance_m, restricted
    to the nearshore portion (0-500m from the break point). Falls back to
    the two shallowest points in the full profile if fewer than two points
    fall within the nearshore range.

    Returns tan(alpha) as a dimensionless ratio (meters of depth change per
    meter of horizontal distance) — no unit conversion is needed since both
    axes are already meters.
    """
    if len(profile) < 2:
        raise ValueError("compute_beach_slope requires at least 2 profile points")

    ordered = sorted(profile, key=lambda p: p.distance_m)
    nearshore = [p for p in ordered if p.distance_m <= _NEARSHORE_RANGE_M]
    if len(nearshore) < 2:
        nearshore = ordered[:2]

    n = len(nearshore)
    xs = [p.distance_m for p in nearshore]
    ys = [p.depth_m for p in nearshore]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - x_mean) ** 2 for x in xs)

    if denominator == 0:
        raise ValueError("compute_beach_slope: all nearshore points have the same distance_m")

    return numerator / denominator


# ---------------------------------------------------------------------------
# Habitat feature identification (fishing depth-profile annotations)
# ---------------------------------------------------------------------------

_DROPOFF_MIN_DEPTH_CHANGE_M = 5.0
_DROPOFF_MAX_HORIZONTAL_M = 200.0
_LEDGE_MAX_DEPTH_CHANGE_PER_100M = 1.0
_REEF_MAX_DEPTH_M = 20.0
_LOCAL_EXTREMUM_MIN_CHANGE_M = 0.3
_DEEP_WATER_NEIGHBOR_MIN_M = 20.0


@dataclass(frozen=True)
class _Segment:
    i: int
    j: int
    horiz_m: float
    depth_change_m: float

    @property
    def abs_change_m(self) -> float:
        return abs(self.depth_change_m)


def identify_habitat_features(profile: list[BathymetryPoint]) -> list[dict[str, Any]]:
    """Scan *profile* for fishing-relevant seafloor features.

    Returns a list of ``{"type", "distance_m", "depth_m", "description"}``
    dicts. Feature types (PROVIDER-MANUAL §14.7):
      - ``dropoff``: depth change > 5m in < 200m horizontal distance.
      - ``ledge``: a flat segment (< 1m change per 100m) immediately
        adjacent to a dropoff segment.
      - ``reef``: irregular (non-monotonic) depth pattern at shallow
        depths (< 20m), not already classified as a channel or pinnacle.
      - ``channel``: a linear depression — a local depth maximum (a point
        deeper than both neighbors), i.e. a trough in the seafloor.
      - ``pinnacle``: an isolated shallow spot in deeper water — a local
        depth minimum surrounded by neighbors at/above the deep-water
        neighbor threshold (20m).
    """
    points = sorted(profile, key=lambda p: p.distance_m)
    n = len(points)
    features: list[dict[str, Any]] = []
    if n < 2:
        return features

    segments = [
        _Segment(
            i=i,
            j=i + 1,
            horiz_m=points[i + 1].distance_m - points[i].distance_m,
            depth_change_m=points[i + 1].depth_m - points[i].depth_m,
        )
        for i in range(n - 1)
    ]

    # --- Drop-offs ---
    dropoff_segment_indices: set[int] = set()
    for idx, seg in enumerate(segments):
        if (
            0 < seg.horiz_m < _DROPOFF_MAX_HORIZONTAL_M
            and seg.abs_change_m > _DROPOFF_MIN_DEPTH_CHANGE_M
        ):
            p2 = points[seg.j]
            features.append(
                {
                    "type": "dropoff",
                    "distance_m": p2.distance_m,
                    "depth_m": p2.depth_m,
                    "description": (
                        f"Drop-off: {seg.abs_change_m:.1f}m depth change over {seg.horiz_m:.0f}m"
                    ),
                }
            )
            dropoff_segment_indices.add(idx)

    # --- Ledges: flat segment adjacent to a drop-off segment ---
    for idx, seg in enumerate(segments):
        if seg.horiz_m <= 0:
            continue
        normalized_change_per_100m = seg.abs_change_m * (100.0 / seg.horiz_m)
        if normalized_change_per_100m >= _LEDGE_MAX_DEPTH_CHANGE_PER_100M:
            continue
        adjacent_is_dropoff = (idx - 1 in dropoff_segment_indices) or (
            idx + 1 in dropoff_segment_indices
        )
        if adjacent_is_dropoff:
            p1 = points[seg.i]
            features.append(
                {
                    "type": "ledge",
                    "distance_m": p1.distance_m,
                    "depth_m": p1.depth_m,
                    "description": (
                        f"Ledge: flat area ({seg.abs_change_m:.1f}m change over "
                        f"{seg.horiz_m:.0f}m) adjacent to a steep drop-off"
                    ),
                }
            )

    # --- Reef / channel / pinnacle: local extrema at interior points ---
    for i in range(1, n - 1):
        p_prev, p_cur, p_next = points[i - 1], points[i], points[i + 1]
        d1 = p_cur.depth_m - p_prev.depth_m
        d2 = p_next.depth_m - p_cur.depth_m

        if abs(d1) < _LOCAL_EXTREMUM_MIN_CHANGE_M or abs(d2) < _LOCAL_EXTREMUM_MIN_CHANGE_M:
            continue

        # Pinnacle: local minimum (shallower than both neighbors), with
        # both neighbors in water deep enough to call it "isolated shallow".
        if (
            d1 < 0
            and d2 > 0
            and p_prev.depth_m >= _DEEP_WATER_NEIGHBOR_MIN_M
            and p_next.depth_m >= _DEEP_WATER_NEIGHBOR_MIN_M
        ):
            features.append(
                {
                    "type": "pinnacle",
                    "distance_m": p_cur.distance_m,
                    "depth_m": p_cur.depth_m,
                    "description": (
                        f"Possible pinnacle: isolated shallow spot "
                        f"({p_cur.depth_m:.1f}m) in deeper water"
                    ),
                }
            )
        # Channel: local maximum (deeper than both neighbors) — a trough.
        elif d1 > 0 and d2 < 0:
            features.append(
                {
                    "type": "channel",
                    "distance_m": p_cur.distance_m,
                    "depth_m": p_cur.depth_m,
                    "description": (
                        f"Possible channel: linear depression "
                        f"({p_cur.depth_m:.1f}m) between shallower neighbors"
                    ),
                }
            )
        # Reef: irregular (non-monotonic) pattern at shallow depth, not
        # already explained by a channel/pinnacle above.
        elif d1 * d2 < 0 and p_cur.depth_m < _REEF_MAX_DEPTH_M:
            features.append(
                {
                    "type": "reef",
                    "distance_m": p_cur.distance_m,
                    "depth_m": p_cur.depth_m,
                    "description": (
                        f"Possible reef structure: irregular depth pattern at "
                        f"{p_cur.depth_m:.1f}m depth"
                    ),
                }
            )

    return features


# ---------------------------------------------------------------------------
# 2-D depth grid download for SWAN (T7.5)
# ---------------------------------------------------------------------------

_GETSAMPLES_BATCH_SIZE = 1000


def download_swan_depth_grid(
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> dict[str, Any]:
    """Download a 2-D CUDEM depth grid covering *bbox* at *resolution_m* spacing.

    Uses the NCEI ArcGIS ImageServer ``getSamples`` multipoint endpoint
    (POST, batched at 1000 points per request).  Returns a dict in the
    format ``cudem_to_swan_bottom()`` expects::

        {"lat_first", "lon_first", "lat_last", "lon_last",
         "ni", "nj", "depths": [[float]]}

    ``depths`` uses CUDEM sign convention (negative = ocean, positive = land).
    """
    import json as _json
    import urllib.parse
    import urllib.request

    lon_sw, lat_sw, lon_ne, lat_ne = bbox

    deg_per_m_lat = 1.0 / 111_320.0
    deg_per_m_lon = 1.0 / (111_320.0 * math.cos(math.radians((lat_sw + lat_ne) / 2.0)))
    dlat = resolution_m * deg_per_m_lat
    dlon = resolution_m * deg_per_m_lon

    ny = max(2, int(round((lat_ne - lat_sw) / dlat)) + 1)
    nx = max(2, int(round((lon_ne - lon_sw) / dlon)) + 1)

    all_points: list[list[float]] = []
    for j in range(ny):
        lat = lat_sw + j * (lat_ne - lat_sw) / (ny - 1)
        for i in range(nx):
            lon = lon_sw + i * (lon_ne - lon_sw) / (nx - 1)
            all_points.append([round(lon, 6), round(lat, 6)])

    logger.info(
        "CUDEM 2-D grid download: %d x %d = %d points, bbox=%s, res=%.0fm",
        nx, ny, len(all_points), bbox, resolution_m,
    )

    all_values: list[float] = []
    _seen_datums: set[str] = set()
    for batch_start in range(0, len(all_points), _GETSAMPLES_BATCH_SIZE):
        batch = all_points[batch_start:batch_start + _GETSAMPLES_BATCH_SIZE]
        geometry = _json.dumps({"points": batch})
        post_data = urllib.parse.urlencode({
            "geometry": geometry,
            "geometryType": "esriGeometryMultipoint",
            "f": "json",
        }).encode("utf-8")
        req = urllib.request.Request(
            _NCEI_GETSAMPLES_URL,
            data=post_data,
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read())

        for sample in data.get("samples", []):
            raw = sample.get("value")
            if raw is None or str(raw).strip().lower() == "nodata":
                all_values.append(-15.0)
            else:
                all_values.append(float(raw))
            vd = sample.get("VerticalDatum")
            if vd:
                _seen_datums.add(str(vd).strip())

    depths_2d: list[list[float]] = []
    for j in range(ny):
        row = all_values[j * nx : (j + 1) * nx]
        depths_2d.append(row)

    logger.info(
        "CUDEM 2-D grid download complete: %d x %d values, ocean=%d land=%d",
        nx, ny,
        sum(1 for v in all_values if v <= 0),
        sum(1 for v in all_values if v > 0),
    )

    # T4.1 — Determine vertical datum from CRM response.
    # CRM getSamples typically does NOT return a VerticalDatum attribute; the
    # default path (UNKNOWN_CRM + WARNING) is expected in practice.
    if len(_seen_datums) == 1:
        vertical_datum: str = next(iter(_seen_datums))
    else:
        vertical_datum = "UNKNOWN_CRM"
        logger.warning(
            "CRM bathymetry has unknown vertical datum"
            " — SWAN depth calculations may have up to ~1m vertical bias"
        )

    return {
        "lat_first": lat_sw,
        "lon_first": lon_sw,
        "lat_last": lat_ne,
        "lon_last": lon_ne,
        "ni": nx,
        "nj": ny,
        "depths": depths_2d,
        "vertical_datum": vertical_datum,
    }
