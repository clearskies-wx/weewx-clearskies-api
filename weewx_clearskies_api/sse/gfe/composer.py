"""Single-pass forecast text composition engine (ADR-082 T5.1).

Assembles the outputs of the individual GFE phrase generators (sky,
temperature, wind, weather/precipitation, snow/ice, temperature trend,
extreme-temperature descriptor) into one NWS-style forecast sentence for a
single :class:`~weewx_clearskies_api.sse.forecast_model.ForecastPeriod`.

This is a **simplified single-pass sequential assembly**, not a port of
GFE's tree-based phrase composition (`Phrase.py`/`PhraseBuilder.py`
multi-sub-period grid traversal). ADR-082 scopes this engine to
single-station, single-period text: one call in, one sentence out. No
phrase wording is invented here — every display fragment comes from the
existing `sse/gfe/*_phrases.py` modules; this module only decides which
phrases to call and how to order and join them.
"""

from __future__ import annotations

from weewx_clearskies_api.sse.forecast_model import ForecastPeriod
from weewx_clearskies_api.sse.gfe.sky_phrases import sky_phrase, sky_pop_suppression
from weewx_clearskies_api.sse.gfe.snow_ice_phrases import ice_phrase, snow_phrase
from weewx_clearskies_api.sse.gfe.temp_phrases import (
    temp_descriptor,
    temp_phrase,
    temp_trend_phrase,
)
from weewx_clearskies_api.sse.gfe.wind_phrases import wind_phrase
from weewx_clearskies_api.sse.gfe.wx_phrases import weather_phrase

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
        prefix = "Highs" if period.is_daytime else "Lows"
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
