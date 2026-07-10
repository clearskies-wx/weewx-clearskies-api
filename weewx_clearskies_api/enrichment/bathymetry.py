"""NOAA CUDEM bathymetry setup utility (Phase 3, T3.1; PROVIDER-MANUAL §14.7).

**Not a dispatch-registered provider module.** This is a one-time,
per-spot data-access utility invoked from the wizard/admin surf-spot
configuration flow — never per-request. Its output (a list of
``BathymetryPoint``) is stored in ``api.conf`` under
``[marine][[locations]][[[<id>]]][[[[surf]]]] bathymetric_profile`` (see
``config/marine_config.py``) and later consumed by ``enrichment/wave_transform.py``
(T3.2) for the Battjes gamma formula, and by the fishing habitat-annotation
pipeline.

Data source (v1 decision, lead confirmed 2026-07-10):
    v1 uses the OpenTopoData CUDEM REST endpoint
    (``https://api.opentopodata.org/v1/cudem``), which serves NOAA's
    Continuously Updated Digital Elevation Model at 1/3 arc-second
    resolution (~10m). This is a simple, stable, keyless REST API.

    Future upgrade: NCEI THREDDS/OPeNDAP direct access to the full-resolution
    CUDEM dataset at 1/9 arc-second (~3.4m) — finer detail on reef structures,
    ledges, and channel edges. THREDDS OPeNDAP subsetting is materially more
    complex to integrate (NetCDF/OPeNDAP client, dataset/grid discovery per
    region) and was deferred out of v1 scope; OpenTopoData is sufficient for
    slope computation and large habitat features at v1.

Elevation sign convention: OpenTopoData/CUDEM elevation is relative to
Mean Sea Level (MSL). Negative elevation = underwater depth. Depth values
returned by this module are always non-negative meters (``depth_m = max(0,
-elevation)``); points that come back with positive elevation (land) are
treated as zero depth.

Regional deep-water thresholds and hardcoded fallback profiles encode
rough, non-authoritative continental-shelf characteristics (steep Pacific
shelf vs. gradual Gulf shelf, etc.) — see ``REGION_DEEP_WATER_THRESHOLDS_M``
and ``FALLBACK_DEPTH_PROFILES_M`` below.

Units: this module works exclusively in meters (depth, distance) throughout.
No unit conversion is performed or required here — CUDEM/OpenTopoData
elevations are always meters, and stored profile distances are always
meters; the project's ``UnitTransformer`` only enters the picture at API
response time when a request is scoped to a non-metric unit system, which
is out of scope for this setup-time module.

Attribution (PROVIDER-MANUAL §14.7): NOAA CUDEM data requires attribution
and a navigation disclaimer wherever bathymetric data is displayed — see
``ATTRIBUTION`` / ``DISCLAIMER`` below.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.config.marine_config import BathymetryPoint
from weewx_clearskies_api.providers._common.errors import ProviderError
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
# OpenTopoData CUDEM endpoint (v1 data source — see module docstring)
# ---------------------------------------------------------------------------

PROVIDER_ID = "cudem"
DOMAIN = "bathymetry"
_OPENTOPODATA_URL = "https://api.opentopodata.org/v1/cudem"
_USER_AGENT = "weewx-clearskies-api-bathymetry/0.1 (NOAA CUDEM via OpenTopoData)"
_MAX_LOCATIONS_PER_REQUEST = 100  # OpenTopoData hard cap

# 1 call/sec per OpenTopoData's documented rate limit for the free tier.
_rate_limiter = RateLimiter(
    name="opentopodata-cudem",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=1,
    window_seconds=1,
)

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


class _OpenTopoDataResult(BaseModel):
    """One location result from the OpenTopoData ``/v1/cudem`` response."""

    model_config = ConfigDict(extra="ignore")

    elevation: float | None = None


class _OpenTopoDataResponse(BaseModel):
    """OpenTopoData ``/v1/cudem`` response envelope."""

    model_config = ConfigDict(extra="ignore")

    status: str | None = None
    results: list[_OpenTopoDataResult] = Field(default_factory=list)


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
# used when CUDEM/OpenTopoData is unavailable (network failure, quota, etc.).
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
        "Bathymetry: CUDEM/OpenTopoData unavailable for region=%r; "
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


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Depth query (batched OpenTopoData call)
# ---------------------------------------------------------------------------


def _query_depths_m(points: list[tuple[float, float]]) -> list[float | None]:
    """Query depth (meters, non-negative) at each (lat, lon) in *points*.

    Batches into groups of at most ``_MAX_LOCATIONS_PER_REQUEST`` locations
    per OpenTopoData request. Returns ``None`` for any location OpenTopoData
    didn't return an elevation for (should not normally happen for valid
    coastal coordinates, but handled defensively).

    Raises the canonical ``ProviderError`` taxonomy on any network/protocol
    failure — never re-wrapped, propagates as-is per project convention.
    Callers that want graceful fallback must catch ``ProviderError``
    themselves (see ``download_bathymetric_profile``).
    """
    client = _get_http_client()
    depths: list[float | None] = []

    for batch in _chunked(points, _MAX_LOCATIONS_PER_REQUEST):
        _rate_limiter.acquire()
        locations_param = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in batch)
        response = client.get(_OPENTOPODATA_URL, params={"locations": locations_param})

        try:
            wire = _OpenTopoDataResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            from weewx_clearskies_api.providers._common.errors import ProviderProtocolError

            logger.error(
                "OpenTopoData CUDEM response validation failed: %s. "
                "Response body (first 2000 chars): %.2000s",
                exc,
                response.text,
            )
            raise ProviderProtocolError(
                f"OpenTopoData CUDEM response validation failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        for result in wire.results:
            if result.elevation is None:
                depths.append(None)
            else:
                # Negative elevation = underwater depth (MSL-relative).
                # Positive elevation (land) clamps to zero depth.
                depths.append(max(0.0, -result.elevation))

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
