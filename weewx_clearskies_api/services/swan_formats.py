"""SWAN input file format writers for the SWAN nearshore wave model pipeline.

These utilities convert provider data formats (HRRR wind field, CUDEM bathymetry,
WaveWatch III boundary conditions) into ASCII files that the SWAN wave model reads.

Grid conventions used throughout this module:
  - SWAN operates in spherical (lat/lon degree) mode.
  - BOTTOM.txt: depth in metres, positive = ocean (below MSL), negative = land.
    CUDEM sign convention is the *opposite* (negative = ocean); this module flips it.
  - WIND.txt: U (geographic east, m/s), V (geographic north, m/s); one block per
    timestep.  HRRR winds are already earth-relative (rotated in hrrr.py).
  - BOUND_SPEC.txt: TPAR parametric boundary spectrum from WW3 scalar wave data.

Nested grid support (T7.2):
  ``build_swan_input`` builds the SWAN command INPUT file for either the outer
  grid (uses hrrr_bbox, contains NESTOUT command, no output points) or the
  inner nest (uses swan_domain_bbox, uses NGRID to read outer boundary, writes
  TABLE output at configured surf spot coordinates).

References:
  - SWAN User Manual v41.45, §5 (input file syntax)
  - PROVIDER-MANUAL §14.14 (HRRR), §14.15 (SWAN+SWAN runner)
  - SWAN-CORRECTIONS-PLAN.md
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _swan_time(dt: datetime) -> str:
    """Format a UTC datetime as SWAN's YYYYMMDD.HHmmss token."""
    return dt.strftime("%Y%m%d.%H%M%S")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string (with optional Z suffix) to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


# ---------------------------------------------------------------------------
# UTM coordinate transformer (for SWAN Cartesian mode)
#
# SWAN SETUP requires Cartesian (metric) coordinates — the radiation stress
# gradient computation needs meters, not degrees.  We convert lon/lat to UTM
# for the SWAN INPUT file and convert Xp/Yp back to lon/lat when parsing
# TABLE output.  No external library needed — UTM is a standard Transverse
# Mercator projection with well-known constants.
# ---------------------------------------------------------------------------

_UTM_K0 = 0.9996
_UTM_A  = 6378137.0        # WGS-84 semi-major axis (m)
_UTM_E  = 0.00669437999014  # WGS-84 first eccentricity squared
_UTM_E2 = _UTM_E / (1 - _UTM_E)


def utm_zone(lon: float) -> int:
    """Return the UTM zone number for a longitude."""
    return int((lon + 180.0) / 6.0) + 1


def lonlat_to_utm(lon: float, lat: float, zone: int | None = None) -> tuple[float, float]:
    """Convert WGS-84 lon/lat to UTM easting/northing (meters).

    Returns (easting, northing).  Northern hemisphere only (our use case).
    """
    if zone is None:
        zone = utm_zone(lon)
    lon0 = (zone - 1) * 6.0 - 180.0 + 3.0  # central meridian
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    lon0_rad = math.radians(lon0)

    N = _UTM_A / math.sqrt(1 - _UTM_E * math.sin(lat_rad) ** 2)
    T = math.tan(lat_rad) ** 2
    C = _UTM_E2 * math.cos(lat_rad) ** 2
    A = math.cos(lat_rad) * (lon_rad - lon0_rad)

    M = _UTM_A * (
        (1 - _UTM_E / 4 - 3 * _UTM_E**2 / 64 - 5 * _UTM_E**3 / 256) * lat_rad
        - (3 * _UTM_E / 8 + 3 * _UTM_E**2 / 32 + 45 * _UTM_E**3 / 1024) * math.sin(2 * lat_rad)
        + (15 * _UTM_E**2 / 256 + 45 * _UTM_E**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * _UTM_E**3 / 3072) * math.sin(6 * lat_rad)
    )

    easting = _UTM_K0 * N * (
        A + (1 - T + C) * A**3 / 6
        + (5 - 18 * T + T**2 + 72 * C - 58 * _UTM_E2) * A**5 / 120
    ) + 500000.0

    northing = _UTM_K0 * (
        M + N * math.tan(lat_rad) * (
            A**2 / 2
            + (5 - T + 9 * C + 4 * C**2) * A**4 / 24
            + (61 - 58 * T + T**2 + 600 * C - 330 * _UTM_E2) * A**6 / 720
        )
    )

    return easting, northing


def utm_to_lonlat(easting: float, northing: float, zone: int) -> tuple[float, float]:
    """Convert UTM easting/northing back to WGS-84 lon/lat.

    Northern hemisphere only.  Returns (lon, lat).
    """
    lon0 = (zone - 1) * 6.0 - 180.0 + 3.0
    e1 = (1 - math.sqrt(1 - _UTM_E)) / (1 + math.sqrt(1 - _UTM_E))
    x = easting - 500000.0
    y = northing

    M = y / _UTM_K0
    mu = M / (_UTM_A * (1 - _UTM_E / 4 - 3 * _UTM_E**2 / 64 - 5 * _UTM_E**3 / 256))

    phi1 = mu + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
    phi1 += (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
    phi1 += (151 * e1**3 / 96) * math.sin(6 * mu)
    phi1 += (1097 * e1**4 / 512) * math.sin(8 * mu)

    N1 = _UTM_A / math.sqrt(1 - _UTM_E * math.sin(phi1) ** 2)
    T1 = math.tan(phi1) ** 2
    C1 = _UTM_E2 * math.cos(phi1) ** 2
    R1 = _UTM_A * (1 - _UTM_E) / (1 - _UTM_E * math.sin(phi1) ** 2) ** 1.5
    D = x / (N1 * _UTM_K0)

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D**2 / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1**2 - 9 * _UTM_E2) * D**4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1**2 - 252 * _UTM_E2 - 3 * C1**2) * D**6 / 720
    )

    lon_rad = (
        D - (1 + 2 * T1 + C1) * D**3 / 6
        + (5 - 2 * C1 + 28 * T1 - 3 * C1**2 + 8 * _UTM_E2 + 24 * T1**2) * D**5 / 120
    ) / math.cos(phi1)

    return math.degrees(lon_rad) + lon0, math.degrees(lat)


