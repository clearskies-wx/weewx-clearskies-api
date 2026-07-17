"""SWAN nearshore wave model subprocess orchestrator for the TruShore pipeline.

Responsibilities:
  1. Write all SWAN input files (BOTTOM.txt, WIND.txt, BOUND_SPEC.txt, INPUT,
     OUTPUT_POINTS.txt) into a temporary directory.
  2. Spawn the SWAN executable as a subprocess, feeding it INPUT on stdin.
  3. Parse the TABLE output file and convert each row to a MarineForecastPoint.
  4. Return results keyed by surf spot ID.

Key design decisions:
  - Runs SWAN in a temp directory; the directory is NOT cleaned up automatically
    so that operators can inspect input/output files after failures.
  - SWAN is invoked as a subprocess (not via Python bindings) per ADR-093.
  - The return type is dict[str, list[MarineForecastPoint]] keyed by spot_id.
    This is the approved deviation from the plan's under-specified flat list
    (confirmed by coordinator 2026-07-16).

References:
  - PROVIDER-MANUAL.md §14.15 (SWAN+TruShore runner)
  - SWAN-TRUSHORE-PLAN.md T2.2 + T2.4
  - ADR-093 (SWAN+TruShore nearshore model)
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.services.swan_formats import (
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
# SWAN INPUT file builder
# ---------------------------------------------------------------------------


def _swan_time(dt: datetime) -> str:
    """Format a UTC datetime as SWAN's YYYYMMDD.HHmmss token."""
    return dt.strftime("%Y%m%d.%H%M%S")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string (with optional Z suffix) to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _build_input_file(
    dims: dict[str, Any],
    valid_times: list[str],
    spots: dict[str, tuple[float, float]],
    output_interval_hr: float = 1.0,
    compute_dt_min: int = 10,
) -> str:
    """Render the SWAN ASCII INPUT command file.

    Args:
        dims: Grid dimension dict from cudem_to_swan_bottom or hrrr_to_swan_wind.
              Keys: mxc, myc, dlon, dlat, lon_sw, lat_sw.
        valid_times: Ordered list of ISO-8601 UTC strings from HRRR grids.
                     First entry is t_start, last entry is t_end.
        spots: dict mapping spot_id → (lon, lat) — only used to confirm there are spots.
        output_interval_hr: Hours between TABLE output rows.
        compute_dt_min: SWAN internal time step in minutes.

    Returns:
        String content of the SWAN INPUT command file.
    """
    if not valid_times:
        raise ValueError("_build_input_file: valid_times is empty")
    if not spots:
        raise ValueError("_build_input_file: no surf spots defined")

    t_start = _parse_iso(valid_times[0])
    t_end = _parse_iso(valid_times[-1])
    swan_t_start = _swan_time(t_start)
    swan_t_end = _swan_time(t_end)

    # HRRR provides 1-hour wind grids; wind INPUT is stationary within each hour
    # NONSTAT wind time step matches HRRR temporal resolution = 1 HR
    wind_dt_hr = 1  # one-hour steps between HRRR wind snapshots
    # Wind INPUT time range matches HRRR valid_times
    wind_t_end = swan_t_end

    mxc = dims["mxc"]
    myc = dims["myc"]
    dlon = dims["dlon"]
    dlat = dims["dlat"]
    lon_sw = dims["lon_sw"]
    lat_sw = dims["lat_sw"]

    # The INPGRID for BOTTOM and WIND use the same regular grid parameters.
    # CGRID REG: xpc ypc alpc xlenc ylenc mxc myc
    #   xpc, ypc = SW corner (lon, lat)
    #   alpc = 0.0 (no grid rotation)
    #   xlenc, ylenc = physical size in degrees (lon_ne-lon_sw, lat_ne-lat_sw)
    xlenc = mxc * dlon
    ylenc = myc * dlat

    # INPGRID size parameters: same as CGRID
    inpgrid_xlenc = xlenc
    inpgrid_ylenc = ylenc

    output_dt_min = int(round(output_interval_hr * 60))

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
        # BOTTOM grid: static (no NONSTAT keyword)
        f"INPGRID BOTTOM REG {lon_sw:.6f} {lat_sw:.6f} 0. {mxc} {myc} {dlon:.6f} {dlat:.6f}",
        "READINP BOTTOM 1. 'BOTTOM.txt' 3 0 FREE",
        "",
        # WIND grid: non-stationary (time-varying)
        (
            f"INPGRID WIND REG {lon_sw:.6f} {lat_sw:.6f} 0."
            f" {mxc} {myc} {dlon:.6f} {dlat:.6f}"
            f" NONSTAT {swan_t_start} {wind_dt_hr} HR {wind_t_end}"
        ),
        "READINP WIND 1. 'WIND.txt' 3 0 FREE",
        "",
        # Boundary conditions: uniform WW3 spectrum on western and southern sides
        "BOUNDSPEC SIDE W CCW CONSTANT FILE 'BOUND_SPEC.txt' 1",
        "BOUNDSPEC SIDE S CCW CONSTANT FILE 'BOUND_SPEC.txt' 1",
        "",
        # Source term settings (PROVIDER-MANUAL §14.15)
        "GEN3 WESTIN",
        "BREAKING CONSTANT 1.0 0.73",
        "FRICTION JON 0.067",
        "TRIADS",
        "DIFFRACTION",
        "",
        # Output points from file
        "POINTS 'SPOTS' FILE 'OUTPUT_POINTS.txt'",
        "",
        # TABLE output: time, coordinates, Hs, mean period, mean wave direction
        (
            f"TABLE 'SPOTS' HEAD 'OUTPUT_TABLE.txt'"
            f" XP YP HS TM01 DIR OUTPUT {swan_t_start} {output_dt_min} MIN"
        ),
        "",
        # Non-stationary computation
        f"COMPUTE NONST {swan_t_start} {compute_dt_min} MIN {swan_t_end}",
        "",
        "STOP",
    ]
    return "\n".join(lines) + "\n"


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

        # Resolve required column indices
        try:
            i_xp = col_idx["XP"]
            i_yp = col_idx["YP"]
            i_hs = col_idx["HS"]
            i_tm = col_idx["TM01"]
            i_dir = col_idx["DIR"]
        except KeyError:
            # Header not yet parsed or missing expected columns — skip
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

        # Physical validation
        if not _is_valid_point(hs, tm01, mwd):
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
    """Orchestrates a full SWAN run for one set of input fields.

    Config keys (all required unless marked optional):
      domain_bbox (list[float]):     [lon_sw, lat_sw, lon_ne, lat_ne]
      surf_spots (dict):             {spot_id: {"lon": float, "lat": float}}
      swan_binary (str):             Path to the SWAN executable (e.g. "/usr/local/bin/swanrun")
      grid_resolution_m (float):    SWAN grid spacing in metres (default 200)
      compute_dt_min (int, opt):    SWAN time step in minutes (default 10)
      output_interval_hr (float, opt): TABLE output interval in hours (default 1)
      swan_timeout_s (int, opt):    Subprocess timeout seconds (default 900)

    Usage:
        runner = SWANRunner(config)
        results = runner.run(hrrr_wind_field, ww3_boundary, cudem_bathymetry)

    run() returns dict[spot_id, list[MarineForecastPoint]].
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._domain_bbox: tuple[float, float, float, float] = tuple(
            config["domain_bbox"]
        )  # type: ignore[assignment]
        self._surf_spots: dict[str, tuple[float, float]] = {
            sid: (float(sdata["lon"]), float(sdata["lat"]))
            for sid, sdata in config["surf_spots"].items()
        }
        self._swan_binary: str = config["swan_binary"]
        self._grid_resolution_m: float = float(config.get("grid_resolution_m", 200.0))
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
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> dict[str, list[MarineForecastPoint]]:
        """Execute a complete SWAN run and return per-spot wave forecasts.

        Steps:
          1. Create a temporary working directory.
          2. Write all input files (BOTTOM.txt, WIND.txt, BOUND_SPEC.txt,
             OUTPUT_POINTS.txt, INPUT).
          3. Run the SWAN subprocess.
          4. Parse OUTPUT_TABLE.txt.
          5. Return results.

        The temporary directory is NOT deleted on failure so that operators can
        inspect SWAN's Errfile and PRINT output.

        Args:
            hrrr_wind_field: Return value of hrrr.fetch().
            ww3_boundary:    Return value of wavewatch.fetch().
            cudem_bathymetry: Dict with keys lat_first, lon_first, lat_last,
                              lon_last, ni, nj, depths (list[list[float]]).

        Returns:
            dict[spot_id, list[MarineForecastPoint]] — empty list for spots
            where all output rows failed physical validation.

        Raises:
            SWANRunError: The SWAN subprocess exited with non-zero status or
                          timed out.
            ValueError:   Required config keys are missing or input data is empty.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="swan_run_"))
        grid_info = self._write_input_files(
            tmpdir, hrrr_wind_field, ww3_boundary, cudem_bathymetry
        )
        self._spawn_swan(tmpdir)
        return self._parse_output(tmpdir, grid_info)

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    def _write_input_files(
        self,
        tmpdir: Path,
        hrrr_wind_field: dict[str, Any],
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
    ) -> dict[str, Any]:
        """Write SWAN input files and return grid_info needed for parse step.

        Files written to tmpdir:
          BOTTOM.txt          — depth grid (SWAN convention: positive = ocean)
          WIND.txt            — interpolated HRRR wind components (IDLA=3)
          BOUND_SPEC.txt      — TPAR boundary spectrum from WW3
          OUTPUT_POINTS.txt   — (lon lat) pairs for each surf spot
          INPUT               — SWAN command file (fed via stdin)

        Returns grid_info dict with SWAN grid dimensions and valid_times.
        """
        # Generate BOTTOM.txt
        bottom_dims, bottom_text = cudem_to_swan_bottom(
            cudem_bathymetry, self._domain_bbox, self._grid_resolution_m
        )
        (tmpdir / "BOTTOM.txt").write_text(bottom_text, encoding="ascii")

        # Generate WIND.txt
        wind_dims, wind_text = hrrr_to_swan_wind(
            hrrr_wind_field, self._domain_bbox, self._grid_resolution_m
        )
        (tmpdir / "WIND.txt").write_text(wind_text, encoding="ascii")

        # Generate BOUND_SPEC.txt
        boundary_text = ww3_to_swan_boundary(ww3_boundary)
        if not boundary_text:
            # Fall back to a calm boundary condition so SWAN can still run
            from datetime import timedelta
            grids = hrrr_wind_field.get("grids", [])
            if grids:
                t0 = datetime.fromisoformat(
                    grids[0]["valid_time"].replace("Z", "+00:00")
                ).astimezone(UTC)
                t1 = datetime.fromisoformat(
                    grids[-1]["valid_time"].replace("Z", "+00:00")
                ).astimezone(UTC)
                boundary_text = (
                    "TPAR\n"
                    f"{_swan_time(t0)}  0.500  8.000  270.0  30.0\n"
                    f"{_swan_time(t1)}  0.500  8.000  270.0  30.0\n"
                )
        (tmpdir / "BOUND_SPEC.txt").write_text(boundary_text, encoding="ascii")

        # Generate OUTPUT_POINTS.txt: lon lat per line, in surf_spots insertion order
        pts_lines: list[str] = []
        for spot_id, (lon, lat) in self._surf_spots.items():
            pts_lines.append(f"{lon:.6f}   {lat:.6f}")
        (tmpdir / "OUTPUT_POINTS.txt").write_text(
            "\n".join(pts_lines) + "\n", encoding="ascii"
        )

        # Grid info: prefer wind_dims (has valid_times); bottom_dims has same geometry
        grid_info: dict[str, Any] = {**bottom_dims, **wind_dims}

        # Generate SWAN INPUT command file
        input_text = _build_input_file(
            grid_info,
            grid_info["valid_times"],
            self._surf_spots,
            self._output_interval_hr,
            self._compute_dt_min,
        )
        (tmpdir / "INPUT").write_text(input_text, encoding="ascii")

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
