"""NWPS nearshore wave data provider module (PROVIDER-MANUAL §14.6).

Five responsibilities per the §1 Module Contract (PROVIDER-MANUAL):
  1. Outbound API call — NOMADS HTTP file download of GRIB2 from
     ``https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwps/prod/``.
  2. Response parsing — eccodes/pygrib via ``grib_processor.read_grib_fields()``.
  3. Canonical field translation — GRIB2 shortNames mapped to canonical
     ``NearshoreWaveData`` fields; current components (UCUR/VCUR) composed
     into speed/direction.
  4. Capability declaration — CAPABILITY symbol consumed at startup; set to
     None when eccodes/pygrib unavailable so the provider is not registered.
  5. Error handling — NOMADS 404 (missing cycle) → log + cycle fallback;
     network errors → canonical taxonomy via ProviderHTTPClient.

Live-verified NOMADS directory structure (2026-07-10):
  ``{base}/{region}.{YYYYMMDD}/{wfo}/{HH}/CG{n}/``
  ``{wfo}_nwps_CG{n}_{YYYYMMDD}_{HHMM}.grib2``

  Example: er.20260710/ilm/06/CG1/ilm_nwps_CG1_20260710_0600.grib2

  Region prefixes (verified live):
    er → akq box car chs gyx ilm lwx mhx okx phi
    sr → bro crp hgx jax key lch lix mfl mlb mob sju tae tbw
    wr → eka lox mfr mtr pqr sew sgx
    ar → aer afg ajk alu
    pr → gum hfo

  v1 simplification: CG1 only. CG0 is the outer domain; CG2–CG5 nested
  grids are not always present. Higher-resolution CG support can be added
  as a follow-up if operators request it.

GRIB2 field extraction:
  One consolidated file per CG per cycle (~27 MB) contains all forecast
  hours and fields. Extract by GRIB2 shortName.

v1.5 fields (rip current probability, total water level, wave runup):
  On NOMADS, these appear as separate text files (e.g.
  ``nwps.t06z.5m_CG1_ripprob.ilm.txt``) in ~12 WFOs, NOT in the GRIB2.
  The GRIB2 may also contain these as shortNames in some WFOs. This module
  attempts extraction from the GRIB2 first; text-file parsing is deferred
  to a follow-up. Fields are show-when-available: None when absent.

Cache: Key = (provider_id, wfo, cg_grid, cycle). TTL 1800s (30 min).

ruff: noqa: N815  (GRIB2 field names are mixed-case)
"""

# ruff: noqa: N815

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
    GeographicallyUnsupported,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.providers._common.nws_zones import get_cwa
from weewx_clearskies_api.providers.marine.grib_processor import (
    GRIB_AVAILABLE,
    GribFieldData,
    GribResult,
    bilinear_interpolate,
    extract_nearest_value,
    read_grib_fields,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "nwps"
DOMAIN = "marine"

NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwps/prod"

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (NWPS nearshore wave provider)"

_CACHE_TTL_SECONDS = 1800  # 30 min per PROVIDER-MANUAL §14.6

_MAX_CYCLE_FALLBACKS = 2  # try up to 3 cycles (current + 2 previous)
_CYCLE_HOURS = (0, 6, 12, 18)

# GRIB2 files are ~27 MB; allow 60s read timeout.
_GRIB_READ_TIMEOUT = 60.0

# ---------------------------------------------------------------------------
# WFO → NOMADS region mapping (live-verified 2026-07-10)
# ---------------------------------------------------------------------------

_WFO_TO_REGION: dict[str, str] = {}

_REGION_WFOS: dict[str, list[str]] = {
    "er": ["akq", "box", "car", "chs", "gyx", "ilm", "lwx", "mhx", "okx", "phi"],
    "sr": ["bro", "crp", "hgx", "jax", "key", "lch", "lix", "mfl", "mlb", "mob", "sju", "tae", "tbw"],
    "wr": ["eka", "lox", "mfr", "mtr", "pqr", "sew", "sgx"],
    "ar": ["aer", "afg", "ajk", "alu"],
    "pr": ["gum", "hfo"],
}

for _region, _wfos in _REGION_WFOS.items():
    for _wfo in _wfos:
        _WFO_TO_REGION[_wfo] = _region


# ---------------------------------------------------------------------------
# GRIB2 field mapping (PROVIDER-MANUAL §14.6)
# ---------------------------------------------------------------------------

_CORE_FIELDS = ["HTSGW", "PERPW", "DIRPW", "UCUR", "VCUR"]
_OPTIONAL_FIELDS = ["BODO"]
_V15_FIELDS = ["RIPCUR", "TWL", "RUNUP"]
_ALL_FIELDS = _CORE_FIELDS + _OPTIONAL_FIELDS + _V15_FIELDS


# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.6)
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
            "waveHeight",
            "wavePeriod",
            "waveDirection",
            "currentSpeed",
            "currentDirection",
            "bottomOrbitalVelocity",
            "ripCurrentProbability",
            "totalWaterLevel",
            "waveRunup",
        ),
        geographic_coverage="us_coastal",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL_SECONDS,
        operator_notes=(
            "NOAA NWPS nearshore wave model (GRIB2 via NOMADS). "
            "US coastal coverage (36 WFOs + Great Lakes). "
            "Requires eccodes or pygrib for GRIB2 processing. "
            "No API key required."
        ),
        refresh_interval=_CACHE_TTL_SECONDS,
        attribution=ProviderAttribution(
            attribution_required=True,
            display_name="NOAA NWPS",
            attribution_text="Data courtesy of NOAA National Weather Service",
            text_prefix="Data courtesy of",
            text_provider_name="NOAA National Weather Service",
            url="https://polar.ncep.noaa.gov/nwps/",
        ),
    )
