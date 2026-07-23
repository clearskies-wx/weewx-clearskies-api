"""SWAN nearshore wave model subprocess orchestrator.

Two execution modes:
  - Legacy 2-level: run() — outer grid (~3km) + inner nest (~200m).
  - 3-level nesting: run_3level() — Level 1 (1km) + Level 2 (100m) + Level 3 (10m).

The 3-level mode (SWAN-FIXES-PLAN Phase 14c) uses physics-based domain sizing
from swan_domain.py and per-level CUDEM bathymetry. NESTOUT flows:
  Level 1 → NESTOUT → Level 2 (BOUNDNEST1 + NESTOUT) → Level 3 (BOUNDNEST1).

Responsibilities:
  1. Blend HRRR (hours 0–48) and GFS (hours 48–72) into uniform hourly wind.
  2. Write per-level SWAN input files (BOTTOM, WIND, boundary, INPUT).
  3. Spawn SWAN subprocess per level, handle hotstart persistence.
  4. Parse Level 3 TABLE output into MarineForecastPoints keyed by spot.

References:
  - PROVIDER-MANUAL.md §14.15 (SWAN runner)
  - PROVIDER-MANUAL.md §14.16 (GFS wind provider)
  - SWAN-FIXES-PLAN.md (Phases 1-16)
  - ADR-093 (SWAN nearshore model)
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from weewx_clearskies_api.metrics import (
    SWAN_CONVERGENCE_FAILURES_TOTAL,
    SWAN_LAST_RUN_VALID_FRACTION,
)
from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_formats import (
    build_swan_input,
    compute_spot_transect,
    cudem_to_swan_bottom,
    hrrr_to_swan_wind,
    ww3_to_swan_boundary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_15m_point(
    spot_lon: float,
    spot_lat: float,
    beach_facing_degrees: float,
    bathymetric_profile: Any = None,
    fallback_km: float = 2.5,
) -> tuple[float, float]:
    """Return the (lon, lat) of the ~15 m depth reference point offshore.

    Used by T3.3 to locate the deep-water reference SPECOUT point for each
    surf spot.  Checks the cached bathymetric profile first; falls back to
    projecting ``fallback_km`` offshore along ``beach_facing_degrees``.

    Args:
        spot_lon: Representative longitude of the surf spot.
        spot_lat: Representative latitude of the surf spot.
        beach_facing_degrees: Compass bearing (0=N, 90=E) from shore toward
            the open ocean.
        bathymetric_profile: Optional profile data.  Accepted shapes:
            - list of dicts with ``distance_m`` and ``depth_m`` keys (the
              standard CUDEM profile format stored in _spot_configs).
            - dict with a ``"profile"`` key whose value is the list above.
        fallback_km: Distance to project offshore when no profile is available
            or the profile does not reach 15 m depth (default 2.5 km).

    Returns:
        (lon, lat) of the ~15 m depth point.
    """
    bearing_rad = math.radians(beach_facing_degrees)
    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(spot_lat))

    # Resolve profile data to a list of {distance_m, depth_m} dicts.
    profile_data: list[dict] | None = None
    if isinstance(bathymetric_profile, dict):
        profile_data = bathymetric_profile.get("profile")
    elif isinstance(bathymetric_profile, list):
        profile_data = bathymetric_profile

    if profile_data:
        for point in sorted(profile_data, key=lambda p: float(p.get("distance_m", 0))):
            if float(point.get("depth_m", 0)) >= 15.0:
                dist_km = float(point["distance_m"]) / 1000.0
                return (
                    spot_lon + dist_km * math.sin(bearing_rad) / km_per_deg_lon,
                    spot_lat + dist_km * math.cos(bearing_rad) / km_per_deg_lat,
                )

    # Fallback: 2.5 km offshore along beach_facing_degrees
    return (
        spot_lon + fallback_km * math.sin(bearing_rad) / km_per_deg_lon,
        spot_lat + fallback_km * math.cos(bearing_rad) / km_per_deg_lat,
    )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SWANRunError(Exception):
    """Raised when the SWAN subprocess exits non-zero or times out.

    Attributes:
        stderr: The captured standard error output from the SWAN process.
        returncode: Process exit code (None if process timed out).
    """

    def __init__(self, message: str, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Validation thresholds (PROVIDER-MANUAL §14.15)
# ---------------------------------------------------------------------------

# SWAN exception value — set via QUANTITY ... excv=-9. in the INPUT file
# (SWAN user manual §3.5).  Points with this value are dry land or have
# no spectral energy.  Any value <= this sentinel is treated as no-data.
_SWAN_EXCEPTION_VALUE = -9.0

# Upper sanity bounds — values above these indicate numerical instability,
# not physical waves.  Lower bounds are not applied because SWAN legitimately
# produces very small Hs and short Tm01 for weak wind-sea conditions.
_HS_MAX_M = 25.0
_TM_MAX_S = 35.0


def _is_valid_point(hs: float, tm01: float, mwd: float) -> bool:
    """Reject SWAN exception values and numerical instabilities.

    Does NOT reject small-but-real values — SWAN can produce Hs=0.01m and
    Tm01=0.5s for weak wind-sea, and these are physically valid output.
    """
    if math.isnan(hs) or math.isnan(tm01) or math.isnan(mwd):
        return False
    if hs <= _SWAN_EXCEPTION_VALUE or tm01 <= _SWAN_EXCEPTION_VALUE:
        return False
    if hs > _HS_MAX_M or tm01 > _TM_MAX_S:
        return False
    return True


# ---------------------------------------------------------------------------
# Shared helpers (also duplicated in swan_formats for that module's use)
# ---------------------------------------------------------------------------


def _swan_time(dt: datetime) -> str:
    """Format a UTC datetime as SWAN's YYYYMMDD.HHmmss token."""
    return dt.strftime("%Y%m%d.%H%M%S")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string (with optional Z suffix) to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


# ---------------------------------------------------------------------------
# TABLE output parser
# ---------------------------------------------------------------------------


def _parse_table_output(
    table_text: str,
    spots: dict[str, tuple[float, float]],
    coord_tolerance_deg: float = 0.002,
) -> dict[str, list[MarineForecastPoint]]:
    """Parse a SWAN TABLE (HEAD) output file into MarineForecastPoints per spot.

    Column positions are discovered from the SWAN-generated header line rather
    than being hardcoded.  The header line starts with '%' and contains tokens
    like Time, Xp, Yp, Hs, Tm01, Dir.  Units lines (second % line) are skipped.

    Data rows:
      - Column 0:  Time (YYYYMMDD.HHmmss)
      - Xp column: longitude of output point (°)
      - Yp column: latitude of output point (°)
      - Hs column: significant wave height (m)
      - Tm01:      mean wave period (s)
      - Dir:       mean wave direction (° nautical)

    Each row is matched to a spot by comparing (Xp, Yp) against known spot
    coordinates with a tolerance of coord_tolerance_deg.

    Points failing physical validation (_is_valid_point) are silently dropped.

    Args:
        table_text: Content of OUTPUT_TABLE.txt.
        spots: dict mapping spot_id → (lon, lat).
        coord_tolerance_deg: Maximum coordinate difference to match a row to a spot.

    Returns:
        dict[spot_id, list[MarineForecastPoint]] — may be empty if no valid rows.
    """
    result: dict[str, list[MarineForecastPoint]] = {sid: [] for sid in spots}
    total_rows_per_spot: dict[str, int] = {sid: 0 for sid in spots}
    valid_rows_per_spot: dict[str, int] = {sid: 0 for sid in spots}

    col_idx: dict[str, int] = {}
    header_found = False

    for raw_line in table_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("%"):
            # SWAN TABLE HEAD format (user manual §3.5): multiple % lines.
            # The column-name line contains tokens like Xp, Yp, Hsig, Tm01, Dir.
            # Blank % lines, the "Run:" line, and the units line are skipped.
            tokens = line.lstrip("%").split()
            if not header_found and tokens and tokens[0].upper() not in ("RUN:V1", "[DEGR]", "[M]", "[SEC]"):
                # Heuristic: first % line with alphabetic tokens that aren't
                # the Run: label or unit labels is the column header.
                if any(t.upper() in ("XP", "YP", "HSIG", "HSIGN", "HS", "TM01", "DIR", "TIME") for t in tokens):
                    col_idx = {tok.upper(): idx for idx, tok in enumerate(tokens)}
                    header_found = True
            continue

        if not header_found:
            continue

        # Data row
        parts = line.split()
        if not parts:
            continue

        # Resolve required column indices from the SWAN TABLE header.
        # SWAN output quantity names (user manual §3.5 / BLOCK/TABLE docs):
        #   HSIGN → header "Hsig", TIME → header "Time", TM01, DIR, XP, YP.
        # All lookups are uppercased.  Accept "HSIG" as a fallback for HSIGN.
        try:
            i_time = col_idx.get("TIME")
            i_xp = col_idx["XP"]
            i_yp = col_idx["YP"]
            i_hs = col_idx.get("HSIG", col_idx.get("HSIGN", col_idx.get("HS")))
            if i_hs is None:
                raise KeyError("HSIG/HSIGN/HS")
            i_tm = col_idx["TM01"]
            i_dir = col_idx["DIR"]
        except KeyError:
            if header_found and col_idx:
                logger.warning(
                    "SWAN TABLE: missing required column — have %s",
                    list(col_idx.keys()),
                )
            continue

        # T3.2 — optional extended columns (present in CURVE TABLE, absent in
        # old POINTS TABLE).  Accepted name variants for SWAN header tokens.
        i_hswell  = col_idx.get("HSWELL")
        i_depth   = col_idx.get("DEPTH")
        i_qb      = col_idx.get("QB")
        i_dissurf = col_idx.get("DISSURF") or col_idx.get("DISS")
        i_setup   = col_idx.get("SETUP")
        i_dspr    = col_idx.get("DSPR")

        max_idx = max(i_xp, i_yp, i_hs, i_tm, i_dir)
        for _opt in (i_time, i_hswell, i_depth, i_qb, i_dissurf, i_setup, i_dspr):
            if _opt is not None:
                max_idx = max(max_idx, _opt)
        if len(parts) < max_idx + 1:
            continue

        try:
            xp = float(parts[i_xp])
            yp = float(parts[i_yp])
            hs = float(parts[i_hs])
            tm01 = float(parts[i_tm])
            mwd = float(parts[i_dir])
            time_token = parts[i_time] if i_time is not None else None
        except (ValueError, IndexError):
            continue

        def _opt_float(idx: int | None) -> float | None:
            if idx is None or idx >= len(parts):
                return None
            try:
                v = float(parts[idx])
                # Treat SWAN exception value as no-data
                return None if v <= _SWAN_EXCEPTION_VALUE else v
            except (ValueError, TypeError):
                return None

        hswell   = _opt_float(i_hswell)
        depth    = _opt_float(i_depth)
        qb       = _opt_float(i_qb)
        dissurf  = _opt_float(i_dissurf)
        setup_v  = _opt_float(i_setup)
        dspr     = _opt_float(i_dspr)

        # Match (Xp, Yp) to a spot first so we can count per-spot totals.
        matched_id: str | None = None
        for spot_id, (spot_lon, spot_lat) in spots.items():
            if (
                abs(xp - spot_lon) <= coord_tolerance_deg
                and abs(yp - spot_lat) <= coord_tolerance_deg
            ):
                matched_id = spot_id
                break

        if matched_id is None:
            logger.warning(
                "SWAN output unmatched coord: Xp=%.4f Yp=%.4f (spots=%s, tol=%.4f)",
                xp, yp,
                {sid: (slon, slat) for sid, (slon, slat) in spots.items()},
                coord_tolerance_deg,
            )
            continue

        total_rows_per_spot[matched_id] += 1

        # Physical validation — log at WARNING temporarily for T7.5 debugging
        if not _is_valid_point(hs, tm01, mwd):
            logger.warning(
                "SWAN output rejected: Xp=%.4f Yp=%.4f Hs=%.2f Tm01=%.2f Dir=%.1f "
                "(time=%s)",
                xp, yp, hs, tm01, mwd, time_token,
            )
            continue

        # Convert SWAN time token YYYYMMDD.HHmmss → ISO-8601
        if time_token is not None:
            try:
                date_part, time_part = time_token.split(".")
                iso = (
                    f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                    f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
                )
            except (ValueError, IndexError):
                continue
        else:
            # TIME column missing — should not happen since we request it,
            # but handle gracefully.
            logger.warning("SWAN TABLE row has no TIME column — skipping")
            continue

        valid_rows_per_spot[matched_id] += 1

        result[matched_id].append(
            MarineForecastPoint(
                time=iso,
                waveHeight=round(hs, 2),
                wavePeriod=round(tm01, 1),
                waveDirection=round(mwd, 1),
                swellHeight=round(hswell, 2) if hswell is not None else None,
                depth=round(depth, 2) if depth is not None else None,
                breakingFraction=round(qb, 4) if qb is not None else None,
                breakingDissipation=round(dissurf, 2) if dissurf is not None else None,
                setup=round(setup_v, 3) if setup_v is not None else None,
                directionalSpread=round(dspr, 1) if dspr is not None else None,
            )
        )

    for sid in spots:
        total = total_rows_per_spot[sid]
        valid = valid_rows_per_spot[sid]
        logger.info(
            "SWAN TABLE spot %s: %d/%d valid points (%.0f%%)",
            sid, valid, total,
            100 * valid / max(total, 1),
        )

    return result


