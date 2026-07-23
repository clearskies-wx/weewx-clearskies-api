"""Production SurfBeat strip runner.

Generates and runs a SWAN SurfBeat strip computation for infragravity (IG)
wave analysis at a surf spot.

The strip is a small regular 2D SWAN grid in local strip coordinates:

- x = cross-shore, west → east = offshore → shore
- y = alongshore (bathymetry is identical along each row — alongshore uniform)
- SURFBEAT enabled with two-COMPUTE procedure
- Shoreline OBSTACLE on the east side with IG reflection

SWAN syntax follows ``docs/reference/swan-commands-extract.md`` §SURFBEAT and
plan §0.4 (SURF-MODEL-FIX-PLAN.md).  Every INPUT command is verified against
SWAN 41.51AB.

Known pitfalls enforced here (from §0.4 "Known SWAN pitfalls"):
- Bare COMPUTE (not "COMPUTE STAT") — "STAT" triggers "Illegal keyword: STAT".
- OBSTACLE RDIFF [pown] — pown is REQUIRED (omitting → "No value for POWN").
- SURFBEAT requires regular 2D REG grid, stationary mode, west boundary only.
- SPECOUT integer values are scaled: actual density = integer × FACTOR value.
- BOTTOM.txt idla=3: row 0 = south, columns west → east (offshore → shore).

Public API::

    result = run_surfbeat_strip(
        spot_id="hb_pier",
        profile=profile_nx2,        # Nx2 [[dist_from_shore_m, depth_m], ...]
        hs=1.0, tp=14.0, direction=270.0,
        cfjon=0.038,
        swan_binary="/usr/local/bin/swan.exe",
    )
    # result.hs_ig_shoreline — IG Hs at the most shoreward station (m)
    # result.set_timing_minutes — 1 / f_peak_ig in minutes (or None)
    # result.set_amplitude_m   — display alias for hs_ig_shoreline (or None)
    # result.hs_sw_profile     — [{"distance_m": …, "hs_m": …}, …] offshore→shore
    # result.runtime_ms        — SWAN subprocess wall-clock time (ms)
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Module-level cache: most recent SurfBeat result per location ID.
# Written by surf.py after a successful SurfBeat strip run.
# Read by beach_profile.py to supply the blended approach-zone Hs profile.
_surfbeat_result_cache: dict[str, "SurfBeatResult"] = {}


def cache_surfbeat_result(location_id: str, result: "SurfBeatResult") -> None:
    """Store the most recent SurfBeat result for a location (called from surf.py)."""
    _surfbeat_result_cache[location_id] = result


def get_cached_surfbeat_result(location_id: str) -> "SurfBeatResult | None":
    """Get the most recent SurfBeat result for a location (called from beach_profile.py)."""
    return _surfbeat_result_cache.get(location_id)


# IG / sea-swell split frequency — coordinator design decision (SURF-MODEL-FIX-PLAN §T2.1)
_IG_SPLIT_HZ: float = 0.04

# Default SWAN subprocess timeout (seconds)
_DEFAULT_TIMEOUT_S: int = 120

# Default base directory for SWAN working directories
_DEFAULT_WORKDIR_BASE: str = "/var/run/weewx-clearskies/swan"

# Station spacing (m) for approach-zone blend profile (T2.3: stations ≤ 25 m apart)
_STATION_SPACING_M: float = 25.0


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SurfBeatRunError(Exception):
    """Raised when the SurfBeat SWAN strip run fails.

    Attributes:
        stderr: Captured standard error from the SWAN process (truncated).
        returncode: Process exit code (``None`` if the process timed out).
    """

    def __init__(
        self,
        message: str,
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SurfBeatStripConfig:
    """Grid and physics configuration for the SurfBeat strip.

    Defaults match the verified benchmark template from
    1D-MODEL-BENCHMARK-BRIEF §7.3 and swan-commands-extract.md §SURFBEAT.

    Attributes:
        dx: Cross-shore grid spacing (m).
        ny: Number of alongshore mesh cells.
        dy: Alongshore grid spacing (m).
        gamma: SWAN wave-breaking coefficient (BREAKING CONSTANT 1.0 gamma).
        dir_spread: Directional spread for the parametric west boundary (deg).
        refl_coef: Shoreline IG reflection coefficient (REFL [reflc], 0–1).
        rdiff_pown: Diffuse reflection power (RDIFF [pown]).  REQUIRED — omitting
            causes a SWAN severe error ("No value for variable POWN").
    """

    dx: float = 5.0
    ny: int = 20
    dy: float = 25.0
    gamma: float = 0.73
    dir_spread: float = 10.0
    refl_coef: float = 0.5
    rdiff_pown: int = 2


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SurfBeatResult:
    """Output of a SurfBeat strip run.

    Attributes:
        hs_ig_shoreline: IG significant wave height at the most shoreward
            output station (m).
        set_timing_minutes: Set timing estimated from the IG spectral peak
            period (minutes).  ``None`` when the IG band has no measurable
            energy.
        set_amplitude_m: Display-friendly alias for ``hs_ig_shoreline`` (m).
            ``None`` when the IG computation produced no valid energy.
            Maps to the API field ``setAmplitudeM`` (API-MANUAL §17).
        hs_sw_profile: Sea-swell Hs at each output station, ordered
            offshore → shore.  Each element is
            ``{"distance_m": float, "hs_m": float}`` where ``distance_m``
            is distance from the shoreline (m).
        runtime_ms: Wall-clock time of the SWAN subprocess (milliseconds).
    """

    hs_ig_shoreline: float
    set_timing_minutes: float | None
    set_amplitude_m: float | None
    hs_sw_profile: list[dict]
    runtime_ms: float


# ---------------------------------------------------------------------------
# Internal — profile interpolation
# ---------------------------------------------------------------------------


def _interp_profile(profile: np.ndarray, dx: float) -> tuple[np.ndarray, float]:
    """Interpolate a bathymetric profile onto a regular cross-shore grid.

    Args:
        profile: Nx2 array ``[[distance_from_shore_m, depth_m], ...]``.
            Depths must be positive (below water).  Row order is arbitrary —
            the function sorts internally.
        dx: Cross-shore grid spacing (m).

    Returns:
        ``(depths, strip_length)`` where ``depths[0]`` is the offshore end,
        ``depths[-1]`` is the shoreline end, and all values are ≥ 0.01 m.
        ``strip_length`` is the total cross-shore extent (m).

    Raises:
        ValueError: Profile has fewer than 2 points or zero/negative extent.
    """
    if profile.shape[0] < 2:
        raise ValueError(
            f"Profile must have ≥2 points, got {profile.shape[0]}"
        )

    # Sort by distance_from_shore ascending (shore first, offshore last)
    idx_sort = np.argsort(profile[:, 0])
    distances = profile[idx_sort, 0]   # ascending: shore → offshore
    depths_raw = profile[idx_sort, 1]  # corresponding depths

    d_min = float(distances[0])   # smallest distance (shoreline end)
    d_max = float(distances[-1])  # largest distance (offshore end)
    strip_length = d_max - d_min

    if strip_length <= 0.0:
        raise ValueError(
            f"Profile cross-shore extent is {strip_length:.2f} m (must be > 0)"
        )

    # Regular grid: n_points uniformly spaced from d_min to d_max
    n_points = int(round(strip_length / dx)) + 1
    d_regular = np.linspace(d_min, d_max, n_points)

    # Interpolate depth at regular grid points
    depths_regular = np.interp(d_regular, distances, depths_raw)
    depths_regular = np.maximum(depths_regular, 0.01)  # avoid dry/zero cells

    # Reverse so index 0 = offshore (d_max) = west, index -1 = shore (d_min) = east.
    # This matches BOTTOM.txt idla=3 west→east convention and the strip's
    # coordinate frame (x=0 at west boundary, x=xlenc at east/shore boundary).
    depths_regular = depths_regular[::-1]

    return depths_regular, strip_length


# ---------------------------------------------------------------------------
# Internal — SWAN INPUT / BOTTOM file generation
# ---------------------------------------------------------------------------


def _generate_strip_files(
    spot_id: str,
    profile: np.ndarray,
    hs: float,
    tp: float,
    direction: float,
    cfjon: float,
    workdir: Path,
    config: SurfBeatStripConfig,
    wind_speed_ms: float = 0.0,
    wind_direction_deg: float = 0.0,
) -> dict:
    """Generate SWAN INPUT + BOTTOM.txt + WIND.txt for the SurfBeat strip.

    INPUT syntax strictly follows swan-commands-extract.md §SURFBEAT and
    SURF-MODEL-FIX-PLAN §0.4.  Key rules enforced:

    - Regular 2D REG grid (not 1D, not curvilinear) — required by SURFBEAT.
    - MODE STATIONARY — required by SURFBEAT.
    - BOUND SIDE WEST only — required by SURFBEAT.
    - Bare COMPUTE (×2, not "COMPUTE STAT").
    - OBSTACLE TRANSM 0.0 REFL [reflc] RDIFF [pown] — pown is REQUIRED.
    - SURFBEAT command placed before COMPUTEs.
    - BOTTOM.txt idla=3: row 0 = south, columns west → east.
    - WIND.txt: uniform wind field (HRRR interpolated to spot coordinates).

    Args:
        spot_id: Spot identifier (used in logging only).
        profile: Nx2 ``[[distance_from_shore_m, depth_m], ...]`` array.
        hs: Offshore significant wave height at the west boundary (m).
        tp: Peak wave period (s).
        direction: Incident wave direction, nautical convention (° clockwise
            from north, "coming from").  270° = from west = shore-normal.
        cfjon: JONSWAP bottom friction coefficient.
        workdir: Directory where INPUT and BOTTOM.txt are written.
        config: Strip grid and physics configuration.

    Returns:
        dict with keys:
        - ``nx``, ``ny`` — mesh cell counts
        - ``xlenc``, ``ylenc`` — grid extents (m)
        - ``station_names`` — list of station name strings
        - ``station_distances_from_shore`` — list of floats (m)
        - ``n_stations`` — number of output stations
    """
    depths, strip_length = _interp_profile(profile, config.dx)

    # Grid dimensions: mesh count = point count − 1
    nx = len(depths) - 1
    ny = config.ny
    xlenc = nx * config.dx   # total cross-shore extent (m)
    ylenc = ny * config.dy   # total alongshore extent (m)
    xp, yp = 0.0, 0.0       # grid origin in strip-local coordinates

    # --- BOTTOM.txt ---
    # idla=3: row 0 = south, last row = north; columns west (i=0) → east (i=nx).
    # Bathymetry is alongshore-uniform: every row is identical.
    # depths[0] = offshore (west), depths[nx] = shore (east).
    bottom_path = workdir / "BOTTOM.txt"
    with bottom_path.open("w", encoding="ascii") as fh:
        for _row in range(ny + 1):  # ny+1 rows = ny mesh rows + 1 boundary row
            fh.write(" ".join(f"{d:.2f}" for d in depths) + "\n")

    # --- Output stations along the centerline ---
    # Spacing ≤ 25 m (T2.3 requirement: station within 25 m of any SwellTrack
    # break point).  For dx=5 m → step=5 cells = 25 m spacing.
    station_step = max(1, round(_STATION_SPACING_M / config.dx))
    y_center = ylenc / 2.0

    station_indices = list(range(0, nx + 1, station_step))
    if station_indices[-1] != nx:
        station_indices.append(nx)  # always include the shore boundary

    station_xs = [i * config.dx for i in station_indices]
    station_distances_from_shore = [xlenc - x for x in station_xs]
    station_names = [f"S{i:02d}" if i < 100 else f"S{i}" for i in range(len(station_xs))]

    # --- Frequency range ---
    # IG energy starts at ~0.004 Hz (250 s period); sea-swell extends to 1.0 Hz.
    # 60 bins: CIRCLE 36 0.004 1.0 60 — matches verified benchmark template.
    flow = 0.004
    fhigh = 1.0
    nfreq = 60

    # --- Write INPUT ---
    input_path = workdir / "INPUT"
    with input_path.open("w", encoding="ascii") as f:
        f.write("PROJECT 'SBstrip' '001'\n")
        f.write("$\n")
        f.write("SET 0. 90. 0.05 200 3\n")
        f.write("SET NAUTICAL\n")
        f.write("COORDINATES CARTESIAN\n")
        f.write("$\n")
        f.write("MODE STATIONARY\n")
        f.write("$\n")
        # Regular 2D grid — SURFBEAT requires REG, not 1D or curvilinear
        f.write(
            f"CGRID REG {xp:.1f} {yp:.1f} 0. {xlenc:.1f} {ylenc:.1f} "
            f"{nx} {ny} CIRCLE 36 {flow} {fhigh} {nfreq}\n"
        )
        f.write("$\n")
        f.write(
            f"INPGRID BOTTOM REG {xp:.1f} {yp:.1f} 0. {nx} {ny} "
            f"{config.dx:.1f} {config.dy:.1f}\n"
        )
        # idla=3: south→north rows, west→east columns; scale=1.0; FREE format
        f.write("READINP BOTTOM 1. 'BOTTOM.txt' 3 0 FREE\n")
        f.write("$\n")
        # Uniform wind field (HRRR at spot coordinates for this timestep).
        # GEN3 WESTHUYSEN requires a wind input — SWAN refuses to run
        # quadruplets without one.
        _wind_rad = math.radians(wind_direction_deg)
        _wx = -wind_speed_ms * math.sin(_wind_rad)
        _wy = -wind_speed_ms * math.cos(_wind_rad)
        f.write(
            f"INPGRID WIND REG {xp:.1f} {yp:.1f} 0. {nx} {ny} "
            f"{config.dx:.1f} {config.dy:.1f}\n"
        )
        f.write("READINP WIND 1. 'WIND.txt' 3 0 FREE\n")
        wind_path = workdir / "WIND.txt"
        with wind_path.open("w", encoding="ascii") as wf:
            for _row in range(ny + 1):
                wf.write(" ".join(f"{_wx:.4f}" for _ in range(nx + 1)) + "\n")
            for _row in range(ny + 1):
                wf.write(" ".join(f"{_wy:.4f}" for _ in range(nx + 1)) + "\n")
        f.write("$\n")
        # West boundary only — SURFBEAT rejects any other boundary specification
        # v1: parametric JONSWAP (design decision; L2 SPECOUT boundary is future work)
        f.write(
            f"BOUND SIDE WEST CCW CONstant PAR "
            f"{hs:.3f} {tp:.1f} {direction:.1f} {config.dir_spread:.1f}\n"
        )
        f.write("$\n")
        # Physics (same as L2/L3 production runs per swan-commands-extract.md table)
        f.write("GEN3 WESTHUYSEN\n")
        f.write(f"BREAKING CONSTANT 1.0 {config.gamma}\n")
        f.write(f"FRICTION JON {cfjon}\n")
        f.write("TRIAD\n")
        f.write("$\n")
        f.write("QUANTITY HSIGN TM01 DIR excv=-9.\n")
        f.write("$\n")
        # Shoreline OBSTACLE on the east boundary with IG reflection.
        # RDIFF [pown] is REQUIRED — omitting pown causes SWAN severe error
        # "No value for variable POWN" (observed in benchmark Round 1).
        x_east = xlenc
        f.write(
            f"OBSTACLE TRANSM 0.0 REFL {config.refl_coef:.2f} "
            f"RDIFF {config.rdiff_pown} "
            f"LINE {x_east:.1f} 0.0 {x_east:.1f} {ylenc:.1f}\n"
        )
        f.write("$\n")
        # SURFBEAT command must appear before the COMPUTEs
        f.write("SURFBEAT\n")
        f.write("$\n")
        # Output stations along the centerline
        for sname, sx in zip(station_names, station_xs):
            f.write(f"POINTS '{sname}' {sx:.1f} {y_center:.1f}\n")
        f.write("$\n")
        # SPECOUT at every station (SPEC2D ABS — 2D directional spectrum,
        # absolute variance density).  Parser uses only the shore station.
        for sname in station_names:
            f.write(f"SPECOUT '{sname}' SPEC2D ABS 'SPEC_{sname}.txt'\n")
        f.write("$\n")
        # TABLE at every station for Hs_sw profile
        for sname in station_names:
            f.write(
                f"TABLE '{sname}' HEAD 'TABLE_{sname}.txt' "
                f"HSIGN HSWELL TM01 DIR DEPTH QB\n"
            )
        f.write("$\n")
        # Two bare COMPUTEs — NOT "COMPUTE STAT" (triggers "Illegal keyword: STAT")
        # First COMPUTE:  sea-swell spectrum + bound infragravity waves
        # Second COMPUTE: reflected (free) infragravity waves (shoreline OBSTACLE)
        f.write("COMPUTE\n")
        f.write("COMPUTE\n")
        f.write("$\n")
        f.write("STOP\n")

    return {
        "nx": nx,
        "ny": ny,
        "xlenc": xlenc,
        "ylenc": ylenc,
        "station_names": station_names,
        "station_distances_from_shore": station_distances_from_shore,
        "n_stations": len(station_names),
    }


# ---------------------------------------------------------------------------
# Internal — SWAN subprocess
# ---------------------------------------------------------------------------


def _run_swan_subprocess(
    workdir: Path,
    swan_binary: str,
    timeout_s: int,
) -> None:
    """Run the SWAN executable and check for failure.

    SWAN reads the INPUT command file from stdin and writes all output files
    to ``workdir``.  Stdout/stderr are captured.

    Error detection (matches swan_runner.py pattern):
    1. Non-zero exit code → SurfBeatRunError.
    2. SWAN can exit 0 despite writing "Severe error" to stderr or Errfile —
       both are checked.

    Raises:
        SurfBeatRunError: Non-zero exit code, timeout, binary not found, or
            severe error detected in SWAN output.
    """
    input_file = workdir / "INPUT"

    try:
        proc = subprocess.run(
            [swan_binary],
            stdin=input_file.open("r", encoding="ascii"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workdir),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SurfBeatRunError(
            f"SWAN timed out after {timeout_s}s (workdir={workdir})",
            stderr="",
            returncode=None,
        ) from exc
    except FileNotFoundError as exc:
        raise SurfBeatRunError(
            f"SWAN binary not found: {swan_binary!r}",
            stderr="",
            returncode=None,
        ) from exc

    stderr_text = proc.stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise SurfBeatRunError(
            f"SWAN exited with code {proc.returncode} (workdir={workdir})",
            stderr=stderr_text.strip()[:2000],
            returncode=proc.returncode,
        )

    # SWAN (Fortran) can exit 0 despite a severe error.  Check both
    # stderr and the Errfile that SWAN writes on fatal conditions.
    errfile_path = workdir / "Errfile"
    errfile_text = ""
    if errfile_path.exists():
        errfile_text = errfile_path.read_text(encoding="utf-8", errors="replace")

    combined = stderr_text + errfile_text
    if "Severe error" in combined or "SNAME is too long" in combined:
        raise SurfBeatRunError(
            f"SWAN exited 0 but reported severe errors (workdir={workdir})",
            stderr=combined.strip()[:2000],
            returncode=0,
        )


# ---------------------------------------------------------------------------
# Internal — output parsing
# ---------------------------------------------------------------------------


def _parse_table_hsign(table_path: Path) -> float:
    """Return HSIGN from the last data row of a SWAN TABLE HEAD file.

    With two COMPUTEs, the TABLE file has two data rows.  The last row
    contains the final state (after the second COMPUTE adds reflected free IG).

    Args:
        table_path: Path to ``TABLE_Snn.txt``.

    Returns:
        Hs_sw at this station from the final compute (m).

    Raises:
        SurfBeatRunError: File missing, HSIGN column absent, or no data rows.
    """
    if not table_path.exists():
        raise SurfBeatRunError(
            f"TABLE output not found: {table_path}"
        )

    lines = table_path.read_text(encoding="utf-8", errors="replace").splitlines()

    # Locate the header row: first non-blank, non-comment line whose first
    # token is NOT a valid float (i.e. it is a column name, not data).
    header_cols: list[str] = []
    data_start: int = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("$"):
            continue
        tokens = stripped.split()
        try:
            float(tokens[0])
            # First parseable line is data (rare but possible if header was
            # suppressed).  Treat this line as data_start with no header.
            data_start = i
            break
        except (ValueError, IndexError):
            # This is the header row.
            header_cols = tokens
            data_start = i + 1
            break

    if "HSIGN" not in header_cols:
        raise SurfBeatRunError(
            f"HSIGN column not found in TABLE header of {table_path} "
            f"(columns found: {header_cols})"
        )

    hsign_col = header_cols.index("HSIGN")

    # Collect all numeric data rows
    data_rows: list[list[float]] = []
    for line in lines[data_start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("$"):
            continue
        try:
            row = [float(t) for t in stripped.split()]
            data_rows.append(row)
        except ValueError:
            continue

    if not data_rows:
        raise SurfBeatRunError(
            f"No numeric data rows in TABLE file {table_path}"
        )

    last_row = data_rows[-1]
    if hsign_col >= len(last_row):
        raise SurfBeatRunError(
            f"HSIGN column index {hsign_col} out of range in {table_path} "
            f"(row has {len(last_row)} columns)"
        )

    return float(last_row[hsign_col])


def _parse_specout_header(
    lines: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Extract frequency and direction arrays from a SWAN SPECOUT file header.

    Reads the ``RFREQ`` and ``NDIR`` sections, which appear near the top of
    every SWAN SPECOUT SPEC2D file.

    Args:
        lines: All lines of the SPECOUT file (already read).

    Returns:
        ``(frequencies_hz, directions_deg)`` as 1D float64 arrays.

    Raises:
        SurfBeatRunError: RFREQ or NDIR section not found.
    """
    frequencies: list[float] = []
    directions: list[float] = []
    nfreq = 0
    ndir = 0
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        if stripped == "RFREQ":
            i += 1
            nfreq = int(lines[i].strip())
            tokens: list[str] = []
            while len(tokens) < nfreq and i + 1 < len(lines):
                i += 1
                tokens.extend(lines[i].split())
            frequencies = [float(t) for t in tokens[:nfreq]]

        elif stripped == "NDIR":
            i += 1
            ndir = int(lines[i].strip())
            tokens = []
            while len(tokens) < ndir and i + 1 < len(lines):
                i += 1
                tokens.extend(lines[i].split())
            directions = [float(t) for t in tokens[:ndir]]

        if frequencies and directions:
            break
        i += 1

    if not frequencies:
        raise SurfBeatRunError(
            "RFREQ section not found in SPECOUT header — file may be corrupt"
        )
    if not directions:
        raise SurfBeatRunError(
            "NDIR section not found in SPECOUT header — file may be corrupt"
        )

    return np.array(frequencies, dtype=np.float64), np.array(directions, dtype=np.float64)


