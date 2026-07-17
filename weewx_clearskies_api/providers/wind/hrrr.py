"""HRRR wind provider module (PROVIDER-MANUAL §14.14, ADR-093, ADR-094).

Fetches HRRR (High-Resolution Rapid Refresh) 10m AGL wind forecasts from
NOMADS Grib Filter for a configurable bounding box and rotates the
grid-relative wind components to earth-relative using the Lambert Conformal
Conic projection parameters extracted from the GRIB2 metadata.

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — NOMADS Grib Filter for HRRR 2D surface fields,
     one HTTP request per forecast hour (f00–f18 for standard cycles), with
     model-cycle fallback when the latest cycle returns 404.
  2. Response parsing — GRIB2 via eccodes (primary) or pygrib (fallback);
     extracts UGRD and VGRD at 10m above ground level.
  3. Wind rotation — Lambert Conformal Conic grid-relative to earth-relative
     using the full NCEP GRIB2 formula (PROVIDER-MANUAL §14.14):

       n     = cone_factor(latin1, latin2)          [derived from GRIB2 metadata]
       alpha = radians(n × (lon_p − lov))           [per grid point p]
       U_earth = U_grid × cos(alpha) − V_grid × sin(alpha)
       V_earth = U_grid × sin(alpha) + V_grid × cos(alpha)

     Source: NCEP Office Note 388, Appendix C.
     HRRR parameters: lov=262.5°, latin1=latin2=38.5°, n≈0.6225.

  4. Capability declaration — CAPABILITY symbol; None when no GRIB2 backend
     is available, so the provider is not registered.
  5. Error handling — 404 on all cycle attempts → ProviderUnavailableError;
     network failures → canonical taxonomy; GRIB2 parse error →
     ProviderProtocolError.

Data source (primary):
  NOMADS Grib Filter: https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl
  Supports geographic subsetting, variable selection, GRIB2 output. No API key.

Data source (backup):
  AWS S3: s3://noaa-hrrr-bdp-pds/ (not implemented in v1 — future T1.x).

Schedule: HRRR runs hourly (00Z–23Z). Data available ~45–60 min after run hour.
Cache TTL: 3300s (55 min) per PROVIDER-MANUAL §14.14.
Rate limit: 2 req/s to NOMADS (shared NOAA infrastructure).

Availability: Active only when the [nearshore] pip extra is installed. Not part
of the standard provider registry startup — invoked by the SWAN runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.errors import (
    ProviderProtocolError,
    ProviderUnavailableError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GRIB2 backend detection (eccodes primary, pygrib fallback)
# ---------------------------------------------------------------------------

_GRIB_BACKEND: str | None = None

try:
    import eccodes as _eccodes_lib  # type: ignore[import-untyped]

    _GRIB_BACKEND = "eccodes"
except ImportError:
    try:
        import pygrib as _pygrib_lib  # type: ignore[import-untyped]

        _GRIB_BACKEND = "pygrib"
    except ImportError:
        pass

GRIB_AVAILABLE: bool = _GRIB_BACKEND is not None

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "hrrr"
DOMAIN = "wind"

NOMADS_GRIB_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (HRRR wind provider)"

_CACHE_TTL_SECONDS = 3300           # 55 min per PROVIDER-MANUAL §14.14
_GRIB_READ_TIMEOUT = 60.0           # large GRIB2 files; allow a full minute
_MAX_CYCLE_FALLBACKS = 2            # try up to 3 cycles (current + 2 previous)
_MAX_FORECAST_HOURS = 18            # standard HRRR cycle (extended 00/06/12/18Z = 48)
_CYCLE_AVAILABILITY_LAG = timedelta(hours=1)  # data available ~45–60 min after run

# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.14)
# ---------------------------------------------------------------------------

if GRIB_AVAILABLE:
    from weewx_clearskies_api.providers._common.capability import (
        ProviderAttribution,
        ProviderCapability,
    )

    CAPABILITY: ProviderCapability | None = ProviderCapability(
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
        supplied_canonical_fields=(
            "windUEarth",
            "windVEarth",
        ),
        geographic_coverage="us",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL_SECONDS,
        operator_notes=(
            "NOAA HRRR 10m AGL wind forecast via NOMADS Grib Filter. "
            "US CONUS coverage. No API key required. "
            "Requires eccodes or pygrib for GRIB2 processing ([nearshore] extra). "
            "Invoked by the SWAN runner — not part of the standard cache warmer."
        ),
        refresh_interval=_CACHE_TTL_SECONDS,
        attribution=ProviderAttribution(
            attribution_required=True,
            display_name="NOAA HRRR",
            attribution_text="Data courtesy of NOAA/ESRL/GSD",
            text_prefix="Data courtesy of",
            text_provider_name="NOAA/ESRL/GSD",
            url="https://rapidrefresh.noaa.gov/hrrr/",
        ),
    )
else:
    CAPABILITY = None

# ---------------------------------------------------------------------------
# Rate limiter (PROVIDER-MANUAL §14.14 — 2 req/s to NOMADS)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="hrrr-wind",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# HTTP client (elevated read_timeout for GRIB2 downloads)
# ---------------------------------------------------------------------------

_http_client = ProviderHTTPClient(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    user_agent=_USER_AGENT,
    read_timeout=_GRIB_READ_TIMEOUT,
)

# ---------------------------------------------------------------------------
# Internal data structure — raw extraction from one GRIB2 file
# ---------------------------------------------------------------------------


@dataclass
class _HrrrGribData:
    """Raw extracted data from one HRRR GRIB2 file (grid-relative, pre-rotation)."""

    u_grid: list[list[float]]             # grid-relative U [nj][ni]
    v_grid: list[list[float]]             # grid-relative V [nj][ni]
    lons_2d: list[list[float]] | None     # per-grid-point longitude [nj][ni], [0..360]
    lov: float                            # Lambert Conformal orientation longitude [0..360]
    latin1: float                         # first standard parallel (degrees)
    latin2: float                         # second standard parallel (degrees)
    ni: int                               # grid columns
    nj: int                               # grid rows
    lat_first: float
    lon_first: float
    lat_last: float
    lon_last: float


# ---------------------------------------------------------------------------
# Lambert Conformal wind rotation (PROVIDER-MANUAL §14.14)
# ---------------------------------------------------------------------------


def _compute_cone_factor(latin1_deg: float, latin2_deg: float) -> float:
    """Compute the Lambert Conformal cone factor n from the standard parallels.

    Tangent case (latin1 == latin2): n = sin(latin1).
    Secant case (two distinct parallels): n = log(cos(φ1)/cos(φ2))
      / log(tan(π/4+φ2/2) / tan(π/4+φ1/2)).

    HRRR uses latin1 = latin2 = 38.5°  →  n = sin(38.5°) ≈ 0.6225.

    Source: NCEP Office Note 388, Appendix C; WMO GRIB2 edition 2 code tables.
    """
    lat1 = math.radians(latin1_deg)
    lat2 = math.radians(latin2_deg)
    if abs(latin1_deg - latin2_deg) < 1e-6:
        # Tangent cone — single standard parallel
        return math.sin(lat1)
    # Secant cone — two distinct standard parallels
    return math.log(math.cos(lat1) / math.cos(lat2)) / math.log(
        math.tan(math.pi / 4 + lat2 / 2) / math.tan(math.pi / 4 + lat1 / 2)
    )


def _rotate_wind_vector(
    u_grid: float,
    v_grid: float,
    lon: float,
    lov: float,
    cone_factor: float,
) -> tuple[float, float]:
    """Rotate one wind vector from grid-relative to earth-relative.

    Args:
        u_grid: Grid-relative east-west wind (m/s, positive = grid east).
        v_grid: Grid-relative north-south wind (m/s, positive = grid north).
        lon: Grid-point longitude in [0..360] convention.
        lov: Lambert Conformal orientation longitude in [0..360].
        cone_factor: Pre-computed cone factor n = f(latin1, latin2).

    Returns:
        (U_earth, V_earth): Earth-relative wind components (geographic E/N).
    """
    alpha = math.radians(cone_factor * (lon - lov))
    cos_a = math.cos(alpha)
    sin_a = math.sin(alpha)
    u_earth = u_grid * cos_a - v_grid * sin_a
    v_earth = u_grid * sin_a + v_grid * cos_a
    return u_earth, v_earth


def _rotate_wind_field(
    u_grid: list[list[float]],
    v_grid: list[list[float]],
    lons_2d: list[list[float]] | None,
    lon_first: float,
    lon_last: float,
    lov: float,
    cone_factor: float,
    ni: int,
    nj: int,
) -> tuple[list[list[float]], list[list[float]]]:
    """Rotate an entire 2-D wind grid from grid-relative to earth-relative.

    When lons_2d is None (eccodes lat/lon extraction failed), the longitude at
    each grid column is approximated by linear interpolation between the corner
    longitudes.  This is acceptable for small coastal bounding boxes where the
    variation in rotation angle across columns is small.
    """
    u_earth: list[list[float]] = [[0.0] * ni for _ in range(nj)]
    v_earth: list[list[float]] = [[0.0] * ni for _ in range(nj)]

    if lons_2d is not None:
        for j in range(nj):
            for i in range(ni):
                u_e, v_e = _rotate_wind_vector(
                    u_grid[j][i], v_grid[j][i], lons_2d[j][i], lov, cone_factor
                )
                u_earth[j][i] = u_e
                v_earth[j][i] = v_e
    else:
        # Approximate: interpolate lon linearly across columns
        lon_step = (lon_last - lon_first) / max(ni - 1, 1) if ni > 1 else 0.0
        for j in range(nj):
            for i in range(ni):
                lon = lon_first + lon_step * i
                u_e, v_e = _rotate_wind_vector(
                    u_grid[j][i], v_grid[j][i], lon, lov, cone_factor
                )
                u_earth[j][i] = u_e
                v_earth[j][i] = v_e

    return u_earth, v_earth


# ---------------------------------------------------------------------------
# GRIB2 extraction — eccodes backend
# ---------------------------------------------------------------------------


def _extract_eccodes(file_path: str) -> _HrrrGribData | None:
    """Extract HRRR UGRD/VGRD at 10m AGL from a GRIB2 file using eccodes.

    Returns None if the expected wind fields are not present in the file.
    """
    eccodes = _eccodes_lib

    u_vals: list[list[float]] | None = None
    v_vals: list[list[float]] | None = None
    lons_2d: list[list[float]] | None = None
    ni = nj = 0
    lat_first = lon_first = lat_last = lon_last = 0.0
    lov = latin1 = latin2 = 0.0
    meta_extracted = False

    with open(file_path, "rb") as fh:
        while True:
            msgid = eccodes.codes_grib_new_from_file(fh)
            if msgid is None:
                break
            try:
                type_of_level: str = eccodes.codes_get(msgid, "typeOfLevel")
                level: int = eccodes.codes_get(msgid, "level")
                short_name: str = eccodes.codes_get(msgid, "shortName")

                # Identify 10m AGL wind components by level type + shortName.
                # NOMADS Grib Filter restricts the file to only these variables
                # (var_UGRD=on, var_VGRD=on, lev_10_m_above_ground=on), but we
                # double-check here to be defensive about unexpected extras.
                is_u10 = (
                    type_of_level == "heightAboveGround"
                    and level == 10
                    and short_name in ("10u", "UGRD", "u")
                )
                is_v10 = (
                    type_of_level == "heightAboveGround"
                    and level == 10
                    and short_name in ("10v", "VGRD", "v")
                )

                if not (is_u10 or is_v10):
                    continue

                _ni: int = eccodes.codes_get(msgid, "Ni")
                _nj: int = eccodes.codes_get(msgid, "Nj")

                if not meta_extracted:
                    ni, nj = _ni, _nj
                    lat_first = float(
                        eccodes.codes_get(msgid, "latitudeOfFirstGridPointInDegrees")
                    )
                    lon_first = float(
                        eccodes.codes_get(msgid, "longitudeOfFirstGridPointInDegrees")
                    )
                    lat_last = float(
                        eccodes.codes_get(msgid, "latitudeOfLastGridPointInDegrees")
                    )
                    lon_last = float(
                        eccodes.codes_get(msgid, "longitudeOfLastGridPointInDegrees")
                    )

                    # Lambert Conformal projection parameters for wind rotation
                    try:
                        lov = float(eccodes.codes_get(msgid, "LovInDegrees"))
                        latin1 = float(eccodes.codes_get(msgid, "Latin1InDegrees"))
                        latin2 = float(eccodes.codes_get(msgid, "Latin2InDegrees"))
                    except Exception:
                        logger.warning(
                            "HRRR GRIB2: could not extract Lambert Conformal parameters "
                            "(LoV/Latin1/Latin2); wind rotation will use lon_first/lon_last "
                            "approximation"
                        )

                    # Per-grid-point longitudes for accurate per-point rotation.
                    # eccodes computes these from the Lambert Conformal projection.
                    try:
                        all_lons = list(
                            eccodes.codes_get_double_array(msgid, "longitudes")
                        )
                        lons_2d = [
                            [all_lons[j * _ni + i] for i in range(_ni)]
                            for j in range(_nj)
                        ]
                    except Exception:
                        logger.debug(
                            "HRRR GRIB2: could not extract per-point longitudes; "
                            "will approximate using corner coordinates"
                        )

                    meta_extracted = True

                raw = eccodes.codes_get_values(msgid)
                vals: list[list[float]] = [
                    [float(raw[j * _ni + i]) for i in range(_ni)] for j in range(_nj)
                ]

                if is_u10:
                    u_vals = vals
                elif is_v10:
                    v_vals = vals

            except Exception:
                logger.warning("HRRR GRIB2 eccodes: failed to read message", exc_info=True)
            finally:
                eccodes.codes_release(msgid)

    if u_vals is None or v_vals is None:
        return None

    return _HrrrGribData(
        u_grid=u_vals,
        v_grid=v_vals,
        lons_2d=lons_2d,
        lov=lov,
        latin1=latin1,
        latin2=latin2,
        ni=ni,
        nj=nj,
        lat_first=lat_first,
        lon_first=lon_first,
        lat_last=lat_last,
        lon_last=lon_last,
    )


# ---------------------------------------------------------------------------
# GRIB2 extraction — pygrib backend
# ---------------------------------------------------------------------------


def _extract_pygrib(file_path: str) -> _HrrrGribData | None:
    """Extract HRRR UGRD/VGRD at 10m AGL from a GRIB2 file using pygrib.

    Returns None if the expected wind fields are not present in the file.
    """
    pygrib = _pygrib_lib

    u_vals: list[list[float]] | None = None
    v_vals: list[list[float]] | None = None
    lons_2d: list[list[float]] | None = None
    ni = nj = 0
    lat_first = lon_first = lat_last = lon_last = 0.0
    lov = latin1 = latin2 = 0.0
    meta_extracted = False

    grbs = pygrib.open(file_path)
    try:
        for grb in grbs:
            short_name: str = grb.shortName
            type_of_level: str = grb.typeOfLevel
            level: int = grb.level

            is_u10 = (
                type_of_level == "heightAboveGround"
                and level == 10
                and short_name in ("10u", "UGRD", "u")
            )
            is_v10 = (
                type_of_level == "heightAboveGround"
                and level == 10
                and short_name in ("10v", "VGRD", "v")
            )

            if not (is_u10 or is_v10):
                continue

            try:
                data_2d = grb.values
                _nj = len(data_2d)
                _ni = len(data_2d[0]) if _nj > 0 else 0
                vals: list[list[float]] = [
                    [float(data_2d[j][i]) for i in range(_ni)] for j in range(_nj)
                ]

                if not meta_extracted:
                    ni, nj = _ni, _nj
                    _lats, _lons = grb.latlons()
                    lat_first = float(_lats[0][0])
                    lon_first = float(_lons[0][0])
                    lat_last = float(_lats[-1][-1])
                    lon_last = float(_lons[-1][-1])
                    lons_2d = [
                        [float(_lons[j][i]) for i in range(_ni)] for j in range(_nj)
                    ]

                    # Lambert Conformal parameters — try both degree and raw (μ°) forms
                    try:
                        _lov = getattr(grb, "LovInDegrees", None)
                        if _lov is None:
                            _lov = getattr(grb, "LoV", 0.0) / 1_000_000
                        _lat1 = getattr(grb, "Latin1InDegrees", None)
                        if _lat1 is None:
                            _lat1 = getattr(grb, "Latin1", 0.0) / 1_000_000
                        _lat2 = getattr(grb, "Latin2InDegrees", None)
                        if _lat2 is None:
                            _lat2 = getattr(grb, "Latin2", 0.0) / 1_000_000
                        lov, latin1, latin2 = float(_lov), float(_lat1), float(_lat2)
                    except Exception:
                        logger.warning(
                            "HRRR GRIB2 pygrib: could not extract Lambert Conformal parameters"
                        )

                    meta_extracted = True

                if is_u10:
                    u_vals = vals
                elif is_v10:
                    v_vals = vals

            except Exception:
                logger.warning("HRRR GRIB2 pygrib: failed to read message", exc_info=True)
    finally:
        grbs.close()

    if u_vals is None or v_vals is None:
        return None

    return _HrrrGribData(
        u_grid=u_vals,
        v_grid=v_vals,
        lons_2d=lons_2d,
        lov=lov,
        latin1=latin1,
        latin2=latin2,
        ni=ni,
        nj=nj,
        lat_first=lat_first,
        lon_first=lon_first,
        lat_last=lat_last,
        lon_last=lon_last,
    )


# ---------------------------------------------------------------------------
# Backend selector
# ---------------------------------------------------------------------------


def _extract_hrrr_grib(file_path: str) -> _HrrrGribData | None:
    """Extract HRRR 10m wind from a GRIB2 file. Selects eccodes or pygrib."""
    if not GRIB_AVAILABLE:
        raise RuntimeError(
            "No GRIB2 backend available. "
            "Install eccodes: apt install libeccodes-dev && pip install eccodes"
        )
    if _GRIB_BACKEND == "eccodes":
        return _extract_eccodes(file_path)
    return _extract_pygrib(file_path)


# ---------------------------------------------------------------------------
# NOMADS Grib Filter URL construction
# ---------------------------------------------------------------------------


def _build_grib_filter_url(
    cycle_dt: datetime,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> str:
    """Build the NOMADS Grib Filter URL for one HRRR forecast hour.

    NOMADS Grib Filter URL pattern (live-documented at
    https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl):

      filter_hrrr_2d.pl?file=hrrr.tHHz.wrfsfcfFF.grib2
        &var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on
        &subregion=&leftlon=LON_MIN&rightlon=LON_MAX
        &toplat=LAT_MAX&bottomlat=LAT_MIN
        &dir=/hrrr.YYYYMMDD/conus

    The query string is built manually (not via httpx params=) because the
    NOMADS Grib Filter is sensitive to parameter ordering and empty-string
    values (subregion=) that httpx may suppress.

    Args:
        cycle_dt: Model cycle datetime in UTC.
        forecast_hour: Forecast hour index (0–18 for standard cycles).
        bbox: (lon_min, lat_min, lon_max, lat_max). Negative lons accepted.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    date_str = cycle_dt.strftime("%Y%m%d")
    cycle_hour = cycle_dt.hour

    parts = [
        f"file=hrrr.t{cycle_hour:02d}z.wrfsfcf{forecast_hour:02d}.grib2",
        "var_UGRD=on",
        "var_VGRD=on",
        "lev_10_m_above_ground=on",
        "subregion=",
        f"leftlon={lon_min}",
        f"rightlon={lon_max}",
        f"toplat={lat_max}",
        f"bottomlat={lat_min}",
        f"dir=/hrrr.{date_str}/conus",
    ]
    return f"{NOMADS_GRIB_FILTER}?{'&'.join(parts)}"


