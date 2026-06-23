"""Structured observation model (ADR-070 T7.1).

METAR-like intermediate representation populated from the enrichment pipeline
on each observation cycle, before text generation.  All fields are nullable.

Maps local sensor data (weewx loop packets, smoothed via input_smoother) to
WMO/METAR fields with CAELUS-to-okta conversion.

Module provides a single public function: build_observation().
"""

from __future__ import annotations

import dataclasses
import logging

import weewx_clearskies_api.sse.sky_condition as _sky_module
from weewx_clearskies_api.sse.conditions_text import _precip_label
from weewx_clearskies_api.sse.enrichment.input_smoother import get_smoothed
from weewx_clearskies_api.sse.fog_condition import detect_fog_mist
from weewx_clearskies_api.sse.haze_condition import detect_haze
from weewx_clearskies_api.sse.sky_condition import classify as sky_classify

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CAELUS display-label → METAR sky code and representative okta value
#
# classify() returns display string labels (not enum names).  Day-variant
# labels ("Sunny", "Mostly Sunny") are NOT produced by classify() directly —
# classify() returns the night/neutral form.  Daytime display variants are
# applied by conditions_text._to_display_label().  We map both forms here so
# the observation model is robust if labels ever flow in from another source.
#
# Representative okta values use the midpoint of each range:
#   CLR (0):   0
#   FEW (1-2): 1   (lower bound — sparse cloud, lean light)
#   SCT (3-4): 3   (lower bound — "scattered" threshold)
#   BKN (5-7): 6   (midpoint)
#   OVC (8):   8
# ---------------------------------------------------------------------------

