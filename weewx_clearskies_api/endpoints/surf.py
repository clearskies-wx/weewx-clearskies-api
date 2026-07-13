"""GET /surf, GET /surf/{location_id} — surf quality forecast (API-MANUAL §18, T5.3).

Behavior:
  GET /surf (no locationId): list configured surf locations. 404 when no
    location has "surf" in its activities list.
  GET /surf/{location_id}: full surf bundle for one location. 404 when the
    location doesn't exist or doesn't have "surf" enabled.

Data flow per API-MANUAL §17/§18 and PROVIDER-MANUAL §14:
  1. NWPS nearshore wave data (providers/marine/nwps.py), corrected by the
     four wave_transform.py supplements (breaker index, structure effects,
     interpolation, topographic adjustment).
  2. Falls back to WaveWatch III offshore data (providers/marine/wavewatch.py)
     when NWPS is unavailable — matches PROVIDER-MANUAL §14.6 "No fallback
     transformation pipeline" (WaveWatch data passes through unmodified,
     wave_transform is simply not applied to it).
  3. enrichment/surf_scorer.py's score_surf() produces the SurfForecast.
  4. NDBC (providers/buoy/ndbc.py) supplies spectral swell decomposition and
     wind speed/direction (surf_scorer's wind-quality input).
  5. CO-OPS (providers/tides/coops.py) supplies tide predictions for the
     surf page's tide overlay (informational, not scored).
  6. NWS SRF (providers/marine/nws_srf.py) supplies the county-zone forecast
     (rip current risk, surf height range, UV, water temp) as zoneForecast.

Each provider section is wrapped in its own try/except — one provider
failing does not take down the whole response (graceful degradation per
task brief). Response is a plain dict following the standard envelope
(API-MANUAL §2 "Response envelope") since no SurfBundleResponse Pydantic
model exists yet in models/responses.py (out of scope for this task —
models/responses.py is not to be modified here); this mirrors the existing
pattern in endpoints/almanac.py's get_solunar().

wire_surf_config(settings) stores the parsed MarineConfig (from
config/marine_config.py's load_marine_config()) module-level. Note the
parameter is the MarineConfig instance itself, not the top-level Settings
object — marine config is not yet threaded onto Settings
(config/settings.py is out of scope for this task). Router registration in
app.py is a follow-up task (app.py is out of scope here).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.enrichment import wave_transform
from weewx_clearskies_api.enrichment.surf_scorer import score_surf
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock
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
    }


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

    wave_height_internal, _ = _wave_height_unit()

    # --- NWPS nearshore wave data + wave_transform supplements ---
    supplemented: dict | None = None
    try:
        from weewx_clearskies_api.providers.marine import nwps  # noqa: PLC0415

        nwps_result = nwps.fetch(lat=location.lat, lon=location.lon, wfo_override=location.nwps_wfo)
        nearshore = nwps_result.get("nearshore") if nwps_result else None
        if nearshore and nearshore.get("waveHeight") is not None:
            supplemented = wave_transform.apply_supplements(
                {
                    "wave_height": nearshore.get("waveHeight"),
                    "wave_period": nearshore.get("wavePeriod"),
                    "wave_direction": nearshore.get("waveDirection"),
                },
                spot_config,
                location.lat,
                location.lon,
            )
    except Exception:
        logger.warning(
            "surf endpoint: NWPS fetch/supplement failed for %s", location_id, exc_info=True
        )

    # --- WaveWatch III offshore fallback (PROVIDER-MANUAL §14.6: no
    # transformation pipeline applied to WaveWatch data when NWPS is down) ---
    if supplemented is None:
        try:
            from weewx_clearskies_api.providers.marine import wavewatch  # noqa: PLC0415

            ww_result = wavewatch.fetch(lat=location.lat, lon=location.lon)
            points = ww_result.get("forecast") or []
            if points:
                first = points[0]
                supplemented = {
                    "wave_height": first.waveHeight,
                    "wave_period": first.wavePeriod,
                    "wave_direction": first.waveDirection,
                }
        except Exception:
            logger.warning(
                "surf endpoint: WaveWatch III fallback failed for %s", location_id, exc_info=True
            )

    # --- NDBC spectral swell decomposition + wind ---
    spectral_components: list[dict] = []
    wind_speed_mps: float | None = None
    wind_direction: float | None = None
    try:
        if location.ndbc_station_ids:
            from weewx_clearskies_api.providers.buoy import ndbc  # noqa: PLC0415

            ndbc_result = ndbc.fetch(station_id=location.ndbc_station_ids[0], include_spectral=True)
            obs = ndbc_result.get("observation")
            if obs is not None:
                wind_speed_mps = obs.windSpeed
                wind_direction = obs.windDirection
            spectral_components = [c.model_dump() for c in (ndbc_result.get("spectral") or [])]
    except Exception:
        logger.warning("surf endpoint: NDBC fetch failed for %s", location_id, exc_info=True)

    # --- Score surf quality ---
    forecast_entries: list[dict] = []
    if supplemented is not None and supplemented.get("wave_height") is not None:
        surf_forecast = score_surf(
            wave_height=supplemented["wave_height"],
            wave_period=supplemented.get("wave_period") or 0.0,
            wave_direction=supplemented.get("wave_direction") or 0.0,
            wind_speed=wind_speed_mps,
            wind_direction=wind_direction,
            spectral_components=spectral_components or None,
            spot_config=spot_config,
            time_utc=utc_isoformat(datetime.now(tz=UTC)),
        )
        entry = surf_forecast.model_dump()
        entry["waveHeightAtBreak"] = _convert_unit(
            entry["waveHeightAtBreak"], "meter", wave_height_internal
        )
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
        logger.warning("surf endpoint: CO-OPS fetch failed for %s", location_id, exc_info=True)

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
        logger.warning("surf endpoint: NWS SRF fetch failed for %s", location_id, exc_info=True)

    # --- Unit conversion for spectral components (group_wave_height) ---
    for component in spectral_components:
        component["height"] = _convert_unit(component["height"], "meter", wave_height_internal)

    now_str = utc_isoformat(datetime.now(tz=UTC))
    bundle = {
        "locationId": location.id,
        "locationName": location.name,
        "coordinates": {"lat": location.lat, "lon": location.lon},
        "forecast": forecast_entries,
        "zoneForecast": zone_forecast,
        "spectralComponents": spectral_components,
        "tidePredictions": tide_predictions,
        "source": "nwps+wavewatch+ndbc+coops+nws_srf",
        "generatedAt": now_str,
    }

    return {
        "data": bundle,
        "stationClock": build_station_clock().model_dump(by_alias=True),
        "freshness": build_freshness("surf").model_dump(by_alias=True),
        "units": _units_block(),
        "generatedAt": now_str,
    }