def _bilinear_interp(
    grid: list[list[float]],
    lat_first: float,
    lon_first: float,
    lat_last: float,
    lon_last: float,
    nj: int,
    ni: int,
    target_lat: float,
    target_lon: float,
) -> float:
    """Bilinear interpolation of a 2-D scalar grid at (target_lat, target_lon).

    Grid indexing: grid[j][i], j=0 is lat_first (south), j=nj-1 is lat_last (north).
    i=0 is lon_first (west), i=ni-1 is lon_last (east).

    Returns NaN if the target point is outside the grid.
    """
    if nj < 2 or ni < 2:
        return float("nan")

    dlat = (lat_last - lat_first) / (nj - 1)
    dlon = (lon_last - lon_first) / (ni - 1)

    if dlat == 0.0 or dlon == 0.0:
        return float("nan")

    fi = (target_lon - lon_first) / dlon
    fj = (target_lat - lat_first) / dlat

    i0 = int(math.floor(fi))
    j0 = int(math.floor(fj))

    # Out-of-bounds check
    if i0 < 0 or j0 < 0 or i0 >= ni - 1 or j0 >= nj - 1:
        # Clamp to nearest edge for points that are just barely outside the grid
        i0 = max(0, min(i0, ni - 2))
        j0 = max(0, min(j0, nj - 2))
        if (
            target_lon < lon_first - abs(dlon)
            or target_lon > lon_last + abs(dlon)
            or target_lat < lat_first - abs(dlat)
            or target_lat > lat_last + abs(dlat)
        ):
            return float("nan")

    i1 = min(i0 + 1, ni - 1)
    j1 = min(j0 + 1, nj - 1)

    tx = max(0.0, min(1.0, fi - i0))
    ty = max(0.0, min(1.0, fj - j0))

    return (
        grid[j0][i0] * (1 - tx) * (1 - ty)
        + grid[j0][i1] * tx * (1 - ty)
        + grid[j1][i0] * (1 - tx) * ty
        + grid[j1][i1] * tx * ty
    )


def _compute_swan_grid_dims(
    grid_bbox: tuple[float, float, float, float],
    grid_resolution_m: float,
) -> dict[str, Any]:
    """Derive SWAN grid dimensions from a geographic bounding box and resolution.

    Uses a flat-earth (equirectangular) approximation appropriate for coastal
    domains of ≤100 km extent.

    Args:
        grid_bbox: (lon_sw, lat_sw, lon_ne, lat_ne) in degrees.
        grid_resolution_m: Target grid spacing in metres. Default 200 m.

    Returns:
        dict with keys: mxc, myc, dlon, dlat, nx, ny, lon_sw, lat_sw, lon_ne, lat_ne.
        mxc/myc are the number of SWAN grid *intervals* (SWAN convention: mxc+1 points).
    """
    lon_sw, lat_sw, lon_ne, lat_ne = grid_bbox
    mid_lat = (lat_sw + lat_ne) / 2.0
    metres_per_deg_lat = 111_000.0
    metres_per_deg_lon = 111_000.0 * math.cos(math.radians(mid_lat))

    lon_size_deg = lon_ne - lon_sw
    lat_size_deg = lat_ne - lat_sw

    mxc = max(1, round(lon_size_deg * metres_per_deg_lon / grid_resolution_m))
    myc = max(1, round(lat_size_deg * metres_per_deg_lat / grid_resolution_m))

    dlon = lon_size_deg / mxc  # degrees per cell in x
    dlat = lat_size_deg / myc  # degrees per cell in y

    return {
        "mxc": mxc,
        "myc": myc,
        "dlon": dlon,
        "dlat": dlat,
        "nx": mxc + 1,
        "ny": myc + 1,
        "lon_sw": lon_sw,
        "lat_sw": lat_sw,
        "lon_ne": lon_ne,
        "lat_ne": lat_ne,
    }


# ---------------------------------------------------------------------------
# HRRR → SWAN WIND.txt
# ---------------------------------------------------------------------------


def hrrr_to_swan_wind(
    wind_field: dict[str, Any],
    grid_bbox: tuple[float, float, float, float],
    grid_resolution_m: float = 200.0,
) -> tuple[dict[str, Any], str]:
    """Bilinear-interpolate HRRR wind onto the SWAN grid and format as WIND.txt.

    Each HRRR forecast hour becomes one block in WIND.txt.  A block consists of:
      (1) all U-component rows (j=0..myc, each row i=0..mxc values)
      (2) all V-component rows (j=0..myc, each row i=0..mxc values)

    This is SWAN's READINP WIND IDLA=3 (row-major, y increasing) format.

    HRRR grid convention: grid[j][i], j=0 = lat_first (south), i=0 = lon_first (west).
    Longitudes in HRRR output may be in 0..360 range; they are normalised to -180..180
    here before interpolation.

    Args:
        wind_field: Return value of hrrr.fetch().  Must contain key "grids" — a list
            of per-forecast-hour dicts with keys: valid_time (ISO-8601), ni, nj,
            lat_first, lon_first, lat_last, lon_last, u_earth, v_earth.
        grid_bbox: (lon_sw, lat_sw, lon_ne, lat_ne) — SWAN domain in degrees.
        grid_resolution_m: SWAN grid cell size in metres.

    Returns:
        (grid_info, wind_text):
          grid_info — dict with SWAN grid dimensions and valid_times list
                       (used by SWANRunner to build the INPUT command file).
          wind_text — string content of WIND.txt.

    Raises:
        ValueError: wind_field contains no grids.
    """
    grids = wind_field.get("grids", [])
    if not grids:
        raise ValueError("hrrr_to_swan_wind: wind_field contains no forecast grids")

    dims = _compute_swan_grid_dims(grid_bbox, grid_resolution_m)
    lon_sw = dims["lon_sw"]
    lat_sw = dims["lat_sw"]
    nx = dims["nx"]
    ny = dims["ny"]
    dlon = dims["dlon"]
    dlat = dims["dlat"]

    lines: list[str] = []
    valid_times: list[str] = []

    for grid in grids:
        u_earth: list[list[float]] = grid["u_earth"]
        v_earth: list[list[float]] = grid["v_earth"]
        src_nj: int = grid["nj"]
        src_ni: int = grid["ni"]
        src_lat_first: float = float(grid["lat_first"])
        src_lon_first: float = float(grid["lon_first"])
        src_lat_last: float = float(grid["lat_last"])
        src_lon_last: float = float(grid["lon_last"])

        # Normalise HRRR longitudes from 0..360 to -180..180 if needed
        if src_lon_first > 180.0:
            src_lon_first -= 360.0
        if src_lon_last > 180.0:
            src_lon_last -= 360.0

        valid_times.append(grid["valid_time"])

        # Build interpolated U and V blocks (IDLA=3: rows = constant j, values = i)
        u_lines: list[str] = []
        v_lines: list[str] = []

        for j in range(ny):
            target_lat = lat_sw + j * dlat
            u_row: list[str] = []
            v_row: list[str] = []
            for i in range(nx):
                target_lon = lon_sw + i * dlon
                u = _bilinear_interp(
                    u_earth,
                    src_lat_first, src_lon_first,
                    src_lat_last, src_lon_last,
                    src_nj, src_ni,
                    target_lat, target_lon,
                )
                v = _bilinear_interp(
                    v_earth,
                    src_lat_first, src_lon_first,
                    src_lat_last, src_lon_last,
                    src_nj, src_ni,
                    target_lat, target_lon,
                )
                # Replace NaN (out-of-bounds) with 0.0 (calm)
                u_row.append(f"{u:.4f}" if not math.isnan(u) else "0.0000")
                v_row.append(f"{v:.4f}" if not math.isnan(v) else "0.0000")
            u_lines.append(" ".join(u_row))
            v_lines.append(" ".join(v_row))

        # Write U block then V block for this timestep
        lines.extend(u_lines)
        lines.extend(v_lines)

    dims["valid_times"] = valid_times
    return dims, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CUDEM → SWAN BOTTOM.txt
