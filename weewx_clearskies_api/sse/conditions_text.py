"""Current conditions text composer (ADR-044, I18N T3.4).

Assembles weatherText from temperature-comfort, sky condition, wind (Beaufort),
and precipitation components.  Each component is independently nullable;
absent components are dropped from the composed string.

Composition is locale-aware with two modes:

  **Template** (European + Filipino locales): reads ``composition.order``
  from the locale file to determine component sequence (e.g. German/Russian
  put sky first, English puts temperature first).  Connector and separator
  strings come from the locale file.

  **Custom** (CJK locales): dispatches to per-locale composer modules in
  ``locales/composers/``.  Japanese uses JMA-influenced compound expressions
  (sky + precipitation joined with 一時 "temporarily").  Chinese uses
  CMA-style space/comma-separated terms.

The ``composition.pattern`` key in each locale file ("template" or "custom")
controls which path is taken.  Custom composers receive named components
and produce the full text string.
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


_COMPONENT_ORDER_DEFAULT = ["temperature", "sky", "wind", "precipitation"]

# Lazy-loaded custom composer modules keyed by composer name.
_custom_composers: dict[str, object] = {}


def _get_custom_composer(name: str) -> object | None:
    """Import and cache a custom composer module by name."""
    if name in _custom_composers:
        return _custom_composers[name]
    try:
        mod = __import__(
            f"weewx_clearskies_api.locales.composers.{name}",
            fromlist=["compose"],
        )
        _custom_composers[name] = mod
        return mod
    except ImportError:
        _custom_composers[name] = None  # type: ignore[assignment]
        return None


def _compose_components(
    components: dict[str, str | None],
    locale: str | None = None,
) -> str:
    """Dispatch to template or custom composer based on locale config.

    *components* maps component names ("temperature", "sky", "wind",
    "precipitation") to their translated labels (or None if absent).
    """
    loc = locale or i18n.get_active_locale()
    pattern = i18n.t("composition.pattern", loc)
    composer_name = i18n.t("composition.composer", loc)

    # Custom composer dispatch for CJK locales.
    if pattern == "custom" and composer_name != "composition.composer":
        mod = _get_custom_composer(composer_name)
        if mod is not None and hasattr(mod, "compose"):
            return mod.compose(components, loc)

    # Template composition: locale-driven order + connectors.
    return _template_compose(components, loc)


def _template_compose(
    components: dict[str, str | None],
    locale: str,
) -> str:
    """Template-based composition with locale-driven order and connectors.

    Reads ``composition.order`` from the locale file to determine component
    sequence.  Uses ``connector_with`` for the last part (preventing
    double-"and" with compound temperature labels like "Warm and Humid"),
    except when the last part is Beaufort-0 ("Calm"), where ``connector_and``
    is more natural.
    """
    # Read order from locale file; fall back to default.
    order = _COMPONENT_ORDER_DEFAULT
    order_raw = i18n._resolve_key(  # noqa: SLF001
        i18n._locales.get(locale, {}), "composition.order"  # noqa: SLF001
    )
    if isinstance(order_raw, list) and all(isinstance(x, str) for x in order_raw):
        order = order_raw

    # Build ordered parts list from named components.
    filtered = [components[k] for k in order if components.get(k)]
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
    components: dict[str, str | None] = {
        "temperature": None,
        "sky": None,
        "wind": None,
        "precipitation": None,
    }

    # 1. Temperature-comfort (2D matrix, ADR-044 §5-7).
    app_temp_f = _to_fahrenheit(app_temp, temp_unit)
    dewpoint_f = _to_fahrenheit(dewpoint, dewpoint_unit)
    out_temp_f = _to_fahrenheit(out_temp, temp_unit)
    heatindex_f = _to_fahrenheit(heatindex, temp_unit)
    windchill_f = _to_fahrenheit(windchill, temp_unit)

    components["temperature"] = _temperature_comfort.classify(
        app_temp=app_temp_f,
        dewpoint=dewpoint_f,
        out_temp=out_temp_f,
        heatindex=heatindex_f,
        windchill=windchill_f,
        locale=locale,
    )

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
    if fog_mist_label is not None:
        effective_sky = fog_mist_label
    else:
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

    components["sky"] = effective_sky

    # 3. Wind (Beaufort label). All Beaufort values including 0 (Calm) are
    # included — calm is a real condition (ADR-044 §4, amended 2026-06-05).
    if wind_speed is not None:
        try:
            b = beaufort(wind_speed, wind_speed_unit, locale)
            wind_label = str(b["label"])

            if b["value"] > 0 and wind_gust is not None:
                speed_mph = convert(wind_speed, wind_speed_unit, "mile_per_hour")
                gust_mph = convert(wind_gust, wind_gust_unit, "mile_per_hour")
                if (
                    speed_mph is not None
                    and gust_mph is not None
                    and gust_mph >= speed_mph + 12.0
                    and gust_mph >= 18.0
                ):
                    connector = i18n.t("composition.connector_and", locale)
                    gusty = i18n.t("wind.gusty", locale)
                    wind_label = f"{wind_label} {connector} {gusty}"

            components["wind"] = wind_label
        except (ValueError, TypeError):
            pass

    # 4. Precipitation.
    components["precipitation"] = _precip_label(
        rain_rate, rain_rate_unit, precip_type=precip_type, snow_rate=snow_rate, locale=locale
    )

    return _compose_components(components, locale)
