"""Current conditions text composer (ADR-044).

Assembles weatherText from temperature-comfort, sky condition, wind (Beaufort),
and precipitation components.  Each component is independently nullable;
absent components are dropped from the composed string.

Component order (ADR-044 §9 amendment, 2026-05-28):
  [temperature-comfort, sky, wind, precipitation]

Composition uses a "with" connector for the final part to prevent double-"and"
when the temperature-comfort label is compound (e.g. "Warm and Humid").

Module-level state is held only in sky_condition and temperature_comfort —
this module is stateless.

I18N (T3.4): precipitation labels and the compose() connectors/separator
resolve from the locale file via i18n.t(). ``sky`` and ``provider_sky``
inputs arrive already translated (sky_condition.classify() translates
internally per T3.4; provider text is source text as-is) — the day/night
vocabulary swap in ``_to_display_label`` looks up the locale's clear/sunny
text via t() rather than hardcoded English literals, so the substitution
stays correct for English today. True key-based (pre-translation) day/night
selection would require reworking sky_condition's public contract and the
provider_sky text path together; deferred to Phase 6 when non-English
locale files are actually populated (currently all fall back to English,
so behavior is unchanged either way).
"""

from __future__ import annotations

from . import sky_condition as _sky_condition_module
from . import temperature_comfort as _temperature_comfort
from weewx_clearskies_api import i18n
from weewx_clearskies_api.units.conversion import convert
from weewx_clearskies_api.units.derived import beaufort


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _precip_label(
    rain_rate: float | None,
    source_unit: str,
    *,
    precip_type: str | None = None,
    snow_rate: float | None = None,
    locale: str | None = None,
) -> str | None:
    """Classify precipitation from rain rate and provider precipitation type.

    Provider precipType is authoritative for WHAT is falling.  The local
    tipping-bucket gauge is authoritative for HOW MUCH — but only when it
    can measure it.  Gauges cannot measure snow (accumulates in funnel
    without tipping), and sleet/hail bounce out.  So:

    - snow:          gauge rate for intensity if > 0, else provider snowRate,
                     else bare "Snow" label (provider confirms falling)
    - freezing-rain: always "Freezing Rain" (trust provider)
    - sleet:         always "Sleet" (trust provider)
    - hail:          always "Hail" (trust provider)
    - rain / None:   existing logic — None when rainRate ≤ 0

    Thresholds (water-equivalent in/hr, AMS/WMO):
      < 0.10 → Light
      0.10–0.30 → Moderate
      > 0.30 → Heavy

    Labels resolve from the locale file (I18N T3.4) via
    i18n.t("precipitation.<key>", locale).
    """
    pt = (precip_type or "").strip().lower()

    # --- Non-rain types: trust provider even when gauge reads zero ----------
    if pt == "freezing-rain":
        return i18n.t("precipitation.freezing_rain", locale)
    if pt == "sleet":
        return i18n.t("precipitation.sleet", locale)
    if pt == "hail":
        return i18n.t("precipitation.hail", locale)

    if pt == "snow":
        rate = _to_inhr(rain_rate, source_unit)
        if rate is not None and rate > 0:
            return i18n.t(f"precipitation.{_intensity('snow', rate)}", locale)
        sr = _to_inhr(snow_rate, source_unit)
        if sr is not None and sr > 0:
            return i18n.t(f"precipitation.{_intensity('snow', sr)}", locale)
        return i18n.t("precipitation.snow", locale)

    # --- Rain (or precipType unknown/absent): existing behavior -------------
    if rain_rate is None or rain_rate <= 0:
        return None
    rate = _to_inhr(rain_rate, source_unit)
    if rate is None:
        return None
    return i18n.t(f"precipitation.{_intensity('rain', rate)}", locale)


def _to_inhr(rate: float | None, source_unit: str) -> float | None:
    """Convert a rate to in/hr, returning None on failure."""
    if rate is None or rate <= 0:
        return None
    if source_unit == "inch_per_hour":
        return rate
    return convert(rate, source_unit, "inch_per_hour")


def _intensity(noun_key: str, rate_inhr: float) -> str:
    """Apply AMS/WMO intensity tiers to a precipitation noun key.

    Returns a compound locale key (e.g. "light_rain", "heavy_snow") — I18N
    T3.4. *noun_key* is "rain" or "snow".
    """
    if rate_inhr < 0.10:
        return f"light_{noun_key}"
    if rate_inhr < 0.30:
        return f"moderate_{noun_key}"
    return f"heavy_{noun_key}"