else:
    CAPABILITY = None


# ---------------------------------------------------------------------------
# Rate limiter (PROVIDER-MANUAL §14.6 — 2 req/s to NOMADS)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="nwps-marine",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
    window_seconds=1,
)


# ---------------------------------------------------------------------------
# HTTP client — elevated read timeout for large GRIB2 downloads
# ---------------------------------------------------------------------------

_http_client = ProviderHTTPClient(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    user_agent=_USER_AGENT,
    read_timeout=_GRIB_READ_TIMEOUT,
)


# ---------------------------------------------------------------------------
# WFO determination
# ---------------------------------------------------------------------------

# Bounding-box fallback for offshore points where NWS /points returns 404.
# Approximate bounds — the CG1 grid itself defines exact coverage.
_WFO_BBOX: list[dict[str, Any]] = [
    {"wfo": "box", "lat": (40.0, 43.5), "lon": (-72.0, -69.0)},
    {"wfo": "okx", "lat": (39.5, 41.5), "lon": (-74.5, -72.0)},
    {"wfo": "phi", "lat": (38.0, 40.5), "lon": (-76.0, -73.5)},
    {"wfo": "akq", "lat": (36.0, 38.5), "lon": (-77.0, -74.5)},
    {"wfo": "mhx", "lat": (34.0, 36.5), "lon": (-77.5, -75.0)},
    {"wfo": "ilm", "lat": (33.0, 34.5), "lon": (-79.0, -77.0)},
    {"wfo": "chs", "lat": (31.5, 33.5), "lon": (-81.5, -79.0)},
    {"wfo": "jax", "lat": (29.0, 32.0), "lon": (-82.0, -79.5)},
    {"wfo": "mlb", "lat": (27.0, 29.5), "lon": (-81.0, -79.0)},
    {"wfo": "mfl", "lat": (25.0, 27.5), "lon": (-81.5, -79.0)},
    {"wfo": "key", "lat": (24.0, 25.5), "lon": (-82.5, -80.0)},
    {"wfo": "tbw", "lat": (26.5, 29.5), "lon": (-84.5, -82.0)},
    {"wfo": "tae", "lat": (29.0, 31.0), "lon": (-86.5, -84.0)},
    {"wfo": "mob", "lat": (29.0, 31.0), "lon": (-89.0, -86.5)},
    {"wfo": "lix", "lat": (28.5, 30.5), "lon": (-90.5, -88.5)},
    {"wfo": "lch", "lat": (29.0, 30.5), "lon": (-94.0, -91.0)},
    {"wfo": "hgx", "lat": (28.0, 30.5), "lon": (-96.0, -94.0)},
    {"wfo": "crp", "lat": (26.5, 28.5), "lon": (-97.5, -96.0)},
    {"wfo": "bro", "lat": (25.5, 27.0), "lon": (-98.0, -97.0)},
    {"wfo": "sew", "lat": (46.0, 49.0), "lon": (-126.0, -122.0)},
    {"wfo": "pqr", "lat": (43.0, 46.5), "lon": (-125.5, -123.0)},
    {"wfo": "mfr", "lat": (41.5, 43.5), "lon": (-125.0, -124.0)},
    {"wfo": "eka", "lat": (39.0, 42.0), "lon": (-125.5, -123.5)},
    {"wfo": "mtr", "lat": (36.0, 39.5), "lon": (-123.5, -121.5)},
    {"wfo": "lox", "lat": (33.5, 36.0), "lon": (-121.0, -117.5)},
    {"wfo": "sgx", "lat": (32.0, 34.0), "lon": (-118.5, -117.0)},
    {"wfo": "hfo", "lat": (18.0, 23.0), "lon": (-161.0, -154.0)},
    {"wfo": "gum", "lat": (12.0, 16.0), "lon": (143.0, 147.0)},
    {"wfo": "sju", "lat": (17.0, 19.5), "lon": (-68.0, -64.0)},
    {"wfo": "aer", "lat": (57.0, 62.0), "lon": (-155.0, -145.0)},
    {"wfo": "ajk", "lat": (54.0, 60.0), "lon": (-140.0, -130.0)},
    {"wfo": "afg", "lat": (62.0, 72.0), "lon": (-170.0, -140.0)},
    {"wfo": "alu", "lat": (50.0, 60.0), "lon": (-180.0, -160.0)},
]