# ---------------------------------------------------------------------------
# Cache key construction (PROVIDER-MANUAL §14.14)
# ---------------------------------------------------------------------------


def _build_cache_key(
    bbox: tuple[float, float, float, float],
    cycle_dt: datetime,
) -> str:
    """Build a SHA-256 cache key per PROVIDER-MANUAL §14.14.

    Key = SHA-256(provider_id, bbox rounded to 2 d.p., cycle_time ISO-8601).
    Including cycle_time ensures each hourly HRRR cycle gets its own cache
    slot — stale cycle data is never served for a new cycle's key.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "lon_min": round(lon_min, 2),
            "lat_min": round(lat_min, 2),
            "lon_max": round(lon_max, 2),
            "lat_max": round(lat_max, 2),
            "cycle_time": cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Cycle determination
# ---------------------------------------------------------------------------


def _compute_hrrr_cycle(now: datetime) -> datetime:
    """Return the most recently expected available HRRR cycle.

    HRRR runs every hour (00Z–23Z). Data is available ~45–60 minutes after
    the nominal cycle hour. A 1-hour lag is applied so we request a cycle that
    has had time to be published to NOMADS. The cycle for the previous hour is
    almost always present; the current hour is frequently not yet ready.

    If the computed cycle's f00 returns 404, fetch() falls back to the
    cycle 1 hour earlier.
    """
    adjusted = now - _CYCLE_AVAILABILITY_LAG
    return adjusted.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# GRIB2 download helper
# ---------------------------------------------------------------------------


def _download_grib(url: str) -> str | None:
    """Download one HRRR GRIB2 file from NOMADS Grib Filter to a temp file.

    Returns:
        Path to the downloaded temp file on success. Caller MUST delete after use.
        None when the server returns 404 (cycle/forecast-hour not yet posted).

    Raises:
        ProviderProtocolError: Non-404 4xx response (unexpected client-side error).
        TransientNetworkError: Network failure after retries.
        QuotaExhausted: NOMADS returned 429.
    """
    _rate_limiter.acquire()

    try:
        response = _http_client.get(url)
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            return None
        raise

    content = response.content

    # GRIB2 files begin with b"GRIB" (4 magic bytes per WMO GRIB2 §5).
    if len(content) < 4 or content[:4] != b"GRIB":
        logger.warning(
            "HRRR NOMADS response is not a GRIB2 file (%d bytes); "
            "treating as empty (possible HTML error page)",
            len(content),
        )
        return None

    fd, path = tempfile.mkstemp(suffix=".grib2", prefix="hrrr_")
    try:
        os.write(fd, content)
    finally:
        os.close(fd)

    return path


# ---------------------------------------------------------------------------
# Public fetch entrypoint (PROVIDER-MANUAL §1, §14.14)
# ---------------------------------------------------------------------------


def fetch(
    *,
    bbox: tuple[float, float, float, float],
    max_forecast_hours: int = _MAX_FORECAST_HOURS,
) -> dict[str, Any]:
    """Fetch HRRR wind forecast for a bounding box.

    Downloads UGRD/VGRD at 10m AGL from NOMADS Grib Filter for each forecast
    hour (f00 through max_forecast_hours), rotates from grid-relative to
    earth-relative using the Lambert Conformal parameters found in the GRIB2
    metadata, and caches the complete wind field for 55 minutes.

    Args:
        bbox: (lon_min, lat_min, lon_max, lat_max) in degrees. Negative lons OK.
        max_forecast_hours: Highest forecast hour to attempt (inclusive).
            Default 18 for standard cycles; pass 48 for extended 00/06/12/18Z.

    Returns:
        dict with:
          "cycle_time": str   ISO-8601 UTC of the model cycle used.
          "lov":        float Lambert Conformal orientation longitude.
          "latin1":     float First standard parallel.
          "latin2":     float Second standard parallel.
          "cone_factor":float Pre-computed cone factor n = f(latin1, latin2).
          "grids":      list  One dict per forecast hour:
              "valid_time":   str   ISO-8601 UTC.
              "forecast_hour":int   (0, 1, 2, ...)
              "ni":           int   Grid columns.
              "nj":           int   Grid rows.
              "lat_first":    float
              "lon_first":    float
              "lat_last":     float
              "lon_last":     float
              "u_earth":      list[list[float]]  Earth-relative U (east-west, m/s).
              "v_earth":      list[list[float]]  Earth-relative V (north-south, m/s).

    Raises:
        ProviderUnavailableError: f00 returned 404 for all cycle attempts.
            (Data not yet posted to NOMADS. Retry on the next cycle.)
        ProviderProtocolError: Unexpected HTTP error or GRIB2 parse failure.
        TransientNetworkError: Network failure after retries.
        QuotaExhausted: NOMADS rate-limited the request.
        RuntimeError: No GRIB2 backend (eccodes/pygrib) available.
    """
    if not GRIB_AVAILABLE:
        raise RuntimeError(
            "HRRR wind provider requires eccodes or pygrib. "
            "Install the [nearshore] extra: pip install 'weewx-clearskies-api[nearshore]'"
        )

    base_cycle = _compute_hrrr_cycle(datetime.now(UTC))

    for attempt in range(_MAX_CYCLE_FALLBACKS + 1):
        cycle_dt = base_cycle - timedelta(hours=attempt)
        cache_key = _build_cache_key(bbox, cycle_dt)

        cached = get_cache().get(cache_key)
        if cached is not None:
            logger.debug(
                "HRRR cache hit: cycle=%s bbox=%s",
                cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                bbox,
            )
            return cached

        grids: list[dict[str, Any]] = []
        lov: float = 0.0
        latin1: float = 38.5   # HRRR defaults (extracted from first GRIB message)
        latin2: float = 38.5
        cone_factor: float | None = None
        cycle_has_data = False

        for fhour in range(max_forecast_hours + 1):
            url = _build_grib_filter_url(cycle_dt, fhour, bbox)
            grib_path = _download_grib(url)

            if grib_path is None:
                if fhour == 0:
                    # f00 absent → this cycle not yet posted; try earlier cycle
                    logger.info(
                        "HRRR cycle %s f00 not available (404); trying earlier cycle",
                        cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                else:
                    # f00 succeeded but a later hour is absent → cycle exhausted
                    logger.debug(
                        "HRRR cycle %s: f%02d returned 404 — stopping at %d forecast hours",
                        cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        fhour,
                        fhour,
                    )
                break  # try next cycle attempt or exit inner loop

            try:
                grib_data = _extract_hrrr_grib(grib_path)
            except Exception as exc:
                raise ProviderProtocolError(
                    f"HRRR GRIB2 extraction failed: "
                    f"cycle={cycle_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"f{fhour:02d}: {exc}",
                    provider_id=PROVIDER_ID,
                    domain=DOMAIN,
                ) from exc
            finally:
                try:
                    os.unlink(grib_path)
                except OSError:
                    pass

            if grib_data is None:
                logger.warning(
                    "HRRR GRIB2: no wind fields found in downloaded file "
                    "(cycle=%s f%02d); skipping timestep",
                    cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    fhour,
                )
                if fhour == 0:
                    break  # treat as unavailable → try earlier cycle
                continue

            # Extract Lambert parameters from the first successful GRIB2 read
            if cone_factor is None:
                lov = grib_data.lov
                latin1 = grib_data.latin1
                latin2 = grib_data.latin2
                cone_factor = _compute_cone_factor(latin1, latin2)

            u_earth, v_earth = _rotate_wind_field(
                grib_data.u_grid,
                grib_data.v_grid,
                grib_data.lons_2d,
                grib_data.lon_first,
                grib_data.lon_last,
                lov,
                cone_factor,
                grib_data.ni,
                grib_data.nj,
            )

            valid_time = cycle_dt + timedelta(hours=fhour)
            grids.append(
                {
                    "valid_time": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "forecast_hour": fhour,
                    "ni": grib_data.ni,
                    "nj": grib_data.nj,
                    "lat_first": grib_data.lat_first,
                    "lon_first": grib_data.lon_first,
                    "lat_last": grib_data.lat_last,
                    "lon_last": grib_data.lon_last,
                    "u_earth": u_earth,
                    "v_earth": v_earth,
                }
            )
            cycle_has_data = True

        if cycle_has_data and grids:
            _cone = cone_factor if cone_factor is not None else _compute_cone_factor(latin1, latin2)
            result: dict[str, Any] = {
                "cycle_time": cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lov": lov,
                "latin1": latin1,
                "latin2": latin2,
                "cone_factor": _cone,
                "grids": grids,
            }
            get_cache().set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
            logger.info(
                "HRRR wind field fetched: cycle=%s %d forecast hour(s) for bbox=%s",
                result["cycle_time"],
                len(grids),
                bbox,
            )
            return result

    # Every cycle attempt returned 404 on f00
    raise ProviderUnavailableError(
        f"HRRR wind data unavailable: all {_MAX_CYCLE_FALLBACKS + 1} cycle attempts "
        f"returned 404 for f00. Latest attempt: "
        f"{base_cycle.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
        "Data may not yet be posted to NOMADS. Retry after the next cycle.",
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    )
