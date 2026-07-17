"""GRIB2 processing with eccodes/pygrib dual backend (PROVIDER-MANUAL §14.6).

Provides read_grib_fields() for extracting named fields from GRIB2 files
into Python lists (no numpy dependency — HRRR and SWAN grids are tractable in pure Python).

Backend selection:
  1. Try ``import eccodes`` (ECMWF's C-library Python binding).
  2. Fall back to ``import pygrib`` (alternative GRIB2 library).
  3. If both fail, set ``GRIB_AVAILABLE = False``.

When marine config is present but no GRIB backend is available, importing
this module does NOT crash — callers check ``GRIB_AVAILABLE`` and raise
with platform-specific install instructions at registration time.

Ported from Phase II ``GRIBProcessor`` (~42 lines), expanded with:
  - Dual-backend support (eccodes primary, pygrib fallback)
  - Grid geo-reference metadata alongside field data
  - No numpy — pure Python lists for small grids
  - Merged duplicate ``apply_breaking_limit`` into single implementation
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

_BACKEND: str | None = None

try:
    import eccodes  # type: ignore[import-untyped]

    _BACKEND = "eccodes"
except ImportError:
    try:
        import pygrib  # type: ignore[import-untyped]

        _BACKEND = "pygrib"
    except ImportError:
        pass

GRIB_AVAILABLE: bool = _BACKEND is not None

_INSTALL_INSTRUCTIONS = (
    "The marine feature requires the eccodes GRIB processing library.\n"
    "Install instructions by platform:\n"
    "  Debian/Ubuntu:  apt install libeccodes-dev && pip install eccodes\n"
    "  RHEL/Fedora:    dnf install eccodes-devel && pip install eccodes\n"
    "  macOS:          brew install eccodes && pip install eccodes\n"
    "  Docker:         eccodes is included by default in the Clear Skies API image.\n"
    "Alternatively, install pygrib: pip install pygrib"
)


def check_grib_available() -> None:
    """Raise RuntimeError with install instructions if no GRIB backend is available."""
    if not GRIB_AVAILABLE:
        raise RuntimeError(
            f"No GRIB2 processing library found (tried eccodes, pygrib).\n{_INSTALL_INSTRUCTIONS}"
        )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class GribFieldData:
    """Extracted GRIB2 field: 2D grid of values with geo-reference metadata."""

    short_name: str
    values: list[list[float]]
    ni: int  # grid columns
    nj: int  # grid rows
    lat_first: float
    lon_first: float
    lat_last: float
    lon_last: float
    end_step: int = 0  # forecast hour (0 = analysis/current conditions)

    @property
    def lat_step(self) -> float:
        if self.nj <= 1:
            return 0.0
        return (self.lat_last - self.lat_first) / (self.nj - 1)

    @property
    def lon_step(self) -> float:
        if self.ni <= 1:
            return 0.0
        return (self.lon_last - self.lon_first) / (self.ni - 1)


@dataclass
class GribResult:
    """Collection of extracted fields from a GRIB2 file."""

    fields: dict[str, GribFieldData] = field(default_factory=dict)

    def get(self, name: str) -> GribFieldData | None:
        return self.fields.get(name)


# ---------------------------------------------------------------------------
# eccodes backend
# ---------------------------------------------------------------------------


def _read_eccodes(
    file_path: str, field_names: set[str], target_step: int | None = None
) -> GribResult:
    """Read GRIB2 fields using eccodes."""
    result = GribResult()

    with open(file_path, "rb") as f:
        while True:
            msgid = eccodes.codes_grib_new_from_file(f)
            if msgid is None:
                break
            try:
                short_name: str = eccodes.codes_get(msgid, "shortName")
                if short_name not in field_names:
                    continue

                end_step = eccodes.codes_get(msgid, "endStep")
                if target_step is not None and end_step != target_step:
                    continue

                raw_values = eccodes.codes_get_values(msgid)
                ni: int = eccodes.codes_get(msgid, "Ni")
                nj: int = eccodes.codes_get(msgid, "Nj")
                lat_first: float = eccodes.codes_get(
                    msgid, "latitudeOfFirstGridPointInDegrees"
                )
                lon_first: float = eccodes.codes_get(
                    msgid, "longitudeOfFirstGridPointInDegrees"
                )
                lat_last: float = eccodes.codes_get(
                    msgid, "latitudeOfLastGridPointInDegrees"
                )
                lon_last: float = eccodes.codes_get(
                    msgid, "longitudeOfLastGridPointInDegrees"
                )

                values_2d: list[list[float]] = []
                for j in range(nj):
                    row_start = j * ni
                    row = [float(raw_values[row_start + i]) for i in range(ni)]
                    values_2d.append(row)

                result.fields[short_name] = GribFieldData(
                    short_name=short_name,
                    values=values_2d,
                    ni=ni,
                    nj=nj,
                    lat_first=lat_first,
                    lon_first=lon_first,
                    lat_last=lat_last,
                    lon_last=lon_last,
                    end_step=end_step,
                )
            except Exception:
                logger.warning("Failed to read GRIB message for field %s", short_name, exc_info=True)
            finally:
                eccodes.codes_release(msgid)

    return result


# ---------------------------------------------------------------------------
# pygrib backend
# ---------------------------------------------------------------------------


def _read_pygrib(
    file_path: str, field_names: set[str], target_step: int | None = None
) -> GribResult:
    """Read GRIB2 fields using pygrib."""
    result = GribResult()

    grbs = pygrib.open(file_path)
    try:
        for grb in grbs:
            short_name: str = grb.shortName
            if short_name not in field_names:
                continue

            end_step = grb.endStep
            if target_step is not None and end_step != target_step:
                continue

            try:
                data_2d = grb.values
                lats, lons = grb.latlons()

                nj = len(data_2d)
                ni = len(data_2d[0]) if nj > 0 else 0

                values_2d: list[list[float]] = []
                for j in range(nj):
                    row = [float(data_2d[j][i]) for i in range(ni)]
                    values_2d.append(row)

                lat_first = float(lats[0][0])
                lon_first = float(lons[0][0])
                lat_last = float(lats[-1][-1])
                lon_last = float(lons[-1][-1])

                result.fields[short_name] = GribFieldData(
                    short_name=short_name,
                    values=values_2d,
                    ni=ni,
                    nj=nj,
                    lat_first=lat_first,
                    lon_first=lon_first,
                    lat_last=lat_last,
                    lon_last=lon_last,
                    end_step=end_step,
                )
            except Exception:
                logger.warning(
                    "Failed to read GRIB message for field %s", short_name, exc_info=True
                )
    finally:
        grbs.close()

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_grib_fields(
    file_path: str, field_names: list[str], target_step: int | None = None
) -> GribResult:
    """Open a GRIB2 file and extract named fields into Python lists.

    Args:
        file_path: Path to the GRIB2 file on disk.
        field_names: GRIB2 shortName values to extract (e.g. ``["HTSGW", "PERPW"]``).
        target_step: Forecast hour (GRIB2 ``endStep``) to select. HRRR GRIB2
            files contain 48 hourly forecast timesteps per field; without
            filtering, messages are read in file order and the last match
            wins. Pass ``target_step=0`` to select only the analysis/current
            conditions timestep. When ``None``, all messages are read and the
            last matching message per field name wins (legacy behavior,
            preserved for backwards compatibility — callers that need a
            specific timestep MUST pass ``target_step`` explicitly).

    Returns:
        GribResult with extracted fields. Fields not present in the file
        (or not present at ``target_step``) are silently absent from the
        result (logged at WARNING).

    Raises:
        RuntimeError: No GRIB backend available.
        FileNotFoundError: ``file_path`` does not exist.
        OSError: File cannot be opened.
    """
    check_grib_available()

    requested = set(field_names)

    if _BACKEND == "eccodes":
        result = _read_eccodes(file_path, requested, target_step)
    else:
        result = _read_pygrib(file_path, requested, target_step)

    missing = requested - set(result.fields.keys())
    if missing:
        logger.warning(
            "GRIB2 file %s: requested fields not found: %s",
            file_path,
            ", ".join(sorted(missing)),
        )

    return result


def extract_nearest_value(
    grid: GribFieldData,
    lat: float,
    lon: float,
) -> float | None:
    """Extract the value at the grid point nearest to (lat, lon).

    Returns None if the point falls outside the grid bounds (with a half-cell
    tolerance) or if the value is a GRIB missing indicator (>=9.999e20).
    """
    if grid.nj <= 0 or grid.ni <= 0:
        return None

    lat_step = grid.lat_step
    lon_step = grid.lon_step

    if lat_step == 0.0 and lon_step == 0.0:
        # Single-cell grid — return the only value
        j, i = 0, 0
    elif lat_step == 0.0:
        j = 0
        i = round((lon - grid.lon_first) / lon_step)
    elif lon_step == 0.0:
        j = round((lat - grid.lat_first) / lat_step)
        i = 0
    else:
        j = round((lat - grid.lat_first) / lat_step)
        i = round((lon - grid.lon_first) / lon_step)

    if j < 0 or j >= grid.nj or i < 0 or i >= grid.ni:
        return None

    val = grid.values[j][i]

    if val >= 9.999e20:
        return None

    return val


def bilinear_interpolate(
    grid: GribFieldData,
    lat: float,
    lon: float,
) -> float | None:
    """Bilinear interpolation of grid values at exact (lat, lon).

    Sub-grid spatial interpolation using the four surrounding grid nodes
    (bilinear; same algorithm applied to HRRR and SWAN output grids).

    Returns None if the point falls outside the grid or any surrounding
    node has a missing value.
    """
    if grid.nj < 2 or grid.ni < 2 or grid.lat_step == 0.0 or grid.lon_step == 0.0:
        return extract_nearest_value(grid, lat, lon)

    lat_step = grid.lat_step
    lon_step = grid.lon_step

    j_float = (lat - grid.lat_first) / lat_step
    i_float = (lon - grid.lon_first) / lon_step

    j0 = int(math.floor(j_float))
    i0 = int(math.floor(i_float))
    j1 = j0 + 1
    i1 = i0 + 1

    if j0 < 0 or j1 >= grid.nj or i0 < 0 or i1 >= grid.ni:
        return extract_nearest_value(grid, lat, lon)

    v00 = grid.values[j0][i0]
    v01 = grid.values[j0][i1]
    v10 = grid.values[j1][i0]
    v11 = grid.values[j1][i1]

    _MISSING = 9.999e20
    if v00 >= _MISSING or v01 >= _MISSING or v10 >= _MISSING or v11 >= _MISSING:
        return extract_nearest_value(grid, lat, lon)

    dj = j_float - j0
    di = i_float - i0

    val = (
        v00 * (1 - dj) * (1 - di)
        + v01 * (1 - dj) * di
        + v10 * dj * (1 - di)
        + v11 * dj * di
    )
    return val


# ---------------------------------------------------------------------------
# Breaking limit (merged from Phase II duplicate implementations)
# ---------------------------------------------------------------------------


def apply_breaking_limit(
    wave_height: float,
    depth: float,
    gamma: float = 0.73,
) -> float:
    """Apply depth-induced breaking limit to wave height.

    Phase II had duplicate ``apply_breaking_limit`` definitions (audit §B.5,
    lines 4160 and 4581). This is the merged implementation that accepts a
    bottom-type-specific γ (breaker index) per ADR-084 Supplement 1.

    Args:
        wave_height: Significant wave height in meters.
        depth: Water depth at the breaking point in meters.
        gamma: Breaker index — default 0.73 (Battjes & Stive 1985 average).
            Site-specific γ from ``compute_breaker_gamma()`` should be passed
            when available.

    Returns:
        Wave height capped at the depth-induced breaking limit (γ × depth).
        If wave_height is already below the limit, returns wave_height unchanged.
    """
    if depth <= 0:
        return 0.0
    h_max = gamma * depth
    return min(wave_height, h_max)


def compute_breaker_gamma(
    beach_slope: float,
    wave_height: float,
    wave_period: float,
) -> float:
    """Compute site-specific breaker index γ via Battjes 1974 formula.

    ADR-084 Supplement 1: γ = 1.06 + 0.14 × ln(ξ)
    where ξ = tan(α) / √(H₀/L₀) is the Iribarren number.

    Args:
        beach_slope: Nearshore bottom slope (tan α), e.g. 0.02 for sand.
        wave_height: Deep-water significant wave height H₀ in meters.
        wave_period: Peak wave period T in seconds.

    Returns:
        Breaker index γ, clamped to [0.5, 1.4] per ADR-084 physical bounds.
    """
    if beach_slope <= 0 or wave_height <= 0 or wave_period <= 0:
        return 0.73  # default constant when inputs invalid

    g = 9.81
    l0 = g * wave_period**2 / (2 * math.pi)  # deep-water wavelength

    steepness = wave_height / l0
    if steepness <= 0:
        return 0.73

    xi = beach_slope / math.sqrt(steepness)  # Iribarren number

    if xi <= 0:
        return 0.73

    gamma = 1.06 + 0.14 * math.log(xi)

    return max(0.5, min(1.4, gamma))