def _determine_wfo(lat: float, lon: float) -> str:
    """Determine which WFO's NWPS domain covers a location.

    First tries the NWS ``/points`` API via ``get_cwa()``. If that returns a
    WFO not in the NWPS production set or raises GeographicallyUnsupported
    (offshore/non-US), falls back to bounding-box lookup.

    Raises:
        GeographicallyUnsupported: No NWPS domain covers the location.
    """
    try:
        cwa = get_cwa(lat, lon).lower()
        if cwa in _WFO_TO_REGION:
            return cwa
        logger.debug(
            "NWS /points returned CWA=%s which has no NWPS domain; trying bbox fallback",
            cwa,
        )
    except GeographicallyUnsupported:
        logger.debug("NWS /points 404 for lat=%s lon=%s; trying bbox fallback", lat, lon)

    best_wfo: str | None = None
    best_dist = float("inf")

    for entry in _WFO_BBOX:
        lat_lo, lat_hi = entry["lat"]
        lon_lo, lon_hi = entry["lon"]
        if lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi:
            center_lat = (lat_lo + lat_hi) / 2
            center_lon = (lon_lo + lon_hi) / 2
            dist = math.hypot(lat - center_lat, lon - center_lon)
            if dist < best_dist:
                best_dist = dist
                best_wfo = entry["wfo"]

    if best_wfo is None:
        raise GeographicallyUnsupported(
            f"No NWPS nearshore domain covers lat={lat}, lon={lon}. "
            "NWPS coverage is limited to US coastal waters.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    return best_wfo


# ---------------------------------------------------------------------------
# Cycle determination
# ---------------------------------------------------------------------------


def _available_cycles(now: datetime) -> list[tuple[datetime, int]]:
    """Return candidate (cycle_datetime, cycle_hour) pairs, most recent first.

    NWPS runs 2–3 cycles per day per WFO (typically 00z, 06z, 12z).
    Data availability lag is ~4–5 hours. We try the most recent plausible
    cycle, then fall back to earlier ones.
    """
    candidates: list[tuple[datetime, int]] = []

    for day_offset in range(2):
        day = now - timedelta(days=day_offset)
        for hour in sorted(_CYCLE_HOURS, reverse=True):
            cycle_dt = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if cycle_dt > now:
                continue
            candidates.append((cycle_dt, hour))
            if len(candidates) > _MAX_CYCLE_FALLBACKS + 1:
                return candidates

    return candidates


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def _build_grib_url(wfo: str, cycle_dt: datetime) -> str:
    """Build the NOMADS URL for an NWPS CG1 GRIB2 file.

    Pattern (live-verified 2026-07-10):
      ``{base}/{region}.{YYYYMMDD}/{wfo}/{HH}/CG1/{wfo}_nwps_CG1_{YYYYMMDD}_{HHMM}.grib2``
    """
    region = _WFO_TO_REGION[wfo]
    date_str = cycle_dt.strftime("%Y%m%d")
    hour_str = f"{cycle_dt.hour:02d}"
    hhmm_str = f"{cycle_dt.hour:02d}00"
    filename = f"{wfo}_nwps_CG1_{date_str}_{hhmm_str}.grib2"

    return f"{NOMADS_BASE}/{region}.{date_str}/{wfo}/{hour_str}/CG1/{filename}"


# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------


def _build_cache_key(wfo: str, cycle_hour: int, date_str: str) -> str:
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "wfo": wfo, "cg": "CG1", "cycle": cycle_hour, "date": date_str},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# GRIB2 download and field extraction
# ---------------------------------------------------------------------------