def _compose(parts: list[str | None], locale: str | None = None) -> str:
    """Join non-empty parts into a natural-language string (ADR-044 §9 amendment).

    Rules:
      1 part  → "{a}"
      2 parts → "{a}{separator}{connector} {b}"
      3+ parts → "{a}{separator}{b}{separator}...{separator}{connector} {last}"

    Separator and connectors resolve from the locale file (I18N T3.4) via
    ``composition.separator``, ``composition.connector_and``, and
    ``composition.connector_with``. The connector is connector_and when the
    last part equals the locale's Beaufort-0 ("Calm") text (saying "with
    Calm" is unnatural), and connector_with otherwise — connector_with
    prevents double-"and" when the first part is a compound temperature-
    comfort label like "Warm and Humid".
    """
    filtered = [p for p in parts if p]
    if not filtered:
        return ""
    if len(filtered) == 1:
        return filtered[0]

    separator = i18n.t("composition.separator", locale)
    connector_and = i18n.t("composition.connector_and", locale)
    connector_with = i18n.t("composition.connector_with", locale)
    calm_text = i18n.t("beaufort.0", locale)

    connector = connector_and if filtered[-1] == calm_text else connector_with
    if len(filtered) == 2:
        return f"{filtered[0]}{separator}{connector} {filtered[1]}"
    return separator.join(filtered[:-1]) + f"{separator}{connector} {filtered[-1]}"


def _to_fahrenheit(value: float | None, source_unit: str) -> float | None:
    """Convert *value* from *source_unit* to °F.  Returns None when value is None."""
    if value is None:
        return None
    if source_unit == "degree_F":
        return value
    return convert(value, source_unit, "degree_F")


