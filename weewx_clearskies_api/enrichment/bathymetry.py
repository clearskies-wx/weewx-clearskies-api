"""NOAA CUDEM bathymetry setup utility (Phase 3, T3.1; Marine Remediation T1.2;
PROVIDER-MANUAL §14.7).

**Not a dispatch-registered provider module.** This is a one-time,
per-spot data-access utility invoked from the wizard/admin surf-spot
configuration flow — never per-request. Its output (a list of
``BathymetryPoint``) is stored in ``api.conf`` under
``[marine][[locations]][[[<id>]]][[[[surf]]]] bathymetric_profile`` (see
``config/marine_config.py``) and later consumed by ``enrichment/wave_transform.py``
(T3.2) for the Battjes gamma formula, and by the fishing habitat-annotation
pipeline.

Data source (T1.2 remediation, superseding the original v1 decision):
    The original v1 access method (OpenTopoData's ``/v1/cudem`` REST
    endpoint) does not actually exist — OpenTopoData's public deployment
    never hosted a ``cudem`` dataset and the endpoint returns 404 "Dataset
    'cudem' not in config" for every request. No real bathymetric data was
    ever retrieved through it; every download silently fell back to the
    hardcoded regional profile.

    This module now queries the NCEI ArcGIS ImageServer ``identify``
    endpoint directly:
    ``https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/identify``.
    This is a free, keyless, government-hosted REST point-query service that
    serves NOAA's Continuously Updated Digital Elevation Model (CUDEM) at
    1/9 arc-second resolution (~3.4m) — finer than the originally-planned
    1/3 arc-second (~10m) OpenTopoData resolution, so reef structures,
    ledges, and channel edges are more clearly visible in addition to the
    larger drop-off/shelf features the v1 brief targeted.

    Unlike OpenTopoData's (nonexistent) batched ``locations=lat,lon|lat,lon``
    request shape, the NCEI ImageServer ``identify`` endpoint is a
    single-point query API: one HTTP request per (lat, lon) sample. See
    ``_query_depths_m`` below.

    Unified bounding-box download (Marine Remediation T1.2): when multiple
    surf/fishing spots are configured, ``download_bathymetric_profiles_unified``
    computes every spot's search/transect/refinement points up front,
    deduplicates points that land on the same CUDEM pixel across spots, and
    issues one shared batch of point queries per phase instead of running
    each spot's full search+profile+refinement sequence independently. The
    single-spot ``download_bathymetric_profile`` function is unchanged and
    still used by the standalone admin re-download endpoint.

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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.config.marine_config import BathymetryPoint
from weewx_clearskies_api.providers._common.errors import (
    ProviderError,
    ProviderProtocolError,
    QuotaExhausted,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

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
# IQR-based outlier smoothing
# ---------------------------------------------------------------------------


def _smooth_outliers_iqr(depths: list[float]) -> list[float]:
    """Replace IQR-outlier depth readings with the mean of their neighbors.

    Standard Tukey fence: values outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR] are
    considered anomalous CUDEM readings (e.g. a single bad pixel) and are
    smoothed using the average of their immediate neighbors rather than
    dropped, to preserve point count/spacing.
    """
    n = len(depths)
    if n < 4:
        return list(depths)

    ordered = sorted(depths)

    def _percentile(data: list[float], pct: float) -> float:
        k = (len(data) - 1) * pct
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return data[int(k)]
        return data[f] + (data[c] - data[f]) * (k - f)

    q1 = _percentile(ordered, 0.25)
    q3 = _percentile(ordered, 0.75)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    smoothed = list(depths)
    for i in range(n):
        if depths[i] < lower or depths[i] > upper:
            prev_val = depths[i - 1] if i > 0 else depths[i + 1]
            next_val = depths[i + 1] if i < n - 1 else depths[i - 1]
            smoothed[i] = (prev_val + next_val) / 2.0
            logger.warning(
                "Bathymetry: IQR outlier smoothed at index %d (%.2fm -> %.2fm)",
                i,
                depths[i],
                smoothed[i],
            )
    return smoothed


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
      5. Smooth outlier readings (IQR method) and return the profile
         sorted by distance ascending.

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

    # --- Step 5: smooth outliers, drop any still-missing readings ---
    clean_pairs = [(d, z) for d, z in zip(distances_m, depths_m, strict=True) if z is not None]
    clean_pairs.sort(key=lambda p: p[0])
    clean_distances = [p[0] for p in clean_pairs]
    clean_depths = _smooth_outliers_iqr([p[1] for p in clean_pairs])

    return [
        BathymetryPoint({"distance_m": d, "depth_m": z})
        for d, z in zip(clean_distances, clean_depths, strict=True)
    ]


# ---------------------------------------------------------------------------
# Bidirectional runtime profile (SWAN bidirectional transect)
# ---------------------------------------------------------------------------

_BIDIR_PROFILE_STEP_M = 50.0  # interval between profile sample points


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
         coastline to the deep endpoint, smooth IQR outliers, and return.

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

    # ---- Step 1: find the coastline ----
    pin_depth = _query_depths_m([(lat, lon)])[0]

    coastline_lat = lat
    coastline_lon = lon

    if pin_depth is None or pin_depth == 0.0:
        # Pin is on land (depth == 0) or outside CUDEM coverage — treat as shore.
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
            if depth is not None and depth == 0.0:
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

    if clean_pairs:
        c_dists = [p[0] for p in clean_pairs]
        c_depths = _smooth_outliers_iqr([p[1] for p in clean_pairs])
    else:
        c_dists = []
        c_depths = []

    profile = [
        {"distance_m": round(d, 1), "depth_m": round(z, 2)}
        for d, z in zip(c_dists, c_depths, strict=True)
    ]

    return {
        "coastline_lat": round(coastline_lat, 6),
        "coastline_lon": round(coastline_lon, 6),
        "profile": profile,
    }


# ---------------------------------------------------------------------------
# Unified bounding-box download (Marine Remediation T1.2)
# ---------------------------------------------------------------------------

# Points within this grid size (~4m, matching CUDEM's ~3.4m 1/9 arc-second
# pixel size) are treated as "the same query" and deduplicated across spots.
# Purely a query-count optimization: collapsing two queries that would
# return the same (or a physically indistinguishable) CUDEM pixel reading
# anyway. Expressed in degrees-of-latitude terms for both axes (a slight
# over-merge at low latitudes where a degree of longitude is physically
# shorter is immaterial here — the true CUDEM pixel is still ~3.4m wide).
_DEDUPE_GRID_DEG = 4.0 / 111_320.0


def _dedupe_points(
    points: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], list[int]]:
    """Collapse near-identical (lat, lon) points into a unique query list.

    Returns ``(unique_points, index_map)`` where ``index_map[i]`` is the
    index into ``unique_points`` that ``points[i]`` maps to. Preserves
    first-seen order so behavior is deterministic and, for a single caller
    with no duplicates, ``unique_points == points`` and ``index_map`` is the
    identity mapping.
    """
    seen: dict[tuple[int, int], int] = {}
    unique: list[tuple[float, float]] = []
    index_map: list[int] = []
    for lat, lon in points:
        key = (round(lat / _DEDUPE_GRID_DEG), round(lon / _DEDUPE_GRID_DEG))
        idx = seen.get(key)
        if idx is None:
            idx = len(unique)
            seen[key] = idx
            unique.append((lat, lon))
        index_map.append(idx)
    return unique, index_map


def download_bathymetric_profiles_unified(
    spots: list[dict[str, Any]],
    *,
    max_search_km: float = _DEFAULT_MAX_SEARCH_KM,
    search_step_km: float = _DEFAULT_SEARCH_STEP_KM,
    num_profile_points: int = _DEFAULT_PROFILE_POINTS,
) -> dict[str, list[BathymetryPoint]]:
    """Download bathymetric depth profiles for multiple spots in one unified pass
    (Marine Remediation T1.2, PROVIDER-MANUAL §14.7 "Unified bounding box download").

    The NCEI ImageServer ``identify`` endpoint is a single-point query API with
    no area/bounding-box export shape (see module docstring), so "one
    operation" here means: instead of N independent
    ``download_bathymetric_profile`` calls (each doing its own sequential
    search + profile + refinement queries), this function computes every
    spot's search/profile/refinement points *up front*, merges them into one
    deduplicated batch per phase (search, then initial transect, then each
    adaptive-refinement round), and issues one shared ``_query_depths_m`` call
    per phase covering every spot at once. Points that fall on the same CUDEM
    pixel (~4m, see ``_dedupe_points``) across different spots' transects are
    queried only once. This is the "unified bounding box" the operator's
    configured locations collectively span — expressed as a merged point set
    rather than a literal ArcGIS area export, since no such export exists for
    this data source.

    *spots* is a list of ``{"id": str, "lat": float, "lon": float,
    "bearing_degrees": float}`` dicts (one per surf/fishing spot needing a
    profile). Returns ``{spot_id: [BathymetryPoint, ...]}``.

    Per-spot algorithm and output quality are identical to
    ``download_bathymetric_profile`` (same region classification, deep-water
    threshold search, linearly-interpolated transect, adaptive gradient
    refinement, and IQR outlier smoothing) — only the query batching differs.
    For a single spot this degenerates to the same behavior as
    ``download_bathymetric_profile`` (the dedup step is a no-op when there's
    nothing to merge against).

    On any provider failure (network error, quota, protocol error) during any
    phase, logs a WARNING and returns the hardcoded regional fallback profile
    for *every* spot in the batch — unlike the per-spot function's isolated
    per-location fallback, a shared batch query failing means every spot in
    that batch was relying on the same in-flight HTTP call, so there is no
    finer-grained partial result to preserve.
    """
    if not spots:
        return {}

    regions: dict[str, str] = {}
    thresholds: dict[str, float] = {}
    for spot in spots:
        region = classify_region(spot["lat"], spot["lon"])
        regions[spot["id"]] = region
        thresholds[spot["id"]] = REGION_DEEP_WATER_THRESHOLDS_M.get(
            region, _DEFAULT_DEEP_WATER_THRESHOLD_M
        )

    def _fallback_all() -> dict[str, list[BathymetryPoint]]:
        return {spot["id"]: _fallback_profile(regions[spot["id"]]) for spot in spots}

    # --- Step 1/2: search outward for each spot's deep-water endpoint ---
    num_steps = int(max_search_km / search_step_km)
    search_distances_m = [search_step_km * 1000.0 * i for i in range(1, num_steps + 1)]

    all_search_points: list[tuple[float, float]] = []
    for spot in spots:
        all_search_points.extend(
            point_along_bearing(spot["lat"], spot["lon"], spot["bearing_degrees"], d)
            for d in search_distances_m
        )

    try:
        unique_points, index_map = _dedupe_points(all_search_points)
        unique_depths = _query_depths_m(unique_points)
    except ProviderError as exc:
        logger.warning(
            "Bathymetry: unified search-phase query failed for %d spot(s): %s. "
            "Using fallback profiles for all spots in this batch.",
            len(spots),
            exc,
        )
        return _fallback_all()
    all_search_depths = [unique_depths[i] for i in index_map]

    deep_water_distance_by_spot: dict[str, float] = {}
    offset = 0
    for spot in spots:
        sid = spot["id"]
        n = len(search_distances_m)
        depths_for_spot = all_search_depths[offset : offset + n]
        offset += n

        threshold_m = thresholds[sid]
        deep_water_distance_m = search_distances_m[-1]
        found_threshold = False
        for distance_m, depth_m in zip(search_distances_m, depths_for_spot, strict=True):
            if depth_m is not None and depth_m >= threshold_m:
                deep_water_distance_m = distance_m
                found_threshold = True
                break
        if not found_threshold:
            logger.warning(
                "Bathymetry: no point reached deep-water threshold %.1fm within "
                "%.1fkm for spot %r (region=%r); using farthest searched point "
                "as the deep-water endpoint.",
                threshold_m,
                max_search_km,
                sid,
                regions[sid],
            )
        deep_water_distance_by_spot[sid] = deep_water_distance_m

    # --- Step 3: initial linearly-interpolated transect per spot ---
    distances_by_spot: dict[str, list[float]] = {}
    profile_point_counts: dict[str, int] = {}
    all_profile_points: list[tuple[float, float]] = []
    for spot in spots:
        sid = spot["id"]
        distances_m = _linspace(0.0, deep_water_distance_by_spot[sid], num_profile_points)
        distances_by_spot[sid] = distances_m
        points = [
            point_along_bearing(spot["lat"], spot["lon"], spot["bearing_degrees"], d)
            for d in distances_m
        ]
        profile_point_counts[sid] = len(points)
        all_profile_points.extend(points)

    try:
        unique_points, index_map = _dedupe_points(all_profile_points)
        unique_depths = _query_depths_m(unique_points)
    except ProviderError as exc:
        logger.warning(
            "Bathymetry: unified profile-phase query failed for %d spot(s): %s. "
            "Using fallback profiles for all spots in this batch.",
            len(spots),
            exc,
        )
        return _fallback_all()
    all_profile_depths = [unique_depths[i] for i in index_map]

    depths_by_spot: dict[str, list[float | None]] = {}
    offset = 0
    for spot in spots:
        sid = spot["id"]
        n = profile_point_counts[sid]
        depths_by_spot[sid] = all_profile_depths[offset : offset + n]
        offset += n

    # --- Step 4: adaptive refinement, batched per round across all spots ---
    for _iteration in range(_MAX_REFINEMENT_ITERATIONS):
        insertions_by_spot: dict[str, list[tuple[float, tuple[float, float]]]] = {}
        all_insert_points: list[tuple[float, float]] = []
        for spot in spots:
            sid = spot["id"]
            distances_m = distances_by_spot[sid]
            depths_m = depths_by_spot[sid]
            spot_insertions: list[tuple[float, tuple[float, float]]] = []
            for i in range(len(distances_m) - 1):
                d0, d1 = depths_m[i], depths_m[i + 1]
                x0, x1 = distances_m[i], distances_m[i + 1]
                if d0 is None or d1 is None or x1 <= x0:
                    continue
                gradient = abs(d1 - d0) / (x1 - x0)
                if gradient > _GRADIENT_REFINEMENT_THRESHOLD:
                    mid_distance = (x0 + x1) / 2.0
                    pt = point_along_bearing(
                        spot["lat"], spot["lon"], spot["bearing_degrees"], mid_distance
                    )
                    spot_insertions.append((mid_distance, pt))
            if spot_insertions:
                insertions_by_spot[sid] = spot_insertions
                all_insert_points.extend(pt for _, pt in spot_insertions)

        if not insertions_by_spot:
            break

        try:
            unique_points, index_map = _dedupe_points(all_insert_points)
            unique_depths = _query_depths_m(unique_points)
        except ProviderError as exc:
            logger.warning(
                "Bathymetry: unified refinement-phase query failed for %d spot(s): "
                "%s. Stopping refinement early; using data collected so far "
                "(not falling back for this batch since search+initial-transect "
                "data already succeeded).",
                len(insertions_by_spot),
                exc,
            )
            break
        all_insert_depths = [unique_depths[i] for i in index_map]

        offset = 0
        for sid, spot_insertions in insertions_by_spot.items():
            n = len(spot_insertions)
            spot_depths = all_insert_depths[offset : offset + n]
            offset += n
            for (mid_distance, _pt), mid_depth in zip(spot_insertions, spot_depths, strict=True):
                distances_by_spot[sid].append(mid_distance)
                depths_by_spot[sid].append(mid_depth)

            paired = sorted(
                zip(distances_by_spot[sid], depths_by_spot[sid], strict=True),
                key=lambda p: p[0],
            )
            distances_by_spot[sid] = [p[0] for p in paired]
            depths_by_spot[sid] = [p[1] for p in paired]

    # --- Step 5: smooth outliers, drop missing readings, build profiles ---
    result: dict[str, list[BathymetryPoint]] = {}
    for spot in spots:
        sid = spot["id"]
        clean_pairs = [
            (d, z)
            for d, z in zip(distances_by_spot[sid], depths_by_spot[sid], strict=True)
            if z is not None
        ]
        clean_pairs.sort(key=lambda p: p[0])
        clean_distances = [p[0] for p in clean_pairs]
        clean_depths = _smooth_outliers_iqr([p[1] for p in clean_pairs])
        result[sid] = [
            BathymetryPoint({"distance_m": d, "depth_m": z})
            for d, z in zip(clean_distances, clean_depths, strict=True)
        ]

    return result


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

    return {
        "lat_first": lat_sw,
        "lon_first": lon_sw,
        "lat_last": lat_ne,
        "lon_last": lon_ne,
        "ni": nx,
        "nj": ny,
        "depths": depths_2d,
    }
