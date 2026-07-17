"""GFS wind provider module (PROVIDER-MANUAL §14.16, ADR-093).

Fetches GFS (Global Forecast System) 10m AGL wind forecasts from NOMADS Grib
Filter for a configurable bounding box and forecast hours 48–72.  GFS uses a
regular latitude-longitude grid (0.25° / ~25 km) — wind components are
earth-relative by default, so no rotation is required.

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — NOMADS Grib Filter for GFS 0.25° surface fields,
     one HTTP request per 3-hour forecast step (f048–f072), with model-cycle
     fallback when the latest cycle returns 404.
  2. Response parsing — GRIB2 via eccodes (primary) or pygrib (fallback);
     extracts UGRD and VGRD at 10m above ground level.
  3. No wind rotation — GFS lat-lon grid winds are already earth-relative
     (componentFlags verified per PROVIDER-MANUAL §14.16).
  4. Capability declaration — CAPABILITY symbol; None when no GRIB2 backend
     is available, so the provider is not registered.
  5. Error handling — 404 on all cycle attempts → ProviderUnavailableError;
     network failures → canonical taxonomy; GRIB2 parse error →
     ProviderProtocolError.

Data source (primary):
  NOMADS Grib Filter: https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl
  Supports geographic subsetting, variable selection, GRIB2 output. No API key.

Data source (backup):
  AWS S3: s3://noaa-gfs-bdp-pds/ (not implemented in v1 — future task).

Schedule: GFS runs 4×/day (00Z, 06Z, 12Z, 18Z).  Data available ~3.5–4.5 hours
after the nominal run hour.  Cycle availability lag: 4.5 hours.
Cache TTL: 21600s (6 hours) per PROVIDER-MANUAL §14.16.
Rate limit: 2 req/s to NOMADS (shared NOAA infrastructure).

Forecast range fetched: hours 48–72 (9 files at 3-hour intervals: f048–f072).
The SWAN runner interpolates 3-hourly GFS wind to hourly resolution to match
the HRRR cadence for hours 48–72 of the surf forecast.

Availability: Active only when the [nearshore] pip extra is installed.  Invoked
by the SWAN runner alongside the HRRR wind provider — not by the cache warmer
directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
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

PROVIDER_ID = "gfs"
DOMAIN = "wind"

NOMADS_GRIB_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (GFS wind provider)"

_CACHE_TTL_SECONDS = 21600          # 6 hours per PROVIDER-MANUAL §14.16
_GRIB_READ_TIMEOUT = 60.0           # large GRIB2 files; allow a full minute
_MAX_CYCLE_FALLBACKS = 2            # try up to 3 cycles (current + 2 previous)

# GFS default forecast range for TruShore (hours 48–72, 3-hour steps)
_DEFAULT_HOURS_START = 48
_DEFAULT_HOURS_END = 72
_GFS_HOUR_STEP = 3                  # GFS 0.25° uses 3-hour timesteps

# GFS cycles: 00Z, 06Z, 12Z, 18Z (4 per day)
_GFS_CYCLE_HOURS = (0, 6, 12, 18)
_CYCLE_AVAILABILITY_LAG = timedelta(hours=4, minutes=30)  # ~3.5–4.5 h post-run

# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.16)
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
        geographic_coverage="global",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL_SECONDS,
        operator_notes=(
            "NOAA GFS 10m AGL wind forecast via NOMADS Grib Filter. "
            "Global coverage at 0.25° resolution. No API key required. "
            "Fetches forecast hours 48–72 (3-hourly) to supplement HRRR for "
            "the 72-hour surf forecast card. Requires eccodes or pygrib for "
            "GRIB2 processing ([nearshore] extra). "
            "Invoked by the SWAN runner — not part of the standard cache warmer."
        ),
        refresh_interval=_CACHE_TTL_SECONDS,
        attribution=ProviderAttribution(
            attribution_required=True,
            display_name="NOAA GFS",
            attribution_text="Data courtesy of NOAA/NCEP",
            text_prefix="Data courtesy of",
            text_provider_name="NOAA/NCEP",
            url="https://www.emc.ncep.noaa.gov/emc/pages/numerical_forecast_systems/gfs.php",
        ),
    )
else:
    CAPABILITY = None

# ---------------------------------------------------------------------------
# Rate limiter (PROVIDER-MANUAL §14.16 — 2 req/s to NOMADS)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="gfs-wind",
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
class _GfsGribData:
    """Raw extracted data from one GFS GRIB2 file (earth-relative, no rotation needed)."""

    u_earth: list[list[float]]   # earth-relative U (east-west) [nj][ni]
    v_earth: list[list[float]]   # earth-relative V (north-south) [nj][ni]
    ni: int                      # grid columns
    nj: int                      # grid rows
    lat_first: float
    lon_first: float
    lat_last: float
    lon_last: float


# ---------------------------------------------------------------------------
# GRIB2 extraction — eccodes backend
# ---------------------------------------------------------------------------


def _extract_eccodes(file_path: str) -> _GfsGribData | None:
    """Extract GFS UGRD/VGRD at 10m AGL from a GRIB2 file using eccodes.

    GFS uses a regular latitude-longitude grid.  Wind components are
    earth-relative by default — no rotation required.

    Returns None if the expected wind fields are not present in the file.
    """
    eccodes = _eccodes_lib

    u_vals: list[list[float]] | None = None
    v_vals: list[list[float]] | None = None
    ni = nj = 0
    lat_first = lon_first = lat_last = lon_last = 0.0
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

                    # Grid corner coordinates. NOMADS subregion-filtered
                    # GRIB2 files may lack the "last grid point" keys, so
                    # fall back to min/max of the coordinate arrays.
                    all_lats = list(
                        eccodes.codes_get_double_array(msgid, "latitudes")
                    )
                    all_lons = list(
                        eccodes.codes_get_double_array(msgid, "longitudes")
                    )
                    try:
                        lat_first = float(
                            eccodes.codes_get(msgid, "latitudeOfFirstGridPointInDegrees")
                        )
                        lon_first = float(
                            eccodes.codes_get(msgid, "longitudeOfFirstGridPointInDegrees")
                        )
                    except Exception:
                        lat_first = all_lats[0]
                        lon_first = all_lons[0]
                    try:
                        lat_last = float(
                            eccodes.codes_get(msgid, "latitudeOfLastGridPointInDegrees")
                        )
                        lon_last = float(
                            eccodes.codes_get(msgid, "longitudeOfLastGridPointInDegrees")
                        )
                    except Exception:
                        lat_last = all_lats[-1]
                        lon_last = all_lons[-1]

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
                logger.warning("GFS GRIB2 eccodes: failed to read message", exc_info=True)
            finally:
                eccodes.codes_release(msgid)

    if u_vals is None or v_vals is None:
        return None

    return _GfsGribData(
        u_earth=u_vals,
        v_earth=v_vals,
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


def _extract_pygrib(file_path: str) -> _GfsGribData | None:
    """Extract GFS UGRD/VGRD at 10m AGL from a GRIB2 file using pygrib.

    GFS uses a regular latitude-longitude grid.  Wind components are
    earth-relative by default — no rotation required.

    Returns None if the expected wind fields are not present in the file.
    """
    pygrib = _pygrib_lib

    u_vals: list[list[float]] | None = None
    v_vals: list[list[float]] | None = None
    ni = nj = 0
    lat_first = lon_first = lat_last = lon_last = 0.0
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
                    meta_extracted = True

                if is_u10:
                    u_vals = vals
                elif is_v10:
                    v_vals = vals

            except Exception:
                logger.warning("GFS GRIB2 pygrib: failed to read message", exc_info=True)
    finally:
        grbs.close()

    if u_vals is None or v_vals is None:
        return None

    return _GfsGribData(
        u_earth=u_vals,
        v_earth=v_vals,
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


def _extract_gfs_grib(file_path: str) -> _GfsGribData | None:
    """Extract GFS 10m wind from a GRIB2 file. Selects eccodes or pygrib."""
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
    """Build the NOMADS Grib Filter URL for one GFS forecast hour.

    NOMADS Grib Filter URL pattern (live-documented at
    https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl):

      filter_gfs_0p25.pl?file=gfs.tHHz.pgrb2.0p25.fFFF
        &var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on
        &subregion=&leftlon=LON_MIN&rightlon=LON_MAX
        &toplat=LAT_MAX&bottomlat=LAT_MIN
        &dir=/gfs.YYYYMMDD/HH/atmos

    The query string is built manually (not via httpx params=) because the
    NOMADS Grib Filter is sensitive to parameter ordering and empty-string
    values (subregion=) that httpx may suppress.

    Args:
        cycle_dt: Model cycle datetime in UTC (must be 00Z, 06Z, 12Z, or 18Z).
        forecast_hour: Forecast hour index (e.g. 48, 51, ..., 72).
        bbox: (lon_min, lat_min, lon_max, lat_max). Negative lons accepted.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    date_str = cycle_dt.strftime("%Y%m%d")
    cycle_hour = cycle_dt.hour

    parts = [
        f"file=gfs.t{cycle_hour:02d}z.pgrb2.0p25.f{forecast_hour:03d}",
        "var_UGRD=on",
        "var_VGRD=on",
        "lev_10_m_above_ground=on",
        "subregion=",
        f"leftlon={lon_min}",
        f"rightlon={lon_max}",
        f"toplat={lat_max}",
        f"bottomlat={lat_min}",
        f"dir=/gfs.{date_str}/{cycle_hour:02d}/atmos",
    ]
    return f"{NOMADS_GRIB_FILTER}?{'&'.join(parts)}"