# ---------------------------------------------------------------------------


def cudem_to_swan_bottom(
    depth_grid: dict[str, Any],
    grid_bbox: tuple[float, float, float, float],
    grid_resolution_m: float = 200.0,
) -> tuple[dict[str, Any], str]:
    """Convert a CUDEM depth grid to SWAN BOTTOM.txt with sign flip.

    CUDEM elevation sign convention: negative = ocean (below MSL), positive = land.
    SWAN BOTTOM sign convention:     positive = ocean (depth below MSL), negative = land.
    Conversion: swan_depth = -cudem_depth.

    The output file uses SWAN IDLA=3 (row-major, j=0 = south → j=myc = north),
    with mxc+1 values per row.

    Args:
        depth_grid: dict with keys:
          lat_first, lon_first, lat_last, lon_last — source grid corners (degrees)
          ni — number of source columns (west→east)
          nj — number of source rows (south→north)
          depths — list[list[float]] [nj][ni], CUDEM convention (neg=ocean, pos=land).
          If depths is empty or None, a uniform 15 m ocean depth is assumed.
        grid_bbox: (lon_sw, lat_sw, lon_ne, lat_ne) — SWAN domain.
        grid_resolution_m: SWAN grid resolution in metres.

    Returns:
        (grid_info, bottom_text):
          grid_info — dict with SWAN grid dimensions.
          bottom_text — string content of BOTTOM.txt.
    """
    dims = _compute_swan_grid_dims(grid_bbox, grid_resolution_m)
    lon_sw = dims["lon_sw"]
    lat_sw = dims["lat_sw"]
    nx = dims["nx"]
    ny = dims["ny"]
    dlon = dims["dlon"]
    dlat = dims["dlat"]

    src_depths: list[list[float]] = depth_grid.get("depths") or []
    src_nj: int = depth_grid.get("nj", len(src_depths))
    src_ni: int = depth_grid.get("ni", len(src_depths[0]) if src_depths else 0)
    src_lat_first: float = float(depth_grid.get("lat_first", lat_sw))
    src_lon_first: float = float(depth_grid.get("lon_first", lon_sw))
    src_lat_last: float = float(depth_grid.get("lat_last", dims["lat_ne"]))
    src_lon_last: float = float(depth_grid.get("lon_last", dims["lon_ne"]))

    _DEFAULT_OCEAN_DEPTH_M = 15.0  # used when source grid is unavailable

    lines: list[str] = []
    for j in range(ny):
        target_lat = lat_sw + j * dlat
        row_vals: list[str] = []
        for i in range(nx):
            target_lon = lon_sw + i * dlon
            if src_depths and src_nj > 1 and src_ni > 1:
                cudem_val = _bilinear_interp(
                    src_depths,
                    src_lat_first, src_lon_first,
                    src_lat_last, src_lon_last,
                    src_nj, src_ni,
                    target_lat, target_lon,
                )
                if math.isnan(cudem_val):
                    cudem_val = -_DEFAULT_OCEAN_DEPTH_M  # default ocean if out-of-bounds
            else:
                cudem_val = -_DEFAULT_OCEAN_DEPTH_M

            # Sign flip: CUDEM (neg=ocean) → SWAN (pos=ocean)
            swan_depth = -cudem_val
            row_vals.append(f"{swan_depth:.2f}")
        lines.append(" ".join(row_vals))

    return dims, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# WW3 → SWAN BOUND_SPEC.txt (TPAR format)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-shore transect helper (T3.1)
# ---------------------------------------------------------------------------