def _parse_transect_table(
    table_text: str,
    spot_id: str,
    transect_points: list[dict],
    coord_tolerance_deg: float = 0.003,
    utm_zone: int | None = None,
) -> list[MarineForecastPoint]:
    """Parse a single-spot CURVE TABLE file (T3.2).

    Similar to _parse_table_output but for a per-spot TABLE file where all
    rows belong to *spot_id*.  Transect points are matched by (Xp, Yp) to
    retrieve depth_m and distance_m metadata.

    Args:
        table_text: Content of the per-spot TABLE file (e.g. TABLE_1.txt).
        spot_id: The surf spot this file was written for.
        transect_points: list of dicts from compute_spot_transect(), each with
            keys "lon", "lat", "depth_m", "distance_m".
        coord_tolerance_deg: Tolerance for matching Xp/Yp to transect points.
            When utm_zone is set, this is interpreted as meters (use ~200).
        utm_zone: When set, Xp/Yp in TABLE output are UTM meters (Cartesian
            mode). Transect lookup is built in UTM; tolerance in meters.

    Returns:
        list[MarineForecastPoint] — one per valid row; may contain multiple
        points per timestep (one per transect output point).
    """
    from weewx_clearskies_api.services.swan_formats import lonlat_to_utm  # noqa: PLC0415

    # Build fast lookup: (x, y) → (depth_m, distance_m)
    # When utm_zone is set, keys are UTM (meters); otherwise lon/lat (degrees).
    transect_lookup: dict[tuple[float, float], tuple[float, float]] = {}
    for tp in transect_points:
        if utm_zone is not None:
            tx, ty = lonlat_to_utm(float(tp["lon"]), float(tp["lat"]), utm_zone)
            key = (round(tx, 1), round(ty, 1))
        else:
            key = (round(float(tp["lon"]), 4), round(float(tp["lat"]), 4))
        transect_lookup[key] = (float(tp.get("depth_m", 0.0)), float(tp.get("distance_m", 0.0)))

    coord_tol = 200.0 if utm_zone is not None else coord_tolerance_deg

    raw_lines = table_text.splitlines()
    logger.debug(
        "SWAN CURVE TABLE %r: %d raw lines",
        spot_id, len(raw_lines),
    )

    col_idx: dict[str, int] = {}
    header_found = False
    results: list[MarineForecastPoint] = []
    total_rows = 0
    valid_rows = 0

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("%"):
            tokens = line.lstrip("%").split()
            if not header_found and tokens and tokens[0].upper() not in ("RUN:V1", "[DEGR]", "[M]", "[SEC]"):
                if any(t.upper() in ("XP", "YP", "HSIG", "HSIGN", "HS", "TM01", "DIR", "TIME") for t in tokens):
                    col_idx = {tok.upper(): idx for idx, tok in enumerate(tokens)}
                    header_found = True
            continue

        if not header_found:
            continue

        parts = line.split()
        if not parts:
            continue

        try:
            i_time = col_idx.get("TIME")
            i_xp = col_idx["XP"]
            i_yp = col_idx["YP"]
            i_hs = col_idx.get("HSIG", col_idx.get("HSIGN", col_idx.get("HS")))
            if i_hs is None:
                raise KeyError("HSIG/HSIGN/HS")
            i_tm = col_idx["TM01"]
            i_dir = col_idx["DIR"]
        except KeyError:
            continue

        i_hswell  = col_idx.get("HSWELL")
        i_depth   = col_idx.get("DEPTH")
        i_qb      = col_idx.get("QB")
        i_dissurf = col_idx.get("DISSURF") or col_idx.get("DISS")
        i_setup   = col_idx.get("SETUP")
        i_dspr    = col_idx.get("DSPR")

        max_idx = max(i_xp, i_yp, i_hs, i_tm, i_dir)
        for _opt in (i_time, i_hswell, i_depth, i_qb, i_dissurf, i_setup, i_dspr):
            if _opt is not None:
                max_idx = max(max_idx, _opt)
        if len(parts) < max_idx + 1:
            continue

        try:
            xp = float(parts[i_xp])
            yp = float(parts[i_yp])
            hs = float(parts[i_hs])
            tm01 = float(parts[i_tm])
            mwd = float(parts[i_dir])
            time_token = parts[i_time] if i_time is not None else None
        except (ValueError, IndexError):
            continue

        total_rows += 1

        def _opt_val(idx: int | None) -> float | None:
            if idx is None or idx >= len(parts):
                return None
            try:
                v = float(parts[idx])
                return None if v <= _SWAN_EXCEPTION_VALUE else v
            except (ValueError, TypeError):
                return None

        hswell   = _opt_val(i_hswell)
        depth_swan = _opt_val(i_depth)  # SWAN DEPTH column
        qb       = _opt_val(i_qb)
        dissurf  = _opt_val(i_dissurf)
        setup_v  = _opt_val(i_setup)
        dspr     = _opt_val(i_dspr)

        if not _is_valid_point(hs, tm01, mwd):
            continue

        valid_rows += 1

        if time_token is None:
            continue
        try:
            date_part, time_part = time_token.split(".")
            iso = (
                f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
            )
        except (ValueError, IndexError):
            continue

        # Determine depth and distance from transect lookup (prefer TABLE DEPTH
        # column; fall back to transect interpolation).
        # In Cartesian mode, Xp/Yp are UTM meters; round accordingly.
        if utm_zone is not None:
            xp_rounded = round(xp, 1)
            yp_rounded = round(yp, 1)
        else:
            xp_rounded = round(xp, 4)
            yp_rounded = round(yp, 4)

        depth_m: float | None = depth_swan
        distance_m: float | None = None

        # Search transect lookup within tolerance
        best_match_dist = float("inf")
        for (tp_x, tp_y), (tp_depth, tp_dist) in transect_lookup.items():
            d = math.sqrt((xp_rounded - tp_x) ** 2 + (yp_rounded - tp_y) ** 2)
            if d < coord_tol and d < best_match_dist:
                best_match_dist = d
                if depth_m is None:
                    depth_m = tp_depth
                distance_m = tp_dist

        results.append(
            MarineForecastPoint(
                time=iso,
                waveHeight=round(hs, 2),
                wavePeriod=round(tm01, 1),
                waveDirection=round(mwd, 1),
                swellHeight=round(hswell, 2) if hswell is not None else None,
                depth=round(depth_m, 2) if depth_m is not None else None,
                breakingFraction=round(qb, 4) if qb is not None else None,
                breakingDissipation=round(dissurf, 2) if dissurf is not None else None,
                setup=round(setup_v, 3) if setup_v is not None else None,
                directionalSpread=round(dspr, 1) if dspr is not None else None,
                distanceFromShore=round(distance_m, 1) if distance_m is not None else None,
            )
        )

    logger.info(
        "SWAN TABLE spot %s: %d/%d valid points (%.0f%%)",
        spot_id, valid_rows, total_rows,
        100 * valid_rows / max(total_rows, 1),
    )

    return results


# ---------------------------------------------------------------------------
# SWANRunner
# ---------------------------------------------------------------------------
# WLEVEL.txt and CURRENT.txt writers (T2.1, T2.2)
# ---------------------------------------------------------------------------


def _interp_tide_to_wind_times(
    tide_predictions: list[dict],
    wind_times_iso: list[str],
) -> list[float]:
    """Linear-interpolate CO-OPS tide heights onto wind forecast timesteps.

    Args:
        tide_predictions: List of dicts with "time" (ISO-8601) and "height" (m).
        wind_times_iso: Ordered list of ISO-8601 UTC strings (one per wind timestep).

    Returns:
        List of tidal heights (m) at each wind timestep, same length as
        wind_times_iso.  Returns 0.0 for timesteps outside the tide range.
    """
    if not tide_predictions:
        return [0.0] * len(wind_times_iso)

    # Parse and sort tide predictions
    parsed: list[tuple[float, float]] = []  # (unix_s, height_m)
    for pt in tide_predictions:
        t_str = pt.get("time", "")
        h = pt.get("height")
        if not t_str or h is None:
            continue
        try:
            dt = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(UTC)
            parsed.append((dt.timestamp(), float(h)))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])

    if not parsed:
        return [0.0] * len(wind_times_iso)

    result: list[float] = []
    for ts_iso in wind_times_iso:
        try:
            wt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(UTC)
            wt_s = wt.timestamp()
        except (ValueError, TypeError):
            result.append(0.0)
            continue

        # Find bracketing tide predictions
        if wt_s <= parsed[0][0]:
            result.append(parsed[0][1])
        elif wt_s >= parsed[-1][0]:
            result.append(parsed[-1][1])
        else:
            # Linear interpolation between the two surrounding predictions
            for i in range(len(parsed) - 1):
                t0, h0 = parsed[i]
                t1, h1 = parsed[i + 1]
                if t0 <= wt_s <= t1:
                    alpha = (wt_s - t0) / (t1 - t0) if t1 != t0 else 0.0
                    result.append(h0 + alpha * (h1 - h0))
                    break
            else:
                result.append(0.0)

    return result


def _write_wlevel_txt(
    tide_predictions: list[dict],
    wind_times_iso: list[str],
    mxc: int,
    myc: int,
) -> str:
    """Build WLEVEL.txt content for SWAN READINP WLEV (IDLA=3, FREE format).

    One block per wind timestep.  Each block is (myc+1) rows × (mxc+1) columns,
    all cells set to the same tidal elevation (meters, positive up from the
    DEM's native datum (must match BOTTOM)).
    Tides are treated as spatially uniform across the domain — valid for the
    ~30km inner nest where the tidal gradient is negligible compared to the
    forecast uncertainty.

    Args:
        tide_predictions: CO-OPS tidal predictions (time, height dicts).
        wind_times_iso: Ordered ISO-8601 timesteps (one per SWAN wind grid).
        mxc: Number of SWAN grid intervals in x (mxc+1 columns).
        myc: Number of SWAN grid intervals in y (myc+1 rows).

    Returns:
        String content of WLEVEL.txt.  Empty string if tide interpolation
        produces no data (caller skips WLEVEL input in that case).
    """
    heights = _interp_tide_to_wind_times(tide_predictions, wind_times_iso)
    if not heights:
        return ""

    nx = mxc + 1  # number of columns (x points)
    ny = myc + 1  # number of rows (y points)

    lines: list[str] = []
    for h in heights:
        h_str = f"{h:.4f}"
        # Each row: nx identical values
        row = " ".join([h_str] * nx)
        # Write ny rows with the same value
        for _ in range(ny):
            lines.append(row)

    return "\n".join(lines) + "\n"


def _write_wlevel_grid_txt(grids: list[list[list[float]]]) -> str:
    """Build WLEVEL.txt from pre-computed 2D grids (one per timestep).

    Used when setup_profile is provided — each cell receives tide + setup(x, y)
    instead of a uniform tide value.  Writes in SWAN IDLA=3 convention:
    south-to-north rows (j=0 = southernmost), west-to-east columns (i=0 = westernmost),
    one row per file line.

    Args:
        grids: List of 2D grids indexed grids[timestep][j][i].  j=0 is the
               southernmost row; i=0 is the westernmost column.  Each value is
               the total water level at that grid point (m, positive up from datum).

    Returns:
        String content of WLEVEL.txt ready to write to disk.
        Returns empty string if the list is empty.
    """
    if not grids:
        return ""
    lines: list[str] = []
    for grid in grids:
        for row in grid:
            lines.append(" ".join(f"{v:.4f}" for v in row))
    return "\n".join(lines) + "\n"