def _extract_last_spectrum(
    lines: list[str],
    nfreq: int,
    ndir: int,
) -> np.ndarray:
    """Extract the 2D spectrum from the last COMPUTE block in a SPECOUT file.

    SPECOUT SPEC2D ABS block structure::

        FACTOR
          <scale>          ← floating-point scale (1 line)
          <int> <int> ...  ← ndir × nfreq integers (may span multiple lines)

    With two COMPUTEs there are two FACTOR blocks.  This function returns
    the spectrum from the **last** block (second COMPUTE = reflected free IG).

    Args:
        lines: All lines of the SPECOUT file (already read).
        nfreq: Number of frequency bins (from RFREQ header).
        ndir: Number of directional bins (from NDIR header).

    Returns:
        2D array ``(ndir, nfreq)`` of variance density in m²/Hz/rad.
        Negative values (SWAN exception markers) are clamped to 0.

    Raises:
        SurfBeatRunError: Fewer than 2 FACTOR blocks (second COMPUTE did not
            complete), or insufficient integer data.
    """
    # Locate all FACTOR lines
    factor_positions = [
        i for i, line in enumerate(lines) if line.strip() == "FACTOR"
    ]

    if len(factor_positions) < 2:
        raise SurfBeatRunError(
            f"Expected ≥2 FACTOR blocks in SPECOUT (two-COMPUTE procedure), "
            f"found {len(factor_positions)}.  "
            f"The second COMPUTE may not have completed — check SWAN output."
        )

    last_factor_idx = factor_positions[-1]

    # Scale factor is on the line immediately after FACTOR
    try:
        scale = float(lines[last_factor_idx + 1].strip())
    except (IndexError, ValueError) as exc:
        raise SurfBeatRunError(
            "Could not read FACTOR scale value from SPECOUT"
        ) from exc

    # Read ndir × nfreq integers from lines after the scale line.
    # Values may span multiple lines; stop as soon as we have enough.
    expected = ndir * nfreq
    int_tokens: list[int] = []
    j = last_factor_idx + 2
    while len(int_tokens) < expected and j < len(lines):
        for tok in lines[j].split():
            try:
                int_tokens.append(int(tok))
            except ValueError:
                pass  # skip any non-integer tokens (e.g. blank lines)
        j += 1

    if len(int_tokens) < expected:
        raise SurfBeatRunError(
            f"Not enough spectral data integers in SPECOUT: "
            f"expected {expected} ({ndir}×{nfreq}), got {len(int_tokens)}"
        )

    # Reshape to (ndir, nfreq) and apply FACTOR scale
    arr = np.array(int_tokens[:expected], dtype=np.float64).reshape(ndir, nfreq)
    spectrum = arr * scale  # units: m²/Hz/rad

    # Clamp negatives: SWAN exception markers produce large negative values
    # after multiplication (e.g. round(-999 / 1e-8) × 1e-8 = -999).
    np.maximum(spectrum, 0.0, out=spectrum)

    return spectrum