# ---------------------------------------------------------------------------
# Cache key construction (PROVIDER-MANUAL §14.16)
# ---------------------------------------------------------------------------


def _build_cache_key(
    bbox: tuple[float, float, float, float],
    cycle_dt: datetime,
) -> str:
    """Build a SHA-256 cache key per PROVIDER-MANUAL §14.16.

    Key = SHA-256(provider_id, bbox rounded to 2 d.p., cycle_time ISO-8601).
    Including cycle_time ensures each 6-hourly GFS cycle gets its own cache
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


def _compute_gfs_cycle(now: datetime) -> datetime:
    """Return the most recently expected available GFS cycle.

    GFS runs at 00Z, 06Z, 12Z, 18Z.  Data is available ~3.5–4.5 hours after
    the nominal cycle hour.  A 4.5-hour lag is applied so we request a cycle
    that has had time to be published to NOMADS.

    The returned datetime is snapped to the most recent 6-hour boundary at or
    before (now - 4.5h).  Seconds/microseconds are zeroed.

    If the computed cycle's f048 returns 404, fetch() falls back to the
    cycle 6 hours earlier.
    """
    adjusted = now - _CYCLE_AVAILABILITY_LAG
    # Snap to the most recent GFS cycle hour (00/06/12/18)
    cycle_hour = max(h for h in _GFS_CYCLE_HOURS if h <= adjusted.hour)
    return adjusted.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# GRIB2 download helper
# ---------------------------------------------------------------------------


def _download_grib(url: str) -> str | None:
    """Download one GFS GRIB2 file from NOMADS Grib Filter to a temp file.

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
            "GFS NOMADS response is not a GRIB2 file (%d bytes); "
            "treating as empty (possible HTML error page)",
            len(content),
        )
        return None

    fd, path = tempfile.mkstemp(suffix=".grib2", prefix="gfs_")
    try:
        os.write(fd, content)
    finally:
        os.close(fd)

    return path