def _write_current_txt(
    ofs_currents: list[dict],
    wind_times_iso: list[str],
    mxc: int,
    myc: int,
) -> str:
    """Build CURRENT.txt content for SWAN READINP CURRENT (IDLA=3, FREE format).

    One block per wind timestep.  Each block = U grid (myc+1 rows × mxc+1 cols)
    followed immediately by V grid (same dims).  U = east component (m/s),
    V = north component (m/s).

    ofs_currents entries are matched to wind timesteps by the closest available
    OFS timestep within a 2-hour window.  When no OFS timestep is available
    within 2 hours, that wind timestep gets zero currents (calm).

    Args:
        ofs_currents: List of dicts, each with:
            "time" — ISO-8601 UTC string of the OFS timestep.
            "u_grid" — list[list[float]] shape (ny, nx), east current m/s.
            "v_grid" — list[list[float]] shape (ny, nx), north current m/s.
        wind_times_iso: Ordered ISO-8601 timesteps (one per SWAN wind grid).
        mxc: SWAN grid intervals in x.
        myc: SWAN grid intervals in y.

    Returns:
        String content of CURRENT.txt.  Empty string if ofs_currents is empty.
    """
    if not ofs_currents:
        return ""

    nx = mxc + 1
    ny = myc + 1

    # Parse OFS timesteps
    ofs_parsed: list[tuple[float, list[list[float]], list[list[float]]]] = []
    for entry in ofs_currents:
        t_str = entry.get("time", "")
        u_grid = entry.get("u_grid")
        v_grid = entry.get("v_grid")
        if not t_str or u_grid is None or v_grid is None:
            continue
        try:
            dt = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(UTC)
            ofs_parsed.append((dt.timestamp(), u_grid, v_grid))
        except (ValueError, TypeError):
            continue
    ofs_parsed.sort(key=lambda x: x[0])

    if not ofs_parsed:
        return ""

    _ZERO_ROW = " ".join(["0.0000"] * nx)
    _ZERO_BLOCK: list[str] = [_ZERO_ROW] * ny

    def _nearest_ofs(wt_s: float) -> tuple[list[list[float]], list[list[float]]] | None:
        """Return (u_grid, v_grid) for the OFS entry closest to wt_s (within 2h)."""
        best_diff = float("inf")
        best_u: list[list[float]] | None = None
        best_v: list[list[float]] | None = None
        for ofs_t, u, v in ofs_parsed:
            diff = abs(ofs_t - wt_s)
            if diff < best_diff:
                best_diff = diff
                best_u = u
                best_v = v
        if best_diff > 7200:  # > 2 hours → no match
            return None
        return best_u, best_v  # type: ignore[return-value]

    lines: list[str] = []
    for ts_iso in wind_times_iso:
        try:
            wt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(UTC)
            wt_s = wt.timestamp()
        except (ValueError, TypeError):
            lines.extend(_ZERO_BLOCK)  # U block
            lines.extend(_ZERO_BLOCK)  # V block
            continue

        match = _nearest_ofs(wt_s)
        if match is None:
            lines.extend(_ZERO_BLOCK)
            lines.extend(_ZERO_BLOCK)
            continue

        u_grid, v_grid = match

        # U block: ny rows × nx cols
        for j in range(ny):
            if j < len(u_grid):
                row = u_grid[j]
                vals = [f"{row[i]:.4f}" if i < len(row) else "0.0000" for i in range(nx)]
            else:
                vals = ["0.0000"] * nx
            lines.append(" ".join(vals))

        # V block: ny rows × nx cols
        for j in range(ny):
            if j < len(v_grid):
                row = v_grid[j]
                vals = [f"{row[i]:.4f}" if i < len(row) else "0.0000" for i in range(nx)]
            else:
                vals = ["0.0000"] * nx
            lines.append(" ".join(vals))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------


