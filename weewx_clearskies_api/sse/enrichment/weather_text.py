"""Weather text enrichment for GET /api/v1/current.

Composes the weatherText string by combining smoothed sensor readings with
the sky condition classifier, then injects it into the /current response.
Also derives the WMO weatherCode from the composed conditions.
"""

import logging

import weewx_clearskies_api.sse.sky_condition as _sky_module
from weewx_clearskies_api.sse.conditions_text import _precip_label, build_weather_text
from weewx_clearskies_api.sse.enrichment.input_smoother import get_smoothed
from weewx_clearskies_api.sse.fog_condition import detect_fog_mist
from weewx_clearskies_api.sse.haze_condition import detect_haze
from weewx_clearskies_api.sse.observation_model import build_observation
from weewx_clearskies_api.sse.sky_condition import classify as sky_classify
from weewx_clearskies_api.sse.text_generator import generate_standard, generate_verbose

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
    fog_mist_state: str | None,
    is_hazy: bool = False,
    out_temp: float | None = None,
) -> int:
    """Map current conditions state to a WMO present-weather code (subset).

    Priority order: precipitation > fog (rime or plain) > mist > haze > sky.

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

    Rime fog code (WMO 48):
      fog_mist_state == "Foggy" AND out_temp <= 32.0 °F → 48
      (depositing rime fog: supercooled fog droplets deposit ice on surfaces)

    Fog code (WMO 45):
      fog_mist_state == "Foggy" AND (out_temp > 32.0 °F OR out_temp is None) → 45

    Mist code (WMO 10):
      fog_mist_state == "Misty" → 10

    Haze code (WMO 05):
      is_hazy=True     → 5

    Sky codes (WMO okta-based cloudiness):
      "Heavy Overcast" / "Overcast"                               → 4
      "Cloudy" / "Mostly Cloudy"                                  → 3
      "Partly Cloudy"                                             → 2
      "Mostly Clear" / "Mostly Sunny" + Scattered Clouds variants → 1
      "Clear" / "Sunny" + Scattered Clouds variants / None        → 0

    Args:
        effective_sky: Sky condition string from the solar classifier or provider.
        rain_label: Precipitation label from _precip_label().
        fog_mist_state: "Foggy", "Misty", or None from detect_fog_mist().
        is_hazy: True when haze detection has fired.
        out_temp: Outside temperature in °F (smoothed).  Used to distinguish
                  depositing rime fog (code 48, ≤ 32 °F) from plain fog (code 45).
                  When None, rime fog is not emitted and plain fog (45) is used.

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
    if fog_mist_state == "Foggy":
        if out_temp is not None and out_temp <= 32.0:
            return 48  # depositing rime fog (ADR-070)
        return 45
    if fog_mist_state == "Misty":
        return 10
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

    # Fog/mist detection: multi-parameter algorithm (ADR-069).
    # detect_fog_mist() returns 'Foggy', 'Misty', 'Hazy', or None.
    # 'Hazy' from this module means PM2.5 disambiguation (near-saturated air
    # with elevated particulates); it is merged with the haze_label pathway.
    _out_temp = get_smoothed("outTemp")
    _dewpoint = get_smoothed("dewpoint")
    _rain_rate = get_smoothed("rainRate")
    _fog_mist_result = detect_fog_mist(
        out_temp=_out_temp,
        dewpoint=_dewpoint,
        wind_speed=get_smoothed("windSpeed"),
        rain_rate=_rain_rate,
        kcs=_sky_module.get_current_kcs(),
        is_daytime=_sky_module.is_daytime(),
        pm25=get_smoothed("pollutantPM25"),
    )

    # Separate fog/mist label from PM-disambiguation hazy result.
    # When fog_condition returns 'Hazy', treat it like haze_condition's result
    # (feeds into haze_label pathway).  fog_mist_label carries only
    # 'Foggy' or 'Misty' (or None).
    if _fog_mist_result == "Hazy":
        _fog_mist_label: str | None = None
        _fog_is_hazy = True
    else:
        _fog_mist_label = _fog_mist_result
        _fog_is_hazy = False

    # Provider cross-check for fog/mist: require the provider's
    # visibility-equipped station to corroborate before labeling fog.
    # Suppresses marine-layer humidity false positives where T-Td is
    # tight but ground-level visibility is fine.
    # When provider data is stale/unavailable, local detection stands
    # (absence of provider data is not evidence of absence).
    if _fog_mist_label in ("Foggy", "Misty"):
        from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
            get_provider_weather_text as _get_pwt_xcheck,
        )
        _pw_text_xcheck, _pw_age_xcheck = _get_pwt_xcheck()
        if _pw_text_xcheck is not None:
            _pw_lower_xcheck = _pw_text_xcheck.lower()
            if not any(kw in _pw_lower_xcheck for kw in ("fog", "mist")):
                _fog_mist_label = None

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
        rain_rate=_rain_rate,
    )

    # Merge haze signals: either source may trigger "Hazy".
    _effective_haze_label: str | None = _haze_label if (_haze_label is not None) else ("Hazy" if _fog_is_hazy else None)

    # Nighttime provider deferral (API-MANUAL §8 nighttime mode, ADR-071):
    # When it's nighttime, local haze detection is inactive (el ≤ 10°).
    # Check provider weather text for haze/smoke indicators.  Fog/mist
    # continues from local detection unaffected; this block only fires when
    # no haze label has been established yet.
    if not _is_day and _effective_haze_label is None:
        from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
            get_provider_weather_text,
        )
        _pw_text, _pw_age = get_provider_weather_text()
        if _pw_text is not None:
            _pw_lower = _pw_text.lower()
            if any(kw in _pw_lower for kw in ("haze", "hazy", "smoke", "smoky")):
                _effective_haze_label = "Hazy"

    # Missing-pyranometer deferral (ADR-068 v2): daytime but no Kcs data
    # means the station lacks a pyranometer.  Local haze detection (Channel 1)
    # cannot fire without Kcs, so defer to provider weather text for
    # haze/smoke indicators — same mechanism as nighttime deferral.
    if _is_day and _effective_haze_label is None and _sky_module.get_current_kcs() is None:
        from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
            get_provider_weather_text as _get_pwt_rad,
        )
        _pw_text_rad, _pw_age_rad = _get_pwt_rad()
        if _pw_text_rad is not None:
            _pw_lower_rad = _pw_text_rad.lower()
            if any(kw in _pw_lower_rad for kw in ("haze", "hazy", "smoke", "smoky")):
                _effective_haze_label = "Hazy"

    # Missing-hygrometer deferral (ADR-068 v2): no dewpoint means local
    # fog/mist detection cannot fire (it requires dewpoint for the
    # temperature-depression algorithm).  Defer to provider weather text
    # for fog/mist indicators.
    if _fog_mist_label is None and _dewpoint is None:
        from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
            get_provider_weather_text as _get_pwt_fog,
        )
        _pw_text_fog, _pw_age_fog = _get_pwt_fog()
        if _pw_text_fog is not None:
            _pw_lower_fog = _pw_text_fog.lower()
            if "fog" in _pw_lower_fog:
                _fog_mist_label = "Foggy"
            elif "mist" in _pw_lower_fog:
                _fog_mist_label = "Misty"

    _precip_type = obs_data.get("precipType") if obs_data else None
    _snow_rate = get_smoothed("snowRate")

    return build_weather_text(
        sky=sky_classify(),
        rain_rate=_rain_rate,
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
        haze_label=_effective_haze_label,
        fog_mist_label=_fog_mist_label,
    )


def enrich_weather_text(data: dict) -> dict:  # type: ignore[type-arg]
    """Inject weather text fields and code into a /current response envelope.

    Writes into ``data["data"]``:
    - ``weatherText`` — terse (backward-compatible compound form)
    - ``weatherCode`` — WMO present-weather integer
    - ``weatherTextStandard`` — NWS one-sentence-per-component format
    - ``weatherTextVerbose`` — full narrative format

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
        _provider_sky2: str | None = (
            _cloud_pct_to_sky(_cloud_pct, is_day=_is_day_code)
            if isinstance(_cloud_pct, (int, float))
            else None
        )
        _out_temp = get_smoothed("outTemp")
        _dewpoint = get_smoothed("dewpoint")
        _rain_rate2 = get_smoothed("rainRate")

        # Fog/mist detection for weather code derivation (ADR-069).
        # detect_fog_mist() is stateful (updates _fog_history); calling it
        # twice in rapid succession is safe — the coherence filter is
        # idempotent on the same timestamp.
        _fog_mist_result2 = detect_fog_mist(
            out_temp=_out_temp,
            dewpoint=_dewpoint,
            wind_speed=get_smoothed("windSpeed"),
            rain_rate=_rain_rate2,
            kcs=_sky_module.get_current_kcs(),
            is_daytime=_sky_module.is_daytime(),
            pm25=get_smoothed("pollutantPM25"),
        )

        # Separate fog/mist from PM-disambiguation hazy result.
        if _fog_mist_result2 == "Hazy":
            _fog_mist_state2: str | None = None
            _fog_is_hazy2 = True
        else:
            _fog_mist_state2 = _fog_mist_result2
            _fog_is_hazy2 = False

        # Provider cross-check for fog/mist: require the provider's
        # visibility-equipped station to corroborate before labeling fog.
        # Suppresses marine-layer humidity false positives where T-Td is
        # tight but ground-level visibility is fine.
        # When provider data is stale/unavailable, local detection stands
        # (absence of provider data is not evidence of absence).
        if _fog_mist_state2 in ("Foggy", "Misty"):
            from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
                get_provider_weather_text as _get_pwt_xcheck2,
            )
            _pw_text_xcheck2, _pw_age_xcheck2 = _get_pwt_xcheck2()
            if _pw_text_xcheck2 is not None:
                _pw_lower_xcheck2 = _pw_text_xcheck2.lower()
                if not any(kw in _pw_lower_xcheck2 for kw in ("fog", "mist")):
                    _fog_mist_state2 = None

        # Determine effective sky the same way build_weather_text() does:
        # solar classifier during daytime, provider_sky at night / on startup.
        # When fog/mist is detected, fog_mist_label becomes the sky label in
        # build_weather_text(); reflect that here for code derivation.
        _sky_from_solar = sky_classify()
        if _sky_from_solar is not None and _sky_module.is_daytime():
            _effective_sky = _sky_from_solar
        else:
            _effective_sky = _provider_sky2

        # When fog/mist is active, the effective_sky for code purposes is the
        # fog/mist label itself (it replaces the sky component in the text).
        if _fog_mist_state2 is not None:
            _effective_sky = _fog_mist_state2

        _precip_type2 = obs_data.get("precipType") if obs_data else None
        _snow_rate_raw = obs_data.get("snowRate") if obs_data else None
        _snow_rate2 = (
            _snow_rate_raw.get("value") if isinstance(_snow_rate_raw, dict) else _snow_rate_raw
        )
        _rain_label = _precip_label(
            _rain_rate2, "inch_per_hour",
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
            rain_rate=_rain_rate2,
        )

        # Merge haze signals from both detection paths.
        _is_hazy_code = (_haze_label_code is not None) or _fog_is_hazy2

        # Nighttime provider deferral (API-MANUAL §8 nighttime mode, ADR-071):
        # Mirror the same logic used in compose_weather_text() for code derivation.
        if not _sky_module.is_daytime() and not _is_hazy_code:
            from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
                get_provider_weather_text,
            )
            _pw_text2, _pw_age2 = get_provider_weather_text()
            if _pw_text2 is not None:
                _pw_lower2 = _pw_text2.lower()
                if any(kw in _pw_lower2 for kw in ("haze", "hazy", "smoke", "smoky")):
                    _is_hazy_code = True

        # Missing-pyranometer deferral (ADR-068 v2): mirror compose_weather_text().
        if _sky_module.is_daytime() and not _is_hazy_code and _sky_module.get_current_kcs() is None:
            from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
                get_provider_weather_text as _get_pwt_rad2,
            )
            _pw_text_rad2, _pw_age_rad2 = _get_pwt_rad2()
            if _pw_text_rad2 is not None:
                _pw_lower_rad2 = _pw_text_rad2.lower()
                if any(kw in _pw_lower_rad2 for kw in ("haze", "hazy", "smoke", "smoky")):
                    _is_hazy_code = True

        # Missing-hygrometer deferral (ADR-068 v2): mirror compose_weather_text().
        # When _dewpoint is None, fog/mist detection cannot fire.  Defer to
        # provider weather text to supply fog/mist state for code derivation.
        if _fog_mist_state2 is None and _dewpoint is None:
            from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (  # noqa: PLC0415
                get_provider_weather_text as _get_pwt_fog2,
            )
            _pw_text_fog2, _pw_age_fog2 = _get_pwt_fog2()
            if _pw_text_fog2 is not None:
                _pw_lower_fog2 = _pw_text_fog2.lower()
                if "fog" in _pw_lower_fog2:
                    _fog_mist_state2 = "Foggy"
                    _effective_sky = "Foggy"
                elif "mist" in _pw_lower_fog2:
                    _fog_mist_state2 = "Misty"
                    _effective_sky = "Misty"

        weather_code = _derive_weather_code(
            effective_sky=_effective_sky,
            rain_label=_rain_label,
            fog_mist_state=_fog_mist_state2,
            is_hazy=_is_hazy_code,
            out_temp=_out_temp,
        )

        if isinstance(obs, dict):
            obs["weatherCode"] = weather_code
        else:
            data["weatherCode"] = weather_code

        # Standard and verbose text generation (ADR-070, API-MANUAL §8).
        # Build the structured observation model, then generate the two
        # additional verbosity levels.  Terse (weatherText) stays as-is above.
        _observation = build_observation(obs_data)
        _standard = generate_standard(_observation)
        _verbose = generate_verbose(_observation)

        if isinstance(obs, dict):
            obs["weatherTextStandard"] = _standard
            obs["weatherTextVerbose"] = _verbose
        else:
            data["weatherTextStandard"] = _standard
            data["weatherTextVerbose"] = _verbose

    except Exception:  # noqa: BLE001
        logger.exception("weather_text enrichment failed")
        obs = data.get("data")
        if isinstance(obs, dict):
            obs.setdefault("weatherText", None)
            obs.setdefault("weatherCode", None)
            obs.setdefault("weatherTextStandard", None)
            obs.setdefault("weatherTextVerbose", None)
        else:
            data.setdefault("weatherText", None)
            data.setdefault("weatherCode", None)
            data.setdefault("weatherTextStandard", None)
            data.setdefault("weatherTextVerbose", None)
    return data