@dataclass
class NwpsExtraction:
    """Extracted nearshore wave fields from a single NWPS GRIB2 file."""

    wave_height: float | None = None  # HTSGW (m)
    wave_period: float | None = None  # PERPW (s)
    wave_direction: float | None = None  # DIRPW (degrees)
    current_speed: float | None = None  # √(UCUR² + VCUR²) (m/s)
    current_direction: float | None = None  # atan2 of UCUR/VCUR (degrees, oceanographic)
    bottom_orbital_velocity: float | None = None  # BODO (m/s)
    rip_current_probability: float | None = None  # RIPCUR (v1.5)
    total_water_level: float | None = None  # TWL (v1.5)
    wave_runup: float | None = None  # RUNUP (v1.5)
    cycle_time: str = ""  # ISO-8601 UTC


def _download_grib(url: str) -> str | None:
    """Download a GRIB2 file to a temp path. Returns path or None on 404."""
    _rate_limiter.acquire()

    try:
        response = _http_client.get(url)
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            return None
        raise

    content = response.content

    if len(content) < 4 or content[:4] != b"GRIB":
        logger.warning("NWPS download from %s: not a valid GRIB file (size=%d)", url, len(content))
        return None

    fd, path = tempfile.mkstemp(suffix=".grib2", prefix="nwps_")
    try:
        os.write(fd, content)
    finally:
        os.close(fd)

    return path