def _compute_ig_stats(
    frequencies: np.ndarray,
    spectrum: np.ndarray,
    ig_split_hz: float = _IG_SPLIT_HZ,
) -> tuple[float, float | None]:
    """Compute Hs_ig and set timing from the IG band of a 2D variance spectrum.

    Design decisions (SURF-MODEL-FIX-PLAN §T2.1, coordinator):

    - **IG band:** f < ``ig_split_hz`` (default 0.04 Hz).
    - **Omnidirectional spectrum:** E_omni(f) = Σ_θ E(f, θ) · dθ
      where dθ = 2π / ndir (full CIRCLE 36 grid → dθ = 10° = π/18 rad).
    - **IG variance:** m0_ig = ∫ E_omni(f) df  for f < 0.04 Hz  (trapz).
    - **Hs_ig:** 4 · sqrt(m0_ig) — standard significant wave height (WMO/IAHR).
    - **Set timing:** T_ig = 1 / f_peak_ig (s), converted to minutes.

    Args:
        frequencies: 1D array of frequency bins (Hz), shape (nfreq,).
        spectrum: 2D variance density (ndir, nfreq) in m²/Hz/rad.
        ig_split_hz: IG/sea-swell split frequency (Hz).

    Returns:
        ``(hs_ig, set_timing_minutes)`` — ``set_timing_minutes`` is ``None``
        when the IG band contains no measurable energy (m0_ig ≤ 0).
    """
    ndir = spectrum.shape[0]
    dtheta = 2.0 * math.pi / ndir  # rad per directional bin (CIRCLE 36 → π/18)

    # Omnidirectional spectrum: integrate over all directions (m²/Hz)
    e_omni = np.sum(spectrum, axis=0) * dtheta  # shape: (nfreq,)

    # IG band mask
    ig_mask = frequencies < ig_split_hz
    if not np.any(ig_mask):
        logger.warning(
            "No frequency bins below %.4f Hz — IG output not possible "
            "(check CGRID frequency range)", ig_split_hz
        )
        return 0.0, None

    f_ig = frequencies[ig_mask]
    e_ig = e_omni[ig_mask]

    # IG variance by trapezoidal integration
    m0_ig = float(np.trapz(e_ig, f_ig))
    if m0_ig <= 0.0:
        return 0.0, None

    # Hs_ig = 4 · sqrt(m0_ig)  — standard WMO/IAHR significant wave height
    hs_ig = 4.0 * math.sqrt(m0_ig)

    # Set timing from the frequency of peak IG energy
    peak_idx = int(np.argmax(e_ig))
    f_peak_ig = float(f_ig[peak_idx])
    set_timing_minutes: float | None = None
    if f_peak_ig > 0.0:
        set_timing_minutes = (1.0 / f_peak_ig) / 60.0

    return hs_ig, set_timing_minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_surfbeat_strip(
    spot_id: str,
    profile: np.ndarray,
    hs: float,
    tp: float,
    direction: float,
    cfjon: float,
    swan_binary: str,
    workdir_base: str = _DEFAULT_WORKDIR_BASE,
    config: SurfBeatStripConfig | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    wind_speed_ms: float = 0.0,
    wind_direction_deg: float = 0.0,
) -> SurfBeatResult:
    """Run the SurfBeat strip for a surf spot and return IG wave statistics.

    Workflow:

    1. Generate SWAN INPUT + BOTTOM.txt in
       ``{workdir_base}/surfbeat_{spot_id}/``.
    2. Run SWAN as a subprocess (two-COMPUTE procedure: sea-swell + bound IG,
       then reflected free IG).
    3. Parse TABLE output at all centerline stations → Hs_sw profile.
    4. Parse SPECOUT at the most shoreward station → Hs_ig, set timing.
    5. Return ``SurfBeatResult`` and clean up the working directory.

    The working directory is deleted on success.  On failure it is preserved
    for post-mortem inspection.

    Args:
        spot_id: Spot identifier used for directory naming and log messages.
        profile: Nx2 array ``[[distance_from_shore_m, depth_m], ...]``.
            Depths must be positive (below water surface).  Row order does not
            matter — the function sorts by distance internally.  Must have
            ≥2 points.
        hs: Offshore significant wave height at the west (offshore) boundary
            of the strip (m).
        tp: Peak wave period (s).
        direction: Incident wave direction in **nautical convention** (degrees
            clockwise from north, "coming from").  270° = from west =
            shore-normal for a west-facing beach.
        cfjon: JONSWAP bottom friction coefficient.  Recommended values:
            0.038 for swell conditions, 0.067 for wind sea
            (SURF-MODEL-FIX-PLAN §T0.3 / API-MANUAL §17).
        swan_binary: Absolute path to the SWAN executable.
        workdir_base: Base directory under which the per-spot working directory
            is created.  Default: ``/var/run/weewx-clearskies/swan``.
        config: Strip grid and physics configuration.  ``None`` uses defaults
            from :class:`SurfBeatStripConfig`.
        timeout_s: SWAN subprocess timeout in seconds.  Default: 120.

    Returns:
        :class:`SurfBeatResult` containing IG wave statistics, sea-swell
        profile, and runtime.

    Raises:
        SurfBeatRunError: SWAN failed to run, timed out, or output parsing
            failed.
        ValueError: Invalid ``profile`` array (wrong shape, too few points,
            or zero cross-shore extent).
    """
    if config is None:
        config = SurfBeatStripConfig()

    # --- Input validation ---
    if profile.ndim != 2 or profile.shape[1] != 2:
        raise ValueError(
            f"profile must be a Nx2 array, got shape {profile.shape}"
        )
    if profile.shape[0] < 2:
        raise ValueError(
            f"profile must have ≥2 points, got {profile.shape[0]}"
        )

    workdir = Path(workdir_base) / f"surfbeat_{spot_id}"

    logger.info(
        "SurfBeat strip START: spot=%s Hs=%.2fm Tp=%.1fs Dir=%.1f° "
        "cfjon=%.4f workdir=%s",
        spot_id, hs, tp, direction, cfjon, workdir,
    )

    # Create a clean working directory (remove any remnant from a previous run)
    if workdir.exists():
        shutil.rmtree(str(workdir))
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SurfBeatRunError(
            f"Cannot create SurfBeat workdir {workdir}: {exc}"
        ) from exc

    try:
        # ------------------------------------------------------------------
        # 1. Generate INPUT + BOTTOM.txt
        # ------------------------------------------------------------------
        strip_meta = _generate_strip_files(
            spot_id=spot_id,
            profile=profile,
            hs=hs,
            tp=tp,
            direction=direction,
            cfjon=cfjon,
            workdir=workdir,
            config=config,
            wind_speed_ms=wind_speed_ms,
            wind_direction_deg=wind_direction_deg,
        )
        station_names: list[str] = strip_meta["station_names"]
        station_distances: list[float] = strip_meta["station_distances_from_shore"]
        n_stations: int = strip_meta["n_stations"]

        logger.info(
            "SurfBeat strip: grid %d×%d cells, %.0f m cross-shore, %d stations",
            strip_meta["nx"], strip_meta["ny"],
            strip_meta["xlenc"], n_stations,
        )

        # ------------------------------------------------------------------
        # 2. Run SWAN (two-COMPUTE procedure)
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        _run_swan_subprocess(workdir, swan_binary, timeout_s)
        runtime_ms = (time.monotonic() - t0) * 1000.0

        logger.info("SurfBeat strip: SWAN completed in %.0f ms", runtime_ms)

        # ------------------------------------------------------------------
        # 3. Parse TABLE at all stations → Hs_sw profile
        # ------------------------------------------------------------------
        hs_sw_profile: list[dict] = []
        for sname, dist in zip(station_names, station_distances):
            tbl_path = workdir / f"TABLE_{sname}.txt"
            hs_m = _parse_table_hsign(tbl_path)
            hs_sw_profile.append({"distance_m": dist, "hs_m": hs_m})

        # Sort offshore → shore (descending distance_m = offshore first)
        hs_sw_profile.sort(key=lambda d: d["distance_m"], reverse=True)

        # ------------------------------------------------------------------
        # 4. Parse SPECOUT at the most shoreward station → Hs_ig, set timing
        # ------------------------------------------------------------------
        # The last station name corresponds to the shore end of the strip
        # (station_xs[-1] = xlenc → distance_from_shore ≈ 0).
        shore_station = station_names[-1]
        specout_path = workdir / f"SPEC_{shore_station}.txt"

        if not specout_path.exists():
            raise SurfBeatRunError(
                f"SPECOUT output not found for shore station "
                f"{shore_station!r}: {specout_path}"
            )

        spec_lines = specout_path.read_text(encoding="utf-8", errors="replace").splitlines()
        frequencies, directions = _parse_specout_header(spec_lines)
        nfreq = len(frequencies)
        ndir = len(directions)

        spectrum = _extract_last_spectrum(spec_lines, nfreq, ndir)
        hs_ig, set_timing_minutes = _compute_ig_stats(frequencies, spectrum)

        logger.info(
            "SurfBeat strip DONE: spot=%s Hs_ig=%.4f m set_timing=%s min "
            "Hs_sw_shore=%.4f m runtime=%.0f ms",
            spot_id,
            hs_ig,
            f"{set_timing_minutes:.1f}" if set_timing_minutes is not None else "None",
            hs_sw_profile[-1]["hs_m"] if hs_sw_profile else float("nan"),
            runtime_ms,
        )

        result = SurfBeatResult(
            hs_ig_shoreline=hs_ig,
            set_timing_minutes=set_timing_minutes,
            set_amplitude_m=hs_ig if hs_ig > 0.0 else None,
            hs_sw_profile=hs_sw_profile,
            runtime_ms=runtime_ms,
        )

    except Exception:
        # Preserve workdir on any failure for post-mortem debugging
        logger.exception(
            "SurfBeat strip FAILED for spot=%s — workdir preserved at %s",
            spot_id, workdir,
        )
        raise

    # ------------------------------------------------------------------
    # 5. Clean up working directory on success
    # ------------------------------------------------------------------
    try:
        shutil.rmtree(str(workdir))
        logger.debug("SurfBeat strip: cleaned up workdir %s", workdir)
    except OSError:
        logger.warning(
            "SurfBeat strip: could not clean up workdir %s (non-fatal)", workdir
        )

    return result
