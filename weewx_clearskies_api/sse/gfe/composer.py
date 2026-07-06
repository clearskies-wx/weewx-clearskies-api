"""Single-pass text composition engine for forecast AND current conditions
(ADR-082 T5.1, T7.2).

Assembles the outputs of the individual GFE phrase generators (sky,
temperature, wind, weather/precipitation, snow/ice, temperature trend,
extreme-temperature descriptor) into one NWS-style forecast sentence for a
single :class:`~weewx_clearskies_api.sse.forecast_model.ForecastPeriod`, and
(T7.2) an analogous current-conditions sentence for a single
:class:`~weewx_clearskies_api.sse.observation_model.Observation` at the
standard/verbose verbosity tiers.

This is a **simplified single-pass sequential assembly**, not a port of
GFE's tree-based phrase composition (`Phrase.py`/`PhraseBuilder.py`
multi-sub-period grid traversal). ADR-082 scopes this engine to
single-station, single-period text: one call in, one sentence out. No
phrase wording is invented here — every display fragment comes from the
existing `sse/gfe/*_phrases.py` modules; this module only decides which
phrases to call and how to order and join them.

**Current-conditions preservation (ADR-082 settled decision #12):** the
current-conditions detection systems (SkyPyEye sky classification,
temperature-comfort matrix, sensor-based precipitation, haze/fog
detection, input smoothing) are NOT touched by this module — they remain
in `sse/sky_condition.py`, `sse/temperature_comfort.py`,
`sse/haze_condition.py`, `sse/fog_condition.py`, and
`sse/enrichment/input_smoother.py`, and their already-computed outputs
arrive here on the `Observation` dataclass. This module only upgrades the
temperature (decade phrasing, extreme descriptors) and wind (hybrid
Beaufort/GFE scale via shared thresholds, improved gust phrasing) phrase
CONTENT for the standard/verbose tiers. The terse tier is unaffected and
remains `sse/conditions_text.py`'s `build_weather_text()`.

**Current-conditions unit rendering:** `Observation.temperature`,
`.dewpoint`, `.wind_speed`, and `.wind_gust` are always US units (°F, mph)
per `observation_model.py`'s documented contract — the same contract the
retired `sse/text_generator.py` relied on. GFE threshold/exception-table
branch selection (`temp_phrases.temp_phrase`, `temp_phrases.temp_descriptor`)
is designed around Fahrenheit-scale values, so branch selection always runs
on the raw °F/mph Observation value. Only numerals that do not carry GFE
threshold semantics (the rendered temperature/dewpoint decade digits, the
rendered wind speed/gust digits) are converted to the operator's configured
unit system at render time — mirroring the pattern `text_generator.py` used
via its own `configure(unit_system)` / `_f_to_c()` / `_mph_to_kmh()` /
`_mph_to_ms()` helpers (approved by lead, ADR-082 T7.2 round, 2026-07-06).
`temp_descriptor()`'s output is a categorical adjective with no embedded
numeral, so it is always evaluated on the raw °F value with no conversion
step needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from weewx_clearskies_api.i18n import t
from weewx_clearskies_api.sse.forecast_model import ForecastPeriod
from weewx_clearskies_api.sse.gfe.sky_phrases import sky_phrase, sky_pop_suppression
from weewx_clearskies_api.sse.gfe.snow_ice_phrases import ice_phrase, snow_phrase
from weewx_clearskies_api.sse.gfe.temp_phrases import (
    temp_descriptor,
    temp_phrase,
    temp_trend_phrase,
)
from weewx_clearskies_api.sse.gfe.thresholds import WIND_GUST_DIFFERENCE, WIND_NULL_THRESHOLD
from weewx_clearskies_api.sse.gfe.wind_phrases import wind_phrase
from weewx_clearskies_api.sse.gfe.wx_phrases import weather_phrase

if TYPE_CHECKING:
    from weewx_clearskies_api.sse.observation_model import Observation

# ForecastPeriod.precip_type is a word-enum ("rain" / "snow" /
# "freezing-rain" / "sleet") per period_aggregator.py's docstring, not a GFE
# weather-type code. wx_phrases.weather_phrase() expects the GFE code, so
# this composition-layer mapping bridges the two. Approved by lead
# 2026-07-05 (T5.1/T5.2 scope acknowledgment).
_PRECIP_TYPE_TO_GFE_CODE: dict[str, str] = {
    "rain": "R",
    "snow": "S",
    "freezing-rain": "ZR",
    "sleet": "IP",
}

# Default weather-phrase intensity used when no provider intensity signal is
# available at this layer. Approved by lead 2026-07-05: "m" (moderate) per
# wx_phrases.WX_INTENSITY_CODES.
_DEFAULT_WX_INTENSITY = "m"

# Fallback coverage word (GFE "Chc" == "chance of") used only when
# ForecastPeriod.precip_coverage was not derived upstream.
_DEFAULT_WX_COVERAGE = "Chc"

# 8-point compass cardinals, index = round(degrees / 45) % 8.
_CARDINALS: tuple[str, ...] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _degrees_to_cardinal(degrees: float | None) -> str | None:
    """Convert wind direction *degrees* (0-360) to an 8-point cardinal label.

    Returns ``None`` when *degrees* is ``None`` (no wind direction data).
    """
    if degrees is None:
        return None
    index = int(((degrees % 360) + 22.5) // 45) % 8
    return _CARDINALS[index]


def _capitalize_sentence(text: str) -> str:
    """Capitalize the first character of *text*, leaving the rest untouched."""
    if not text:
        return text
    return text[0].upper() + text[1:]


def compose_forecast_text(period: ForecastPeriod, locale: str) -> str:
    """Compose a complete NWS-style forecast text for one period.

    Sequentially generates each applicable phrase, then joins the non-empty
    results with ". " (period + space), capitalizing the first letter of
    each sentence fragment and terminating the composed string with a
    period. Returns ``""`` when no phrase generator produced output (e.g. an
    entirely empty period).
    """
    phrases: list[str] = []

    # 1. Sky — suppressed when PoP is high enough that precipitation
    # dominates the forecast (GFE pop_sky_lower_threshold).
    if period.sky_percent is not None:
        pop_for_sky = period.pop if period.pop is not None else 0.0
        if not sky_pop_suppression(pop_for_sky):
            phrases.append(sky_phrase(period.sky_percent, period.is_daytime, locale))

    # 2. Temperature — "Highs"/"Lows" prefix per day/night period.
    temp_min = period.temp_low if period.temp_low is not None else period.temp_high
    temp_max = period.temp_high if period.temp_high is not None else period.temp_low
    if temp_min is not None and temp_max is not None:
        prefix_key = "forecast.temp.highs" if period.is_daytime else "forecast.temp.lows"
        prefix = t(prefix_key, locale)
        phrases.append(
            f"{prefix} {temp_phrase(temp_min, temp_max, period.is_daytime, locale)}"
        )

    # 3. Wind — direction converted to cardinal before calling wind_phrase.
    if period.wind_speed_max is not None:
        cardinal = _degrees_to_cardinal(period.wind_direction)
        phrases.append(
            wind_phrase(
                period.wind_speed_min,
                period.wind_speed_max,
                period.wind_gust,
                cardinal,
                locale,
            )
        )

    # 4. Weather / precipitation — only when a recognized precip type and a
    # PoP value are both present. weather_phrase() itself suppresses
    # low-confidence PoP-related types below POP_WX_LOWER_THRESHOLD.
    if period.precip_type and period.pop is not None:
        gfe_code = _PRECIP_TYPE_TO_GFE_CODE.get(period.precip_type)
        if gfe_code is not None:
            coverage = period.precip_coverage or _DEFAULT_WX_COVERAGE
            wx = weather_phrase(gfe_code, coverage, _DEFAULT_WX_INTENSITY, period.pop, locale)
            if wx:
                phrases.append(wx)

    # 5. Snow / ice accumulation.
    if period.snow_amount is not None and period.pop is not None:
        snow = snow_phrase(period.snow_amount, period.snow_amount, period.pop, locale)
        if snow:
            phrases.append(snow)
    if period.ice_accumulation is not None and period.ice_accumulation > 0:
        phrases.append(ice_phrase(period.ice_accumulation, locale))

    # 6. Temperature trend across the period.
    if period.temp_trend is not None:
        trend_value = period.temp_high if period.is_daytime else period.temp_low
        if trend_value is not None:
            trend = temp_trend_phrase(period.temp_trend, trend_value, period.is_daytime, locale)
            if trend:
                phrases.append(trend)

    # 7. Extreme-temperature descriptor (heat/cold advisories).
    extreme_value = period.temp_high if period.is_daytime else period.temp_low
    if extreme_value is not None:
        heat_index = period.feels_like_max if period.is_daytime else None
        wind_chill = period.feels_like_min if not period.is_daytime else None
        descriptor = temp_descriptor(
            extreme_value, heat_index, wind_chill, period.is_daytime, locale
        )
        if descriptor:
            phrases.append(descriptor)

    non_empty = [phrase for phrase in phrases if phrase]
    if not non_empty:
        return ""

    return ". ".join(_capitalize_sentence(phrase) for phrase in non_empty) + "."


def compose_nws_passthrough(narrative: str | None) -> str | None:
    """Return *narrative* unchanged for the NWS provider.

    NWS supplies its own human-written `detailedForecast` text; the GFE text
    engine is not invoked for that provider — this function documents and
    enforces that pass-through contract at the composition boundary.
    """
    return narrative


# ---------------------------------------------------------------------------
# Current-conditions composition (ADR-082 T7.2)
# ---------------------------------------------------------------------------

_unit_system: str = "US"


def configure(unit_system: str) -> None:
    """Set the rendering unit system for current-conditions text output.

    Must be called at startup before any current-conditions text is
    generated. Valid values: "US", "METRIC", "METRICWX". Mirrors the
    `configure()` pattern the retired `sse/text_generator.py` module used
    — see the module docstring above for why conversion happens here
    (render time) rather than inside the GFE phrase generators.
    """
    global _unit_system  # noqa: PLW0603
    _unit_system = unit_system


def _f_to_c(value: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (value - 32.0) * 5.0 / 9.0


def _mph_to_kmh(value: float) -> float:
    """Convert miles per hour to kilometres per hour."""
    return value * 1.60934


def _mph_to_ms(value: float) -> float:
    """Convert miles per hour to metres per second."""
    return value * 0.44704


def _convert_temp_for_display(value: float) -> float:
    """Convert a Fahrenheit value to the configured unit system for display.

    Only used for numerals that feed GFE decade phrasing (`temp_phrase`),
    which carries no embedded unit token — see module docstring.
    """
    if _unit_system in ("METRIC", "METRICWX"):
        return _f_to_c(value)
    return value


def _wind_unit_label() -> str:
    """Return the wind-speed unit label for the configured unit system."""
    if _unit_system == "METRIC":
        return "km/h"
    if _unit_system == "METRICWX":
        return "m/s"
    return "mph"


def _convert_wind_for_display(value: float) -> float:
    """Convert an mph value to the configured unit system for display."""
    if _unit_system == "METRIC":
        return _mph_to_kmh(value)
    if _unit_system == "METRICWX":
        return _mph_to_ms(value)
    return value


def _format_wind_number(value: float) -> str:
    """Render a wind speed/gust number rounded to the nearest integer."""
    return str(int(round(value)))


# Full 8-point compass words (current-conditions wind sentence style, per
# API-MANUAL.md SS8 "South winds around 8 mph" example) — distinct from
# _degrees_to_cardinal()'s abbreviated cardinals ("S") used by the forecast
# wind_phrase(). Ported from the retired text_generator._wind_direction_label.
_CURRENT_WIND_DIRECTIONS: tuple[tuple[float, str], ...] = (
    (22.5, "North"),
    (67.5, "Northeast"),
    (112.5, "East"),
    (157.5, "Southeast"),
    (202.5, "South"),
    (247.5, "Southwest"),
    (292.5, "West"),
    (337.5, "Northwest"),
    (360.0, "North"),  # wrap-around: 337.5-360
)


def _wind_direction_word(degrees: float | None) -> str | None:
    """Convert wind direction in degrees to a full 8-point compass word.

    Returns ``None`` when *degrees* is ``None``.
    """
    if degrees is None:
        return None
    normalized = float(degrees) % 360.0
    for upper, label in _CURRENT_WIND_DIRECTIONS:
        if normalized < upper:
            return label
    return "North"  # unreachable after wrap-around entry


def _current_wind_sentence(
    wind_speed: float | None,
    wind_direction: float | None,
    wind_gust: float | None,
) -> str | None:
    """Build the current-conditions wind sentence with render-time unit conversion.

    Threshold comparisons (calm / gust-significance) run on the *original*
    mph Observation value — `WIND_NULL_THRESHOLD` and `WIND_GUST_DIFFERENCE`
    are GFE constants defined in mph (`sse/gfe/thresholds.py`). Only the
    rendered numerals are converted to the operator's unit system.

    This deliberately does NOT delegate to `gfe.wind_phrases.wind_phrase()`
    — that module's i18n templates (`wind.magnitude_around`/`_up_to`/
    `_range`, `wind.gust_suffix` in `locales/*.json`) hardcode the literal
    "mph" unit suffix inside the template string with no `{unit}`
    placeholder, so feeding it a km/h- or m/s-converted number would
    mislabel the output. Reusing it would require a locale-file schema
    change across 13 locales, out of scope for this wiring task (flagged
    to the lead as a follow-up, ADR-082 T7.2 round 2026-07-06).

    Returns ``None`` when *wind_speed* is ``None``.
    """
    if wind_speed is None:
        return None

    if wind_speed < WIND_NULL_THRESHOLD:
        return "Calm winds."

    unit = _wind_unit_label()
    speed_display = _format_wind_number(_convert_wind_for_display(wind_speed))
    direction = _wind_direction_word(wind_direction)

    base = (
        f"{direction} winds around {speed_display} {unit}"
        if direction is not None
        else f"Winds around {speed_display} {unit}"
    )

    if wind_gust is not None and (wind_gust - wind_speed) > WIND_GUST_DIFFERENCE:
        gust_display = _format_wind_number(_convert_wind_for_display(wind_gust))
        return f"{base} with gusts to around {gust_display} {unit}."

    return f"{base}."


def _current_sky_label(obs: Observation, locale: str) -> str | None:
    """Resolve the display sky label for current-conditions text.

    Priority:
    1. `obs.sky_label` (SkyPyEye 7-level classification, preserved per
       ADR-082 decision #12) — day/night display mapping applied via the
       preserved `conditions_text._to_display_label()` helper (already
       reused across module boundaries the same way
       `conditions_text._precip_label` is — see `observation_model.py` and
       `enrichment/weather_text.py`). Deliberately reused as-is rather than
       porting the retired `text_generator._DAY_SKY_MAP` (which also
       mapped "Partly Cloudy" -> "Partly Sunny"): `_to_display_label()`
       does not have that entry, so daytime "Partly Cloudy" renders
       unchanged here, same as it already does for the terse tier today.
       Matching the preserved terse-tier helper's existing behavior across
       all three verbosity tiers was judged preferable to diverging from
       it to restore a retired module's slightly richer mapping — flagged
       to the lead as a minor follow-up candidate, not fixed here (would
       require adding a `sky.partly_sunny` key to the preserved terse-tier
       i18n namespace across locales).
    2. `obs.cloud_cover_pct` via the GFE 6-bucket `sky_phrase()` — this is
       exactly the "provider cloud-cover percentage, not pyranometer data"
       case ADR-082 decision #12 reserves the GFE bucket table for.

    Returns ``None`` when neither source is available.
    """
    if obs.sky_label is not None:
        from weewx_clearskies_api.sse.conditions_text import _to_display_label  # noqa: PLC0415

        return _to_display_label(obs.sky_label, obs.is_daytime, locale)
    if obs.cloud_cover_pct is not None:
        return sky_phrase(obs.cloud_cover_pct, obs.is_daytime, locale)
    return None


def _current_present_weather_sentence(obs: Observation) -> str | None:
    """Build the standalone fog/mist/haze sentence for the standard tier.

    Unchanged from the retired `text_generator._present_weather_sentence`
    — ADR-082 decision #12 preserves fog/mist/haze detection and their
    textual representation as station-specific, GFE-has-no-equivalent
    systems. NWS convention: these appear as a SEPARATE sentence from sky,
    fog/mist takes priority over haze (matches detection-priority ordering
    upstream in `observation_model.py`).
    """
    if obs.fog_mist_state is not None:
        return f"{obs.fog_mist_state}."
    if obs.haze_detected:
        return "Hazy."
    return None


def _current_temperature_sentence(
    temp_f: float | None, is_daytime: bool, locale: str
) -> str | None:
    """Build the GFE decade-phrase temperature sentence ("Temperature in the mid 80s.").

    Passing the same (converted) value as both min and max naturally
    produces decade phrasing for a single-instant reading — see
    `temp_phrases.temp_phrase()`'s docstring: a same-decade span always
    applies when min == max.
    """
    if temp_f is None:
        return None
    display_value = _convert_temp_for_display(temp_f)
    phrase = temp_phrase(display_value, display_value, is_daytime, locale)
    return f"Temperature {phrase}."


def _current_temp_descriptor_sentence(
    temp_f: float | None, is_daytime: bool, locale: str
) -> str | None:
    """Build the extreme-temperature descriptor sentence (e.g. "Very Hot.").

    Always evaluated on the raw °F Observation value (never the
    unit-converted display value) — `EXTREME_TEMP_DESCRIPTORS`' thresholds
    (`temp > 99`, `temp < 20`, ...) are Fahrenheit-specific by construction,
    and the descriptor word itself carries no embedded numeral, so there is
    nothing to convert for display. `Observation` does not carry
    heat_index/wind_chill (those are TERSE-tier temperature-comfort inputs,
    ADR-082 decision #12), so only the temperature-only branches of the
    GFE table can fire here.
    """
    if temp_f is None:
        return None
    descriptor = temp_descriptor(temp_f, None, None, is_daytime, locale)
    if descriptor is None:
        return None
    return f"{_capitalize_sentence(descriptor)}."


def _current_dewpoint_sentence_verbose(
    dewpoint_f: float | None, is_daytime: bool, locale: str
) -> str | None:
    """Build the verbose-tier dew point sentence ("Dew point in the lower 60s.")."""
    if dewpoint_f is None:
        return None
    display_value = _convert_temp_for_display(dewpoint_f)
    phrase = temp_phrase(display_value, display_value, is_daytime, locale)
    return f"Dew point {phrase}."


def _current_sky_narrative_verbose(obs: Observation, locale: str) -> str | None:
    """Build the sky/conditions opening sentence for the verbose tier.

    Ported from the retired `text_generator._sky_narrative_verbose`,
    swapping the literal "{T}°F" numeral for GFE decade phrasing
    (`temp_phrases.temp_phrase`) — fog/mist/haze fusion logic and overall
    sentence shape are otherwise unchanged (ADR-082: only phrase content is
    upgraded, the verbose narrative composition pattern is not).
    """
    sky = _current_sky_label(obs, locale)
    fog = obs.fog_mist_state
    haze = obs.haze_detected
    temp = obs.temperature
    precip = obs.precipitation_label

    if fog is not None:
        sky_clause = "with fog limiting visibility" if fog == "Foggy" else "with mist"
    elif sky is not None:
        sky_lower = sky.lower()
        haze_eligible = (
            "clear" in sky_lower
            or "sunny" in sky_lower
            or "scattered clouds" in sky_lower
            or "partly" in sky_lower
        )
        if haze and haze_eligible:
            if obs.is_daytime and ("sunny" in sky_lower or "clear" in sky_lower):
                sky_clause = "under hazy sunshine"
            else:
                sky_clause = "under hazy skies"
        elif precip is not None:
            sky_clause = f"under {sky_lower} skies with {precip.lower()}"
        else:
            sky_clause = f"under {sky_lower} skies"
    elif haze:
        sky_clause = "under hazy conditions"
    elif precip is not None:
        sky_clause = f"with {precip.lower()}"
    else:
        sky_clause = None

    if temp is not None:
        display_value = _convert_temp_for_display(temp)
        phrase = temp_phrase(display_value, display_value, obs.is_daytime, locale)
        if sky_clause is not None:
            return f"Currently {phrase} {sky_clause}."
        return f"Currently {phrase}."

    if sky_clause is not None:
        return _capitalize_sentence(sky_clause) + "."

    return None


def _compose_current_standard(obs: Observation, locale: str) -> str:
    """Assemble standard-tier current-conditions text.

    Component order matches API-MANUAL.md SS8's documented example (Sky,
    present-weather, temperature[+extreme descriptor], wind): "Sunny.
    Hazy. Temperature in the mid 80s. South winds around 8 mph."
    """
    sky = _current_sky_label(obs, locale)
    sky_sentence: str | None = None
    if sky is not None:
        sky_sentence = (
            f"{sky} with {obs.precipitation_label}."
            if obs.precipitation_label is not None
            else f"{sky}."
        )

    parts: list[str | None] = [
        sky_sentence,
        _current_present_weather_sentence(obs),
        _current_temperature_sentence(obs.temperature, obs.is_daytime, locale),
        _current_temp_descriptor_sentence(obs.temperature, obs.is_daytime, locale),
        _current_wind_sentence(obs.wind_speed, obs.wind_direction, obs.wind_gust),
    ]

    return " ".join(phrase for phrase in parts if phrase)


def _compose_current_verbose(obs: Observation, locale: str) -> str:
    """Assemble verbose-tier current-conditions text.

    Component order matches API-MANUAL.md SS8's documented example (fused
    sky/haze/fog opening narrative, extreme descriptor, dew point, wind):
    "Currently 85°F under hazy sunshine. Dew point 72°F. South winds
    around 8 mph." — upgraded here with GFE decade phrasing.
    """
    parts: list[str | None] = [
        _current_sky_narrative_verbose(obs, locale),
        _current_temp_descriptor_sentence(obs.temperature, obs.is_daytime, locale),
        _current_dewpoint_sentence_verbose(obs.dewpoint, obs.is_daytime, locale),
        _current_wind_sentence(obs.wind_speed, obs.wind_direction, obs.wind_gust),
    ]

    return " ".join(phrase for phrase in parts if phrase)


def compose_current_text(obs: Observation, verbosity: str, locale: str) -> str:
    """Compose current-conditions text at the standard or verbose tier.

    ADR-082 T7.2: the GFE engine is the upgrade path for current-conditions
    standard/verbose text. Current-conditions detection/composition systems
    (SkyPyEye sky classification, temperature-comfort matrix, sensor
    precipitation, haze/fog detection, input smoothing) are PRESERVED per
    ADR-082 settled decision #12 and are not touched here — this function
    only assembles their already-computed outputs (carried on *obs*) using
    the upgraded GFE phrase generators for temperature (decade phrasing +
    extreme descriptors) and wind (hybrid Beaufort/GFE scale via shared
    thresholds, improved gust phrasing). The terse tier is NOT handled here
    — it remains `sse.conditions_text.build_weather_text()`.

    Args:
        obs:       Structured current-observation snapshot.
        verbosity: ``"standard"`` or ``"verbose"``.
        locale:    Locale code for GFE phrase resolution.

    Returns:
        Composed text, or ``""`` when no phrase produced output (mirrors
        `compose_forecast_text()`'s empty-string convention).

    Raises:
        ValueError: *verbosity* is not ``"standard"`` or ``"verbose"``.
    """
    if verbosity == "standard":
        return _compose_current_standard(obs, locale)
    if verbosity == "verbose":
        return _compose_current_verbose(obs, locale)
    raise ValueError(f"compose_current_text: unsupported verbosity {verbosity!r}")