class SWANRunner:
    """Orchestrates a two-level nested SWAN run for one set of input fields.

    Two sequential SWAN runs per cycle (PROVIDER-MANUAL §14.15):
      1. Outer grid (continental shelf approach, ~200km × 150km, 2–3 km resolution)
         — propagates WW3 swell across the shelf; writes NESTOUT boundary files.
      2. Inner nest (tight nearshore, ~20–30km × 10–15km, 200–500m resolution)
         — reads outer boundary via NGRID; outputs TABLE at surf spot coordinates.

    Config keys (all required unless marked optional):
      outer_bbox (list[float]):       [lon_sw, lat_sw, lon_ne, lat_ne] for outer grid.
                                      Falls back to ``domain_bbox`` if absent (compat).
      inner_bbox (list[float]):       [lon_sw, lat_sw, lon_ne, lat_ne] for inner nest.
      surf_spots (dict):              {spot_id: {"lon": float, "lat": float}}
      swan_binary (str):              Path to the SWAN executable.
      outer_grid_resolution_km (float, opt):  Outer grid resolution in km (default 3.0).
      inner_nest_resolution_m (float, opt):   Inner nest resolution in m (default 200.0).
      compute_dt_min (int, opt):      SWAN time step in minutes (default 10).
      output_interval_hr (float, opt): TABLE output interval in hours (default 1).
      swan_timeout_s (int, opt):      Subprocess timeout seconds (default 900).
      omp_num_threads (int, opt):     OpenMP thread cap; 0 = all cores (default 0).

    Usage:
        runner = SWANRunner(config)
        results = runner.run(hrrr_wind_field, gfs_wind_field, ww3_boundary, cudem_bathymetry)

    run() returns dict[spot_id, list[MarineForecastPoint]].
    When gfs_wind_field is None a shortened forecast (HRRR hours only) is produced.
    """

    # ── SWAN nesting file convention ──
    # In a 3-level nested run (Level 1 → Level 2 → Level 3), the middle level
    # must BOTH read boundary spectra from its parent (BOUNDNEST1) AND write
    # boundary spectra for its child (NESTOUT).  SWAN reads BOUNDNEST1 data
    # progressively throughout the simulation; if NESTOUT writes to the SAME
    # file, it corrupts the data BOUNDNEST1 is still reading.
    #
    # Convention:
    #   _NEST_INPUT_FILE  — the file BOUNDNEST1 reads (copied from parent's NESTOUT output)
    #   _NESTOUT_FILE     — the file NESTOUT writes (new data for the child level)
    #
    # The runner copies parent_dir/_NESTOUT_FILE → child_dir/_NEST_INPUT_FILE
    # so the filenames never collide within a single SWAN working directory.
    #
    # See: SWAN User Manual §4.5.5 (BOUNDNEST1), §4.7 (NESTOUT).
    # See: SWAN-FIXES-PLAN.md Bug 1 (2026-07-19).
    _NEST_INPUT_FILE = "nest_in.dat"
    _NESTOUT_FILE = "nest_out.dat"
    _HOTSTART_FILE = "hotstart.dat"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

        # Outer grid bbox — use outer_bbox, fall back to domain_bbox for compat
        outer_raw = config.get("outer_bbox") or config.get("domain_bbox")
        if outer_raw is None:
            raise ValueError("SWANRunner: config requires 'outer_bbox' (or 'domain_bbox')")
        self._outer_bbox: tuple[float, float, float, float] = tuple(outer_raw)  # type: ignore[assignment]

        # Inner nest bbox — required for nested mode
        inner_raw = config.get("inner_bbox")
        if inner_raw is None:
            raise ValueError("SWANRunner: config requires 'inner_bbox'")
        self._inner_bbox: tuple[float, float, float, float] = tuple(inner_raw)  # type: ignore[assignment]

        self._surf_spots: dict[str, tuple[float, float]] = {
            sid: (float(sdata["lon"]), float(sdata["lat"]))
            for sid, sdata in config["surf_spots"].items()
        }
        self._swan_binary: str = config["swan_binary"]

        # Resolution — outer in km converted to m; inner in m
        outer_km = float(config.get("outer_grid_resolution_km", 3.0))
        self._outer_resolution_m: float = outer_km * 1000.0
        self._inner_resolution_m: float = float(config.get("inner_nest_resolution_m", 200.0))

        self._compute_dt_min: int = int(config.get("compute_dt_min", 10))
        self._output_interval_hr: float = float(config.get("output_interval_hr", 1.0))
        self._swan_timeout_s: int = int(config.get("swan_timeout_s", 900))
        # omp_num_threads: 0 = let OpenMP use all available cores (default).
        # Positive integer = cap SWAN CPU usage to that many threads.
        self._omp_num_threads: int = int(config.get("omp_num_threads", 0))

        # T3.1 — per-spot configs for cross-shore transect (beach_facing_degrees,
        # bathymetric_profile).  When provided, the inner grid uses CURVE output
        # instead of POINTS.  None = old POINTS + TABLE behaviour.
        self._spot_configs: dict[str, dict] = config.get("spot_configs") or {}

        # T3.3 — populated by _parse_output() with per-spot SPECOUT spectral
        # decomposition results.  Accessed by the caller after run_with_tmpdir().
        self._spectral_results: dict[str, list[dict]] = {}

        # T4.1 — Convergence gate config (SWAN-L3-STABILITY-PLAN Phase 4).
        # convergence_retry=False (default/current): fail loudly, preserve the
        #   failed working directory untouched for debugging, no retry, no hotstart
        #   save, API continues to serve the last-good run.
        # convergence_retry=True (future): degradation ladder (Phase 4 TODO below).
        self._convergence_retry: bool = bool(config.get("convergence_retry", False))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        hrrr_wind_field: dict[str, Any],
        gfs_wind_field: dict[str, Any] | None,
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
    ) -> dict[str, list[MarineForecastPoint]]:
        """Execute a two-level nested SWAN run and return per-spot wave forecasts.

        Steps:
          1. Blend HRRR + GFS wind fields into a 72-hour (or shortened) uniform
             hourly wind input via _stitch_wind().
          2. Create a temporary working directory with outer/ and inner/ subdirs.
          3. Run the outer grid (shelf approach): writes NESTOUT boundary data.
          4. Run the inner nest (nearshore): reads NESTOUT via NGRID; writes TABLE.
          5. Parse inner nest OUTPUT_TABLE.txt and return per-spot results.

        The temporary directory is NOT deleted on failure so that operators can
        inspect SWAN's Errfile and PRINT output in outer/ and inner/.

        Args:
            hrrr_wind_field: Return value of hrrr.fetch(). Provides hours 0–48.
            gfs_wind_field:  Return value of gfs.fetch(). Provides hours 48–72.
                             When None, the forecast is shortened to HRRR hours only.
            ww3_boundary:    Return value of wavewatch.fetch().
            cudem_bathymetry: Dict with keys lat_first, lon_first, lat_last,
                              lon_last, ni, nj, depths (list[list[float]]).
            tide_predictions: CO-OPS tidal predictions for the forecast period
                              (list of dicts with "time" ISO-8601 and "height" meters).
                              When provided, WLEVEL.txt is written and INPGRID WLEVEL
                              is included in the SWAN INPUT file.  None → no WLEVEL.
            ofs_currents: OFS surface current data (list of dicts with "time",
                          "u_grid" and "v_grid" arrays per timestep).
                          When provided, CURRENT.txt is written.  None → no CURRENT.
            structures: List of structure dicts (keys: "type", "coordinates") for
                        OBSTACLE commands.  None/empty → no OBSTACLE commands.

        Returns:
            dict[spot_id, list[MarineForecastPoint]] — empty list for spots
            where all output rows failed physical validation.

        Raises:
            SWANRunError: A SWAN subprocess exited with non-zero status or timed out.
            ValueError:   Required config keys are missing or input data is empty.
        """
        blended_wind = self._stitch_wind(hrrr_wind_field, gfs_wind_field)
        tmpdir = Path(tempfile.mkdtemp(prefix="swan_run_"))
        logger.info("SWAN nested run starting in %s", tmpdir)
        self._run_outer_grid(
            tmpdir, blended_wind, ww3_boundary, cudem_bathymetry,
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )
        return self._run_inner_nest(
            tmpdir, blended_wind, cudem_bathymetry,
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    def _stitch_wind(
        self,
        hrrr_wind_field: dict[str, Any],
        gfs_wind_field: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Blend HRRR (hours 0–48) and GFS (hours 48–72) into a uniform hourly wind field.

        HRRR grids are at 1-hour intervals (hours 0–48, 49 grids on extended cycles).
        GFS grids are at 3-hour intervals (hours 48–72, 9 grids: f048, f051, ..., f072).
        GFS grids are linearly interpolated to 1-hour resolution per PROVIDER-MANUAL §14.16
        so the combined wind field is uniform hourly (73 grids total for the full 72-hour
        forecast).

        When gfs_wind_field is None the blended wind equals the HRRR field unchanged
        (shortened forecast — HRRR hours only).

        Returns:
            dict with key ``"grids"`` containing the combined grid list.
        """
        hrrr_grids = list(hrrr_wind_field.get("grids", []))
        if not hrrr_grids:
            raise ValueError("_stitch_wind: HRRR wind field has no grids")

        if gfs_wind_field is None:
            logger.warning("_stitch_wind: gfs_wind_field is None — producing shortened forecast (HRRR hours only)")
            return {"grids": hrrr_grids}

        gfs_grids = list(gfs_wind_field.get("grids", []))
        if not gfs_grids:
            logger.warning("_stitch_wind: GFS wind field has empty grids — producing shortened forecast (HRRR hours only)")
            return {"grids": hrrr_grids}

        # Interpolate GFS from 3-hourly to hourly.
        # HRRR covers hours 0–48 (last grid at hour 48).
        # GFS starts at hour 48; we want hours 49–72 at 1-hour intervals.
        # For each consecutive pair of GFS grids (t0, t1 = 3 hours later):
        #   - t0 itself maps to a time already in HRRR (or the last HRRR grid) — skip for i=0
        #   - generate intermediate hourly grids between t0 and t1
        #   - add t1 only at the end (the last GFS grid)

        interpolated_gfs: list[dict[str, Any]] = []

        for i in range(len(gfs_grids) - 1):
            g0 = gfs_grids[i]
            g1 = gfs_grids[i + 1]
            t0 = _parse_iso(g0["valid_time"])
            t1 = _parse_iso(g1["valid_time"])
            dt_hours = (t1 - t0).total_seconds() / 3600.0
            n_steps = max(1, int(round(dt_hours)))

            # For i=0, g0 is GFS hour 48 which is already the last HRRR grid —
            # skip it to avoid duplicating hour 48 in the combined field.
            if i > 0:
                interpolated_gfs.append(g0)

            # Interpolate intermediate hourly steps between g0 and g1
            if n_steps > 1:
                u0: list[list[float]] = g0["u_earth"]
                v0: list[list[float]] = g0["v_earth"]
                u1: list[list[float]] = g1["u_earth"]
                v1: list[list[float]] = g1["v_earth"]
                nj: int = g0["nj"]
                ni: int = g0["ni"]

                for step in range(1, n_steps):
                    alpha = step / n_steps
                    t_interp = t0 + timedelta(hours=step)
                    u_interp = [
                        [u0[j][k] * (1.0 - alpha) + u1[j][k] * alpha for k in range(ni)]
                        for j in range(nj)
                    ]
                    v_interp = [
                        [v0[j][k] * (1.0 - alpha) + v1[j][k] * alpha for k in range(ni)]
                        for j in range(nj)
                    ]
                    interpolated_gfs.append({
                        **g0,  # copy bbox metadata (lat_first, lon_first, lat_last, lon_last, ni, nj)
                        "valid_time": t_interp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "u_earth": u_interp,
                        "v_earth": v_interp,
                    })

        # Always add the last GFS grid (hour 72)
        interpolated_gfs.append(gfs_grids[-1])

        combined_grids = hrrr_grids + interpolated_gfs
        logger.info(
            "_stitch_wind: combined %d HRRR + %d interpolated-GFS = %d total grids",
            len(hrrr_grids), len(interpolated_gfs), len(combined_grids),
        )
        return {"grids": combined_grids}

    def _run_outer_grid(
        self,
        tmpdir: Path,
        blended_wind: dict[str, Any],
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
    ) -> None:
        """Run the outer SWAN grid (continental shelf approach domain).

        Writes SWAN input files to tmpdir/outer/, spawns SWAN, and produces
        nest_out.dat in tmpdir/outer/ for the inner nest to consume.

        Args:
            tmpdir: Root temporary directory for this SWAN run.
            blended_wind: Stitched HRRR+GFS wind field from _stitch_wind().
            ww3_boundary: Return value of wavewatch.fetch().
            cudem_bathymetry: CUDEM depth grid dict.
            tide_predictions: CO-OPS tidal predictions (passed to _write_input_files).
            ofs_currents: OFS surface currents (passed to _write_input_files).
            structures: Coastal structures for OBSTACLE commands.
        """
        outer_dir = tmpdir / "outer"
        outer_dir.mkdir(exist_ok=True)
        grid_info = self._write_input_files(
            outer_dir, blended_wind, ww3_boundary, cudem_bathymetry, "outer",
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )
        logger.info(
            "SWAN outer grid: %d×%d cells at %.1f km resolution in %s",
            grid_info["mxc"], grid_info["myc"],
            self._outer_resolution_m / 1000.0, outer_dir,
        )
        self._spawn_swan_with_hotstart_retry(outer_dir, "outer")
        self._save_hotstart(outer_dir, "outer")
        logger.info("SWAN outer grid complete")

    def _run_inner_nest(
        self,
        tmpdir: Path,
        blended_wind: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
    ) -> dict[str, list[MarineForecastPoint]]:
        """Run the inner nested SWAN grid (tight nearshore domain around surf spots).

        Copies nest_out.dat from tmpdir/outer/ into tmpdir/inner/ as nest_in.dat,
        writes SWAN input files, spawns SWAN, and parses TABLE output at surf spots.

        Args:
            tmpdir: Root temporary directory (must contain outer/ with nest_out.dat).
            blended_wind: Stitched HRRR+GFS wind field.
            cudem_bathymetry: CUDEM depth grid dict.
            tide_predictions: CO-OPS tidal predictions (passed to _write_input_files).
            ofs_currents: OFS surface currents (passed to _write_input_files).
            structures: Coastal structures for OBSTACLE commands.

        Returns:
            dict[spot_id, list[MarineForecastPoint]]
        """
        inner_dir = tmpdir / "inner"
        inner_dir.mkdir(exist_ok=True)

        # Copy outer NESTOUT output → inner BOUNDNEST1 input (different filenames).
        src = tmpdir / "outer" / self._NESTOUT_FILE
        dst = inner_dir / self._NEST_INPUT_FILE
        if src.exists():
            shutil.copy2(str(src), str(dst))
        else:
            logger.warning(
                "SWAN inner nest: outer grid did not produce %s — inner nest will run without nesting",
                self._NESTOUT_FILE,
            )

        grid_info = self._write_input_files(
            inner_dir, blended_wind, {}, cudem_bathymetry, "inner",
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )
        logger.info(
            "SWAN inner nest: %d×%d cells at %.0f m resolution in %s",
            grid_info["mxc"], grid_info["myc"],
            self._inner_resolution_m, inner_dir,
        )
        self._spawn_swan_with_hotstart_retry(inner_dir, "inner")
        self._save_hotstart(inner_dir, "inner")
        logger.info("SWAN inner nest complete")
        return self._parse_output(inner_dir, grid_info)

    def run_stationary_inner(
        self,
        tmpdir: Path,
        wind_field: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> dict[str, list[MarineForecastPoint]]:
        """Run a stationary SWAN computation on the inner nest only (legacy 2-level).

        Uses the most recent nest_out.dat from the last full run
        (expected at tmpdir/outer/nest_out.dat).  Produces a single-timestep
        snapshot of the nearshore wave field with the latest wind.

        NOTE: For 3-level runs, use run_stationary_level3() instead — this
        method uses the old 2-level paths (outer/inner).

        Per SWAN manual §4.7: "For small domains (< 100 km), a stationary
        computation is recommended."  Inner nest is ~20km.
        """
        inner_dir = tmpdir / "inner"
        inner_dir.mkdir(exist_ok=True)

        # Copy outer NESTOUT output → inner BOUNDNEST1 input (different filenames).
        src = tmpdir / "outer" / self._NESTOUT_FILE
        dst = inner_dir / self._NEST_INPUT_FILE
        if src.exists():
            shutil.copy2(str(src), str(dst))
        else:
            logger.warning(
                "SWAN stationary: no %s from outer grid — "
                "running without nesting (quick update may be inaccurate)",
                self._NESTOUT_FILE,
            )

        grid_info = self._write_input_files(
            inner_dir, wind_field, {}, cudem_bathymetry, "inner",
            stationary=True,
        )
        logger.info(
            "SWAN stationary inner: %d×%d cells at %.0f m resolution",
            grid_info["mxc"], grid_info["myc"],
            self._inner_resolution_m,
        )
        self._spawn_swan_with_hotstart_retry(inner_dir, "inner")
        self._save_hotstart(inner_dir, "inner")
        logger.info("SWAN stationary inner complete")
        return self._parse_output(inner_dir, grid_info)

    # ------------------------------------------------------------------
    # 3-Level Nested Execution (Phase 14c — SWAN-FIXES-PLAN)
    # ------------------------------------------------------------------

    def run_3level(
        self,
        domains: "DomainSizing",
        bathymetry: dict[str, Any],
        hrrr_wind_field: dict[str, Any],
        gfs_wind_field: dict[str, Any] | None,
        ww3_boundary: dict[str, Any],
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
        setup_profiles_by_spot: dict[str, list[dict]] | None = None,
    ) -> dict[str, list["MarineForecastPoint"]]:
        """Execute a 3-level nested SWAN run and return per-spot wave forecasts.

        Levels:
          1. Coarse (1 km) — propagates WW3 across continental shelf
          2. Nearshore (100 m) — refraction/shoaling to 30m depth
          3. Surf zone (10 m) — per cluster, breaking/sandbars to shore

        NESTOUT file flow:
          Level 1 → nest_l2.dat → Level 2
          Level 2 → nest_l3_{idx}.dat → Level 3 (per cluster)

        Args:
            domains: DomainSizing from swan_domain.compute_domains().
            bathymetry: {"level1": grid, "level2": grid, "level3": {idx: grid}}
                from download_all_bathymetry().
            hrrr_wind_field: HRRR wind field (hours 0-48).
            gfs_wind_field: GFS wind field (hours 48-72), or None.
            ww3_boundary: WaveWatch III boundary spectra.
            tide_predictions: CO-OPS tidal predictions (optional).
            ofs_currents: OFS surface currents (optional).
            structures: Coastal structures for OBSTACLE (optional).
            setup_profiles_by_spot: Wave-setup profiles keyed by spot_id, from
                wave_setup.compute_setup_profile().  When provided, L3 WLEVEL grids
                contain tide + analytic setup (Stage 2).  None or missing spot →
                uniform tide fallback (Stage 1, same as previous behaviour).

        Returns:
            dict[spot_id, list[MarineForecastPoint]]
        """
        from weewx_clearskies_api.services.swan_domain import DomainSizing

        blended_wind = self._stitch_wind(hrrr_wind_field, gfs_wind_field)

        # Use persistent working dir (not tmpdir) so hotstart files survive between runs
        workdir = Path("/var/run/weewx-clearskies/swan")
        workdir.mkdir(parents=True, exist_ok=True)
        logger.info("SWAN 3-level run starting in %s", workdir)

        # --- Level 1: Coarse grid (1 km) ---
        l1_dir = workdir / "level1"
        l1_dir.mkdir(exist_ok=True)
        l1_bbox = (
            domains.level1.lon_min, domains.level1.lat_min,
            domains.level1.lon_max, domains.level1.lat_max,
        )
        l2_bbox = (
            domains.level2.lon_min, domains.level2.lat_min,
            domains.level2.lon_max, domains.level2.lat_max,
        )

        # Level 1's NESTOUT must target Level 2's domain
        saved_inner_bbox = self._inner_bbox
        saved_inner_res = self._inner_resolution_m
        self._inner_bbox = l2_bbox
        self._inner_resolution_m = domains.level2.resolution_m

        l1_grid = self._write_input_files(
            l1_dir, blended_wind, ww3_boundary,
            bathymetry.get("level1", {}), "outer",
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
            override_bbox=l1_bbox,
            override_resolution_m=domains.level1.resolution_m,
        )

        self._inner_bbox = saved_inner_bbox
        self._inner_resolution_m = saved_inner_res

        logger.info(
            "SWAN L1: %d×%d cells at 1 km in %s",
            l1_grid["mxc"], l1_grid["myc"], l1_dir,
        )
        self._spawn_swan_with_hotstart_retry(l1_dir, "level1")
        if not self._check_convergence(l1_dir, "level1"):
            # Failed workdir preserved untouched; API continues to serve last-good run.
            return {}
        self._save_hotstart(l1_dir, "level1")
        logger.info("SWAN Level 1 complete")

        # --- Level 2: Nearshore grid (100 m) ---
        # Level 2 must BOTH read L1's boundary AND write NESTOUT for L3.
        # Strategy: run as "outer" (writes NESTOUT), then patch the INPUT file
        # to replace BOUNDSPEC SIDE with BOUNDNEST1 (reads L1's output).
        l2_dir = workdir / "level2"
        l2_dir.mkdir(exist_ok=True)
        # Copy L1 NESTOUT output → L2 BOUNDNEST1 input.
        # CRITICAL: these MUST be different filenames.  Level 2 runs as "outer"
        # (writes NESTOUT to _NESTOUT_FILE) and is patched to add BOUNDNEST1
        # (reads _NEST_INPUT_FILE).  If both pointed at the same file, SWAN
        # would overwrite the L1 boundary data it was still reading, producing
        # a corrupted boundary file and zero wave energy in Level 3.
        # See SWAN-FIXES-PLAN.md Bug 1 (2026-07-19).
        l1_nest = l1_dir / self._NESTOUT_FILE
        l2_nest_in = l2_dir / self._NEST_INPUT_FILE
        if l1_nest.exists():
            shutil.copy2(str(l1_nest), str(l2_nest_in))
        else:
            logger.warning("SWAN L2: Level 1 did not produce %s — running without nesting", self._NESTOUT_FILE)

        # Temporarily override self._inner_bbox so the NESTOUT command targets
        # the union of all Level 3 cluster bboxes (SWAN interpolates to each
        # Level 3 grid's boundary from this single NESTOUT file).
        l3_bboxes = [
            c.grid for c in domains.level3_clusters if c.grid is not None
        ]
        if l3_bboxes:
            l3_union_bbox = (
                min(g.lon_min for g in l3_bboxes),
                min(g.lat_min for g in l3_bboxes),
                max(g.lon_max for g in l3_bboxes),
                max(g.lat_max for g in l3_bboxes),
            )
            # Use the finest resolution among L3 grids for NESTOUT dimensions
            l3_res = l3_bboxes[0].resolution_m
        else:
            l3_union_bbox = l2_bbox
            l3_res = 10.0

        saved_inner_bbox = self._inner_bbox
        saved_inner_res = self._inner_resolution_m
        self._inner_bbox = l3_union_bbox
        self._inner_resolution_m = l3_res

        l2_grid = self._write_input_files(
            l2_dir, blended_wind, {}, bathymetry.get("level2", {}), "outer",
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
            override_bbox=l2_bbox,
            override_resolution_m=domains.level2.resolution_m,
        )

        self._inner_bbox = saved_inner_bbox
        self._inner_resolution_m = saved_inner_res

        # Patch INPUT: replace BOUNDSPEC SIDE lines with BOUNDNEST1
        input_path = l2_dir / "INPUT"
        input_text = input_path.read_text(encoding="ascii")
        patched_lines = []
        for line in input_text.splitlines():
            if line.startswith("BOUNDSPEC SIDE"):
                continue  # Remove BOUNDSPEC SIDE commands
            patched_lines.append(line)
        # Insert BOUNDNEST1 before the first GEN3 line
        final_lines = []
        boundnest_inserted = False
        for line in patched_lines:
            if line.startswith("GEN3") and not boundnest_inserted:
                final_lines.append(
                    f"BOUNDNEST1 NEST '{self._NEST_INPUT_FILE}' CLOSED"
                )
                final_lines.append("")
                boundnest_inserted = True
            final_lines.append(line)
        input_path.write_text("\n".join(final_lines) + "\n", encoding="ascii")

        # T3.3 — Insert deep-water reference SPECOUT (DWR) commands for each
        # surf spot into the L2 INPUT file.  Each SPECOUT is placed at the
        # ~15 m depth point along the spot's beach_facing_degrees bearing.
        # Parse COMPUTE line for OUTPUT timing (nonstationary runs only).
        if self._surf_spots:
            from weewx_clearskies_api.services.swan_formats import lonlat_to_utm  # noqa: PLC0415
            l2_input_text = input_path.read_text(encoding="ascii")
            _dwr_tbeg: str | None = None
            _dwr_is_nonstat = False
            for _line in l2_input_text.splitlines():
                stripped = _line.strip()
                if stripped.startswith("COMPUTE NONSTATIONARY"):
                    _parts = stripped.split()
                    if len(_parts) >= 3:
                        _dwr_tbeg = _parts[2]
                    _dwr_is_nonstat = True
                    break
            _output_dt_min = int(self._output_interval_hr * 60)
            _l2_utm_zone = l2_grid.get("_utm_zone")

            _dwr_lines: list[str] = []
            for _n, (_sid, (_slon, _slat)) in enumerate(
                self._surf_spots.items(), start=1
            ):
                _dwr_name = f"DWR{_n}"  # max 8 chars; supports up to DWR9999
                _spec_file = f"SPEC_DWR_{_n}.txt"
                _cfg = (self._spot_configs or {}).get(_sid, {})
                _bfacing = float(_cfg.get("beach_facing_degrees", 270.0))
                _profile = _cfg.get("bathymetric_profile") or _cfg.get("runtime_profile")
                _p_lon, _p_lat = _compute_15m_point(
                    _slon, _slat, _bfacing, _profile
                )
                _sx, _sy = lonlat_to_utm(_p_lon, _p_lat, _l2_utm_zone)
                _dwr_lines.append(f"POINTS '{_dwr_name}' {_sx:.2f} {_sy:.2f}")
                _specout = f"SPECOUT '{_dwr_name}' SPEC2D ABS '{_spec_file}'"
                if _dwr_is_nonstat and _dwr_tbeg:
                    _specout += f" OUTPUT {_dwr_tbeg} {_output_dt_min} MIN"
                _dwr_lines.append(_specout)
                _dwr_lines.append("")

            _dwr_block = "\n".join(_dwr_lines)
            # Insert before the first COMPUTE line (must follow NESTOUT)
            _patched = re.sub(
                r"(\nCOMPUTE\b)",
                "\n" + _dwr_block + r"\1",
                l2_input_text,
                count=1,
            )
            if _patched != l2_input_text:
                input_path.write_text(_patched, encoding="ascii")
                logger.debug(
                    "SWAN L2: inserted %d DWR SPECOUT command(s) before COMPUTE",
                    len(self._surf_spots),
                )
            else:
                logger.warning(
                    "SWAN L2: DWR SPECOUT insertion failed — COMPUTE line not found in INPUT"
                )

        logger.info(
            "SWAN L2: %d×%d cells at 100 m in %s (NESTOUT→L3, BOUNDNEST1←L1)",
            l2_grid["mxc"], l2_grid["myc"], l2_dir,
        )
        self._spawn_swan_with_hotstart_retry(l2_dir, "level2")
        if not self._check_convergence(l2_dir, "level2"):
            # Failed workdir preserved untouched; API continues to serve last-good run.
            return {}
        self._save_hotstart(l2_dir, "level2")

        # T3.3 — Parse L2 DWR SPECOUT files and populate self._spectral_results
        # as the baseline for all spots.  L3 runs will override per-cluster spots
        # with L3 handoff SPECOUT data; spots that skip L3 keep the DWR entry.
        if self._surf_spots:
            from weewx_clearskies_api.services.swan_spectral import (  # noqa: PLC0415
                parse_and_decompose,
                parse_specout_file,
            )
            for _n, _sid in enumerate(self._surf_spots.keys(), start=1):
                _dwr_path = l2_dir / f"SPEC_DWR_{_n}.txt"
                if _dwr_path.exists():
                    _dwr_text = _dwr_path.read_text(encoding="utf-8", errors="replace")
                    _raw_spectra = parse_specout_file(_dwr_text)
                    _decomposed = parse_and_decompose(_dwr_path)
                    _decomp_by_t = {
                        e.get("time", ""): e.get("components", [])
                        for e in _decomposed
                    }
                    _entries: list[dict] = [
                        {
                            "time": _sp.get("time", ""),
                            "freqs_hz": _sp.get("freqs_hz", []),
                            "dirs_deg": _sp.get("dirs_deg", []),
                            "energy": _sp.get("energy", []),
                            "components": _decomp_by_t.get(_sp.get("time", ""), []),
                        }
                        for _sp in _raw_spectra
                    ]
                    if _entries:
                        self._spectral_results[_sid] = _entries
                        logger.debug(
                            "SWAN DWR SPECOUT: spot %r → %d timestep(s) from L2",
                            _sid, len(_entries),
                        )
                else:
                    logger.debug(
                        "SWAN DWR SPECOUT: %s not found for spot %r",
                        _dwr_path.name, _sid,
                    )

        logger.info("SWAN Level 2 complete")

        # --- Level 3: Surf zone grids (10 m) — per cluster ---
        all_results: dict[str, list[MarineForecastPoint]] = {}

        for idx, cluster in enumerate(domains.level3_clusters):
            if cluster.grid is None:
                continue

            # T3.1 — Skip L3 for this cluster when all spots have l3_enabled="off",
            # or all spots have l3_enabled="auto" and no structures are present.
            # "on" beats everything; "auto"+structures → run L3; anything else → skip.
            _spot_cfgs_full = self._spot_configs or {}
            _l3_flags = [
                _spot_cfgs_full.get(sid, {}).get("l3_enabled", "auto")
                for sid in cluster.spot_ids
            ]
            _l3_any_on = any(f == "on" for f in _l3_flags)
            _l3_all_off = all(f == "off" for f in _l3_flags)
            _has_structures = bool(structures)
            _l3_run_cluster = _l3_any_on or (not _l3_all_off and _has_structures)
            if not _l3_run_cluster:
                logger.info(
                    "SWAN L3[%d]: skipping cluster %s "
                    "(l3_enabled=off or auto with no structures; DWR serves as handoff)",
                    idx, cluster.spot_ids,
                )
                continue

            l3_dir = workdir / f"level3_{idx}"
            l3_dir.mkdir(exist_ok=True)
            l3_bbox = (
                cluster.grid.lon_min, cluster.grid.lat_min,
                cluster.grid.lon_max, cluster.grid.lat_max,
            )

            # T3.4 — Hotstart invalidation on L3 grid resize.
            # Compare the current bbox hash against the stored hash from the
            # previous run.  On mismatch, delete the stale hotstart so SWAN
            # starts cold (no risk of hotstart/grid dimension mismatch crash).
            _bbox_key = (
                f"{l3_bbox[0]:.6f},{l3_bbox[1]:.6f},"
                f"{l3_bbox[2]:.6f},{l3_bbox[3]:.6f}"
            )
            _bbox_hash = hashlib.md5(_bbox_key.encode()).hexdigest()[:8]
            _bbox_hash_file = workdir / f"level3_{idx}_bbox_hash.txt"
            if _bbox_hash_file.exists():
                _stored_hash = _bbox_hash_file.read_text(encoding="ascii").strip()
                if _stored_hash != _bbox_hash:
                    _hs_to_delete = workdir / f"level3_{idx}_{self._HOTSTART_FILE}"
                    if _hs_to_delete.exists():
                        _hs_to_delete.unlink()
                    logger.info(
                        "L3 grid resized for cluster %d — cold start required, hotstart invalidated",
                        idx,
                    )

            # Copy L2 NESTOUT output → L3 BOUNDNEST1 input (different filenames).
            l2_nest = l2_dir / self._NESTOUT_FILE
            l3_nest_in = l3_dir / self._NEST_INPUT_FILE
            if l2_nest.exists():
                shutil.copy2(str(l2_nest), str(l3_nest_in))
            else:
                logger.warning(
                    "SWAN L3[%d]: Level 2 did not produce %s — running without nesting",
                    idx, self._NESTOUT_FILE,
                )

            # Scope spots to this cluster only — prevent out-of-domain transects
            saved_surf_spots = self._surf_spots
            saved_spot_configs = self._spot_configs
            self._surf_spots = {
                sid: self._surf_spots[sid]
                for sid in cluster.spot_ids
                if sid in self._surf_spots
            }
            self._spot_configs = {
                sid: self._spot_configs[sid]
                for sid in cluster.spot_ids
                if sid in self._spot_configs
            } if self._spot_configs else {}

            # T7.2: Determine setup profile and beach bearing for this cluster.
            # Use the first spot in the cluster as representative.  Setup is
            # spatially similar across the ~500m cluster footprint.
            # Note: _spot_configs is already scoped to this cluster's spot_ids above.
            _cluster_setup_profile: list[dict] | None = None
            _cluster_bearing: float | None = None
            if setup_profiles_by_spot and cluster.spot_ids:
                _rep_spot = cluster.spot_ids[0]
                _prof = setup_profiles_by_spot.get(_rep_spot)
                if _prof:
                    _cluster_setup_profile = _prof
                    _spot_cfg = (self._spot_configs or {}).get(_rep_spot, {})
                    _bearing_val = _spot_cfg.get("beach_facing_degrees")
                    if _bearing_val is not None:
                        _cluster_bearing = float(_bearing_val)

            l3_bathy = bathymetry.get("level3", {}).get(idx, {})
            l3_grid = self._write_input_files(
                l3_dir, blended_wind, {}, l3_bathy, "inner",
                tide_predictions=tide_predictions,
                ofs_currents=ofs_currents,
                structures=structures,
                override_bbox=l3_bbox,
                override_resolution_m=cluster.grid.resolution_m,
                setup_profile=_cluster_setup_profile,
                beach_bearing=_cluster_bearing,
            )
            logger.info(
                "SWAN L3[%d]: %d×%d cells at 10 m (%d spots) in %s"
                " (WLEVEL: %s)",
                idx, l3_grid["mxc"], l3_grid["myc"],
                len(cluster.spot_ids), l3_dir,
                "tide+setup" if _cluster_setup_profile else "tide-only",
            )
            self._spawn_swan_with_hotstart_retry(l3_dir, f"level3_{idx}")
            if self._check_convergence(l3_dir, f"level3_{idx}"):
                self._save_hotstart(l3_dir, f"level3_{idx}")
                # T3.4 — Persist bbox hash after successful L3 run so the next
                # cycle can detect grid resizes and invalidate the hotstart.
                _bbox_hash_file.write_text(_bbox_hash, encoding="ascii")
                cluster_results = self._parse_output(l3_dir, l3_grid)
                all_results.update(cluster_results)
                logger.info("SWAN Level 3 cluster %d complete", idx)
            else:
                # Failed workdir preserved; hotstart and spot-cache NOT updated.
                logger.error(
                    "SWAN L3 cluster %d: skipping hotstart save and cache update "
                    "(convergence failed, workdir preserved at %s)",
                    idx, l3_dir,
                )

            # Restore full spot set for next cluster (always — even on failure)
            self._surf_spots = saved_surf_spots
            self._spot_configs = saved_spot_configs

        logger.info("SWAN 3-level run complete — %d spots resolved", len(all_results))
        return all_results

    def run_stationary_level3(
        self,
        workdir: Path,
        wind_field: dict[str, Any],
        domains: "DomainSizing",
        bathymetry: dict[str, Any],
        *,
        tide_predictions: list[dict] | None = None,
        structures: list[dict] | None = None,
        setup_profiles_by_spot: dict[str, list[dict]] | None = None,
    ) -> dict[str, list["MarineForecastPoint"]]:
        """Stationary Level 3 only — quick update using latest Level 2 NESTOUT.

        Runs a single-timestep snapshot of the surf-zone wave field for each
        Level 3 cluster, using the most recent Level 2 boundary file from the
        last full 3-level run.  This is the 3-level equivalent of the old
        ``run_stationary_inner()`` — same purpose, but uses Level 3 geometry
        and reads from ``level2/`` instead of ``outer/``.

        The boundary file at ``workdir/level2/{_NESTOUT_FILE}`` must exist from
        a previous ``run_3level()`` call.  If missing, returns empty results
        (the cache warmer preserves last-good data per PROVIDER-MANUAL §14.15).

        Args:
            tide_predictions: Single-element list ``[{"time": iso, "height": m}]``
                for the compute instant.  When provided, WLEVEL.txt is written so
                the L3 static INPUT carries the tidal water level.  ``None`` → no
                WLEVEL (same behaviour as before this fix — explicit degradation).
            structures: Coastal structures to emit as OBSTACLE commands, in the
                same format as the full-run path (``{"type": ..., "coordinates":
                [[lon, lat], ...]}``) or bearing/length/distance entries resolved
                by the T3.1 fix.  ``None``/empty → no OBSTACLE commands.
            setup_profiles_by_spot: Wave-setup profiles keyed by spot_id (T7.2).
                When provided, L3 WLEVEL includes tide + analytic setup per grid cell.
                ``None`` or missing spot → uniform tide (Stage 1 fallback).
        """
        l2_boundary = workdir / "level2" / self._NESTOUT_FILE
        if not l2_boundary.exists():
            logger.warning(
                "SWAN stationary L3: no Level 2 NESTOUT (%s) — "
                "skipping quick update", self._NESTOUT_FILE,
            )
            return {}

        all_results: dict[str, list[MarineForecastPoint]] = {}

        for idx, cluster in enumerate(domains.level3_clusters):
            if cluster.grid is None:
                continue

            l3_dir = workdir / f"level3_{idx}"
            l3_dir.mkdir(exist_ok=True)

            # Copy L2 NESTOUT → L3 BOUNDNEST1 input
            shutil.copy2(str(l2_boundary), str(l3_dir / self._NEST_INPUT_FILE))

            # Scope to this cluster's spots only
            saved_surf_spots = self._surf_spots
            saved_spot_configs = self._spot_configs
            self._surf_spots = {
                sid: self._surf_spots[sid]
                for sid in cluster.spot_ids
                if sid in self._surf_spots
            }
            self._spot_configs = {
                sid: self._spot_configs[sid]
                for sid in cluster.spot_ids
                if sid in self._spot_configs
            } if self._spot_configs else {}

            l3_bathy = bathymetry.get("level3", {}).get(idx, {})
            l3_bbox = (
                cluster.grid.lon_min, cluster.grid.lat_min,
                cluster.grid.lon_max, cluster.grid.lat_max,
            )

            # T7.2: Determine setup profile and beach bearing for this cluster.
            _stat_setup_profile: list[dict] | None = None
            _stat_bearing: float | None = None
            if setup_profiles_by_spot and cluster.spot_ids:
                _rep_spot = cluster.spot_ids[0]
                _prof = setup_profiles_by_spot.get(_rep_spot)
                if _prof:
                    _stat_setup_profile = _prof
                    _spot_cfg = (self._spot_configs or {}).get(_rep_spot, {})
                    _bearing_val = _spot_cfg.get("beach_facing_degrees")
                    if _bearing_val is not None:
                        _stat_bearing = float(_bearing_val)

            l3_grid = self._write_input_files(
                l3_dir, wind_field, {}, l3_bathy, "inner",
                stationary=True,
                override_bbox=l3_bbox,
                override_resolution_m=cluster.grid.resolution_m,
                tide_predictions=tide_predictions,  # T3.2: static WLEVEL at compute time
                structures=structures,               # T3.2: coastal OBSTACLE commands
                setup_profile=_stat_setup_profile,   # T7.2: analytic setup (Stage 2)
                beach_bearing=_stat_bearing,          # T7.2: offshore direction
            )

            logger.info(
                "SWAN stationary L3[%d]: %d×%d cells (%d spots, WLEVEL: %s)",
                idx, l3_grid["mxc"], l3_grid["myc"], len(cluster.spot_ids),
                "tide+setup" if _stat_setup_profile else "tide-only",
            )
            self._spawn_swan_with_hotstart_retry(l3_dir, f"level3_{idx}")
            # T4.2: Do NOT save hotstart — stationary quick update must NOT overwrite
            # the nonstationary chain's persistent hotstart files.  A diverged stationary
            # run must not contaminate the next full run's warm-start.
            logger.info(
                "SWAN L3 stationary: skipping hotstart save (isolation from nonstationary chain)"
            )

            if self._check_convergence(l3_dir, f"level3_{idx}"):
                cluster_results = self._parse_output(l3_dir, l3_grid)
                all_results.update(cluster_results)
            else:
                # Failed workdir preserved; spot-cache NOT updated for this cluster.
                logger.error(
                    "SWAN stationary L3[%d]: convergence failed, skipping cache update "
                    "(workdir preserved at %s)",
                    idx, l3_dir,
                )

            self._surf_spots = saved_surf_spots
            self._spot_configs = saved_spot_configs

        logger.info("SWAN stationary L3 complete — %d spots resolved", len(all_results))
        return all_results

    def _write_input_files(
        self,
        run_dir: Path,
        wind_field: dict[str, Any],
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        grid_level: str,
        stationary: bool = False,
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
        override_bbox: tuple[float, float, float, float] | None = None,
        override_resolution_m: float | None = None,
        setup_profile: list[dict] | None = None,
        beach_bearing: float | None = None,
    ) -> dict[str, Any]:
        """Write SWAN input files for a given grid level and return grid_info.

        Files written to run_dir:
          BOTTOM.txt          — depth grid (SWAN positive-down convention)
          WIND.txt            — interpolated wind components (IDLA=3)
          WLEVEL.txt          — time-varying water level grid (CO-OPS tides; when provided)
          CURRENT.txt         — time-varying U/V current grids (OFS; when provided)
          BOUND_W.txt         — WW3 TPAR west-side boundary (outer only)
          BOUND_S.txt         — WW3 TPAR south-side boundary (outer only)
          OUTPUT_POINTS.txt   — (lon lat) pairs for surf spots (inner only)
          INPUT               — SWAN command file

        Args:
            run_dir: Subdirectory (outer/ or inner/) in the main tmpdir.
            wind_field: Blended wind field dict with ``"grids"`` list.
            ww3_boundary: WW3 data (used for outer level only; ignored for inner).
            cudem_bathymetry: CUDEM depth grid dict.
            grid_level: ``"outer"`` or ``"inner"``.
            stationary: When True, build a single-snapshot stationary computation.
            tide_predictions: CO-OPS tidal predictions (list of dicts with "time"
                ISO-8601 and "height" meters).  When provided, WLEVEL.txt is written
                and has_wlevel=True is passed to build_swan_input.  None → no WLEVEL.
            ofs_currents: OFS surface currents (list of dicts with "time", "u_grid",
                "v_grid" — each grid is a list[list[float]] at (myc+1)×(mxc+1)).
                When provided, CURRENT.txt is written.  None → no CURRENT.
            structures: Coastal structures for OBSTACLE commands passed to
                build_swan_input.  None/empty → no OBSTACLE commands.
            setup_profile: Wave-setup profile from wave_setup.compute_setup_profile()
                — list of {"distance_m": float, "setup_m": float} ordered shore to
                offshore.  When provided together with tide_predictions and
                beach_bearing, each WLEVEL grid cell receives tide + setup(cross-shore
                distance) instead of a uniform tide value.  None → uniform tide.
            beach_bearing: Offshore direction in compass degrees (0=N, 90=E, …).
                Used to compute cross-shore distance for each grid cell when building
                the spatially-varying WLEVEL grid.  Required when setup_profile is set;
                ignored otherwise.

        Returns:
            grid_info dict with SWAN grid dimensions and valid_times.
        """
        if override_bbox is not None and override_resolution_m is not None:
            bbox = override_bbox
            resolution_m = override_resolution_m
        elif grid_level == "outer":
            bbox = self._outer_bbox
            resolution_m = self._outer_resolution_m
        else:
            bbox = self._inner_bbox
            resolution_m = self._inner_resolution_m

        # BOTTOM.txt
        bottom_dims, bottom_text = cudem_to_swan_bottom(cudem_bathymetry, bbox, resolution_m)
        (run_dir / "BOTTOM.txt").write_text(bottom_text, encoding="ascii")

        # WIND.txt — reuse hrrr_to_swan_wind which accepts any wind_field with "grids"
        wind_dims, wind_text = hrrr_to_swan_wind(wind_field, bbox, resolution_m)
        (run_dir / "WIND.txt").write_text(wind_text, encoding="ascii")

        # WLEVEL.txt — time-varying water level from CO-OPS tidal predictions (T2.1).
        # Stage 1 (setup_profile absent): uniform tide per timestep — valid for L1/L2
        # where spatial tide gradients are negligible.
        # Stage 2 (setup_profile + beach_bearing present): each cell receives
        # tide + setup(cross-shore distance) — spatially varying WLEVEL for L3.
        # See SWAN-L3-STABILITY-PLAN §T7.2 and wave_setup.py for physics.
        has_wlevel = False
        if tide_predictions:
            use_setup = (
                setup_profile is not None
                and beach_bearing is not None
                and len(setup_profile) > 0
            )

            if use_setup:
                # Stage 2: compose tide + analytic setup for each grid point.
                from weewx_clearskies_api.services.wave_setup import (  # noqa: PLC0415
                    build_wlevel_with_setup,
                )

                heights = _interp_tide_to_wind_times(
                    tide_predictions, wind_dims["valid_times"]
                )
                mxc = wind_dims["mxc"]
                myc = wind_dims["myc"]
                dlon = wind_dims["dlon"]
                dlat = wind_dims["dlat"]
                lat_sw = wind_dims["lat_sw"]
                mid_lat = lat_sw + dlat * myc / 2.0
                metres_per_deg_lat = 111_000.0
                metres_per_deg_lon = 111_000.0 * math.cos(math.radians(mid_lat))
                wl_grid_dims = {
                    "mxc": mxc,
                    "myc": myc,
                    "dx": dlon * metres_per_deg_lon,
                    "dy": dlat * metres_per_deg_lat,
                }

                wlevel_grids: list[list[list[float]]] = [
                    build_wlevel_with_setup(h, setup_profile, wl_grid_dims, beach_bearing)
                    for h in heights
                ]
                wlevel_text = _write_wlevel_grid_txt(wlevel_grids)

                if wlevel_text:
                    (run_dir / "WLEVEL.txt").write_text(wlevel_text, encoding="ascii")
                    has_wlevel = True

                    # Find break-point distance and estimate Hb for logging.
                    # setup_profile is ordered shore→offshore; scan from offshore
                    # end to find the last non-zero setup value (= break point).
                    _dist_break = 0.0
                    _hb_est = 0.0
                    _gamma = 0.73
                    for _p in reversed(setup_profile):
                        if abs(_p["setup_m"]) > 1e-6:
                            _dist_break = float(_p["distance_m"])
                            # setdown at break: eta_b = -(1/16)*gamma^2*d_break
                            # => d_break = -16*eta_b/gamma^2
                            _eta_b = float(_p["setup_m"])
                            _d_break_est = max(0.0, -_eta_b * 16.0 / (_gamma ** 2))
                            _hb_est = _gamma * _d_break_est
                            break

                    _eta_shore = float(setup_profile[0]["setup_m"]) if setup_profile else 0.0
                    # Log once per WLEVEL write (representative first-timestep values)
                    _tide0 = heights[0] if heights else 0.0
                    logger.info(
                        "SWAN L3 WLEVEL: tide=%.2fm + setup=%.3fm"
                        " (Hb=%.2fm at %.0fm from shore)",
                        _tide0, _eta_shore, _hb_est, _dist_break,
                    )
                    logger.debug(
                        "SWAN %s: wrote WLEVEL.txt (%d chars, %d timesteps,"
                        " setup-aware grid %dx%d)",
                        grid_level, len(wlevel_text), len(heights), mxc, myc,
                    )

            else:
                # Stage 1: uniform tide per timestep (L1, L2, and L3 first run)
                wlevel_text = _write_wlevel_txt(
                    tide_predictions,
                    wind_dims["valid_times"],
                    wind_dims["mxc"],
                    wind_dims["myc"],
                )
                if wlevel_text:
                    (run_dir / "WLEVEL.txt").write_text(wlevel_text, encoding="ascii")
                    has_wlevel = True
                    logger.debug(
                        "SWAN %s: wrote WLEVEL.txt (%d chars) from %d tide predictions",
                        grid_level, len(wlevel_text), len(tide_predictions),
                    )

        # CURRENT.txt — time-varying surface currents from OFS (T2.2).
        # One block per timestep: U grid (myc+1)×(mxc+1) then V grid same dims.
        has_current = False
        if ofs_currents:
            current_text = _write_current_txt(
                ofs_currents,
                wind_dims["valid_times"],
                wind_dims["mxc"],
                wind_dims["myc"],
            )
            if current_text:
                (run_dir / "CURRENT.txt").write_text(current_text, encoding="ascii")
                has_current = True
                logger.debug(
                    "SWAN %s: wrote CURRENT.txt (%d chars) from %d OFS timesteps",
                    grid_level, len(current_text), len(ofs_currents),
                )

        # WW3 boundary files (outer only)
        if grid_level == "outer":
            boundary_text = ww3_to_swan_boundary(ww3_boundary)
            if not boundary_text:
                # Calm boundary fallback
                grids = wind_field.get("grids", [])
                if grids:
                    t0 = _parse_iso(grids[0]["valid_time"])
                    t1 = _parse_iso(grids[-1]["valid_time"])
                    boundary_text = (
                        "TPAR\n"
                        f"{_swan_time(t0)}  0.500  8.000  270.0  30.0\n"
                        f"{_swan_time(t1)}  0.500  8.000  270.0  30.0\n"
                    )
            (run_dir / "BOUND_W.txt").write_text(boundary_text, encoding="ascii")
            (run_dir / "BOUND_S.txt").write_text(boundary_text, encoding="ascii")

        # T3.1 — build per-spot transect data (when spot_configs provided and
        # this is the inner level).  Used for both CURVE output in build_swan_input
        # and transect-aware parsing in _parse_output.
        transect_spot_order: list[str] = []
        transect_points_map: dict[str, list[dict]] = {}

        if grid_level == "inner" and self._spot_configs:
            for n, (spot_id, (spot_lon, spot_lat)) in enumerate(self._surf_spots.items(), start=1):
                cfg = self._spot_configs.get(spot_id)
                if cfg is None:
                    continue
                # Prefer the runtime bidirectional profile (distance_m from
                # the actual coastline) over the wizard-configured profile.
                # runtime_profile is a dict with "profile", "coastline_lat",
                # "coastline_lon"; fall back to bathymetric_profile (list) if
                # the runtime download did not succeed.
                _runtime = cfg.get("runtime_profile")
                if isinstance(_runtime, dict) and "profile" in _runtime:
                    _profile_list: list[dict] = _runtime["profile"]
                    _coast_lat: float | None = _runtime.get("coastline_lat")
                    _coast_lon: float | None = _runtime.get("coastline_lon")
                else:
                    _profile_list = cfg.get("bathymetric_profile") or []
                    _coast_lat = None
                    _coast_lon = None

                tr = compute_spot_transect(
                    spot_lon,
                    spot_lat,
                    float(cfg.get("beach_facing_degrees", 0.0)),
                    _profile_list,
                    coastline_lat=_coast_lat,
                    coastline_lon=_coast_lon,
                )
                transect_spot_order.append(spot_id)
                transect_points_map[spot_id] = tr["transect_points"]

        # Merge grid info — wind_dims has valid_times; bottom_dims has geometry
        grid_info: dict[str, Any] = {**bottom_dims, **wind_dims}

        # OUTPUT_POINTS.txt (inner only — only when NOT using CURVE transect)
        if grid_level == "inner" and not transect_spot_order:
            from weewx_clearskies_api.services.swan_formats import lonlat_to_utm, utm_zone  # noqa: PLC0415
            center_lon = bottom_dims["lon_sw"] + bottom_dims["mxc"] * bottom_dims["dlon"] / 2
            _utm_zone = utm_zone(center_lon)
            pts_lines: list[str] = []
            for _sid, (lon, lat) in self._surf_spots.items():
                x, y = lonlat_to_utm(lon, lat, _utm_zone)
                pts_lines.append(f"{x:.2f}   {y:.2f}")
            (run_dir / "OUTPUT_POINTS.txt").write_text(
                "\n".join(pts_lines) + "\n", encoding="ascii"
            )

        # Attach transect metadata so _parse_output can retrieve it.
        if transect_spot_order:
            grid_info["transect_spot_order"] = transect_spot_order
            grid_info["transect_points"] = transect_points_map

        # Compute inner_dims for outer NESTOUT command
        inner_dims_for_input: dict[str, Any] | None = None
        if grid_level == "outer":
            from weewx_clearskies_api.services.swan_formats import _compute_swan_grid_dims
            inner_dims_for_input = _compute_swan_grid_dims(self._inner_bbox, self._inner_resolution_m)

        # Hotstart: copy previous run's hotstart file into this run dir.
        # The persistent copy lives one level up (tmpdir / "{level}_hotstart.dat")
        # so it survives the outer/inner subdir cleanup between runs.
        hotstart_arg: str | None = None
        persistent_hot = run_dir.parent / f"{grid_level}_{self._HOTSTART_FILE}"
        if persistent_hot.exists():
            shutil.copy2(str(persistent_hot), str(run_dir / self._HOTSTART_FILE))
            hotstart_arg = self._HOTSTART_FILE
            logger.info("SWAN %s: using hotstart from previous run", grid_level)

        # SWAN INPUT command file
        input_text = build_swan_input(
            dims=grid_info,
            valid_times=grid_info["valid_times"],
            spots=self._surf_spots,
            grid_level=grid_level,
            inner_dims=inner_dims_for_input,
            output_interval_hr=self._output_interval_hr,
            compute_dt_min=self._compute_dt_min,
            # Outer level writes NESTOUT to _NESTOUT_FILE; inner level reads
            # BOUNDNEST1 from _NEST_INPUT_FILE.  Different filenames prevent
            # the read/write collision that zeroed the forecast (Bug 1, 2026-07-19).
            nest_boundary_file=(
                self._NESTOUT_FILE if grid_level == "outer"
                else self._NEST_INPUT_FILE
            ),
            hotstart_file=hotstart_arg,
            stationary=stationary,
            has_wlevel=has_wlevel,
            has_current=has_current,
            structures=structures,
            spot_configs=self._spot_configs if grid_level == "inner" else None,
        )
        (run_dir / "INPUT").write_text(input_text, encoding="ascii")

        return grid_info

    def _spawn_swan_with_hotstart_retry(
        self, run_dir: Path, grid_level: str
    ) -> None:
        """Run SWAN, retrying once cold if a hotstart file causes a crash.

        When an operator adds/removes a surf spot, the grid dimensions change
        and the previous hotstart file becomes incompatible.  Rather than
        crashing the entire cycle, delete the stale hotstart and re-run cold.
        """
        persistent_hot = run_dir.parent / f"{grid_level}_{self._HOTSTART_FILE}"
        had_hotstart = (run_dir / self._HOTSTART_FILE).exists()

        try:
            self._spawn_swan(run_dir)
        except SWANRunError:
            if not had_hotstart:
                raise
            logger.warning(
                "SWAN %s: crashed with hotstart loaded — deleting stale "
                "hotstart and retrying cold",
                grid_level,
            )
            persistent_hot.unlink(missing_ok=True)
            (run_dir / self._HOTSTART_FILE).unlink(missing_ok=True)
            # Rewrite INPUT without INIT HOTSTART
            input_path = run_dir / "INPUT"
            input_text = input_path.read_text(encoding="ascii")
            input_text = "\n".join(
                line for line in input_text.splitlines()
                if "INIT HOTSTART" not in line
            ) + "\n"
            input_path.write_text(input_text, encoding="ascii")
            self._spawn_swan(run_dir)

    def _spawn_swan(self, tmpdir: Path) -> None:
        """Run the SWAN executable as a subprocess.

        SWAN reads its command file from stdin and writes output files to the
        current working directory.  All stdout/stderr is captured.

        Raises:
            SWANRunError: Non-zero exit code or subprocess timeout.
        """
        input_file = tmpdir / "INPUT"

        # OpenMP thread control: pass OMP_NUM_THREADS when explicitly configured.
        # omp_num_threads == 0 means "let OpenMP decide" (all available cores) —
        # in that case we do NOT set OMP_NUM_THREADS so OpenMP's own default
        # applies.  Explicitly setting it to os.cpu_count() would interfere with
        # any operator-level OMP_NUM_THREADS already in the environment.
        cpu_count = os.cpu_count() or 1
        if self._omp_num_threads > 0:
            logger.info(
                "SWAN runner: OMP_NUM_THREADS=%d", self._omp_num_threads
            )
            swan_env: dict[str, str] | None = {
                **os.environ,
                "OMP_NUM_THREADS": str(self._omp_num_threads),
            }
        else:
            logger.info(
                "SWAN runner: using all %d available cores (default)", cpu_count
            )
            swan_env = None  # inherit environment unchanged

        try:
            result = subprocess.run(
                [self._swan_binary],
                stdin=input_file.open("r", encoding="ascii"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(tmpdir),
                timeout=self._swan_timeout_s,
                check=False,
                env=swan_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise SWANRunError(
                f"SWAN timed out after {self._swan_timeout_s}s in {tmpdir}",
                stderr="",
                returncode=None,
            ) from exc
        except FileNotFoundError as exc:
            raise SWANRunError(
                f"SWAN binary not found: {self._swan_binary}",
                stderr="",
                returncode=None,
            ) from exc

        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            raise SWANRunError(
                f"SWAN exited with code {result.returncode} in {tmpdir}",
                stderr=stderr_text,
                returncode=result.returncode,
            )

        # SWAN (Fortran) can exit 0 despite writing "Severe error" to stderr
        # or its Errfile.  Check both so errors don't silently produce empty
        # output that the run_marker then locks in.
        errfile_path = tmpdir / "Errfile"
        errfile_text = ""
        if errfile_path.exists():
            errfile_text = errfile_path.read_text(encoding="utf-8", errors="replace")
        combined_errors = stderr_text + errfile_text
        if "Severe error" in combined_errors or "SNAME is too long" in combined_errors:
            raise SWANRunError(
                f"SWAN exited 0 but reported severe errors in {tmpdir}",
                stderr=combined_errors.strip()[:2000],
                returncode=0,
            )

    def _save_hotstart(self, run_dir: Path, grid_level: str) -> None:
        """Copy the hotstart file from the run dir to the persistent parent dir."""
        src = run_dir / self._HOTSTART_FILE
        if src.exists():
            dst = run_dir.parent / f"{grid_level}_{self._HOTSTART_FILE}"
            shutil.copy2(str(src), str(dst))
            logger.info(
                "SWAN %s: hotstart saved (%d bytes) for next run",
                grid_level, src.stat().st_size,
            )
        else:
            logger.warning("SWAN %s: no hotstart file produced", grid_level)

    def _check_convergence(self, run_dir: Path, grid_level: str) -> bool:
        """Check SWAN run convergence using three independent signals.

        All three metric values are logged at INFO regardless of pass/fail.
        On failure, an additional ERROR is logged naming the specific check.

        The three checks (per SWAN-L3-STABILITY-PLAN T4.1 / brief §5.5 item 2):

          1. **PRINT scan** — any ``******`` overflow/divergence marker in the
             SWAN PRINT file, or (stationary only) final accuracy < 99.5%.
          2. **NaN scan** — ``nan`` occurrences in the hotstart file (text-based;
             works for ASCII hotstarts; binary hotstarts are caught indirectly by
             Check 3).
          3. **Valid-point fraction** — from TABLE output:
             - Nonstationary: fraction of timesteps where ≥50% of wet transect
               points have non-exception HSIGN values; threshold 80%.
             - Stationary: fraction of all points that are non-exception; threshold 50%.

        When ``convergence_retry=True`` (future mode), a degradation ladder fires
        instead of hard failure.  That path is not implemented here.

        Args:
            run_dir: SWAN working directory (contains PRINT, TABLE files, hotstart).
            grid_level: Level identifier for logging (e.g. "level1", "level3_0").

        Returns:
            ``True`` (PASS) when all checks pass.
            ``False`` (FAIL) when any check fails; the failed workdir is left
            untouched so operators can inspect INPUT/PRINT/TABLE/hotstart.
        """
        # Detect stationary run: INPUT contains "COMPUTE NONSTAT..." iff nonstationary.
        is_stationary = True
        input_path = run_dir / "INPUT"
        if input_path.exists():
            try:
                input_text = input_path.read_text(encoding="ascii", errors="replace")
                is_stationary = "COMPUTE NONSTAT" not in input_text.upper()
            except OSError:
                pass

        # ------------------------------------------------------------------ #
        # Check 1: PRINT scan — overflow markers and stationary accuracy      #
        # ------------------------------------------------------------------ #
        print_path = run_dir / "PRINT"
        overflow_count = 0
        accuracy_pct: float | None = None

        if print_path.exists():
            try:
                print_text = print_path.read_text(encoding="utf-8", errors="replace")
                for line in print_text.splitlines():
                    if "******" in line:
                        overflow_count += 1
                    # Track accuracy percentage (last match wins — final iteration).
                    # SWAN format: "accuracy OK in 99.8 % of wet points"
                    #              "accuracy in ****** of wet points"  (overflow case)
                    if "%" in line and re.search(r"accur|wet.?point", line, re.IGNORECASE):
                        m = re.search(r"(\d+\.?\d*)\s*%", line)
                        if m:
                            accuracy_pct = float(m.group(1))
            except OSError as exc:
                logger.warning(
                    "SWAN convergence: cannot read PRINT in %s: %s", run_dir, exc
                )
        else:
            logger.warning(
                "SWAN convergence: PRINT not found in %s (level=%s)", run_dir, grid_level
            )

        print_ok = overflow_count == 0
        # Stationary runs must converge to ≥99.5% of wet points (NUMERIC npnts=99.5).
        if is_stationary and accuracy_pct is not None and accuracy_pct < 99.5:
            print_ok = False

        # ------------------------------------------------------------------ #
        # Check 2: NaN scan — hotstart file                                   #
        # ------------------------------------------------------------------ #
        nan_count = 0
        hotstart_path = run_dir / self._HOTSTART_FILE
        if hotstart_path.exists():
            try:
                hs_bytes = hotstart_path.read_bytes()
                # Case-insensitive byte-level scan.  Catches ASCII/text hotstarts
                # (Fortran free-format output, some SWAN versions).  Binary IEEE 754
                # hotstarts are caught indirectly by Check 3 (exception values in TABLE).
                nan_count = hs_bytes.lower().count(b"nan")
            except OSError as exc:
                logger.warning(
                    "SWAN convergence: cannot read hotstart in %s: %s", run_dir, exc
                )

        nan_ok = nan_count == 0

        # ------------------------------------------------------------------ #
        # Check 3: Valid-point fraction from TABLE output                     #
        # ------------------------------------------------------------------ #
        # Collect per-spot TABLE files (TABLE_1.txt, TABLE_2.txt, …) or the
        # legacy single-file OUTPUT_TABLE.txt.
        table_files: list[Path] = []
        n = 1
        while (run_dir / f"TABLE_{n}.txt").exists():
            table_files.append(run_dir / f"TABLE_{n}.txt")
            n += 1
        if not table_files and (run_dir / "OUTPUT_TABLE.txt").exists():
            table_files.append(run_dir / "OUTPUT_TABLE.txt")

        valid_fraction: float | None = None
        fraction_ok = True

        if table_files:
            # Nonstationary: track per-timestep valid counts.
            # Stationary: simple total/valid counts across all rows.
            timestep_counts: dict[str, list[int]] = {}   # time_key → [total, valid]
            simple_total = 0
            simple_valid = 0

            for table_path in table_files:
                try:
                    text = table_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                col_idx: dict[str, int] = {}
                header_found = False
                i_hs: int | None = None
                i_time: int | None = None

                for raw_line in text.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("%"):
                        tokens = line.lstrip("%").split()
                        if (
                            not header_found
                            and tokens
                            and tokens[0].upper() not in ("RUN:V1", "[DEGR]", "[M]", "[SEC]")
                            and any(
                                t.upper() in ("XP", "YP", "HSIG", "HSIGN", "HS", "TM01", "TIME")
                                for t in tokens
                            )
                        ):
                            col_idx = {tok.upper(): i for i, tok in enumerate(tokens)}
                            header_found = True
                            i_hs = col_idx.get("HSIG", col_idx.get("HSIGN", col_idx.get("HS")))
                            i_time = col_idx.get("TIME")
                        continue
                    if not header_found or i_hs is None:
                        continue
                    parts = line.split()
                    if len(parts) <= i_hs:
                        continue
                    try:
                        hs_val = float(parts[i_hs])
                    except (ValueError, IndexError):
                        continue

                    is_point_valid = (
                        not math.isnan(hs_val)
                        and hs_val > _SWAN_EXCEPTION_VALUE
                        and hs_val <= _HS_MAX_M
                    )

                    if is_stationary:
                        simple_total += 1
                        if is_point_valid:
                            simple_valid += 1
                    else:
                        time_key = (
                            parts[i_time]
                            if i_time is not None and i_time < len(parts)
                            else "unknown"
                        )
                        if time_key not in timestep_counts:
                            timestep_counts[time_key] = [0, 0]
                        timestep_counts[time_key][0] += 1
                        if is_point_valid:
                            timestep_counts[time_key][1] += 1

            if is_stationary:
                valid_fraction = simple_valid / max(simple_total, 1)
                fraction_ok = valid_fraction >= 0.50   # ≥50% of points valid
            else:
                if timestep_counts:
                    n_ok = sum(
                        1
                        for counts in timestep_counts.values()
                        if counts[0] > 0 and counts[1] / counts[0] >= 0.50
                    )
                    valid_fraction = n_ok / len(timestep_counts)
                    fraction_ok = valid_fraction >= 0.80   # ≥80% of timesteps valid
                # If no timestep data, leave valid_fraction=None → log 100% (best-effort)
        else:
            # L1/L2 produce no TABLE output — skip fraction check.
            logger.debug(
                "SWAN convergence %s: no TABLE files found — skipping valid-fraction check",
                grid_level,
            )

        # ------------------------------------------------------------------ #
        # Log all metrics at INFO (mandatory regardless of pass/fail)         #
        # ------------------------------------------------------------------ #
        logger.info(
            "SWAN convergence %s: overflow_count=%d, accuracy=%.1f%%, "
            "nan_count=%d, valid_fraction=%.1f%%",
            grid_level,
            overflow_count,
            accuracy_pct if accuracy_pct is not None else 0.0,
            nan_count,
            (valid_fraction * 100.0) if valid_fraction is not None else 100.0,
        )

        # ------------------------------------------------------------------ #
        # Evaluate pass/fail and emit ERROR on failure                        #
        # ------------------------------------------------------------------ #
        if not print_ok:
            details = f"overflow_count={overflow_count}" + (
                f", final_accuracy={accuracy_pct:.1f}%" if accuracy_pct is not None else ""
            )
            logger.error(
                "SWAN convergence FAILED level=%s: check=print_overflow, details=%s",
                grid_level, details,
            )
            SWAN_CONVERGENCE_FAILURES_TOTAL.labels(level=grid_level, check="print_overflow").inc()
            # TODO: degradation ladder when convergence_retry=True (SWAN-L3-STABILITY-PLAN Phase 4 future)
            return False

        if not nan_ok:
            logger.error(
                "SWAN convergence FAILED level=%s: check=nan_detected, details=nan_count=%d",
                grid_level, nan_count,
            )
            SWAN_CONVERGENCE_FAILURES_TOTAL.labels(level=grid_level, check="nan_detected").inc()
            # TODO: degradation ladder when convergence_retry=True (SWAN-L3-STABILITY-PLAN Phase 4 future)
            return False

        _frac_pct = (valid_fraction * 100.0) if valid_fraction is not None else 0.0
        SWAN_LAST_RUN_VALID_FRACTION.labels(level=grid_level).set(_frac_pct)

        if not fraction_ok:
            logger.error(
                "SWAN convergence FAILED level=%s: check=low_valid_fraction, "
                "details=valid_fraction=%.1f%%",
                grid_level, _frac_pct,
            )
            SWAN_CONVERGENCE_FAILURES_TOTAL.labels(level=grid_level, check="low_valid_fraction").inc()
            # TODO: degradation ladder when convergence_retry=True (SWAN-L3-STABILITY-PLAN Phase 4 future)
            return False
        logger.info(
            "SWAN convergence OK level=%s: accuracy=%.1f%%, valid_fraction=%.1f%%, nan_count=0",
            grid_level,
            accuracy_pct if accuracy_pct is not None else 100.0,
            _frac_pct,
        )
        return True

    def _parse_output(
        self,
        tmpdir: Path,
        grid_info: dict[str, Any],
    ) -> dict[str, list[MarineForecastPoint]]:
        """Read SWAN TABLE output and convert rows to MarineForecastPoints.

        T3.1/T3.2: When the inner grid used CURVE transect output, reads per-spot
        TABLE files (TABLE_1.txt, TABLE_2.txt, …) instead of the legacy
        OUTPUT_TABLE.txt.  Each file is parsed with _parse_transect_table() which
        includes depth, QB, DSPR, HSWELL, DISSURF, SETUP fields.

        Also reads SPECOUT files (SPEC_1.txt, …) and decomposes spectra into
        swell systems; results are stored in self._spectral_results (T3.3).

        Args:
            tmpdir: Directory where SWAN wrote its output.
            grid_info: Grid dimension dict; may include "transect_spot_order" and
                       "transect_points" keys when CURVE output was used.

        Returns:
            dict[spot_id, list[MarineForecastPoint]]

        Raises:
            SWANRunError: No TABLE output found (SWAN may have aborted early).
        """
        from weewx_clearskies_api.services.swan_spectral import (  # noqa: PLC0415
            parse_and_decompose,
            parse_specout_file,
        )

        transect_spot_order: list[str] = grid_info.get("transect_spot_order") or []
        transect_points_map: dict[str, list[dict]] = grid_info.get("transect_points") or {}

        # ---- SPECOUT spectral decomposition (T3.3) ----
        # NOTE: Do NOT reset self._spectral_results here.  run_3level() populates
        # it with L2 DWR baseline entries after the L2 run; each L3 cluster call
        # to _parse_output() then overrides per-spot entries with L3 handoff data.
        # Resetting here would erase the DWR baseline for already-processed spots
        # and, for multi-cluster runs, erase previous clusters' results.
        for n, spot_id in enumerate(transect_spot_order, start=1):
            spec_path = tmpdir / f"SPEC_{n}.txt"
            if spec_path.exists():
                # Decomposed components (for swell display / multiSwell)
                decomposed = parse_and_decompose(spec_path)
                # Raw 2-D spectrum (freqs_hz/dirs_deg/energy) for 1D pipeline
                raw_spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
                raw_spectra = parse_specout_file(raw_spec_text)
                raw_by_time = {r.get("time", ""): r for r in raw_spectra}
                # Merge raw fields into each decomposed entry so callers see a
                # single unified dict per timestep (surf.py T3.3 comment).
                merged_entries: list[dict] = [
                    {
                        "time": entry.get("time", ""),
                        "components": entry.get("components", []),
                        "freqs_hz": raw_by_time.get(entry.get("time", ""), {}).get(
                            "freqs_hz", []
                        ),
                        "dirs_deg": raw_by_time.get(entry.get("time", ""), {}).get(
                            "dirs_deg", []
                        ),
                        "energy": raw_by_time.get(entry.get("time", ""), {}).get(
                            "energy", []
                        ),
                    }
                    for entry in decomposed
                ]
                # Override DWR baseline with L3 handoff data for this spot
                self._spectral_results[spot_id] = merged_entries
                logger.debug(
                    "SWAN SPECOUT: spot %r → %d timestep(s) (L3 handoff, raw+decomposed merged)",
                    spot_id, len(merged_entries),
                )
            else:
                logger.debug("SWAN SPECOUT: %s not found (spot %r)", spec_path.name, spot_id)

        # ---- TABLE parsing ----
        if transect_spot_order:
            # T3.1 path: read per-spot TABLE files (CURVE output)
            result: dict[str, list[MarineForecastPoint]] = {
                sid: [] for sid in self._surf_spots
            }
            all_found = False
            for n, spot_id in enumerate(transect_spot_order, start=1):
                table_path = tmpdir / f"TABLE_{n}.txt"
                if not table_path.exists():
                    logger.warning(
                        "SWAN: per-spot table file %s not found for spot %r",
                        table_path.name, spot_id,
                    )
                    continue
                all_found = True
                table_text = table_path.read_text(encoding="utf-8", errors="replace")
                pts = _parse_transect_table(
                    table_text,
                    spot_id,
                    transect_points_map.get(spot_id) or [],
                    utm_zone=grid_info.get("_utm_zone"),
                )
                result[spot_id] = pts
                logger.info(
                    "SWAN: spot %r → %d transect points parsed from %s",
                    spot_id, len(pts), table_path.name,
                )
            if not all_found:
                # Fall through to legacy OUTPUT_TABLE.txt as last resort
                logger.warning(
                    "SWAN: no per-spot TABLE files found; falling back to OUTPUT_TABLE.txt"
                )
                return self._parse_output_legacy(tmpdir)
            return result

        # ---- Legacy path: single OUTPUT_TABLE.txt ----
        return self._parse_output_legacy(tmpdir)

    def _parse_output_legacy(self, tmpdir: Path) -> dict[str, list[MarineForecastPoint]]:
        """Read the legacy OUTPUT_TABLE.txt (POINTS output, old single-point approach)."""
        table_path = tmpdir / "OUTPUT_TABLE.txt"
        if not table_path.exists():
            errfile_path = tmpdir / "Errfile"
            errfile = (
                errfile_path.read_text(encoding="utf-8", errors="replace")
                if errfile_path.exists()
                else ""
            )
            raise SWANRunError(
                f"SWAN did not produce OUTPUT_TABLE.txt in {tmpdir}",
                stderr=errfile,
                returncode=None,
            )
        table_text = table_path.read_text(encoding="utf-8", errors="replace")
        return _parse_table_output(table_text, self._surf_spots)
