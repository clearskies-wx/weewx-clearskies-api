"""Weather text enrichment for GET /api/v1/current.

Composes the weatherText string by combining smoothed sensor readings with
the sky condition classifier, then injects it into the /current response.
Also derives the WMO weatherCode from the composed conditions.
"""

import logging

import weewx_clearskies_api.sse.sky_condition as _sky_module
from weewx_clearskies_api.sse.conditions_text import _precip_label, build_weather_text
from weewx_clearskies_api.sse.enrichment.input_smoother import get_smoothed
from weewx_clearskies_api.sse.haze_condition import detect_haze
from weewx_clearskies_api.sse.sky_condition import classify as sky_classify

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cloud_pct_to_sky(pct: float | None, *, is_day: bool = False) -> str | None:
    """Map cloud cover percentage to a sky-condition string.

    Used as the night/startup fallback when the solar classifier is unavailable.
    Cannot produce "Scattered Clouds" composites (no Kv from cloud percentage).

    Thresholds (WMO / NWS okta-based approximation):
      ≤ 10 % → "Clear" / "Sunny" (day)
      ≤ 25 % → "Mostly Clear" / "Mostly Sunny" (day)
      ≤ 50 % → "Partly Cloudy"
      ≤ 85 % → "Mostly Cloudy"
      ≤ 95 % → "Cloudy"
      > 95 % → "Overcast"

    Returns None when pct is None or not a numeric type.
    """
    if pct is None or not isinstance(pct, (int, float)):
        return None
    if pct <= 10:
        return "Sunny" if is_day else "Clear"
    if pct <= 25:
        return "Mostly Sunny" if is_day else "Mostly Clear"
    if pct <= 50:
        return "Partly Cloudy"
    if pct <= 85:
        return "Mostly Cloudy"
    if pct <= 95:
        return "Cloudy"
    return "Overcast"


