"""GET /surf, GET /surf/{location_id} — surf quality forecast (API-MANUAL §17/§18, T3.3).

Behavior:
  GET /surf (no locationId): list configured surf locations. 404 when no
    location has "surf" in its activities list.
  GET /surf/{location_id}: full surf bundle for one location. 404 when the
    location doesn't exist or doesn't have "surf" enabled.

Data flow per API-MANUAL §17/§18 and PROVIDER-MANUAL §14.15:

  SWAN is the ONLY nearshore wave model (ADR-093). WaveWatch III
  is NOT used as a surf data source. The surf endpoint serves the last
  successful SWAN cache if the runner fails — no fallback to any
  other model.

  Per-timestep pipeline (API-MANUAL §17 "Data pipeline per forecast
  timestep"):
    1. SwanProvider.fetch(spot_id) → SWAN Hsig (stored as swellHeight).
    2. wave_transform.apply_supplements() → corrected Hsig (waveHeightAtBreak).
    3. breaker_height.hsig_to_face_height(corrected_hsig, Tp, depth, formula)
       → breakingFaceHeight.
    4. breaker_height.hawaiian_height(face_height) → breakingHawaiianHeight.
    5. surf_scorer.score_surf(breakingFaceHeight, wind, ...) → SurfForecast.

  Wind source for surf quality scoring (ADR-094):
    - t=0 (first forecast point / current conditions): station hardware →
      forecast provider → HRRR t=0.
    - t+1h through t+72h (forecast): HRRR forecast wind interpolated to the
      surf spot coordinates (same HRRR field that forced SWAN).
    NDBC buoy wind is NEVER used for surf quality scoring.

  Fallback chain:
    SWAN cache (current) → SWAN (last good, any age)
    → empty forecast list (surfForecastError populated). Never WW3.

  Supporting providers:
    - NDBC (providers/buoy/ndbc.py): spectral swell decomposition ONLY.
    - CO-OPS (providers/tides/coops.py): tide predictions (not scored).
    - NWS SRF (providers/marine/nws_srf.py): county-zone forecast
      (rip current risk, surf height range, UV) as zoneForecast.
    - ocean_data_resolver: water temperature (tiered OFS/ERDDAP/RTOFS fallback).

Response bundle top-level fields (API-MANUAL §17 "Surf endpoint response"):
  nearshoreModel: "SWAN + SwellTrack"
  lastRunTime: ISO-8601 timestamp of the SWAN run that produced this data.
  dataAge: seconds elapsed since that run.
  breakerFormula: "komar_gaughan" or "caldwell" (per spot config).
  surfHeightDisplay: "face" or "hawaiian" (per spot config).

Each provider section is wrapped in its own try/except — one provider
failing does not take down the whole response (graceful degradation per
API-MANUAL §17).

wire_surf_config(settings) stores the parsed MarineConfig module-level.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.enrichment import breaker_height as _breaker_height
from weewx_clearskies_api.enrichment import wave_transform
from weewx_clearskies_api.enrichment.surf_scorer import score_surf
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock, get_station_info
from weewx_clearskies_api.services.compute_client import (
    ComputeServiceError,
    remote_surfbeat as _remote_surfbeat,
    remote_swelltrack as _remote_swelltrack,
)
from weewx_clearskies_api.services.surf_1d_pipeline import run_pipeline as _run_surf_pipeline
from weewx_clearskies_api.services.surfbeat_runner import (
    SurfBeatResult,
    cache_surfbeat_result,
    run_surfbeat_strip,
)
from weewx_clearskies_api.services.swan_formats import compute_spot_transects as _compute_spot_transects
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup — see module docstring)
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None


def wire_surf_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by the surf endpoints.

    Mirrors the wire_marine_config() contract established for the sibling
    /marine endpoint (endpoints/marine.py, T5.1) so callers can pass either
    shape without caring which marine sub-endpoint they're wiring:
      - *settings* is itself a MarineConfig instance -> stored directly.
      - *settings* has a `.marine_config` attribute that is a MarineConfig
        instance (e.g. the future top-level Settings object once marine
        config is threaded onto it) -> that attribute is stored.
      - anything else (including None, or an unrelated object) -> stored as
        None. Never raises.
    """
    global _marine_config  # noqa: PLW0603
    if isinstance(settings, MarineConfig):
        _marine_config = settings
    elif isinstance(getattr(settings, "marine_config", None), MarineConfig):
        _marine_config = settings.marine_config  # type: ignore[attr-defined]
    else:
        _marine_config = None


# ---------------------------------------------------------------------------
# Location lookup helpers
# ---------------------------------------------------------------------------


def _surf_locations() -> list[MarineLocation]:
    if _marine_config is None:
        return []
    return [loc for loc in _marine_config.locations if "surf" in loc.activities]


def _find_location(location_id: str) -> MarineLocation | None:
    if _marine_config is None:
        return None
    for loc in _marine_config.locations:
        if loc.id == location_id:
            return loc
    return None


# ---------------------------------------------------------------------------
# Marine unit resolution (API-MANUAL §16 "Marine unit groups")
# ---------------------------------------------------------------------------

_DISPLAY_LABEL = {
    "foot": "ft",
    "meter": "m",
    "second": "s",
    "knot": "kt",
    "meter_per_second": "m/s",
    "mile_per_hour": "mph",
    "km_per_hour": "km/h",
    "nautical_mile": "nm",
    "mile": "mi",
    "km": "km",
}


def _wave_height_unit() -> tuple[str, str]:
    """Return (internal_unit_name, display_symbol) for wave height/water level."""
    height_default = "foot" if get_target_unit() == "US" else "meter"
    unit = get_group_unit("group_wave_height", height_default)
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _ocean_speed_unit() -> tuple[str, str]:
    unit = get_group_unit("group_ocean_speed", "knot")
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _wave_period_unit() -> tuple[str, str]:
    unit = get_group_unit("group_wave_period", "second")
    return (unit, _DISPLAY_LABEL.get(unit, unit))


def _units_block() -> dict[str, str]:
    _, height_symbol = _wave_height_unit()
    _, speed_symbol = _ocean_speed_unit()
    _, period_symbol = _wave_period_unit()
    return {
        "waveHeight": height_symbol,
        "wavePeriod": period_symbol,
        "windSpeed": speed_symbol,
        # setup is wave setup (m converted to display height unit — same as waveHeight)
        "setup": height_symbol,
    }


# ---------------------------------------------------------------------------
# HRRR wind interpolation helper (ADR-094)
# ---------------------------------------------------------------------------


