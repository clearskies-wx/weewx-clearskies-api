"""SWAN nearshore wave model subprocess orchestrator for the TruShore pipeline.

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
  - PROVIDER-MANUAL.md §14.15 (SWAN+TruShore runner)
  - PROVIDER-MANUAL.md §14.16 (GFS wind provider)
  - SWAN-TRUSHORE-PLAN.md T2.2 + T2.4 + T7.2
  - ADR-093 (SWAN+TruShore nearshore model)
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

_HS_MIN_M = 0.0    # exclusive lower bound (0.0 is flat-calm, physically valid)
_HS_MAX_M = 20.0   # inclusive upper bound
_TM_MIN_S = 1.0    # inclusive lower bound
_TM_MAX_S = 30.0   # inclusive upper bound


def _is_valid_point(hs: float, tm01: float, mwd: float) -> bool:
    """Physical plausibility check on SWAN TABLE output values."""
    if math.isnan(hs) or math.isnan(tm01) or math.isnan(mwd):
        return False
    if hs <= _HS_MIN_M or hs > _HS_MAX_M:
        return False
    if tm01 < _TM_MIN_S or tm01 > _TM_MAX_S:
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
    data_header_count = 0  # count of % lines seen (first = col names, second = units)

    for raw_line in table_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("%"):
            data_header_count += 1
            if data_header_count == 1:
                # First % line: column names
                # Strip leading "%" and split — but SWAN sometimes puts "%   Time ..."
                tokens = line.lstrip("%").split()
                # Normalize to uppercase for case-insensitive matching
                col_idx = {tok.upper(): idx for idx, tok in enumerate(tokens)}
                header_found = True
            # Skip second % line (units) and any subsequent comment lines
            continue

        if not header_found:
            continue

        # Data row
        parts = line.split()
        if not parts:
            continue

        # Resolve required column indices.  SWAN 41.51 uses "Hsig" (→ HSIG)
        # as the column header for significant wave height; older versions
        # may use "Hs" (→ HS).  Accept both.
        try:
            i_xp = col_idx["XP"]
            i_yp = col_idx["YP"]
            i_hs = col_idx.get("HSIG", col_idx.get("HS"))
            if i_hs is None:
                raise KeyError("HSIG/HS")
            i_tm = col_idx["TM01"]
            i_dir = col_idx["DIR"]
        except KeyError:
            if header_found and col_idx:
                logger.warning(
                    "SWAN TABLE: missing required column — have %s",
                    list(col_idx.keys()),
                )
            continue

        # Time is always the first data token (before Xp in SWAN output)
        # The "Time" column header occupies a wide field in the header but the
        # actual time token is the first field in each data row.
        # col_idx["TIME"] points to 0 given the header layout; we use parts[0].
        if len(parts) < max(i_xp, i_yp, i_hs, i_tm, i_dir) + 1:
            continue

        try:
            xp = float(parts[i_xp])
            yp = float(parts[i_yp])
            hs = float(parts[i_hs])
            tm01 = float(parts[i_tm])
            mwd = float(parts[i_dir])
            time_token = parts[0]  # always column 0 regardless of header offset
        except (ValueError, IndexError):
            continue

        # Physical validation — log at WARNING temporarily for T7.5 debugging
        if not _is_valid_point(hs, tm01, mwd):
            logger.warning(
                "SWAN output rejected: Xp=%.4f Yp=%.4f Hs=%.2f Tm01=%.2f Dir=%.1f "
                "(time=%s)",
                xp, yp, hs, tm01, mwd, time_token,
            )
            continue

        # Convert SWAN time token YYYYMMDD.HHmmss → ISO-8601
        try:
            dt_str = time_token  # e.g. "20240101.010000"
            date_part, time_part = dt_str.split(".")
            iso = (
                f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
            )
        except (ValueError, IndexError):
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
            )
        )

    return result


# ---------------------------------------------------------------------------
# SWANRunner
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        hrrr_wind_field: dict[str, Any],
        gfs_wind_field: dict[str, Any] | None,
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
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
        self._run_outer_grid(tmpdir, blended_wind, ww3_boundary, cudem_bathymetry)
        return self._run_inner_nest(tmpdir, blended_wind, cudem_bathymetry)

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
    ) -> None:
        """Run the outer SWAN grid (continental shelf approach domain).

        Writes SWAN input files to tmpdir/outer/, spawns SWAN, and produces
        nest_boundary.dat in tmpdir/outer/ for the inner nest to consume.

        Args:
            tmpdir: Root temporary directory for this SWAN run.
            blended_wind: Stitched HRRR+GFS wind field from _stitch_wind().
            ww3_boundary: Return value of wavewatch.fetch().
            cudem_bathymetry: CUDEM depth grid dict.
        """
        outer_dir = tmpdir / "outer"
        outer_dir.mkdir(exist_ok=True)
        grid_info = self._write_input_files(
            outer_dir, blended_wind, ww3_boundary, cudem_bathymetry, "outer"
        )
        logger.info(
            "SWAN outer grid: %d×%d cells at %.1f km resolution in %s",
            grid_info["mxc"], grid_info["myc"],
            self._outer_resolution_m / 1000.0, outer_dir,
        )
        self._spawn_swan(outer_dir)
        logger.info("SWAN outer grid complete")

    def _run_inner_nest(
        self,
        tmpdir: Path,
        blended_wind: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> dict[str, list[MarineForecastPoint]]:
        """Run the inner nested SWAN grid (tight nearshore domain around surf spots).

        Copies nest_boundary.dat from tmpdir/outer/ into tmpdir/inner/, writes
        SWAN input files, spawns SWAN, and parses TABLE output at surf spot points.

        Args:
            tmpdir: Root temporary directory (must contain outer/ with nest_boundary.dat).
            blended_wind: Stitched HRRR+GFS wind field.
            cudem_bathymetry: CUDEM depth grid dict.

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
            inner_dir, blended_wind, {}, cudem_bathymetry, "inner"
        )
        logger.info(
            "SWAN inner nest: %d×%d cells at %.0f m resolution in %s",
            grid_info["mxc"], grid_info["myc"],
            self._inner_resolution_m, inner_dir,
        )
        self._spawn_swan(inner_dir)
        logger.info("SWAN inner nest complete")
        return self._parse_output(inner_dir, grid_info)

    def _write_input_files(
        self,
        run_dir: Path,
        wind_field: dict[str, Any],
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        grid_level: str,
    ) -> dict[str, Any]:
        """Write SWAN input files for a given grid level and return grid_info.

        Files written to run_dir:
          BOTTOM.txt          — depth grid (SWAN positive-down convention)
          WIND.txt            — interpolated wind components (IDLA=3)
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

        # OUTPUT_POINTS.txt (inner only)
        if grid_level == "inner":
            pts_lines: list[str] = [
                f"{lon:.6f}   {lat:.6f}"
                for _sid, (lon, lat) in self._surf_spots.items()
            ]
            (run_dir / "OUTPUT_POINTS.txt").write_text(
                "\n".join(pts_lines) + "\n", encoding="ascii"
            )

        # Merge grid info — wind_dims has valid_times; bottom_dims has geometry
        grid_info: dict[str, Any] = {**bottom_dims, **wind_dims}

        # Compute inner_dims for outer NESTOUT command
        inner_dims_for_input: dict[str, Any] | None = None
        if grid_level == "outer":
            from weewx_clearskies_api.services.swan_formats import _compute_swan_grid_dims
            inner_dims_for_input = _compute_swan_grid_dims(self._inner_bbox, self._inner_resolution_m)

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

    def _parse_output(
        self,
        tmpdir: Path,
        grid_info: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, list[MarineForecastPoint]]:
        """Read OUTPUT_TABLE.txt and convert rows to MarineForecastPoints.

        Args:
            tmpdir: Directory where SWAN wrote its output.
            grid_info: Grid dimension dict (not used in parsing but available for
                       future use, e.g. interpolating missing values).

        Returns:
            dict[spot_id, list[MarineForecastPoint]]

        Raises:
            SWANRunError: OUTPUT_TABLE.txt is missing (SWAN may have aborted early).
        """
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