def _derive_weather_code(
    *,
    effective_sky: str | None,
    rain_label: str | None,
    is_foggy: bool,
    is_hazy: bool = False,
) -> int:
    """Map current conditions state to a WMO present-weather code (subset).

    Priority order: precipitation > fog > haze > sky.

    Snow codes (WMO group 7x):
      "Heavy Snow"     → 75
      "Moderate Snow"  → 73
      "Light Snow"/"Snow" → 71

    Frozen precipitation:
      "Freezing Rain"  → 66
      "Sleet"          → 79  (ice pellets NOS)
      "Hail"           → 96  (thunderstorm with hail)

    Rain codes (WMO group 6x):
      "Heavy Rain"     → 65
      "Moderate Rain"  → 63
      "Light Rain"     → 61

    Fog code (WMO 45):
      is_foggy=True    → 45

    Haze code (WMO 05):
      is_hazy=True     → 5

    Sky codes (WMO okta-based cloudiness):
      "Heavy Overcast" / "Overcast"                               → 4
      "Cloudy" / "Mostly Cloudy"                                  → 3
      "Partly Cloudy"                                             → 2
      "Mostly Clear" / "Mostly Sunny" + Scattered Clouds variants → 1
      "Clear" / "Sunny" + Scattered Clouds variants / None        → 0

    Returns an int WMO code.
    """
    # Snow
    if rain_label == "Heavy Snow":
        return 75
    if rain_label == "Moderate Snow":
        return 73
    if rain_label in ("Light Snow", "Snow"):
        return 71
    # Frozen precipitation
    if rain_label == "Freezing Rain":
        return 66
    if rain_label == "Sleet":
        return 79
    if rain_label == "Hail":
        return 96
    # Rain
    if rain_label == "Heavy Rain":
        return 65
    if rain_label == "Moderate Rain":
        return 63
    if rain_label == "Light Rain":
        return 61
    if is_foggy:
        return 45
    if is_hazy:
        return 5
    if effective_sky in ("Heavy Overcast", "Overcast"):
        return 4
    if effective_sky in ("Cloudy", "Mostly Cloudy"):
        return 3
    if effective_sky == "Partly Cloudy":
        return 2
    if effective_sky is not None and (
        "Mostly Clear" in effective_sky
        or "Mostly Sunny" in effective_sky
    ):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compose_weather_text(obs_data: dict | None = None) -> str:  # type: ignore[type-arg]
    """Build the weatherText string from current smoothed values.

    Reads all smoothed values from the input_smoother ring buffers and the
    current sky classification from the 30-minute solar analysis window, then
    delegates to build_weather_text().

    All smoothed values are in US units (°F, mph, in/hr) — the weewx default
    internal unit system.  build_weather_text() handles threshold comparisons.

    Args:
        obs_data: Optional observation data dict (``data["data"]`` from the
                  /current response envelope).  Used to extract provider-
                  supplied cloud cover (``cloudcover`` field).

    Returns:
        Composed conditions text (e.g. "Warm and Humid, Partly Cloudy"), or
        "" when no components are available.
    """
    # Derive provider_sky from cloud cover percentage (provider-agnostic field).
    # cloudcover may be a raw number OR a ConvertedValue dict {value, label, formatted}
    # after unit conversion runs before enrichments.
    _cloud_raw = obs_data.get("cloudcover") if obs_data else None
    _cloud_pct = (
        _cloud_raw.get("value") if isinstance(_cloud_raw, dict) else _cloud_raw
    )
    _is_day = _sky_module.is_daytime()
    _provider_sky = (
        _cloud_pct_to_sky(_cloud_pct, is_day=_is_day)
        if isinstance(_cloud_pct, (int, float))
        else None
    )

    # Fog override: when outTemp − dewpoint ≤ 1 °F the air is near-saturated;
    # replace any cloud-cover-derived sky label with "Foggy".
    _out_temp = get_smoothed("outTemp")
    _dewpoint = get_smoothed("dewpoint")
    if (
        _out_temp is not None
        and _dewpoint is not None
        and (_out_temp - _dewpoint) <= 1.0
    ):
        _provider_sky = "Foggy"

    # Haze detection: two-channel confirmation (Kcs deficit + PM).
    # detect_haze() returns 'Hazy' when both channels fire and temporal
    # coherence is satisfied, None otherwise.
    _haze_label = detect_haze(
        kcs=_sky_module.get_current_kcs(),
        solar_elevation=_sky_module.get_solar_elevation(),
        sky_label=sky_classify(),
        pm25=get_smoothed("pollutantPM25"),
        pm10=get_smoothed("pollutantPM10"),
        out_temp=_out_temp,
        dewpoint=_dewpoint,
        rain_rate=get_smoothed("rainRate"),
    )

    _precip_type = obs_data.get("precipType") if obs_data else None
    _snow_rate = get_smoothed("snowRate")

    return build_weather_text(
        sky=sky_classify(),
        rain_rate=get_smoothed("rainRate"),
        rain_rate_unit="inch_per_hour",
        wind_speed=get_smoothed("windSpeed"),
        wind_speed_unit="mile_per_hour",
        wind_gust=get_smoothed("windGust"),
        wind_gust_unit="mile_per_hour",
        app_temp=get_smoothed("appTemp"),
        dewpoint=get_smoothed("dewpoint"),
        out_temp=get_smoothed("outTemp"),
        heatindex=get_smoothed("heatindex"),
        windchill=get_smoothed("windchill"),
        temp_unit="degree_F",
        dewpoint_unit="degree_F",
        provider_sky=_provider_sky,
        precip_type=_precip_type,
        snow_rate=_snow_rate,
        pm25=get_smoothed("pollutantPM25"),
        pm10=get_smoothed("pollutantPM10"),
        haze_label=_haze_label,
    )