def _interpolate_hrrr_wind(
    hrrr_field: dict,
    lat: float,
    lon: float,
    valid_time_iso: str | None = None,
    forecast_hour: int | None = None,
) -> tuple[float | None, float | None]:
    """Interpolate HRRR wind speed and direction to a surf spot.

    Selects the HRRR grid by forecast_hour (when given) or by matching
    valid_time_iso (nearest timestamp when exact match fails). Returns
    (speed_mps, direction_degrees_met) or (None, None) on any failure.

    Direction is meteorological convention (direction FROM which wind blows).
    """
    grids = hrrr_field.get("grids") or []
    if not grids:
        return None, None

    target_grid: dict | None = None

    if forecast_hour is not None:
        for g in grids:
            if g.get("forecast_hour") == forecast_hour:
                target_grid = g
                break
    elif valid_time_iso is not None:
        # Exact match first.
        for g in grids:
            if g.get("valid_time") == valid_time_iso:
                target_grid = g
                break
        if target_grid is None:
            # Nearest by elapsed seconds (handles minor ISO-format differences).
            try:
                target_dt = datetime.fromisoformat(valid_time_iso.replace("Z", "+00:00"))
                best_diff: float | None = None
                for g in grids:
                    gvt = g.get("valid_time", "")
                    try:
                        gdt = datetime.fromisoformat(gvt.replace("Z", "+00:00"))
                        diff = abs((gdt - target_dt).total_seconds())
                        if best_diff is None or diff < best_diff:
                            best_diff = diff
                            target_grid = g
                    except (ValueError, TypeError):
                        pass
            except (ValueError, TypeError):
                pass

    if target_grid is None:
        return None, None

    nj: int = target_grid.get("nj", 0)
    ni: int = target_grid.get("ni", 0)
    lat_first: float = target_grid.get("lat_first", 0.0)
    lat_last: float = target_grid.get("lat_last", 0.0)
    lon_first: float = target_grid.get("lon_first", 0.0)
    lon_last: float = target_grid.get("lon_last", 0.0)
    u_earth = target_grid.get("u_earth")
    v_earth = target_grid.get("v_earth")

    if not u_earth or not v_earth or nj < 2 or ni < 2:
        return None, None

    # Construct uniform coordinate arrays for bilinear interpolation.
    # grid_data is indexed [row (lat)][col (lon)] per wave_transform convention.
    grid_lats = [lat_first + (lat_last - lat_first) * i / (nj - 1) for i in range(nj)]
    grid_lons = [lon_first + (lon_last - lon_first) * j / (ni - 1) for j in range(ni)]

    try:
        u = wave_transform.bilinear_interpolate(u_earth, grid_lats, grid_lons, lat, lon)
        v = wave_transform.bilinear_interpolate(v_earth, grid_lats, grid_lons, lat, lon)
    except Exception:  # noqa: BLE001
        logger.debug("_interpolate_hrrr_wind: bilinear interpolation failed", exc_info=True)
        return None, None

    # Convert earth-relative U/V to speed and meteorological direction.
    # atan2(v, u) gives the direction the wind blows TO (mathematical convention).
    # 270 - degrees(atan2(v, u)) converts to direction FROM (meteorological).
    speed = math.sqrt(u**2 + v**2)
    direction = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return speed, direction


# ---------------------------------------------------------------------------
# SurfBeat bathy profile helper (T2.2)
# ---------------------------------------------------------------------------


def _compute_median_bathy_profile(transects: list) -> np.ndarray | None:
    """Compute pointwise median depth profile across open (non-structure-affected) transects.

    Used to derive the bathymetric boundary condition for the SurfBeat strip
    (T2.2).  "Open" transects are those where ``is_structure_affected`` is
    False.  Only transects that already have a populated ``bathymetric_profile``
    list are included; when none do (first run before CUDEM load), returns None
    and SurfBeat is skipped for that request.

    Args:
        transects: List of ``TransectInfo`` instances from
            ``compute_spot_transects()``.

    Returns:
        Nx2 numpy array ``[[distance_from_shore_m, depth_m], ...]`` sorted by
        distance ascending (shore first), or ``None`` when no usable profiles
        are available.
    """
    open_with_bathy = [
        t for t in transects
        if not getattr(t, "is_structure_affected", False)
        and getattr(t, "bathymetric_profile", None)
    ]
    if not open_with_bathy:
        return None

    # Parse each profile into a 2-column float64 array sorted shore → offshore.
    profiles: list[np.ndarray] = []
    for t in open_with_bathy:
        pts = [
            (float(p["distance_m"]), float(p["depth_m"]))
            for p in t.bathymetric_profile
            if p.get("distance_m") is not None and p.get("depth_m") is not None
        ]
        if len(pts) >= 2:
            arr = np.array(pts, dtype=np.float64)
            arr = arr[np.argsort(arr[:, 0])]  # sort by distance ascending
            profiles.append(arr)

    if not profiles:
        return None
    if len(profiles) == 1:
        return profiles[0]

    # Common distance range: intersection of all profile extents avoids extrapolation.
    d_min = max(float(p[0, 0]) for p in profiles)
    d_max = min(float(p[-1, 0]) for p in profiles)
    if d_max <= d_min:
        # No common range — fall back to the profile with the largest extent.
        largest_idx = int(np.argmax([float(p[-1, 0] - p[0, 0]) for p in profiles]))
        return profiles[largest_idx]

    # 5 m grid (matches SurfBeat strip default dx=5 m)
    n_pts = max(2, int(round((d_max - d_min) / 5.0)) + 1)
    d_common = np.linspace(d_min, d_max, n_pts)

    # Interpolate each profile at the common distances, then take pointwise median.
    depths_stacked = np.vstack([
        np.interp(d_common, p[:, 0], p[:, 1]) for p in profiles
    ])
    median_depths = np.median(depths_stacked, axis=0)
    median_depths = np.maximum(median_depths, 0.01)  # clamp to minimum wet depth

    return np.column_stack([d_common, median_depths])


# ---------------------------------------------------------------------------
# GET /surf — list surf locations
# ---------------------------------------------------------------------------


@router.get("/surf", summary="Configured surf locations", tags=["Marine"])
def list_surf_locations() -> dict:
    """List locations with "surf" in their configured activities.

    Card fields are metadata-only (no live provider fetch for the list
    view, per task brief "quality_stars if available from cache, else
    None") — the detail endpoint (GET /surf/{location_id}) does the live
    fetch + scoring.
    """
    locations = _surf_locations()
    if not locations:
        raise HTTPException(status_code=404, detail="No surf locations configured")

    cards = [
        {
            "locationId": loc.id,
            "name": loc.name,
            "lat": loc.lat,
            "lon": loc.lon,
            "qualityStars": None,
            "conditionsText": None,
        }
        for loc in locations
    ]

    now_str = utc_isoformat(datetime.now(tz=UTC))
    return {
        "data": cards,
        "units": _units_block(),
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("surf").model_dump(by_alias=True),
        "generatedAt": now_str,
    }


# ---------------------------------------------------------------------------
# GET /surf/{location_id} — surf bundle for one location
# ---------------------------------------------------------------------------