def _extract_fields(
    grib_path: str,
    lat: float,
    lon: float,
) -> NwpsExtraction:
    """Extract nearshore wave fields from a downloaded GRIB2 file."""
    result = read_grib_fields(grib_path, _ALL_FIELDS)

    extraction = NwpsExtraction()

    htsgw = result.get("HTSGW")
    if htsgw is not None:
        extraction.wave_height = bilinear_interpolate(htsgw, lat, lon)

    perpw = result.get("PERPW")
    if perpw is not None:
        extraction.wave_period = bilinear_interpolate(perpw, lat, lon)

    dirpw = result.get("DIRPW")
    if dirpw is not None:
        extraction.wave_direction = bilinear_interpolate(dirpw, lat, lon)

    ucur = result.get("UCUR")
    vcur = result.get("VCUR")
    if ucur is not None and vcur is not None:
        u = bilinear_interpolate(ucur, lat, lon)
        v = bilinear_interpolate(vcur, lat, lon)
        if u is not None and v is not None:
            extraction.current_speed = math.sqrt(u**2 + v**2)
            # Oceanographic convention: direction current is flowing TO
            extraction.current_direction = (math.degrees(math.atan2(u, v)) + 360) % 360

    bodo = result.get("BODO")
    if bodo is not None:
        extraction.bottom_orbital_velocity = bilinear_interpolate(bodo, lat, lon)

    # v1.5 fields — show-when-available
    ripcur = result.get("RIPCUR")
    if ripcur is not None:
        extraction.rip_current_probability = extract_nearest_value(ripcur, lat, lon)

    twl = result.get("TWL")
    if twl is not None:
        extraction.total_water_level = bilinear_interpolate(twl, lat, lon)

    runup = result.get("RUNUP")
    if runup is not None:
        extraction.wave_runup = bilinear_interpolate(runup, lat, lon)

    return extraction


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
    wfo_override: str | None = None,
) -> dict[str, Any]:
    """Fetch NWPS nearshore wave data for a location.

    Args:
        lat: Latitude of the marine location.
        lon: Longitude of the marine location.
        wfo_override: If provided, skip WFO determination and use this WFO
            code directly (lowercase). Used when the operator has pre-configured
            the WFO in their marine location config.

    Returns:
        dict with:
          - ``"nearshore"``: NwpsExtraction with wave fields (or None-valued
            if no data available for any recent cycle).
          - ``"wfo"``: str — WFO code used.
          - ``"cycle_time"``: str — ISO-8601 UTC of the NWPS cycle used,
            or empty string if no cycle had data.
          - ``"grid"``: str — always ``"CG1"`` for v1.

    Raises:
        GeographicallyUnsupported: No NWPS domain covers the location.
        RuntimeError: eccodes/pygrib not available (should not reach here
            if CAPABILITY gating works — defense in depth).
        QuotaExhausted: NOMADS rate-limited the request.
        TransientNetworkError: Network failure after retries.
        ProviderProtocolError: Unexpected HTTP error from NOMADS.
    """
    from weewx_clearskies_api.providers.marine.grib_processor import check_grib_available

    check_grib_available()

    wfo = wfo_override or _determine_wfo(lat, lon)
    if wfo not in _WFO_TO_REGION:
        raise GeographicallyUnsupported(
            f"WFO '{wfo}' is not in the NWPS production set. "
            "NWPS coverage is limited to US coastal WFOs.",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    now = datetime.now(UTC)
    date_str = now.strftime("%Y%m%d")
    candidates = _available_cycles(now)

    cache_key = _build_cache_key(wfo, candidates[0][1] if candidates else 0, date_str)
    cached = get_cache().get(cache_key)
    if cached is not None:
        logger.debug("NWPS cache hit for wfo=%s", wfo)
        return cached

    extraction: NwpsExtraction | None = None
    cycle_used: datetime | None = None

    for cycle_dt, cycle_hour in candidates:
        url = _build_grib_url(wfo, cycle_dt)
        logger.debug("NWPS trying cycle %s for wfo=%s: %s", cycle_dt.isoformat(), wfo, url)

        grib_path = _download_grib(url)
        if grib_path is None:
            logger.info(
                "NWPS cycle %s unavailable (404) for wfo=%s; trying earlier cycle",
                cycle_dt.isoformat(),
                wfo,
            )
            continue

        try:
            extraction = _extract_fields(grib_path, lat, lon)
            extraction.cycle_time = cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            cycle_used = cycle_dt
            break
        except Exception:
            logger.warning(
                "NWPS GRIB2 extraction failed for wfo=%s cycle=%s",
                wfo,
                cycle_dt.isoformat(),
                exc_info=True,
            )
        finally:
            try:
                os.unlink(grib_path)
            except OSError:
                pass

    if extraction is None:
        logger.warning(
            "NWPS: no data available for wfo=%s lat=%s lon=%s after %d cycle attempts",
            wfo,
            lat,
            lon,
            len(candidates),
        )
        extraction = NwpsExtraction()

    result: dict[str, Any] = {
        "nearshore": {
            "waveHeight": extraction.wave_height,
            "wavePeriod": extraction.wave_period,
            "waveDirection": extraction.wave_direction,
            "currentSpeed": extraction.current_speed,
            "currentDirection": extraction.current_direction,
            "bottomOrbitalVelocity": extraction.bottom_orbital_velocity,
            "ripCurrentProbability": extraction.rip_current_probability,
            "totalWaterLevel": extraction.total_water_level,
            "waveRunup": extraction.wave_runup,
        },
        "wfo": wfo,
        "cycle_time": extraction.cycle_time,
        "grid": "CG1",
    }

    get_cache().set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)

    logger.info(
        "NWPS nearshore data fetched: wfo=%s lat=%s lon=%s cycle=%s wave_height=%s",
        wfo,
        lat,
        lon,
        extraction.cycle_time or "none",
        extraction.wave_height,
    )

    return result
