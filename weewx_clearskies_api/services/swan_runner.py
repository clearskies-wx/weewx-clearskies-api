"""SWAN nearshore wave model subprocess orchestrator for the SWAN pipeline.

Responsibilities:
  1. Blend HRRR (hours 0–48) and GFS (hours 48–72) wind fields into a single
     72-hour blended wind input (T7.2).
  2. Execute two sequential SWAN runs per cycle: an outer grid (continental shelf
     approach) and an inner nest (tight nearshore domain around surf spots) — T7.2.
  3. Write all SWAN input files (BOTTOM.txt, WIND.txt, BOUND_SPEC.txt, INPUT,
     OUTPUT_POINTS.txt) into per-level subdirectories of a temporary directory.
  4. Spawn the SWAN executable as a subprocess for each level, feeding it INPUT
     on stdin.
  5. Parse the inner nest's TABLE output file and convert each row to a
     MarineForecastPoint.
  6. Return results keyed by surf spot ID.

Key design decisions:
  - Two sequential SWAN runs per cycle (outer grid + inner nest).  Total grid
    points ~8,000–16,000; total peak memory ≤300 MB (PROVIDER-MANUAL §14.15).
  - GFS 3-hourly grids are linearly interpolated to hourly resolution in
    _stitch_wind() so the blended wind field is uniform 1-hour cadence
    (PROVIDER-MANUAL §14.16).
  - Runs SWAN in a temp directory; the directory is NOT cleaned up automatically
    so that operators can inspect input/output files after failures.
  - SWAN is invoked as a subprocess (not via Python bindings) per ADR-093.
  - The return type is dict[str, list[MarineForecastPoint]] keyed by spot_id.
    This is the approved deviation from the plan's under-specified flat list
    (confirmed by coordinator 2026-07-16).

References:
  - PROVIDER-MANUAL.md §14.15 (SWAN+SWAN runner)
  - PROVIDER-MANUAL.md §14.16 (GFS wind provider)
  - SWAN-CORRECTIONS-PLAN.md
  - ADR-093 (SWAN+SWAN nearshore model)
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_formats import (
    build_swan_input,
    compute_spot_transect,
    cudem_to_swan_bottom,
    hrrr_to_swan_wind,
    ww3_to_swan_boundary,
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

    # T7.5 debug: log first few raw lines to see what SWAN produced
    raw_lines = table_text.splitlines()
    logger.warning(
        "SWAN TABLE raw preview (%d lines total): %s",
        len(raw_lines),
        raw_lines[:8],
    )

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

        # Match (Xp, Yp) to a spot
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

    return result


def _parse_transect_table(
    table_text: str,
    spot_id: str,
    transect_points: list[dict],
    coord_tolerance_deg: float = 0.003,
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

    Returns:
        list[MarineForecastPoint] — one per valid row; may contain multiple
        points per timestep (one per transect output point).
    """
    # Build fast lookup: (lon_rounded, lat_rounded) → (depth_m, distance_m)
    transect_lookup: dict[tuple[float, float], tuple[float, float]] = {}
    for tp in transect_points:
        key = (round(float(tp["lon"]), 4), round(float(tp["lat"]), 4))
        transect_lookup[key] = (float(tp.get("depth_m", 0.0)), float(tp.get("distance_m", 0.0)))

    raw_lines = table_text.splitlines()
    logger.debug(
        "SWAN CURVE TABLE %r: %d raw lines",
        spot_id, len(raw_lines),
    )

    col_idx: dict[str, int] = {}
    header_found = False
    results: list[MarineForecastPoint] = []

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
        xp_rounded = round(xp, 4)
        yp_rounded = round(yp, 4)

        depth_m: float | None = depth_swan
        distance_m: float | None = None

        # Search transect lookup within tolerance
        best_dist_deg = float("inf")
        for (tp_lon, tp_lat), (tp_depth, tp_dist) in transect_lookup.items():
            dist_deg = math.sqrt((xp_rounded - tp_lon) ** 2 + (yp_rounded - tp_lat) ** 2)
            if dist_deg < coord_tolerance_deg and dist_deg < best_dist_deg:
                best_dist_deg = dist_deg
                if depth_m is None:  # prefer DEPTH column; use lookup only as fallback
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
    all cells set to the same tidal elevation (meters, positive up from MSL).
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

    _NESTOUT_BOUNDARY_FILE = "nest_boundary.dat"
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
        nest_boundary.dat in tmpdir/outer/ for the inner nest to consume.

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
        self._spawn_swan(outer_dir)
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

        Copies nest_boundary.dat from tmpdir/outer/ into tmpdir/inner/, writes
        SWAN input files, spawns SWAN, and parses TABLE output at surf spot points.

        Args:
            tmpdir: Root temporary directory (must contain outer/ with nest_boundary.dat).
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

        # Copy nest boundary file from outer run into inner working dir
        src = tmpdir / "outer" / self._NESTOUT_BOUNDARY_FILE
        dst = inner_dir / self._NESTOUT_BOUNDARY_FILE
        if src.exists():
            shutil.copy2(str(src), str(dst))
        else:
            logger.warning(
                "SWAN inner nest: outer grid did not produce %s — inner nest will run without nesting",
                self._NESTOUT_BOUNDARY_FILE,
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
        self._spawn_swan(inner_dir)
        self._save_hotstart(inner_dir, "inner")
        logger.info("SWAN inner nest complete")
        return self._parse_output(inner_dir, grid_info)

    def run_stationary_inner(
        self,
        tmpdir: Path,
        wind_field: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> dict[str, list[MarineForecastPoint]]:
        """Run a stationary SWAN computation on the inner nest only.

        Uses the most recent nest_boundary.dat from the last full run
        (expected at tmpdir/outer/nest_boundary.dat or
        tmpdir/outer_hotstart.dat context).  Produces a single-timestep
        snapshot of the nearshore wave field with the latest wind.

        Per SWAN manual §4.7: "For small domains (< 100 km), a stationary
        computation is recommended."  Inner nest is ~20km.
        """
        inner_dir = tmpdir / "inner"
        inner_dir.mkdir(exist_ok=True)

        # Reuse nest_boundary.dat from the last full run's outer grid
        src = tmpdir / "outer" / self._NESTOUT_BOUNDARY_FILE
        dst = inner_dir / self._NESTOUT_BOUNDARY_FILE
        if src.exists():
            shutil.copy2(str(src), str(dst))
        else:
            logger.warning(
                "SWAN stationary: no nest_boundary.dat from outer grid — "
                "running without nesting (quick update may be inaccurate)"
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
        self._spawn_swan(inner_dir)
        self._save_hotstart(inner_dir, "inner")
        logger.info("SWAN stationary inner complete")
        return self._parse_output(inner_dir, grid_info)

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

        Returns:
            grid_info dict with SWAN grid dimensions and valid_times.
        """
        if grid_level == "outer":
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
        # One block per wind timestep: (myc+1) rows × (mxc+1) columns, uniform value
        # across the domain (tides vary slowly over the ~30km inner nest domain).
        has_wlevel = False
        if tide_predictions:
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
                tr = compute_spot_transect(
                    spot_lon,
                    spot_lat,
                    float(cfg.get("beach_facing_degrees", 0.0)),
                    cfg.get("bathymetric_profile") or [],
                )
                transect_spot_order.append(spot_id)
                transect_points_map[spot_id] = tr["transect_points"]

        # OUTPUT_POINTS.txt (inner only — only when NOT using CURVE transect)
        if grid_level == "inner" and not transect_spot_order:
            pts_lines: list[str] = [
                f"{lon:.6f}   {lat:.6f}"
                for _sid, (lon, lat) in self._surf_spots.items()
            ]
            (run_dir / "OUTPUT_POINTS.txt").write_text(
                "\n".join(pts_lines) + "\n", encoding="ascii"
            )

        # Merge grid info — wind_dims has valid_times; bottom_dims has geometry
        grid_info: dict[str, Any] = {**bottom_dims, **wind_dims}

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
            nest_boundary_file=self._NESTOUT_BOUNDARY_FILE,
            hotstart_file=hotstart_arg,
            stationary=stationary,
            has_wlevel=has_wlevel,
            has_current=has_current,
            structures=structures,
            spot_configs=self._spot_configs if grid_level == "inner" else None,
        )
        (run_dir / "INPUT").write_text(input_text, encoding="ascii")

        return grid_info

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
        from weewx_clearskies_api.services.swan_spectral import parse_and_decompose  # noqa: PLC0415

        transect_spot_order: list[str] = grid_info.get("transect_spot_order") or []
        transect_points_map: dict[str, list[dict]] = grid_info.get("transect_points") or {}

        # ---- SPECOUT spectral decomposition (T3.3) ----
        self._spectral_results = {}
        for n, spot_id in enumerate(transect_spot_order, start=1):
            spec_path = tmpdir / f"SPEC_{n}.txt"
            if spec_path.exists():
                spectra = parse_and_decompose(spec_path)
                self._spectral_results[spot_id] = spectra
                logger.debug(
                    "SWAN SPECOUT: spot %r → %d timestep(s) decomposed",
                    spot_id, len(spectra),
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