@router.get("/surf/{location_id}", summary="Surf forecast for a location", tags=["Marine"])
def get_surf(location_id: str) -> dict:
    location = _find_location(location_id)
    if location is None or "surf" not in location.activities:
        raise HTTPException(status_code=404, detail=f"Surf location {location_id!r} not found")

    spot_config = _marine_config.surf_spots.get(location_id) if _marine_config else None
    if spot_config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No surf spot configuration for location {location_id!r}",
        )

    wave_height_internal, height_symbol = _wave_height_unit()
    _wind_internal, _wind_symbol = _ocean_speed_unit()

    # --- Compute offloading config (T3.2) ---
    # When surf_compute_host is set, SwellTrack and SurfBeat computations are
    # POSTed to the remote compute service.  Falls back to in-process on error.
    _compute_host: str | None = _marine_config.surf_compute_host if _marine_config else None
    _compute_secret: str = os.environ.get("SURF_COMPUTE_SECRET", "") if _compute_host else ""
    _compute_verify_tls: bool = (
        _marine_config.surf_compute_verify_tls if _marine_config else True
    )

    # --- SWAN: sole nearshore wave source (ADR-093, API-MANUAL §17) ---
    # Fallback: current cache → last-good cache (any age) → empty list.
    # Never falls to WaveWatch III for surf data.
    swan_result: dict | None = None
    try:
        from weewx_clearskies_api.providers.nearshore import swan  # noqa: PLC0415

        swan_result = swan.fetch(spot_id=location_id)
    except Exception:
        logger.warning(
            "surf endpoint: SWAN fetch failed for %s", location_id, exc_info=True
        )

    swan_points: list[dict] = []
    last_run_time: str | None = None
    data_age_seconds: int | None = None
    surf_forecast_error: str | None = None

    swan_spectral: list[dict] = []  # T3.3 — per-timestep SPECOUT spectral decompositions

    if swan_result is not None:
        swan_points = swan_result.get("forecast") or []
        swan_spectral = swan_result.get("spectral") or []  # T3.3
        last_run_time = swan_result.get("run_time")
        data_age_seconds = swan_result.get("data_age_seconds")
        if not swan_points:
            surf_forecast_error = "surf forecast unavailable"
            logger.warning(
                "surf endpoint: SWAN returned empty forecast for %s", location_id
            )
    else:
        surf_forecast_error = "surf forecast unavailable"
        logger.warning(
            "surf endpoint: no SWAN data cached for %s — SWAN may not have run yet",
            location_id,
        )

    # --- HRRR wind field (ADR-094): for forecast timesteps (t>0) ---
    # Same bbox SWAN uses (±1°) guarantees a cache hit.
    hrrr_field: dict | None = None
    _HRRR_MARGIN_DEG = 1.0
    try:
        from weewx_clearskies_api.providers.wind import hrrr as hrrr_provider  # noqa: PLC0415

        hrrr_bbox = (
            location.lon - _HRRR_MARGIN_DEG,
            location.lat - _HRRR_MARGIN_DEG,
            location.lon + _HRRR_MARGIN_DEG,
            location.lat + _HRRR_MARGIN_DEG,
        )
        hrrr_field = hrrr_provider.fetch(bbox=hrrr_bbox)
    except Exception:
        logger.debug(
            "surf endpoint: HRRR wind unavailable for %s; "
            "falling back to station/forecast wind for all timesteps",
            location_id,
            exc_info=True,
        )

    # --- Wind for t=0: station hardware → forecast provider (ADR-094) ---
    # This is the existing precedence for current-conditions wind scoring.
    # NDBC buoy wind is NEVER used for surf quality scoring (API-MANUAL §17).
    wind_speed_station: float | None = None
    wind_direction_station: float | None = None
    wind_source_station: str = "station"
    try:
        from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
            is_station_served,
        )

        if is_station_served(location.id):
            from sqlalchemy.orm import Session as _Session  # noqa: PLC0415

            from weewx_clearskies_api.db.session import get_engine  # noqa: PLC0415
            from weewx_clearskies_api.db.registry import get_registry  # noqa: PLC0415
            from weewx_clearskies_api.services.archive import (  # noqa: PLC0415
                get_current as _get_current_observation,
            )

            registry = get_registry()
            with _Session(get_engine()) as station_db:
                station_obs = _get_current_observation(station_db, registry)
            if station_obs is not None:
                if station_obs.windSpeed is not None:
                    wind_speed_station = station_obs.windSpeed
                if station_obs.windDir is not None:
                    wind_direction_station = station_obs.windDir
            wind_source_station = "station"
        else:
            from weewx_clearskies_api.services.marine_weather_cache import (  # noqa: PLC0415
                get_marine_weather_cache,
            )

            weather_cache = get_marine_weather_cache()
            if weather_cache is not None:
                conditions = weather_cache.get_current_conditions(location.lat, location.lon)
                if conditions is not None:
                    if conditions.get("windSpeed") is not None:
                        wind_speed_station = conditions["windSpeed"]
                    if conditions.get("windDirection") is not None:
                        wind_direction_station = conditions["windDirection"]
            wind_source_station = "forecast_provider"
    except Exception:
        logger.warning(
            "surf endpoint: wind lookup for t=0 failed for %s", location_id, exc_info=True
        )

    # --- NDBC spectral swell decomposition (API-MANUAL §17) ---
    # NDBC role: spectral decomposition ONLY — not wind, not primary height.
    # A single current reading broadcast across all forecast timesteps.
    spectral_components: list[dict] = []
    try:
        if location.ndbc_station_ids:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            ndbc_result = ndbc.fetch(station_id=location.ndbc_station_ids[0], include_spectral=True)
            spectral_components = [c.model_dump() for c in (ndbc_result.get("spectral") or [])]
    except Exception:
        logger.warning(
            "surf endpoint: NDBC spectral fetch failed for %s", location_id, exc_info=True
        )

    # --- T3.4: Group transect points by timestep ---
    # With CURVE transect output, swan_points contains multiple rows per timestep
    # (one per transect point).  We group by time and select the reference point
    # at ~10m depth for K-G/Caldwell and HSWELL display.
    # With the old single POINTS output, each timestep has exactly one point;
    # the grouping still works (group has one element).

    # Build a lookup: time_iso → list[point_dict]
    _points_by_time: dict[str, list[dict]] = defaultdict(list)
    for _p in swan_points:
        _t = (_p.get("time") if isinstance(_p, dict) else getattr(_p, "time", "")) or ""
        if _t:
            _d = _p if isinstance(_p, dict) else _p.model_dump()
            _points_by_time[_t].append(_d)

    # Order timesteps chronologically
    _ordered_times: list[str] = sorted(_points_by_time.keys())

    # Spectral lookup by timestep: time_iso → list[SpectralWaveComponent-like dicts]
    # (T3.3 / T3.5 — replaces NDBC for multiSwell)
    _spectral_by_time: dict[str, list[dict]] = {}
    for _spec_entry in swan_spectral:
        _spec_t = _spec_entry.get("time", "")
        if _spec_t:
            _spectral_by_time[_spec_t] = _spec_entry.get("components", [])

    # --- Almanac: sunrise/sunset and station TZ for time-of-day scoring (SURF-2 fix) ---
    # Computed once per request (sunrise changes < 3 min/day, well within the
    # ±1 h dawn window).  Failures degrade gracefully: time-of-day adjustments
    # default to 0 for all timesteps when the almanac or station info is absent.
    _sunrise_utc: str | None = None
    _sunset_utc: str | None = None
    _station_tz: str | None = None
    try:
        from weewx_clearskies_api.services.almanac import (  # noqa: PLC0415
            compute_almanac as _compute_almanac,
        )

        _stn_info = get_station_info()
        _station_tz = _stn_info.timezone or None
        _today = datetime.now(tz=UTC).date()
        _almanac_day = _compute_almanac(
            _today,
            location.lat,
            location.lon,
            0.0,  # surf beaches are at sea level
            station_tz=_station_tz or "UTC",
        )
        _sunrise_utc = _almanac_day.sun.rise
        _sunset_utc = _almanac_day.sun.set
    except Exception:
        logger.debug(
            "surf endpoint: almanac lookup failed for time-of-day scoring (%s)",
            location_id,
            exc_info=True,
        )

    # Fallback output depth from bathymetric profile (used when no depth info
    # from SWAN TABLE DEPTH column — old single-point mode).
    # bathymetric_profile was removed from SurfSpotConfig when the wizard-time
    # CUDEM download was replaced by runtime bidirectional profiles; getattr
    # keeps this code path safe for configs that pre-date that change.
    _fallback_depth_m: float | None = None
    _bath_profile = getattr(spot_config, "bathymetric_profile", None)
    if _bath_profile and _bath_profile[0] is not None:
        _fallback_depth_m = _bath_profile[0].depth_m

    # T4.4: Compute surf spot transects once (static geometry — same for all timesteps).
    # Used by the per-partition 1D pipeline (run_pipeline) to model wave transformation
    # independently on each cross-shore transect.  Degrades gracefully on failure.
    _spot_transects: list = []
    try:
        _spot_transects = _compute_spot_transects(
            segment_start_lat=spot_config.segment_start_lat,
            segment_start_lon=spot_config.segment_start_lon,
            segment_end_lat=spot_config.segment_end_lat,
            segment_end_lon=spot_config.segment_end_lon,
            transect_spacing_m=spot_config.transect_spacing_m,
            beach_facing_degrees=spot_config.beach_facing_degrees,
            structures=spot_config.structures or None,
        )
    except Exception:
        logger.warning(
            "surf endpoint: compute_spot_transects failed for %s — "
            "1D pipeline will degrade for all timesteps",
            location_id,
            exc_info=True,
        )

    # Populate bathymetric profiles on transects from cached CUDEM profile.
    # The profile is static geography — computed once and cached at
    # /etc/weewx-clearskies/spot_profiles/{id}.json by the SWAN setup phase.
    if _spot_transects:
        _profile_path = Path("/etc/weewx-clearskies/spot_profiles") / f"{location_id}.json"
        if _profile_path.exists():
            try:
                _cached = json.loads(_profile_path.read_text(encoding="utf-8"))
                _profile_pts = _cached.get("profile", [])
                if _profile_pts:
                    _offshore_only = [
                        p for p in _profile_pts
                        if p.get("depth_m") is not None and p.get("depth_m", 0) > 0
                    ]
                    _offshore_only.sort(key=lambda p: p["distance_m"])
                    for t in _spot_transects:
                        t.bathymetric_profile = _offshore_only
            except Exception:
                logger.debug(
                    "surf endpoint: failed to load CUDEM profile for %s",
                    location_id, exc_info=True,
                )

    # T4.4: Build full-entry SPECOUT lookup by timestep for the handoff pipeline.
    # Each value is the complete _spec_entry dict (time + components today; future
    # SWAN runner updates will add freqs_hz/dirs_deg/energy, which run_pipeline
    # uses automatically without changes here).
    _handoff_specout_by_time: dict[str, dict] = {}
    for _hs_entry in swan_spectral:
        _hs_t = _hs_entry.get("time", "")
        if _hs_t:
            _handoff_specout_by_time[_hs_t] = _hs_entry

    # --- t0 datetime for forecast hour offset calculations (used by SurfBeat and carry-forward) ---
    _t0_dt: datetime | None = None
    if _ordered_times:
        try:
            _t0_dt = datetime.fromisoformat(_ordered_times[0].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # --- SurfBeat IG strip runs (T2.2) ---
    # Run at every cadence_hours mark (default 3 h), covering hours 0–72 (25 runs).
    # Results are keyed by integer forecast hour.  Intermediate hours carry forward
    # the most recent result inside the per-timestep loop below (no interpolation —
    # design decision per SURF-MODEL-FIX-PLAN §T2.2).
    #
    _SURFBEAT_SWAN_BINARY = "/usr/local/bin/swan"

    _surfbeat_by_hour: dict[int, SurfBeatResult | None] = {}

    if spot_config.surfbeat_enabled and _ordered_times and _t0_dt is not None:
        _sb_bathy: np.ndarray | None = None
        if _spot_transects:
            try:
                _sb_bathy = _compute_median_bathy_profile(_spot_transects)
            except Exception:
                logger.warning(
                    "surf endpoint: SurfBeat bathy median failed for %s — "
                    "SurfBeat skipped for this request",
                    location_id,
                    exc_info=True,
                )

        if _sb_bathy is not None:
            for _sb_hr in range(0, 73, spot_config.surfbeat_cadence_hours):
                # Find the SWAN timestep whose elapsed hours since t0 is closest
                # to this cadence hour.
                _best_ts: str | None = None
                _best_diff: float | None = None
                for _ts_candidate in _ordered_times:
                    try:
                        _ts_dt = datetime.fromisoformat(
                            _ts_candidate.replace("Z", "+00:00")
                        )
                        _diff = abs((_ts_dt - _t0_dt).total_seconds() / 3600.0 - _sb_hr)
                        if _best_diff is None or _diff < _best_diff:
                            _best_diff = _diff
                            _best_ts = _ts_candidate
                    except (ValueError, TypeError):
                        pass

                if _best_ts is None:
                    _surfbeat_by_hour[_sb_hr] = None
                    continue

                # Use the most offshore point at this timestep for boundary conditions.
                _sb_pts = _points_by_time.get(_best_ts, [])
                if not _sb_pts:
                    _surfbeat_by_hour[_sb_hr] = None
                    continue

                _offshore_pt = max(
                    _sb_pts,
                    key=lambda p: float(p.get("distanceFromShore") or 0.0),
                )
                _sb_hs_raw = _offshore_pt.get("waveHeight")
                _sb_tp_raw = _offshore_pt.get("wavePeriod")
                _sb_dir_raw = _offshore_pt.get("waveDirection")

                if _sb_hs_raw is None or _sb_tp_raw is None or _sb_dir_raw is None:
                    _surfbeat_by_hour[_sb_hr] = None
                    continue

                try:
                    if _compute_host:
                        try:
                            _sb_result = _remote_surfbeat(
                                _compute_host,
                                _compute_secret,
                                _compute_verify_tls,
                                spot_id=location_id,
                                profile=_sb_bathy,
                                hs=float(_sb_hs_raw),
                                tp=float(_sb_tp_raw),
                                direction=float(_sb_dir_raw),
                                cfjon=spot_config.friction_coefficient,
                                wind_speed_ms=float(ts_wind_speed or 0.0),
                                wind_direction_deg=float(ts_wind_direction or 0.0),
                            )
                        except ComputeServiceError:
                            logger.warning(
                                "surf endpoint: compute service unavailable for SurfBeat"
                                " (%s hr=%d) — falling back to in-process",
                                location_id,
                                _sb_hr,
                                exc_info=True,
                            )
                            _sb_result = run_surfbeat_strip(
                                spot_id=location_id,
                                profile=_sb_bathy,
                                hs=float(_sb_hs_raw),
                                tp=float(_sb_tp_raw),
                                direction=float(_sb_dir_raw),
                                cfjon=spot_config.friction_coefficient,
                                swan_binary=_SURFBEAT_SWAN_BINARY,
                                wind_speed_ms=float(ts_wind_speed or 0.0),
                                wind_direction_deg=float(ts_wind_direction or 0.0),
                            )
                    else:
                        _sb_result = run_surfbeat_strip(
                            spot_id=location_id,
                            profile=_sb_bathy,
                            hs=float(_sb_hs_raw),
                            tp=float(_sb_tp_raw),
                            direction=float(_sb_dir_raw),
                            cfjon=spot_config.friction_coefficient,
                            swan_binary=_SURFBEAT_SWAN_BINARY,
                            wind_speed_ms=float(ts_wind_speed or 0.0),
                            wind_direction_deg=float(ts_wind_direction or 0.0),
                        )
                    _surfbeat_by_hour[_sb_hr] = _sb_result
                    logger.debug(
                        "surf endpoint: SurfBeat hr=%d Hs_ig=%.4f m set_timing=%s min",
                        _sb_hr,
                        _sb_result.hs_ig_shoreline,
                        (
                            f"{_sb_result.set_timing_minutes:.1f}"
                            if _sb_result.set_timing_minutes is not None
                            else "None"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "surf endpoint: SurfBeat strip failed for %s @ hour %d — "
                        "IG fields will be null for timesteps carrying this result",
                        location_id,
                        _sb_hr,
                        exc_info=True,
                    )
                    _surfbeat_by_hour[_sb_hr] = None
        else:
            logger.debug(
                "surf endpoint: SurfBeat skipped for %s — "
                "no open transect bathy profiles available yet",
                location_id,
            )

    # Cache the most recent successful SurfBeat result for beach_profile.py (F1 fix).
    _last_sb = next(
        (v for v in reversed(list(_surfbeat_by_hour.values())) if v is not None),
        None,
    )
    if _last_sb is not None:
        cache_surfbeat_result(location_id, _last_sb)

    # --- Per-timestep pipeline (API-MANUAL §17 "Data pipeline per forecast timestep") ---
    forecast_entries: list[dict] = []
    for ts_idx, valid_time in enumerate(_ordered_times):
        pts_at_time = _points_by_time[valid_time]
        if not pts_at_time:
            continue

        # T3.4+T7.2: Scan for QB peaks (break points) along the transect FIRST,
        # so the reference point can be selected just offshore of the biggest break.
        # Sort transect points deepest-to-shallowest (offshore → onshore).
        # A QB peak is a local maximum >= 0.25 (threshold for meaningful breaking).
        # Multiple peaks identify outer bar + inner bar break zones.
        break_points: list[dict] = []
        _break_src_pts: list[dict] = []         # parallel: full transect pt at each break
        _break_offshore_pts: list[dict | None] = []  # parallel: just-offshore pt per break
        if len(pts_at_time) > 1:
            sorted_pts = sorted(
                pts_at_time,
                key=lambda p: p.get("distanceFromShore") or 0,
                reverse=True,
            )
            for _bp_idx, _bp_pt in enumerate(sorted_pts):
                _qb = _bp_pt.get("breakingFraction")
                if _qb is None or _qb < 0.25:
                    continue
                _prev_qb = (
                    (sorted_pts[_bp_idx - 1].get("breakingFraction") or 0.0)
                    if _bp_idx > 0
                    else 0.0
                )
                _next_qb = (
                    (sorted_pts[_bp_idx + 1].get("breakingFraction") or 0.0)
                    if _bp_idx < len(sorted_pts) - 1
                    else 0.0
                )
                if _qb >= _prev_qb and _qb >= _next_qb:
                    _bp_wh = _bp_pt.get("waveHeight")
                    break_points.append({
                        "distanceFromShore": _bp_pt.get("distanceFromShore"),
                        "depth": _bp_pt.get("depth"),
                        # Convert raw SWAN Hs (meters) to display unit — same
                        # conversion applied to the other height fields below.
                        "waveHeight": (
                            _convert_unit(float(_bp_wh), "meter", wave_height_internal)
                            if _bp_wh is not None
                            else None
                        ),
                    })
                    _break_src_pts.append(_bp_pt)
                    _break_offshore_pts.append(
                        sorted_pts[_bp_idx - 1] if _bp_idx > 0 else None
                    )

        # T3.4+T7.2: Select the reference point.
        # If breaks detected: use the transect point just offshore of the biggest
        # break (highest waveHeight × breakingFraction product). Falls back to
        # closest-to-10m when no breaking is detected (flat/no-break conditions)
        # or when the biggest break is already the most offshore point available.
        def _depth_of(pt: dict) -> float:
            d = pt.get("depth")
            return float(d) if d is not None else 9999.0

        ref_point = pts_at_time[0]
        if break_points:
            _biggest_idx = max(
                range(len(break_points)),
                key=lambda i: (_break_src_pts[i].get("waveHeight") or 0)
                              * (_break_src_pts[i].get("breakingFraction") or 0),
            )
            _offshore_ref = _break_offshore_pts[_biggest_idx]
            if _offshore_ref is not None:
                ref_point = _offshore_ref
            elif len(pts_at_time) > 1:
                ref_point = min(pts_at_time, key=lambda pt: abs(_depth_of(pt) - 10.0))
        elif len(pts_at_time) > 1:
            ref_point = min(pts_at_time, key=lambda pt: abs(_depth_of(pt) - 10.0))

        raw_hsig = ref_point.get("waveHeight")
        if raw_hsig is None:
            continue

        wave_period_pt = float(ref_point.get("wavePeriod") or 0.0)
        wave_direction_pt = float(ref_point.get("waveDirection") or 0.0)

        # T3.4: HSWELL at ~10m depth point is the "Swell Height" display value.
        # Fall back to raw_hsig when HSWELL is absent (old single-point mode).
        swell_height_raw = ref_point.get("swellHeight")
        swell_height_m: float = float(swell_height_raw) if swell_height_raw is not None else float(raw_hsig)

        # T3.4: Output depth from SWAN TABLE DEPTH column (more accurate than
        # the static bathymetric_profile value used previously).
        ref_depth = ref_point.get("depth")
        output_depth_m: float | None = float(ref_depth) if ref_depth is not None else _fallback_depth_m

        # Step 2: wave_transform supplements → corrected Hsig = waveHeightAtBreak
        # grid_data omitted: SWAN outputs at spot/transect coordinates, so
        # Supplement 3 (spatial interpolation) is handled by SWAN internally.
        supplemented = wave_transform.apply_supplements(
            {
                "wave_height": float(raw_hsig),
                "wave_period": wave_period_pt,
                "wave_direction": wave_direction_pt,
            },
            spot_config,
            location.lat,
            location.lon,
        )
        if supplemented is not None and supplemented.get("wave_height") is not None:
            corrected_hsig = supplemented["wave_height"]
            wave_period_pt = supplemented.get("wave_period") or wave_period_pt
            wave_direction_pt = supplemented.get("wave_direction") or wave_direction_pt
        else:
            corrected_hsig = float(raw_hsig)

        # Step 3: breaker height conversion → breakingFaceHeight
        # T3.4: K-G/Caldwell applied at ~10m depth (ref_point depth) instead of
        # the old pin-drop depth.  At 10m, SWAN has handled refraction but not
        # final shoaling-to-breaking, so K-G applies ~60–80% amplification.
        # T4.3: depth_m replaces output_depth_m; source="deep_water" is explicit
        # (deep-water/offshore Hs → full K-G/Caldwell formula, no double-counting).
        face_height_m = _breaker_height.hsig_to_face_height(
            corrected_hsig,
            wave_period_pt,
            depth_m=output_depth_m,
            formula=spot_config.breaker_formula,
            source="deep_water",
        )

        # Step 4: Hawaiian scale → breakingHawaiianHeight
        hawaiian_height_m = _breaker_height.hawaiian_height(face_height_m)

        # Step 5: wind source per timestep (ADR-094)
        # First timestep (closest to now) = t=0 (current conditions).
        # Remaining timesteps = forecast.
        is_first_ts = ts_idx == 0
        if is_first_ts:
            ts_wind_speed = wind_speed_station
            ts_wind_direction = wind_direction_station
            ts_wind_source = wind_source_station
            if ts_wind_speed is None and hrrr_field is not None:
                ts_wind_speed, ts_wind_direction = _interpolate_hrrr_wind(
                    hrrr_field,
                    location.lat,
                    location.lon,
                    forecast_hour=0,
                )
                if ts_wind_speed is not None:
                    ts_wind_source = "hrrr"
        else:
            ts_wind_speed = None
            ts_wind_direction = None
            ts_wind_source = "hrrr"
            if hrrr_field is not None:
                ts_wind_speed, ts_wind_direction = _interpolate_hrrr_wind(
                    hrrr_field,
                    location.lat,
                    location.lon,
                    valid_time_iso=valid_time,
                )

        # T3.5/T4.1: Fetch SWAN SPECOUT components for this timestep.
        # Used by the scorer for swell dominance and cross-swell sub-factors
        # (T4.1), and also for the multiSwell display field (T3.5).
        # Pre-fetched here so it can be passed to score_surf() before the
        # entry dict is built.
        ts_spectral = _spectral_by_time.get(valid_time)

        # T4.4: Run the per-partition 1D swell transformation pipeline.
        # Called once per timestep; handles all partitions × all transects internally.
        # Degrades gracefully when:
        #   - raw SPECOUT (freqs_hz/dirs_deg/energy) is not yet in the cache
        #     (current state — future SWAN runner update adds it; no changes needed here)
        #   - transects have no bathymetric profiles (until CUDEM load at SWAN runtime)
        # Tide level is 0.0: CO-OPS predictions are fetched after this loop.
        # A degraded result triggers the existing SWAN CURVE-based fallback path.
        _ts_handoff_specout = _handoff_specout_by_time.get(valid_time) or {}
        _pipeline_result = None
        _swelltrack_compute_fallback = False
        if _spot_transects:
            try:
                if _compute_host:
                    try:
                        _pipeline_result = _remote_swelltrack(
                            _compute_host,
                            _compute_secret,
                            _compute_verify_tls,
                            specout_data=_ts_handoff_specout,
                            transects=_spot_transects,
                            tide_level=0.0,
                            beach_facing=spot_config.beach_facing_degrees,
                            gamma=0.73,
                            cfjon=spot_config.friction_coefficient,
                            # T4.5: bulk fallback when SPECOUT freqs/dirs/energy absent.
                            bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                            bulk_tp=wave_period_pt if wave_period_pt else None,
                            bulk_dir=wave_direction_pt if wave_direction_pt else None,
                            # T4.5b: canonical partitions from deep-water SPECOUT
                            # so per_partition_breaks uses indices the swell card knows.
                            canonical_partitions=ts_spectral or None,
                        )
                    except ComputeServiceError:
                        logger.warning(
                            "surf endpoint: compute service unavailable for SwellTrack"
                            " (%s @ %s) — falling back to in-process",
                            location_id,
                            valid_time,
                            exc_info=True,
                        )
                        _swelltrack_compute_fallback = True
                        _pipeline_result = _run_surf_pipeline(
                            specout_data=_ts_handoff_specout,
                            transects=_spot_transects,
                            tide_level=0.0,
                            beach_facing=spot_config.beach_facing_degrees,
                            cfjon=spot_config.friction_coefficient,
                            # T4.5: bulk fallback when SPECOUT freqs/dirs/energy absent.
                            bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                            bulk_tp=wave_period_pt if wave_period_pt else None,
                            bulk_dir=wave_direction_pt if wave_direction_pt else None,
                            # T4.5b: canonical partitions from deep-water SPECOUT
                            # so per_partition_breaks uses indices the swell card knows.
                            canonical_partitions=ts_spectral or None,
                        )
                else:
                    _pipeline_result = _run_surf_pipeline(
                        specout_data=_ts_handoff_specout,
                        transects=_spot_transects,
                        tide_level=0.0,
                        beach_facing=spot_config.beach_facing_degrees,
                        cfjon=spot_config.friction_coefficient,
                        # T4.5: bulk fallback when SPECOUT freqs/dirs/energy absent.
                        bulk_hs=float(raw_hsig) if raw_hsig is not None else None,
                        bulk_tp=wave_period_pt if wave_period_pt else None,
                        bulk_dir=wave_direction_pt if wave_direction_pt else None,
                        # T4.5b: canonical partitions from deep-water SPECOUT
                        # so per_partition_breaks uses indices the swell card knows.
                        canonical_partitions=ts_spectral or None,
                    )
            except Exception:
                logger.warning(
                    "surf endpoint: 1D pipeline raised for %s @ %s — degrading to SWAN CURVE",
                    location_id,
                    valid_time,
                    exc_info=True,
                )

        # Step 6: score surf using breakingFaceHeight (ADR-094)
        # T3.5: spectral_components=None — NDBC spectral data is NOT passed to
        # the scorer.  Scoring uses SWAN values only.  The NDBC fetch still
        # runs and spectralComponents is still in the response (reference data),
        # but it no longer feeds the surf score or multiSwell display.
        # T4.1: directional_spread and multi_swell pass SWAN DSPR/SPECOUT data
        # to the organization composite sub-factors.
        surf_forecast = score_surf(
            wave_height=face_height_m,
            wave_period=wave_period_pt,
            wave_direction=wave_direction_pt,
            wind_speed=ts_wind_speed,
            wind_direction=ts_wind_direction,
            spectral_components=None,  # deprecated — NDBC not used in scoring
            spot_config=spot_config,
            time_utc=valid_time,
            sunrise_utc=_sunrise_utc,  # SURF-2: dawn bonus wiring
            sunset_utc=_sunset_utc,    # SURF-2: reserved for future dusk handling
            station_tz=_station_tz,    # SURF-2: afternoon penalty local-time conversion
            wind_source=ts_wind_source,
            directional_spread=ref_point.get("directionalSpread"),  # T4.1: SWAN DSPR at ~10m
            multi_swell=ts_spectral,  # T4.1: SWAN SPECOUT for organization sub-factors
            height_unit=wave_height_internal,  # SURF-10: operator's configured height unit
            height_unit_label=height_symbol,   # SURF-10: e.g. "ft" or "m"
            wind_unit=_wind_internal,          # SURF-10: operator's configured wind unit
            wind_unit_label=_wind_symbol,      # SURF-10: e.g. "mph" or "kn"
        )

        entry = surf_forecast.model_dump()

        # T3.4: Attach QB break points detected above.  None when no peaks
        # were found (flat conditions, single-point mode, or QB data absent).
        entry["breakPoints"] = break_points if break_points else None

        # Overwrite height fields with the four canonical values (unit-converted).
        entry["swellHeight"] = _convert_unit(swell_height_m, "meter", wave_height_internal)
        entry["waveHeightAtBreak"] = _convert_unit(corrected_hsig, "meter", wave_height_internal)
        entry["breakingFaceHeight"] = _convert_unit(face_height_m, "meter", wave_height_internal)
        entry["breakingHawaiianHeight"] = _convert_unit(
            hawaiian_height_m, "meter", wave_height_internal
        )
        entry["windSource"] = ts_wind_source

        # T5.2: DSPR and SETUP from SWAN TABLE at ~10m depth point.
        # directionalSpread is in degrees (dimensionless — no conversion needed).
        # setup is wave setup (water level rise due to wave breaking) in meters;
        # convert to the operator's configured height display unit.
        entry["directionalSpread"] = ref_point.get("directionalSpread")
        _setup_raw = ref_point.get("setup")
        entry["setup"] = (
            _convert_unit(float(_setup_raw), "meter", wave_height_internal)
            if _setup_raw is not None
            else None
        )

        # T3.5: multiSwell from SWAN SPECOUT decomposition for this timestep,
        # NOT from NDBC spectral.  Each timestep gets its own spectral decomposition.
        # Fall back to None (empty) when SPECOUT is unavailable.
        if ts_spectral:
            entry["multiSwell"] = [
                {
                    "height": _convert_unit(c.get("height", 0.0), "meter", wave_height_internal),
                    "period": c.get("period", 0.0),
                    "direction": c.get("direction", 0.0),
                    "energy": c.get("energy", 0.0),
                    "frequencyRange": c.get("frequencyRange", [0.0, 0.0]),
                    "classification": c.get("classification", "swell"),
                }
                for c in ts_spectral
            ]
            # A2-F2 (SURF-23): swellHeight = dominant deep-water SPECOUT partition height.
            # The dominant partition is the one with the highest Hs in ts_spectral.
            # This overrides the nearshore HSWELL value set above from ref_point, which
            # is SWAN TABLE output at ~10 m depth and reflects nearshore shoaling —
            # not the deep-water swell height the display card expects.
            _dominant_hs_m = max((c.get("height", 0.0) for c in ts_spectral), default=0.0)
            entry["swellHeight"] = _convert_unit(_dominant_hs_m, "meter", wave_height_internal)
        else:
            entry["multiSwell"] = None  # SPECOUT not available for this timestep
            # ts_spectral absent: retain the nearshore HSWELL value set above.

        # T4.4/T4.5: Apply 1D pipeline results.
        # On success (degraded=False): override breaking heights with physics-based
        # values; add pipeline-specific response fields.
        # T4.5 bulk-fallback (degraded=True, face_height > 0): apply 1D results
        # (more accurate than SWAN CURVE) but preserve degraded=True in the response
        # so the consumer knows spectral decomposition was not available.
        # Total failure (degraded=True, face_height == 0) or no pipeline result:
        # keep existing SWAN CURVE-based values and mark degraded.
        # swellHeight was overridden from deep-water SPECOUT dominant partition
        # when ts_spectral was available (multiSwell block above); it is NOT
        # overridden here.  When SPECOUT is unavailable, the nearshore HSWELL
        # value from ref_point is preserved as-is.
        _1d_face_m = (
            _pipeline_result.best_peak_face_height_m
            if _pipeline_result is not None
            else 0.0
        )
        _apply_1d = _pipeline_result is not None and _1d_face_m > 0.0
        if _apply_1d:
            _best_face_m = _1d_face_m
            _avg_face_m = _pipeline_result.spot_average_face_height_m
            # Override with pipeline-computed breaking heights.
            entry["breakingFaceHeight"] = _convert_unit(
                _best_face_m, "meter", wave_height_internal
            )
            # waveHeightAtBreak: Hs at the best break point.
            # face_height = 1.27 × Hs_break (H1/10 Rayleigh factor, source="break_point"),
            # so Hs_break = face_height / 1.27.
            _hs_at_best_break_m = _best_face_m / 1.27
            entry["waveHeightAtBreak"] = _convert_unit(
                _hs_at_best_break_m, "meter", wave_height_internal
            )
            # Pipeline-specific fields.
            entry["bestPeakFaceHeight"] = _convert_unit(
                _best_face_m, "meter", wave_height_internal
            )
            entry["spotAverageFaceHeight"] = _convert_unit(
                _avg_face_m, "meter", wave_height_internal
            )
            entry["peelAngle"] = _pipeline_result.peel_angle_deg
            entry["peelClassification"] = _pipeline_result.peel_classification
            entry["peelDirection"] = _pipeline_result.peel_direction
            entry["transectCount"] = _pipeline_result.transect_count
            entry["openTransectCount"] = _pipeline_result.open_transect_count
            # T4.5: preserve degraded flag — True when bulk fallback was used.
            # T3.2: also True when SwellTrack was offloaded but compute service
            # was unreachable and we fell back to in-process execution.
            entry["degraded"] = _pipeline_result.degraded or _swelltrack_compute_fallback
        else:
            # Total failure or no pipeline result: keep SWAN CURVE-based values.
            entry["bestPeakFaceHeight"] = None
            entry["spotAverageFaceHeight"] = None
            entry["peelAngle"] = None
            entry["peelClassification"] = None
            entry["peelDirection"] = None
            entry["transectCount"] = len(_spot_transects) if _spot_transects else 0
            entry["openTransectCount"] = 0
            entry["degraded"] = True

        # T2.2: SurfBeat IG fields — carry-forward from nearest cadence hour.
        # When SurfBeat is disabled, no profiles available, or the strip for the
        # most recent cadence hour failed, all three fields are null (never omitted).
        if spot_config.surfbeat_enabled and _surfbeat_by_hour and _t0_dt is not None:
            try:
                _ts_dt_sb = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
                _ts_elapsed_h = (_ts_dt_sb - _t0_dt).total_seconds() / 3600.0
            except (ValueError, TypeError):
                _ts_elapsed_h = float(ts_idx)

            # Carry-forward: largest cadence hour key <= current elapsed hour.
            _avail_sb_hrs = sorted(k for k in _surfbeat_by_hour if k <= _ts_elapsed_h)
            _nearest_sb_hr: int | None = _avail_sb_hrs[-1] if _avail_sb_hrs else None
            _sb_entry: SurfBeatResult | None = (
                _surfbeat_by_hour.get(_nearest_sb_hr)
                if _nearest_sb_hr is not None
                else None
            )
        else:
            _sb_entry = None

        if _sb_entry is not None:
            entry["setTimingMinutes"] = _sb_entry.set_timing_minutes
            entry["setAmplitudeM"] = (
                _convert_unit(_sb_entry.set_amplitude_m, "meter", wave_height_internal)
                if _sb_entry.set_amplitude_m is not None else None
            )
            entry["igWaveHeightM"] = (
                _convert_unit(_sb_entry.hs_ig_shoreline, "meter", wave_height_internal)
                if _sb_entry.hs_ig_shoreline else None
            )
        else:
            entry["setTimingMinutes"] = None
            entry["setAmplitudeM"] = None
            entry["igWaveHeightM"] = None

        forecast_entries.append(entry)

    # --- CO-OPS tide predictions (informational overlay, not scored) ---
    tide_predictions: list[dict] = []
    try:
        if location.coops_station_ids:
            from weewx_clearskies_api.providers.tides import coops  # noqa: PLC0415

            coops_result = coops.fetch(
                station_id=location.coops_station_ids[0], products=("predictions",)
            )
            tide_predictions = [p.model_dump() for p in coops_result.get("predictions", [])]
    except Exception:
        logger.warning(
            "surf endpoint: CO-OPS fetch failed for %s", location_id, exc_info=True
        )

    # --- NWS Surf Zone Forecast (county-zone rip current / surf height / UV) ---
    zone_forecast: dict | None = None
    try:
        from weewx_clearskies_api.providers.marine import nws_srf  # noqa: PLC0415

        srf_result = nws_srf.fetch(
            lat=location.lat,
            lon=location.lon,
            county_zone=getattr(location, "nws_srf_zone_id", None),
            wfo_override=getattr(location, "nws_srf_wfo", None),
        )
        srf_forecasts = srf_result.get("forecasts") or []
        if srf_forecasts:
            zone_forecast = srf_forecasts[0].model_dump()
    except Exception:
        logger.warning(
            "surf endpoint: NWS SRF fetch failed for %s", location_id, exc_info=True
        )

    # --- Water temperature: ocean data resolver (API-MANUAL §18) ---
    # Primary: OFS regional model → regional ERDDAP → RTOFS/MUR SST (global).
    # Last-resort fallback: NWS SRF text product (stale hand-typed value).
    water_temp: float | None = None
    try:
        from weewx_clearskies_api.services.ocean_data_resolver import (  # noqa: PLC0415
            resolve as resolve_ocean,
        )

        ocean = resolve_ocean(
            lat=location.lat,
            lon=location.lon,
            location_config={
                "ofs_model": getattr(location, "ofs_model", None),
                "ofs_fallback": getattr(location, "ofs_fallback", None),
                "ofs_region": getattr(location, "ofs_region", None),
            },
            needs="surface",
        )
        if ocean.surface_temp is not None:
            water_temp = ocean.surface_temp
    except Exception:
        logger.warning(
            "surf endpoint: ocean data resolver failed for %s", location_id, exc_info=True
        )

    if water_temp is None and zone_forecast is not None:
        water_temp = zone_forecast.get("waterTemp")

    if water_temp is not None:
        target_temp_unit = get_group_unit(
            "group_temperature", "degree_F" if get_target_unit() == "US" else "degree_C"
        )
        water_temp = _convert_unit(water_temp, "degree_C", target_temp_unit)

    # --- Unit conversion for spectral components (group_wave_height) ---
    for component in spectral_components:
        component["height"] = _convert_unit(component["height"], "meter", wave_height_internal)

    now_str = utc_isoformat(datetime.now(tz=UTC))
    bundle: dict = {
        "locationId": location.id,
        "locationName": location.name,
        "coordinates": {"lat": location.lat, "lon": location.lon},
        # SWAN metadata (API-MANUAL §17)
        "nearshoreModel": "SWAN + SwellTrack",
        "lastRunTime": last_run_time,
        "dataAge": data_age_seconds,
        "breakerFormula": spot_config.breaker_formula,
        "surfHeightDisplay": spot_config.surf_height_display,
        "forecast": forecast_entries,
        "zoneForecast": zone_forecast,
        "waterTemp": water_temp,
        "spectralComponents": spectral_components,
        "tidePredictions": tide_predictions,
        "source": "swan+ndbc+coops+nws_srf",
        "generatedAt": now_str,
    }

    # Include error note when no SWAN data is available.
    if surf_forecast_error is not None:
        bundle["surfForecastError"] = surf_forecast_error

    return {
        "data": bundle,
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("surf").model_dump(by_alias=True),
        "units": _units_block(),
        "generatedAt": now_str,
    }