def _to_display_label(
    sky_label: str | None,
    is_daytime: bool,
    locale: str | None = None,
) -> str | None:
    """Map sky classification to day/night display vocabulary.

    Day: Clear→Sunny, Mostly Clear→Mostly Sunny — text resolved from the
    locale file (I18N T3.4) rather than hardcoded English literals.
    Night: labels pass through unchanged.
    """
    if sky_label is None:
        return None
    if is_daytime:
        mostly_clear = i18n.t("sky.mostly_clear", locale)
        mostly_sunny = i18n.t("sky.mostly_sunny", locale)
        clear = i18n.t("sky.clear", locale)
        sunny = i18n.t("sky.sunny", locale)
        label = sky_label.replace(mostly_clear, mostly_sunny)
        return label.replace(clear, sunny)
    return sky_label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_weather_text(
    *,
    sky: str | None = None,
    rain_rate: float | None = None,
    rain_rate_unit: str = "inch_per_hour",
    wind_speed: float | None = None,
    wind_speed_unit: str = "mile_per_hour",
    wind_gust: float | None = None,
    wind_gust_unit: str = "mile_per_hour",
    app_temp: float | None = None,
    dewpoint: float | None = None,
    out_temp: float | None = None,
    heatindex: float | None = None,
    windchill: float | None = None,
    temp_unit: str = "degree_F",
    dewpoint_unit: str = "degree_F",
    provider_sky: str | None = None,
    precip_type: str | None = None,
    snow_rate: float | None = None,
    pm25: float | None = None,
    pm10: float | None = None,
    haze_label: str | None = None,
    fog_mist_label: str | None = None,
    locale: str | None = None,
) -> str:
    """Build the full weatherText string (ADR-044 §9 amendment, 2026-05-28).

    Components are assembled in priority order:
      [temperature-comfort, sky, wind, precipitation]

    Null/absent components are dropped.  All Beaufort values including
    Beaufort 0 ("Calm") are included — calm is a real condition
    (ADR-044 §4, amended 2026-06-05).

    The "and Gusty" qualifier is appended to the Beaufort label when:
      windGust ≥ windSpeed + 12 mph AND windGust ≥ 18 mph  (ADR-044 §4)
    Both thresholds are evaluated in mph regardless of source_unit.
    The gusty check only fires when wind_speed is non-Calm (Beaufort > 0) —
    "Calm and Gusty" is nonsensical.

    Smoothed inputs from ``enrichment.input_smoother`` should be passed by
    the caller when available; this function performs no smoothing itself.

    Args:
        sky:             Sky condition from sky_condition.classify() (may be
                         None if night/startup — falls back to provider_sky).
        rain_rate:       Rain rate value.
        rain_rate_unit:  Unit of rain_rate (default "inch_per_hour").
        wind_speed:      Wind speed value.
        wind_speed_unit: Unit of wind_speed (default "mile_per_hour").
        wind_gust:       Wind gust speed value.
        wind_gust_unit:  Unit of wind_gust (default "mile_per_hour").
        app_temp:        Apparent temperature (feels-like) value.
        dewpoint:        Dewpoint value.
        out_temp:        Dry-bulb temperature value.
        heatindex:       Heat index value.
        windchill:       Wind chill value.
        temp_unit:       Unit for app_temp, out_temp, heatindex, windchill
                         (default "degree_F").
        dewpoint_unit:   Unit of dewpoint (default "degree_F").
        provider_sky:    Provider weather text, used as fallback when the
                         local solar analysis produces None.
        haze_label:      'Hazy' when haze is detected (from haze_condition
                         module), or None.  Appended to the sky component
                         as a compound qualifier (terse: "Sunny, Hazy").
                         Only applied when the effective sky is a clear-sky
                         label (Clear, Sunny, Mostly Clear/Sunny, Scattered
                         Clouds, Partly Cloudy) — never to cloudy/overcast.
        fog_mist_label:  'Foggy' or 'Misty' when fog/mist is detected (from
                         fog_condition module), or None.  When set, replaces
                         the effective sky label entirely — fog and mist are
                         standalone sky conditions, not cloud-cover modifiers.
        locale:          Optional locale code (I18N T3.4). When omitted,
                         labels resolve via the i18n module's active locale
                         (defaults to English).

    Returns:
        Composed conditions text, e.g. "Warm and Humid, Partly Cloudy, with Light Rain",
        or "" when no components are available.
    """
    parts: list[str | None] = []

    # 1. Temperature-comfort (2D matrix, ADR-044 §5-7).
    # Convert all temperature inputs to °F before classifying.
    app_temp_f = _to_fahrenheit(app_temp, temp_unit)
    dewpoint_f = _to_fahrenheit(dewpoint, dewpoint_unit)
    out_temp_f = _to_fahrenheit(out_temp, temp_unit)
    heatindex_f = _to_fahrenheit(heatindex, temp_unit)
    windchill_f = _to_fahrenheit(windchill, temp_unit)

    temp_comfort_label = _temperature_comfort.classify(
        app_temp=app_temp_f,
        dewpoint=dewpoint_f,
        out_temp=out_temp_f,
        heatindex=heatindex_f,
        windchill=windchill_f,
        locale=locale,
    )
    parts.append(temp_comfort_label)

    # 2. Sky condition: use local solar classification only during daytime.
    # At night, fall back to provider sky data (ADR-044 §1b).
    is_day = _sky_condition_module.is_daytime()
    if sky is not None and is_day:
        effective_sky = sky
    else:
        effective_sky = provider_sky
    effective_sky = _to_display_label(effective_sky, is_day, locale)

    # Fog/mist override: when fog_mist_label is set ('Foggy' or 'Misty'),
    # it replaces the effective sky entirely.  Fog and mist are standalone
    # sky conditions — they are not appended to cloud-cover labels.
    # When fog_mist_label is active, the haze qualifier is also suppressed
    # (fog/mist and haze are mutually exclusive; the PM disambiguation gate
    # in fog_condition already returns 'Hazy' for the PM case).
    if fog_mist_label is not None:
        effective_sky = fog_mist_label
    else:
        # Haze qualifier: append ", Hazy" after the sky label when haze is
        # detected and the effective sky is a clear-ish label.  "Hazy and
        # Overcast" is invalid per API-MANUAL §haze detection — the
        # clear-sky-only constraint is enforced both in detect_haze() and
        # here as a belt-and-suspenders guard.
        #
        # Terse format (current default): "Sunny, Hazy"
        # Standard/verbose (future): "Sunny. Hazy." (NWS convention)
        if haze_label is not None and effective_sky is not None:
            _sky_lower = effective_sky.lower()
            _haze_eligible = (
                "clear" in _sky_lower
                or "sunny" in _sky_lower
                or "scattered clouds" in _sky_lower
                or "partly cloudy" in _sky_lower
            )
            if _haze_eligible:
                effective_sky = f"{effective_sky}, {haze_label}"

    parts.append(effective_sky)

    # 3. Wind (Beaufort label). All Beaufort values including 0 (Calm) are
    # included — calm is a real condition (ADR-044 §4, amended 2026-06-05).
    if wind_speed is not None:
        try:
            b = beaufort(wind_speed, wind_speed_unit, locale)
            wind_label = str(b["label"])

            # Gusty check — ADR-044 §4 thresholds in mph.  Only fires for
            # non-Calm wind (Beaufort > 0) — "Calm and Gusty" makes no sense.
            # "Gusty" has no locale key yet (not in the T3.4 scope) — left as
            # literal English pending a future locale-file addition.
            if b["value"] > 0 and wind_gust is not None:
                speed_mph = convert(wind_speed, wind_speed_unit, "mile_per_hour")
                gust_mph = convert(wind_gust, wind_gust_unit, "mile_per_hour")
                if (
                    speed_mph is not None
                    and gust_mph is not None
                    and gust_mph >= speed_mph + 12.0
                    and gust_mph >= 18.0
                ):
                    wind_label = wind_label + " and Gusty"

            parts.append(wind_label)
        except (ValueError, TypeError):
            pass

    # 4. Precipitation.
    parts.append(
        _precip_label(
            rain_rate, rain_rate_unit, precip_type=precip_type, snow_rate=snow_rate, locale=locale
        )
    )

    return _compose(parts, locale)