def enrich_weather_text(data: dict) -> dict:  # type: ignore[type-arg]
    """Inject ``weatherText`` and ``weatherCode`` into a /current response envelope.

    Calls compose_weather_text() for the composed string and writes it
    into the observation sub-dict (``data["data"]["weatherText"]``) so
    weatherText is co-located with all other observation fields rather than
    floating at the envelope top level.

    Also derives and writes ``weatherCode`` (WMO present-weather integer) into
    ``data["data"]["weatherCode"]``.

    Placement logic:
    - When ``data["data"]`` is a dict, writes into that sub-dict.
    - Otherwise falls back to writing at the top level of *data* (e.g. when
      the upstream API returned a non-standard shape).

    Never raises: exceptions are caught, logged, and the key is set to None.
    """
    try:
        obs = data.get("data")
        obs_data = obs if isinstance(obs, dict) else None

        text = compose_weather_text(obs_data)
        value = text or None

        if isinstance(obs, dict):
            obs["weatherText"] = value
        else:
            data["weatherText"] = value

        # Derive weatherCode from the same inputs used for weatherText.
        _cloud_raw2 = obs_data.get("cloudcover") if obs_data else None
        _cloud_pct = (
            _cloud_raw2.get("value") if isinstance(_cloud_raw2, dict) else _cloud_raw2
        )
        _is_day_code = _sky_module.is_daytime()
        _provider_sky: str | None = (
            _cloud_pct_to_sky(_cloud_pct, is_day=_is_day_code)
            if isinstance(_cloud_pct, (int, float))
            else None
        )
        _out_temp = get_smoothed("outTemp")
        _dewpoint = get_smoothed("dewpoint")
        if (
            _out_temp is not None
            and _dewpoint is not None
            and (_out_temp - _dewpoint) <= 1.0
        ):
            _provider_sky = "Foggy"

        # Determine effective sky the same way build_weather_text() does:
        # solar classifier during daytime, provider_sky at night / on startup.
        _sky_from_solar = sky_classify()
        if _sky_from_solar is not None and _sky_module.is_daytime():
            _effective_sky = _sky_from_solar
        else:
            _effective_sky = _provider_sky

        _precip_type2 = obs_data.get("precipType") if obs_data else None
        _snow_rate_raw = obs_data.get("snowRate") if obs_data else None
        _snow_rate2 = (
            _snow_rate_raw.get("value") if isinstance(_snow_rate_raw, dict) else _snow_rate_raw
        )
        _rain_label = _precip_label(
            get_smoothed("rainRate"), "inch_per_hour",
            precip_type=_precip_type2, snow_rate=_snow_rate2,
        )

        # Re-run haze detection with the same inputs used in compose_weather_text().
        # detect_haze() is stateful (updates _haze_history); calling it twice in
        # rapid succession is safe because the temporal coherence filter is idempotent
        # on the same timestamp — the second call just reads the same window.
        _haze_label_code = detect_haze(
            kcs=_sky_module.get_current_kcs(),
            solar_elevation=_sky_module.get_solar_elevation(),
            sky_label=sky_classify(),
            pm25=get_smoothed("pollutantPM25"),
            pm10=get_smoothed("pollutantPM10"),
            out_temp=_out_temp,
            dewpoint=_dewpoint,
            rain_rate=get_smoothed("rainRate"),
        )

        weather_code = _derive_weather_code(
            effective_sky=_effective_sky,
            rain_label=_rain_label,
            is_foggy=(_provider_sky == "Foggy"),
            is_hazy=(_haze_label_code is not None),
        )

        if isinstance(obs, dict):
            obs["weatherCode"] = weather_code
        else:
            data["weatherCode"] = weather_code

    except Exception:  # noqa: BLE001
        logger.exception("weather_text enrichment failed")
        obs = data.get("data")
        if isinstance(obs, dict):
            obs.setdefault("weatherText", None)
            obs.setdefault("weatherCode", None)
        else:
            data.setdefault("weatherText", None)
            data.setdefault("weatherCode", None)
    return data