# ---------------------------------------------------------------------------
# Public fetch entrypoint (PROVIDER-MANUAL §1, §14.16)
# ---------------------------------------------------------------------------


def fetch(
    *,
    bbox: tuple[float, float, float, float],
    hours_start: int = _DEFAULT_HOURS_START,
    hours_end: int = _DEFAULT_HOURS_END,
) -> dict[str, Any]:
    """Fetch GFS wind forecast for a bounding box and forecast hour range.

    Downloads UGRD/VGRD at 10m AGL from NOMADS Grib Filter for each 3-hour
    forecast step from hours_start through hours_end (inclusive), and caches
    the complete wind field for 6 hours.  GFS winds are earth-relative on the
    regular lat-lon grid — no rotation is performed.

    Args:
        bbox: (lon_min, lat_min, lon_max, lat_max) in degrees. Negative lons OK.
        hours_start: First forecast hour to fetch (default 48).  Must be a
            multiple of 3 (GFS 3-hourly step).
        hours_end: Last forecast hour to fetch (inclusive, default 72).  Must
            be a multiple of 3.

    Returns:
        dict with:
          "cycle_time": str   ISO-8601 UTC of the model cycle used.
          "grids":      list  One dict per forecast hour:
              "valid_time":   str   ISO-8601 UTC.
              "forecast_hour":int   (48, 51, 54, ...)
              "ni":           int   Grid columns.
              "nj":           int   Grid rows.
              "lat_first":    float
              "lon_first":    float
              "lat_last":     float
              "lon_last":     float
              "u_earth":      list[list[float]]  Earth-relative U (east-west, m/s).
              "v_earth":      list[list[float]]  Earth-relative V (north-south, m/s).

    Raises:
        ProviderUnavailableError: f048 returned 404 for all cycle attempts.
            (Data not yet posted to NOMADS. Retry on the next cycle.)
        ProviderProtocolError: Unexpected HTTP error or GRIB2 parse failure.
        TransientNetworkError: Network failure after retries.
        QuotaExhausted: NOMADS rate-limited the request.
        RuntimeError: No GRIB2 backend (eccodes/pygrib) available.
    """
    if not GRIB_AVAILABLE:
        raise RuntimeError(
            "GFS wind provider requires eccodes or pygrib. "
            "Install the [nearshore] extra: pip install 'weewx-clearskies-api[nearshore]'"
        )

    base_cycle = _compute_gfs_cycle(datetime.now(UTC))

    # Build the list of forecast hours to fetch (3-hour steps, inclusive)
    forecast_hours = list(range(hours_start, hours_end + 1, _GFS_HOUR_STEP))

    for attempt in range(_MAX_CYCLE_FALLBACKS + 1):
        # GFS cycles are 6 hours apart; fall back by 6 hours per attempt
        cycle_dt = base_cycle - timedelta(hours=attempt * 6)
        cache_key = _build_cache_key(bbox, cycle_dt)

        cached = get_cache().get(cache_key)
        if cached is not None:
            logger.debug(
                "GFS cache hit: cycle=%s bbox=%s",
                cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                bbox,
            )
            return cached

        grids: list[dict[str, Any]] = []
        cycle_has_data = False
        first_fhour = forecast_hours[0] if forecast_hours else hours_start

        for fhour in forecast_hours:
            url = _build_grib_filter_url(cycle_dt, fhour, bbox)
            grib_path = _download_grib(url)

            if grib_path is None:
                if fhour == first_fhour:
                    # First hour absent → this cycle not yet fully posted;
                    # try earlier cycle
                    logger.info(
                        "GFS cycle %s f%03d not available (404); trying earlier cycle",
                        cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        fhour,
                    )
                else:
                    # First hour succeeded but a later hour is absent →
                    # cycle exhausted at this horizon
                    logger.debug(
                        "GFS cycle %s: f%03d returned 404 — stopping at %d forecast hours",
                        cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        fhour,
                        fhour,
                    )
                break  # try next cycle attempt or exit inner loop

            try:
                grib_data = _extract_gfs_grib(grib_path)
            except Exception as exc:
                raise ProviderProtocolError(
                    f"GFS GRIB2 extraction failed: "
                    f"cycle={cycle_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"f{fhour:03d}: {exc}",
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
                    "GFS GRIB2: no wind fields found in downloaded file "
                    "(cycle=%s f%03d); skipping timestep",
                    cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    fhour,
                )
                if fhour == first_fhour:
                    break  # treat as unavailable → try earlier cycle
                continue

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
                    "u_earth": grib_data.u_earth,
                    "v_earth": grib_data.v_earth,
                }
            )
            cycle_has_data = True

        if cycle_has_data and grids:
            result: dict[str, Any] = {
                "cycle_time": cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "grids": grids,
            }
            get_cache().set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
            logger.info(
                "GFS wind field fetched: cycle=%s %d forecast hour(s) for bbox=%s",
                result["cycle_time"],
                len(grids),
                bbox,
            )
            return result

    # Every cycle attempt returned 404 on the first forecast hour
    raise ProviderUnavailableError(
        f"GFS wind data unavailable: all {_MAX_CYCLE_FALLBACKS + 1} cycle attempts "
        f"returned 404 for f{first_fhour:03d}. Latest attempt: "
        f"{base_cycle.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
        "Data may not yet be posted to NOMADS. Retry after the next cycle.",
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    )