def compute_spot_transect(
    spot_lon: float,
    spot_lat: float,
    beach_facing_degrees: float,
    bathymetric_profile: list[dict[str, float]],
    target_deep_m: float = 15.0,
    target_shallow_m: float = 1.0,
    target_spec_m: float = 10.0,
    spacing_m: float = 50.0,
    min_points: int = 10,
    max_points: int = 200,
    *,
    coastline_lat: float | None = None,
    coastline_lon: float | None = None,
    grid_bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Compute the cross-shore CURVE transect for one surf spot.

    The transect extends from the ~``target_deep_m``-depth contour to the
    ~``target_shallow_m``-depth contour, perpendicular to the beach (in the
    ``beach_facing_degrees`` compass direction from shore to ocean).

    Coordinate offsets use a flat-earth equirectangular approximation appropriate
    for coastal domains of ≤30 km extent.

    Args:
        spot_lon: Longitude of the surf spot pin (degrees).
        spot_lat: Latitude of the surf spot pin (degrees).
        beach_facing_degrees: Compass bearing (degrees, clockwise from north)
            pointing from shore toward the ocean.
        bathymetric_profile: Ordered list of dicts with keys "distance_m" (metres
            from the coastline, increasing offshore) and "depth_m" (positive = wet).
            Need not be sorted; this function sorts by distance_m internally.
            When empty or None, default distances (500 m, 300 m, 50 m) are used
            for deep/spec/shallow respectively.
        target_deep_m: Target depth for the offshore (deep) CURVE start (m).
        target_shallow_m: Target depth for the nearshore (shallow) CURVE end (m).
        target_spec_m: Target depth for the SPECOUT single-point output (m).
        spacing_m: Nominal spacing between transect output points (m).
        min_points: Minimum number of CURVE output points (inclusive).
        max_points: Maximum number of CURVE output points (inclusive).
        coastline_lat: Latitude of the actual shoreline (degrees).  When
            provided (from ``download_bidirectional_profile``), coordinate
            offsets are computed FROM this point rather than from the operator's
            pin, so the CURVE transect starts at the true coastline.
        coastline_lon: Longitude of the actual shoreline (degrees).  See
            ``coastline_lat``.
        grid_bbox: When provided as ``(lon_min, lat_min, lon_max, lat_max)``,
            clip the deep-end endpoint to stay within this bounding box.
            Prevents CURVE points from falling outside the Level 3 SWAN grid
            (T23.2).

    Returns:
        dict with keys:
          start_lon, start_lat (float): coordinates of the offshore CURVE endpoint.
          end_lon, end_lat (float): coordinates of the nearshore CURVE endpoint.
          specout_lon, specout_lat (float): coordinates of the ~10 m SPECOUT point.
          n_intervals (int): number of CURVE intervals (total points = n_intervals + 1).
          transect_points (list[dict]): each dict has keys "lon", "lat",
            "depth_m", "distance_m"; ordered from offshore to nearshore.
    """
    # Origin for coordinate offsets — use the coastline when known (bidirectional
    # profile), otherwise fall back to the operator's pin (legacy behaviour).
    origin_lat = coastline_lat if coastline_lat is not None else spot_lat
    origin_lon = coastline_lon if coastline_lon is not None else spot_lon

    meters_per_lat = 111_000.0
    meters_per_lon = 111_000.0 * math.cos(math.radians(origin_lat))
    bearing_rad = math.radians(beach_facing_degrees)

    def _offset(distance_m: float) -> tuple[float, float]:
        """Return (lon, lat) at *distance_m* in the offshore direction from origin."""
        dlon = distance_m * math.sin(bearing_rad) / meters_per_lon
        dlat = distance_m * math.cos(bearing_rad) / meters_per_lat
        return origin_lon + dlon, origin_lat + dlat

    # ---- Derive distances from the bathymetric profile ----
    profile = [p for p in (bathymetric_profile or []) if p.get("depth_m") is not None]
    profile.sort(key=lambda p: p.get("distance_m", 0.0))

    def _dist_at_depth(target_depth: float, fallback: float) -> float:
        """Return distance_m for the point closest to *target_depth*."""
        if not profile:
            return fallback
        # Find the two bracketing entries and linearly interpolate
        best_d = fallback
        best_err = float("inf")
        for idx, pt in enumerate(profile):
            err = abs(pt.get("depth_m", 0.0) - target_depth)
            if err < best_err:
                best_err = err
                best_d = pt.get("distance_m", fallback)
        return float(best_d)

    # Enforce minimum shallow depth above SWAN DEPMIN (0.05 m) to avoid dry CURVE
    # points at the nearshore end (SWAN-FIXES-PLAN T23.2; swan-nesting-reference Q3).
    target_shallow_m = max(target_shallow_m, 0.1)

    d_deep = _dist_at_depth(target_deep_m, 500.0)
    d_spec = _dist_at_depth(target_spec_m, 300.0)
    d_shallow = _dist_at_depth(target_shallow_m, 50.0)

    # Clip deep end to grid bbox if provided (T23.2).
    # Analytically find the largest d_deep such that _offset(d_deep) stays
    # within (lon_min, lat_min, lon_max, lat_max).
    if grid_bbox is not None:
        lon_min, lat_min, lon_max, lat_max = grid_bbox
        dlon_per_m = math.sin(bearing_rad) / meters_per_lon
        dlat_per_m = math.cos(bearing_rad) / meters_per_lat
        d_max = float(d_deep)
        if dlon_per_m > 1e-10:
            d_max = min(d_max, (lon_max - origin_lon) / dlon_per_m)
        elif dlon_per_m < -1e-10:
            d_max = min(d_max, (lon_min - origin_lon) / dlon_per_m)
        if dlat_per_m > 1e-10:
            d_max = min(d_max, (lat_max - origin_lat) / dlat_per_m)
        elif dlat_per_m < -1e-10:
            d_max = min(d_max, (lat_min - origin_lat) / dlat_per_m)
        if d_max < d_deep:
            logger.warning(
                "compute_spot_transect: deep end clipped from %.1f m to %.1f m "
                "to stay within grid bbox %s (spot lon=%.6f lat=%.6f)",
                d_deep, d_max, grid_bbox, spot_lon, spot_lat,
            )
            d_deep = max(d_max, d_shallow + spacing_m)

    # Ensure sensible ordering (deep > spec > shallow)
    if d_deep <= d_shallow:
        d_deep = max(d_shallow + spacing_m * min_points, 100.0)
    if not (d_shallow < d_spec < d_deep):
        d_spec = (d_deep + d_shallow) / 2.0

    # ---- Compute number of intervals ----
    n_points = int(round((d_deep - d_shallow) / spacing_m)) + 1
    n_points = max(min_points, min(max_points, n_points))
    n_intervals = n_points - 1

    # ---- Build transect point list (offshore → nearshore) ----
    transect_points: list[dict[str, Any]] = []
    for k in range(n_points):
        # Linear interpolation from d_deep to d_shallow
        frac = k / max(n_intervals, 1)
        d = d_deep + frac * (d_shallow - d_deep)

        # Interpolate depth from profile at this distance
        depth_at_d = 0.0
        if profile:
            if d <= profile[0].get("distance_m", 0.0):
                depth_at_d = profile[0].get("depth_m", 0.0)
            elif d >= profile[-1].get("distance_m", d):
                depth_at_d = profile[-1].get("depth_m", 0.0)
            else:
                for idx in range(len(profile) - 1):
                    p0 = profile[idx]
                    p1 = profile[idx + 1]
                    d0 = p0.get("distance_m", 0.0)
                    d1 = p1.get("distance_m", 0.0)
                    if d0 <= d <= d1 and d1 > d0:
                        alpha = (d - d0) / (d1 - d0)
                        depth_at_d = p0.get("depth_m", 0.0) * (1 - alpha) + p1.get("depth_m", 0.0) * alpha
                        break
        else:
            # No profile — linearly interpolate depth from assumed values
            depth_at_d = target_deep_m + (target_shallow_m - target_deep_m) * frac

        if depth_at_d <= 0:
            logger.warning(
                "compute_spot_transect: point at distance %.1f m has depth %.2f m "
                "(dry in SWAN) — spot lon=%.6f lat=%.6f",
                d, depth_at_d, spot_lon, spot_lat,
            )

        lon, lat = _offset(d)
        transect_points.append({
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "depth_m": round(depth_at_d, 2),
            "distance_m": round(d, 1),
        })

    start_lon, start_lat = _offset(d_deep)
    end_lon, end_lat = _offset(d_shallow)
    specout_lon, specout_lat = _offset(d_spec)

    return {
        "start_lon": round(start_lon, 6),
        "start_lat": round(start_lat, 6),
        "end_lon": round(end_lon, 6),
        "end_lat": round(end_lat, 6),
        "specout_lon": round(specout_lon, 6),
        "specout_lat": round(specout_lat, 6),
        "n_intervals": n_intervals,
        "transect_points": transect_points,
    }


# ---------------------------------------------------------------------------
# SWAN INPUT command file builder — nested grid support (T7.2)
# ---------------------------------------------------------------------------


def build_swan_input(
    dims: dict[str, Any],
    valid_times: list[str],
    spots: dict[str, tuple[float, float]],
    grid_level: str,
    inner_dims: dict[str, Any] | None = None,
    output_interval_hr: float = 1.0,
    compute_dt_min: int = 10,
    nest_boundary_file: str = "nest_out.dat",
    hotstart_file: str | None = None,
    stationary: bool = False,
    has_wlevel: bool = False,
    has_current: bool = False,
    structures: list[dict] | None = None,
    spot_configs: dict[str, dict] | None = None,
) -> str:
    """Render the SWAN ASCII INPUT command file for a given grid level.

    Two grid levels are supported (PROVIDER-MANUAL §14.15):

    ``"outer"`` — continental shelf approach grid (hrrr_bbox domain).
      - WW3 boundary conditions applied on the west and south sides.
      - ``NESTOUT`` command writes boundary spectra to ``nest_boundary_file``
        for the child level.  ``inner_dims`` must be supplied.
      - No surf spot output points.

    ``"inner"`` — tight nearshore grid (swan_domain_bbox domain).
      - ``BOUNDNEST1`` reads boundary spectra from ``nest_boundary_file``.
      - No WW3 BOUNDSPEC (parent boundary replaces it).
      - ``POINTS`` and ``TABLE`` output commands at all configured surf spots.

    NESTING FILE CONVENTION (SWAN-FIXES-PLAN Bug 1, 2026-07-19):
      The caller must pass DIFFERENT filenames for ``nest_boundary_file`` when
      building outer vs. inner INPUT files.  In a 3-level run, Level 2 is built
      as ``"outer"`` (so NESTOUT writes to this file) and then patched to add
      ``BOUNDNEST1`` (which reads a DIFFERENT file).  If both commands reference
      the same file, SWAN overwrites the parent's boundary data during the run,
      corrupting Level 3's input and producing zero wave energy.

    Args:
        dims: Grid dimension dict for THIS level (mxc, myc, dlon, dlat,
              lon_sw, lat_sw).  Returned by ``hrrr_to_swan_wind`` or
              ``cudem_to_swan_bottom``.
        valid_times: Ordered list of ISO-8601 UTC timestamps (first = t_start,
                     last = t_end).  All intervals assumed 1-hour.
        spots: dict mapping spot_id → (lon, lat).  Used only for the inner
               level's POINTS / TABLE commands.
        grid_level: ``"outer"`` or ``"inner"``.
        inner_dims: Grid dimension dict for the INNER nest; required when
                    ``grid_level == "outer"``.  Used to write the NESTOUT
                    command with the inner nest's geographic extent.
        output_interval_hr: Hours between TABLE output rows (inner only).
        compute_dt_min: SWAN internal time step in minutes.
        nest_boundary_file: Filename for the nesting boundary file.  For
                    ``grid_level == "outer"`` this is the NESTOUT output file;
                    for ``"inner"`` this is the BOUNDNEST1 input file.  The
                    caller MUST pass different values for each level to avoid
                    read/write collision (see SWAN-FIXES-PLAN Bug 1).
        hotstart_file: If set and the file exists in the run directory, add
            ``INIT HOTSTART 'fname'`` and write ``HOTFILE`` after COMPUTE.
        has_wlevel: If True, emit ``INPGRID WLEVEL`` + ``READINP WLEV``
            commands. ``_write_input_files`` must write ``WLEVEL.txt`` when
            this is True.
        has_current: If True, emit ``INPGRID CURRENT`` + ``READINP CURRENT``
            commands. ``_write_input_files`` must write ``CURRENT.txt`` when
            this is True.
        structures: List of structure dicts, each with keys ``type``
            (pier/breakwater/jetty/seawall/groin) and ``coordinates``
            (list of [lon, lat] pairs defining the OBSTACLE LINE).
            When provided, OBSTACLE commands are emitted after the source
            terms section. None/empty → no OBSTACLE commands.
        spot_configs: Optional dict mapping spot_id → config dict with keys
            ``"beach_facing_degrees"`` (float, compass bearing toward ocean) and
            ``"bathymetric_profile"`` (list of {"distance_m", "depth_m"} dicts).
            When provided for the ``"inner"`` level, the existing POINTS + TABLE
            block is replaced with per-spot CURVE + expanded TABLE (HSIGN HSWELL
            TM01 DIR DEPTH QB DISSURF DSPR) + SPECOUT commands (T3.1).
            When None, the existing POINTS + TABLE approach is used unchanged.

    Returns:
        String content of the SWAN INPUT command file.

    Raises:
        ValueError: ``valid_times`` is empty, or ``grid_level == "outer"``
                    without ``inner_dims``.
    """
    if not valid_times:
        raise ValueError("build_swan_input: valid_times is empty")
    if grid_level not in ("outer", "inner"):
        raise ValueError(f"build_swan_input: grid_level must be 'outer' or 'inner', got {grid_level!r}")
    if grid_level == "outer" and inner_dims is None:
        raise ValueError("build_swan_input: inner_dims required for grid_level='outer'")
    if grid_level == "inner" and not spots:
        raise ValueError("build_swan_input: spots must be non-empty for grid_level='inner'")

    t_start = datetime.fromisoformat(valid_times[0].replace("Z", "+00:00")).astimezone(UTC)
    t_end = datetime.fromisoformat(valid_times[-1].replace("Z", "+00:00")).astimezone(UTC)
    swan_t_start = t_start.strftime("%Y%m%d.%H%M%S")
    swan_t_end = t_end.strftime("%Y%m%d.%H%M%S")

    wind_dt_hr = 1  # blended wind field is 1-hour cadence throughout (HRRR + interpolated GFS)
    output_dt_min = int(round(output_interval_hr * 60))

    mxc = dims["mxc"]
    myc = dims["myc"]
    dlon = dims["dlon"]
    dlat = dims["dlat"]
    lon_sw = dims["lon_sw"]
    lat_sw = dims["lat_sw"]

    # UTM projection for Cartesian mode (required for DIFFRACTION stabilization
    # and OBSTACLE coordinate precision — spherical mode not recommended).
    _zone = utm_zone((lon_sw + lon_sw + mxc * dlon) / 2)
    dims["_utm_zone"] = _zone  # stash for output parser

    x_sw, y_sw = lonlat_to_utm(lon_sw, lat_sw, _zone)
    x_ne, y_ne = lonlat_to_utm(lon_sw + mxc * dlon, lat_sw + myc * dlat, _zone)
    xlenc = x_ne - x_sw
    ylenc = y_ne - y_sw
    dx = xlenc / mxc
    dy = ylenc / myc

    lines = [
        "PROJECT 'SWAN' 'v1'",
        "",
        # SET [level] [nor] [depmin] [maxmes] [maxerr] — positional params.
        # MAXERR=3: only stop on severe errors, not warnings or auto-repaired
        # errors (default 1 stops on the boundary-mismatch warning at level 2).
        "SET 0. 90. 0.05 200 3",
        "SET NAUTICAL",
        "COORDINATES CARTESIAN",
        "",
    ]

    if hotstart_file:
        lines.append(f"INIT HOTSTART '{hotstart_file}'")
        lines.append("")

    lines += [
        f"CGRID REG {x_sw:.2f} {y_sw:.2f} 0. {xlenc:.2f} {ylenc:.2f} {mxc} {myc}"
        " CIRCLE 36 0.0418 1.0 31",
        "",
        # BOTTOM grid: static
        f"INPGRID BOTTOM REG {x_sw:.2f} {y_sw:.2f} 0. {mxc} {myc} {dx:.2f} {dy:.2f}",
        "READINP BOTTOM 1. 'BOTTOM.txt' 3 0 FREE",
        "",
        # WIND grid
        (
            f"INPGRID WIND REG {x_sw:.2f} {y_sw:.2f} 0."
            f" {mxc} {myc} {dx:.2f} {dy:.2f}"
            + (f" NONSTAT {swan_t_start} {wind_dt_hr} HR {swan_t_end}" if not stationary else "")
        ),
        "READINP WIND 1. 'WIND.txt' 3 0 FREE",
        "",
    ]

    # WLEVEL (time-varying water level from CO-OPS tidal predictions — T2.1)
    if has_wlevel:
        lines += [
            (
                f"INPGRID WLEVEL REG {x_sw:.2f} {y_sw:.2f} 0."
                f" {mxc} {myc} {dx:.2f} {dy:.2f}"
                + (f" NONSTAT {swan_t_start} {wind_dt_hr} HR {swan_t_end}" if not stationary else "")
            ),
            "READINP WLEV 1. 'WLEVEL.txt' 3 0 FREE",
            "",
        ]

    # CURRENT (time-varying ocean surface currents from OFS — T2.2)
    if has_current:
        lines += [
            (
                f"INPGRID CURRENT REG {x_sw:.2f} {y_sw:.2f} 0."
                f" {mxc} {myc} {dx:.2f} {dy:.2f}"
                + (f" NONSTAT {swan_t_start} {wind_dt_hr} HR {swan_t_end}" if not stationary else "")
            ),
            "READINP CURRENT 1. 'CURRENT.txt' 3 0 FREE",
            "",
        ]

    if grid_level == "outer":
        # WW3 boundary conditions on west and south sides
        lines += [
            "BOUNDSPEC SIDE W CCW CONSTANT FILE 'BOUND_W.txt' 1",
            "BOUNDSPEC SIDE S CCW CONSTANT FILE 'BOUND_S.txt' 1",
            "",
        ]
    else:
        # Inner nest: read boundary spectra from outer grid's NESTOUT file.
        # SWAN 41.51: BOUNDNEST1 NEST 'filename' CLOSED
        # Validated by direct SWAN test on weewx host 2026-07-17.
        lines += [
            f"BOUNDNEST1 NEST '{nest_boundary_file}' CLOSED",
            "",
        ]

    # Source terms — per-level physics selection (T2.1, SWAN-L3-STABILITY-PLAN).
    # SETUP removed from all levels: unsupported in parallel (OpenMP) runs
    # (SWAN User Manual v41.51 p.79) and structurally ill-posed in nested grids
    # (BOUNDNEST1 carries no setup BC; deepest-point anchor is wrong in a nest).
    # DIFFRACTION: removed at L1/L2 (sub-grid; can only destabilize); emitted
    # with smoothing stabilization at L3 only (smnum=27 → εx ≈ 45 m).
    # See docs/reference/swan-commands-extract.md per-level physics table.
    if grid_level == "inner":
        # L3 (10 m surf-zone): stabilized DIFFRACTION.
        # DIFFRACTION 1 0.2 27: idiffr=1, smpar=0.2 (recommended), smnum=27.
        diffraction_cmd: str | None = "DIFFRACTION 1 0.2 27"
    else:
        # L1 (1 km) and L2 (100 m): DIFFRACTION removed — sub-grid resolution.
        diffraction_cmd = None

    logger.info(
        "SWAN %s physics: SETUP=removed (parallel unsupported), DIFFRACTION=%s",
        grid_level,
        diffraction_cmd if diffraction_cmd is not None else "removed",
    )

    _physics: list[str] = [
        "GEN3 WESTHUYSEN",
        "BREAKING CONSTANT 1.0 0.73",
        "FRICTION JON 0.067",
        # T2.4: TRIAD (singular per SWAN user manual) for triad wave-wave
        # interactions in shallow water (Eldeberky 1996 defaults).
        "TRIAD",
    ]
    if diffraction_cmd is not None:
        _physics.append(diffraction_cmd)

    # NUMERIC: under-relaxation for stationary L3 only.  Stabilizes DIFFRACTION
    # in the iterative solver.  "Not meaningful for nonstationary computations"
    # (SWAN User Manual v41.51 p.87) — do NOT emit for nonstationary runs.
    if grid_level == "inner" and stationary:
        _physics.append(
            "NUMERIC STOPC dabs=0.005 drel=0.01 curvat=0.005 npnts=99.5"
            " STAT mxitst=50 alfa=0.01"
        )
        logger.info(
            "SWAN %s: NUMERIC alfa=0.01 emitted (stationary L3)", grid_level
        )

    lines += _physics
    lines += [
        "",
        # Exception values for no-data / dry points (SWAN user manual §3.5).
        # Without this, SWAN uses an implementation-specific default that is
        # hard to distinguish from real near-zero values.  -9.0 is the
        # conventional sentinel used in oceanographic models.
        "QUANTITY HSIGN TM01 DIR excv=-9.",
        "",
    ]

    # OBSTACLE commands — coastal structure transmission/reflection (T2.3).
    # Replaces wave_transform.py Supplement 2 (see ADR-095).  Each structure
    # dict has 'type' (pier/breakwater/jetty/seawall/groin) and 'coordinates'
    # (list of [lon, lat] pairs defining the OBSTACLE LINE).
    _OBSTACLE_PARAMS: dict[str, str] = {
        "pier":       "TRANSM 0.95",
        "breakwater": "DAM DANGremond 2.0 0.5 10.0",
        "jetty":      "DAM GODA 3.0 0.4 0.8",
        "seawall":    "REFL 0.5",
        "groin":      "DAM GODA 2.0 0.4 0.8",
    }
    if structures:
        for structure in structures:
            s_type = str(structure.get("type", "")).lower()
            coords = structure.get("coordinates", [])
            if not coords or s_type not in _OBSTACLE_PARAMS:
                continue
            params = _OBSTACLE_PARAMS[s_type]
            coord_parts = []
            for pt in coords:
                ox, oy = lonlat_to_utm(pt[0], pt[1], _zone)
                coord_parts.append(f"{ox:.2f} {oy:.2f}")
            coord_str = " ".join(coord_parts)
            lines.append(f"OBSTACLE {params} LINE {coord_str}")
        if any(
            structure.get("coordinates")
            and str(structure.get("type", "")).lower() in _OBSTACLE_PARAMS
            for structure in structures
        ):
            lines.append("")

    if grid_level == "outer":
        # SWAN 41.51 nested output: two commands needed.
        #   1. NGRID 'name' xpc ypc alpc xlenc ylenc mxc myc
        #   2. NESTOUT 'name' 'filename' OUTPUT tbegtbl dttbl tunit
        # Validated by direct SWAN test on weewx host 2026-07-17.
        assert inner_dims is not None  # guarded above
        inner_lon_sw = inner_dims["lon_sw"]
        inner_lat_sw = inner_dims["lat_sw"]
        inner_mxc = inner_dims["mxc"]
        inner_myc = inner_dims["myc"]
        inner_dlon = inner_dims["dlon"]
        inner_dlat = inner_dims["dlat"]
        inner_x_sw, inner_y_sw = lonlat_to_utm(inner_lon_sw, inner_lat_sw, _zone)
        inner_x_ne, inner_y_ne = lonlat_to_utm(
            inner_lon_sw + inner_mxc * inner_dlon,
            inner_lat_sw + inner_myc * inner_dlat, _zone,
        )
        inner_xlenc = inner_x_ne - inner_x_sw
        inner_ylenc = inner_y_ne - inner_y_sw
        lines += [
            (
                f"NGRID 'inner'"
                f" {inner_x_sw:.2f} {inner_y_sw:.2f}"
                f" 0.0"
                f" {inner_xlenc:.2f} {inner_ylenc:.2f}"
                f" {inner_mxc} {inner_myc}"
            ),
            (
                f"NESTOUT 'inner' '{nest_boundary_file}'"
                f" OUTPUT {swan_t_start} {output_interval_hr:.0f} HR"
            ),
            "",
        ]
    else:
        # Inner: surf spot output section.
        #
        # T3.1 — when spot_configs is provided, replace the single POINTS +
        # TABLE with a per-spot CURVE (cross-shore transect) + expanded TABLE
        # (HSIGN HSWELL TM01 DIR DEPTH QB DISSURF DSPR) + SPECOUT at
        # the ~10 m transect point.  QUANTITY HSWELL sets the swell frequency
        # cutoff to 0.1 Hz (T > 10 s) before any output commands.
        #
        # When spot_configs is None, fall back to the original POINTS + TABLE
        # approach for backward compatibility.
        if spot_configs:
            lines += [
                "QUANTITY HSWELL fswell=0.1",
                "",
            ]
            logger.info(
                "SWAN TABLE output columns: %s",
                "TIME XP YP HSIGN HSWELL TM01 DIR DEPTH QB DISSURF DSPR",
            )
            spot_order: list[str] = []
            for n, (spot_id, (spot_lon, spot_lat)) in enumerate(spots.items(), start=1):
                cfg = spot_configs.get(spot_id)
                if cfg is None:
                    continue

                _rt = cfg.get("runtime_profile")
                if isinstance(_rt, dict) and "profile" in _rt:
                    _bp = _rt["profile"]
                    _cl = _rt.get("coastline_lat")
                    _cln = _rt.get("coastline_lon")
                else:
                    _bp = cfg.get("bathymetric_profile") or []
                    _cl = None
                    _cln = None

                transect = compute_spot_transect(
                    spot_lon,
                    spot_lat,
                    float(cfg.get("beach_facing_degrees", 0.0)),
                    _bp,
                    coastline_lat=_cl,
                    coastline_lon=_cln,
                )

                spot_order.append(spot_id)
                curve_name = f"CV{n}"    # max 8 chars (CV + up to 6 digits = fine for any realistic N)
                spec_name  = f"SP{n}"   # ditto
                table_file = f"TABLE_{n}.txt"
                spec_file  = f"SPEC_{n}.txt"

                cx1, cy1 = lonlat_to_utm(transect['start_lon'], transect['start_lat'], _zone)
                cx2, cy2 = lonlat_to_utm(transect['end_lon'], transect['end_lat'], _zone)
                sx, sy   = lonlat_to_utm(transect['specout_lon'], transect['specout_lat'], _zone)
                lines += [
                    (
                        f"CURVE '{curve_name}'"
                        f" {cx1:.2f} {cy1:.2f}"
                        f" {transect['n_intervals']}"
                        f" {cx2:.2f} {cy2:.2f}"
                    ),
                    "",
                    (
                        f"TABLE '{curve_name}' HEAD '{table_file}'"
                        f" TIME XP YP HSIGN HSWELL TM01 DIR DEPTH QB DISSURF DSPR"
                        + (f" OUTPUT {swan_t_start} {output_dt_min} MIN" if not stationary else "")
                    ),
                    "",
                    f"POINTS '{spec_name}' {sx:.2f} {sy:.2f}",
                    (
                        f"SPECOUT '{spec_name}' SPEC2D ABS '{spec_file}'"
                        + (f" OUTPUT {swan_t_start} {output_dt_min} MIN" if not stationary else "")
                    ),
                    "",
                ]
        else:
            logger.info(
                "SWAN TABLE output columns: %s",
                "TIME XP YP HSIGN TM01 DIR",
            )
            lines += [
                "POINTS 'SPOTS' FILE 'OUTPUT_POINTS.txt'",
                "",
                (
                    f"TABLE 'SPOTS' HEAD 'OUTPUT_TABLE.txt'"
                    f" TIME XP YP HSIGN TM01 DIR"
                    + (f" OUTPUT {swan_t_start} {output_dt_min} MIN" if not stationary else "")
                ),
                "",
            ]

    # Computation command
    if stationary:
        # SWAN user manual §4.7: "For small domains (< 100 km), a stationary
        # computation is recommended."  Solves the steady-state wave field for
        # the current wind + boundary snapshot — no time-stepping.
        lines.append(f"COMPUTE STAT {swan_t_start}")
    else:
        lines.append(f"COMPUTE NONST {swan_t_start} {compute_dt_min} MIN {swan_t_end}")

    lines.append("HOTFILE 'hotstart.dat'")

    lines += [
        "",
        "STOP",
    ]

    return "\n".join(lines) + "\n"


def ww3_to_swan_boundary(
    ww3_data: dict[str, Any],
    domain_boundary_nodes: list[tuple[float, float]] | None = None,  # noqa: ARG001
) -> str:
    """Write SWAN TPAR parametric boundary spectrum file from WW3 forecast data.

    Converts WW3 scalar wave parameters (Hs, Tp, Dir) into SWAN's TPAR boundary
    spectrum file.  WW3 data is available as scalar parameters (not a full 2-D
    directional spectrum) from wavewatch.fetch(); this function synthesises a
    JONSWAP-shape parametric spectrum with a fixed directional spreading of 30°.

    TPAR file format (SWAN User Manual §5):
      TPAR
      <YYYYMMDD.HHmmss>  <Hs_m>  <Tp_s>  <DIR_deg>  <DSPR_deg>
      ...

    Applied to the INPUT file as:
      BOUND SPEC SIDES W S CCW VARIABLE PAR 'BOUND_SPEC.txt'

    Args:
        ww3_data: Return value of wavewatch.fetch().  Expected keys:
          "forecast" — list of MarineForecastPoint objects (or model_dump() dicts).
          "model_run" — ISO-8601 UTC string of the model run.
        domain_boundary_nodes: Reserved for future 2-D spectrum support — not used
          in this implementation (scalar PAR format does not require node positions).

    Returns:
        Content of BOUND_SPEC.txt (TPAR format).  Empty string if no valid WW3 data.
    """
    forecast = ww3_data.get("forecast", [])
    _DEFAULT_DSPR_DEG = 30.0

    lines = ["TPAR"]

    for pt in forecast:
        # Accept both MarineForecastPoint objects and plain dicts (from cache)
        if hasattr(pt, "time"):
            t_raw = pt.time
            hs = pt.waveHeight
            tp = pt.wavePeriod
            mwd = pt.waveDirection
            # Prefer swell parameters (lower-frequency energy that dominates breaking)
            if pt.swellHeight is not None and pt.swellPeriod is not None:
                hs = pt.swellHeight
                tp = pt.swellPeriod
                mwd = pt.swellDirection if pt.swellDirection is not None else mwd
        else:
            t_raw = pt.get("time", "")
            hs = pt.get("waveHeight")
            tp = pt.get("wavePeriod")
            mwd = pt.get("waveDirection")
            if pt.get("swellHeight") is not None and pt.get("swellPeriod") is not None:
                hs = pt.get("swellHeight", hs)
                tp = pt.get("swellPeriod", tp)
                mwd = pt.get("swellDirection", mwd)

        # Skip rows with missing required parameters
        if not t_raw or hs is None or tp is None:
            continue

        try:
            dt = _parse_iso(t_raw)
            swan_t = _swan_time(dt)
        except (ValueError, AttributeError):
            continue

        dir_val = float(mwd) if mwd is not None else 270.0  # default: westerly

        lines.append(
            f"{swan_t}  {float(hs):.3f}  {float(tp):.3f}  {dir_val:.1f}  {_DEFAULT_DSPR_DEG:.1f}"
        )

    # Return empty string if only the header line is present (no data rows)
    if len(lines) <= 1:
        return ""

    return "\n".join(lines) + "\n"
