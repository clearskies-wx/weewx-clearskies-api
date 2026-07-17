"""SWAN input file format writers for the TruShore nearshore wave model pipeline.

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
  - PROVIDER-MANUAL §14.14 (HRRR), §14.15 (SWAN+TruShore runner)
  - SWAN-TRUSHORE-PLAN.md T2.3, T7.2
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _swan_time(dt: datetime) -> str:
    """Format a UTC datetime as SWAN's YYYYMMDD.HHmmss token."""
    return dt.strftime("%Y%m%d.%H%M%S")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string (with optional Z suffix) to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


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
    nest_boundary_file: str = "nest_boundary.dat",
) -> str:
    """Render the SWAN ASCII INPUT command file for a given grid level.

    Two grid levels are supported (PROVIDER-MANUAL §14.15):

    ``"outer"`` — continental shelf approach grid (hrrr_bbox domain).
      - WW3 boundary conditions applied on the west and south sides.
      - ``NESTOUT`` command written before ``COMPUTE`` to write boundary
        spectra for the inner nest.  ``inner_dims`` must be supplied.
      - No surf spot output points.

    ``"inner"`` — tight nearshore grid (swan_domain_bbox domain).
      - ``NGRID`` command reads outer boundary from ``nest_boundary_file``.
      - No WW3 BOUNDSPEC (outer boundary replaces it).
      - ``POINTS`` and ``TABLE`` output commands at all configured surf spots.

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
        nest_boundary_file: Filename for NESTOUT / NGRID boundary data.

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
    xlenc = mxc * dlon
    ylenc = myc * dlat

    lines = [
        "PROJECT 'TruShore' 'v1'",
        "",
        "SET LEVEL 0.",
        "SET NAUTICAL",
        "COORDINATES SPHERICAL",
        "",
        f"CGRID REG {lon_sw:.6f} {lat_sw:.6f} 0. {xlenc:.6f} {ylenc:.6f} {mxc} {myc}"
        " CIRCLE 36 0.0418 1.0 31",
        "",
        # BOTTOM grid: static
        f"INPGRID BOTTOM REG {lon_sw:.6f} {lat_sw:.6f} 0. {mxc} {myc} {dlon:.6f} {dlat:.6f}",
        "READINP BOTTOM 1. 'BOTTOM.txt' 3 0 FREE",
        "",
        # WIND grid: non-stationary 1-hour intervals
        (
            f"INPGRID WIND REG {lon_sw:.6f} {lat_sw:.6f} 0."
            f" {mxc} {myc} {dlon:.6f} {dlat:.6f}"
            f" NONSTAT {swan_t_start} {wind_dt_hr} HR {swan_t_end}"
        ),
        "READINP WIND 1. 'WIND.txt' 3 0 FREE",
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
        # SWAN 41.51: NGRID 'name' 'filename' reads nesting data written by
        # the outer grid's NESTOUT command.
        lines += [
            f"NGRID 'outer' '{nest_boundary_file}'",
            "",
        ]

    # Source terms (same for both levels)
    lines += [
        "GEN3 WESTHUYSEN",
        "BREAKING CONSTANT 1.0 0.73",
        "FRICTION JON 0.067",
        "TRIADS",
        "DIFFRACTION",
        "",
    ]

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
        inner_xlenc = inner_mxc * inner_dlon
        inner_ylenc = inner_myc * inner_dlat
        lines += [
            (
                f"NGRID 'inner'"
                f" {inner_lon_sw:.6f} {inner_lat_sw:.6f}"
                f" 0.0"
                f" {inner_xlenc:.6f} {inner_ylenc:.6f}"
                f" {inner_mxc} {inner_myc}"
            ),
            (
                f"NESTOUT 'inner' '{nest_boundary_file}'"
                f" OUTPUT {swan_t_start} {output_interval_hr:.0f} HR"
            ),
            "",
        ]
    else:
        # Inner: surf spot output points and TABLE output
        lines += [
            "POINTS 'SPOTS' FILE 'OUTPUT_POINTS.txt'",
            "",
            (
                f"TABLE 'SPOTS' HEAD 'OUTPUT_TABLE.txt'"
                f" XP YP HS TM01 DIR OUTPUT {swan_t_start} {output_dt_min} MIN"
            ),
            "",
        ]

    # Non-stationary computation (same for both levels)
    lines += [
        f"COMPUTE NONST {swan_t_start} {compute_dt_min} MIN {swan_t_end}",
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