_SKY_LABEL_TO_METAR: dict[str, tuple[str, int]] = {
    # CLR — cloudless
    "Clear":                         ("CLR", 0),
    "Sunny":                         ("CLR", 0),
    # FEW — thin clouds / light scattered
    "Mostly Clear":                  ("FEW", 1),
    "Mostly Sunny":                  ("FEW", 1),
    # SCT — scattered / partly cloudy
    "Partly Cloudy":                 ("SCT", 3),
    # BKN — mostly cloudy / cloudy
    "Mostly Cloudy":                 ("BKN", 6),
    "Cloudy":                        ("BKN", 6),
    # OVC — overcast
    "Overcast":                      ("OVC", 8),
    "Heavy Overcast":                ("OVC", 8),
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class Observation:
    """METAR-like structured snapshot of current conditions.

    Populated by build_observation() from the enrichment pipeline.
    All fields are nullable.  pressure_trend is always None here —
    it is populated downstream by the barometer_trend enrichment.
    """

    # Temperature group
    temperature: float | None = None          # °F (outTemp smoothed)
    dewpoint: float | None = None             # °F (dewpoint smoothed)

    # Wind group — all in US units (mph, degrees)
    wind_speed: float | None = None           # mph (windSpeed smoothed)
    wind_direction: float | None = None       # degrees (windDir from obs_data; not in smoother)
    wind_gust: float | None = None            # mph (windGust smoothed)

    # Sky condition — METAR code and supporting detail
    sky_condition: str | None = None          # CLR / FEW / SCT / BKN / OVC
    sky_label: str | None = None              # Original CAELUS display label (for text gen)
    oktas: int | None = None                  # 0–8

    # Present weather — raw detection results (list of active phenomenon strings).
    # Priority ordering (precipitation > fog > mist > haze) is enforced
    # downstream in _derive_weather_code(), not here.  This is a factual
    # snapshot of what each detector reported.
    present_weather: list[str] | None = None  # e.g. ["HZ"], ["FG"], ["RA"]

    # Precipitation detail
    precipitation_label: str | None = None   # "Light Rain", "Heavy Snow", etc.

    # Pressure group
    pressure: float | None = None             # inHg (barometer smoothed)
    pressure_trend: str | None = None         # Rising/Falling/Steady — populated downstream

    # Daytime flag
    is_daytime: bool = True

    # Rain rate
    rain_rate: float | None = None            # in/hr (rainRate smoothed)

    # Haze/fog/mist raw detection flags
    haze_detected: bool = False               # True when detect_haze() returns 'Hazy'
    fog_mist_state: str | None = None         # "Foggy", "Misty", or None

    # Provider cloud cover percentage (blended from forecast provider)
    cloud_cover_pct: float | None = None      # 0–100 %


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_observation(obs_data: dict | None = None) -> Observation:  # type: ignore[type-arg]
    """Build a structured Observation from current enrichment pipeline state.

    Reads smoothed sensor values from input_smoother, classifies sky condition
    from the 30-minute solar analysis window, and runs haze/fog detection.

    This function is a pure factory: it has no side effects, does not mutate
    module-level state, and does not log above DEBUG level.  All detection
    modules (detect_haze, detect_fog_mist) do update their own temporal
    coherence histories as a side effect of detection — that is inherent to
    their stateful design and is not changed here.

    Args:
        obs_data: Optional observation data dict (``data["data"]`` from the
                  /current response envelope).  Used to extract provider-
                  supplied cloud cover (``cloudcover`` field) and precipitation
                  metadata (``precipType``, ``snowRate``).

    Returns:
        Observation instance with all available fields populated.
        Fields with no data remain None (or their declared default).
    """
    obs = Observation()

    # ------------------------------------------------------------------
    # Temperature / dewpoint
    # ------------------------------------------------------------------
    obs.temperature = get_smoothed("outTemp")
    obs.dewpoint = get_smoothed("dewpoint")

    # ------------------------------------------------------------------
    # Wind group
    # windDir is not tracked in input_smoother buffers.  Pull from obs_data
    # (last archive record) as a best-available value.  The field may arrive
    # as a raw numeric or as a ConvertedValue dict {value, label, formatted}.
    # ------------------------------------------------------------------
    obs.wind_speed = get_smoothed("windSpeed")
    _wind_dir_raw = obs_data.get("windDir") if obs_data else None
    _wind_dir: float | None = (
        _wind_dir_raw.get("value") if isinstance(_wind_dir_raw, dict) else _wind_dir_raw
    )
    obs.wind_direction = float(_wind_dir) if isinstance(_wind_dir, (int, float)) else None
    obs.wind_gust = get_smoothed("windGust")

    # ------------------------------------------------------------------
    # Rain rate
    # ------------------------------------------------------------------
    obs.rain_rate = get_smoothed("rainRate")

    # ------------------------------------------------------------------
    # Daytime flag
    # ------------------------------------------------------------------
    obs.is_daytime = _sky_module.is_daytime()

    # ------------------------------------------------------------------
    # Sky condition: classify() → METAR code + okta
    # ------------------------------------------------------------------
    sky_label = sky_classify()
    obs.sky_label = sky_label
    if sky_label is not None:
        entry = _SKY_LABEL_TO_METAR.get(sky_label)
        if entry is not None:
            obs.sky_condition, obs.oktas = entry
        else:
            # Unknown label — log at debug and leave sky_condition/oktas as None.
            logger.debug(
                "observation_model: unrecognised sky label %r; sky_condition set to None",
                sky_label,
            )

    # ------------------------------------------------------------------
    # Provider cloud cover percentage
    # Handles both raw number and ConvertedValue dict (after unit conversion
    # runs before enrichments in observations.py).
    # ------------------------------------------------------------------
    _cloud_raw = obs_data.get("cloudcover") if obs_data else None
    _cloud_pct: float | None = (
        _cloud_raw.get("value") if isinstance(_cloud_raw, dict) else _cloud_raw
    )
    if isinstance(_cloud_pct, (int, float)):
        obs.cloud_cover_pct = float(_cloud_pct)

    # ------------------------------------------------------------------
    # Pressure (current value only — trend populated downstream)
    # ------------------------------------------------------------------
    obs.pressure = get_smoothed("barometer")

    # ------------------------------------------------------------------
    # Fog/mist detection (ADR-069)
    # detect_fog_mist() returns 'Foggy', 'Misty', 'Hazy', or None.
    # 'Hazy' from fog_condition means PM2.5 disambiguation (near-saturated
    # air with elevated particulates) — merge into haze pathway.
    # ------------------------------------------------------------------
    _fog_mist_result = detect_fog_mist(
        out_temp=obs.temperature,
        dewpoint=obs.dewpoint,
        wind_speed=obs.wind_speed,
        rain_rate=obs.rain_rate,
        kcs=_sky_module.get_current_kcs(),
        is_daytime=obs.is_daytime,
        pm25=get_smoothed("pollutantPM25"),
    )

    if _fog_mist_result == "Hazy":
        # PM disambiguation: treat as haze, not fog/mist.
        obs.fog_mist_state = None
        _fog_is_hazy = True
    else:
        obs.fog_mist_state = _fog_mist_result  # "Foggy", "Misty", or None
        _fog_is_hazy = False

    # Provider cross-check for fog/mist: require the provider's
    # visibility-equipped station to corroborate before labeling fog.
    # Mirrors the cross-check in weather_text.py compose/enrich functions.
    if obs.fog_mist_state in ("Foggy", "Misty"):
        from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
            get_provider_weather_text as _get_pwt_obs,
        )
        _pw_text_obs, _pw_age_obs = _get_pwt_obs()
        if _pw_text_obs is not None:
            _pw_lower_obs = _pw_text_obs.lower()
            if not any(kw in _pw_lower_obs for kw in ("fog", "mist")):
                obs.fog_mist_state = None

    # ------------------------------------------------------------------
    # Haze detection (ADR-067)
    # detect_haze() returns 'Hazy' or None.
    # ------------------------------------------------------------------
    _haze_result = detect_haze(
        kcs=_sky_module.get_current_kcs(),
        solar_elevation=_sky_module.get_solar_elevation(),
        sky_label=sky_label,
        pm25=get_smoothed("pollutantPM25"),
        pm10=get_smoothed("pollutantPM10"),
        out_temp=obs.temperature,
        dewpoint=obs.dewpoint,
        rain_rate=obs.rain_rate,
    )

    obs.haze_detected = (_haze_result is not None) or _fog_is_hazy

    # ------------------------------------------------------------------
    # Precipitation label
    # ------------------------------------------------------------------
    _precip_type = obs_data.get("precipType") if obs_data else None
    _snow_rate_raw = obs_data.get("snowRate") if obs_data else None
    _snow_rate: float | None = (
        _snow_rate_raw.get("value") if isinstance(_snow_rate_raw, dict) else _snow_rate_raw
    )

    obs.precipitation_label = _precip_label(
        obs.rain_rate,
        "inch_per_hour",
        precip_type=_precip_type,
        snow_rate=_snow_rate,
    )

    # ------------------------------------------------------------------
    # Present weather list — raw detection snapshot.
    # Priority ordering is enforced downstream; this records what fired.
    # ------------------------------------------------------------------
    pw: list[str] = []
    if obs.precipitation_label is not None:
        # Map precipitation label to METAR/WMO present-weather codes.
        _pl = obs.precipitation_label
        if "Snow" in _pl:
            pw.append("SN")
        elif "Freezing Rain" in _pl:
            pw.append("FZRA")
        elif "Sleet" in _pl:
            pw.append("PL")   # ice pellets
        elif "Hail" in _pl:
            pw.append("GR")
        elif "Rain" in _pl:
            pw.append("RA")

    if obs.fog_mist_state == "Foggy":
        pw.append("FG")
    elif obs.fog_mist_state == "Misty":
        pw.append("BR")

    if obs.haze_detected:
        pw.append("HZ")

    obs.present_weather = pw if pw else None

    logger.debug(
        "observation_model: built Observation sky=%r code=%r oktas=%r haze=%r fog=%r precip=%r",
        obs.sky_label,
        obs.sky_condition,
        obs.oktas,
        obs.haze_detected,
        obs.fog_mist_state,
        obs.precipitation_label,
    )

    return obs
