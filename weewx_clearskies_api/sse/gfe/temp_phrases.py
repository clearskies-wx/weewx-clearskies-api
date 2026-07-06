"""Temperature decade phrase generation (GFE SS2 port, ADR-082).

Ports the temperature phrasing logic from the NWS Graphical Forecast
Editor (GFE) text formatter (`ScalarPhrases.py`, `getTempPhrase()` /
`getDecadeStr()` / `tempPhrase_exceptions()` / `tempPhrase_boundary_dict()`
/ `temp_trends_words()` / `extremeTemps_words()`). Threshold data lives in
`thresholds.py`; this module only composes display strings from those
tables via `weewx_clearskies_api.i18n.t()`.

Decade/threshold precedence (per lead clarification, T4.1/T4.2): the
same-decade span check takes priority over the exact-range diff
threshold. `TEMP_DIFF_THRESHOLD` only forces an exact numeric range when
min and max fall in *different* decades and the spread exceeds it; within
a single decade, decade phrasing always applies (a same-decade spread is
at most 9 degrees by construction) — see `gfe-source-code-analysis.md`
section 2.3's worked example (min 83, max 89 -> "in the 80s").

Full source analysis: `docs/reference/nws-text-system/gfe-source-code-analysis.md`
section 2. Decision record: ADR-082.
"""

from __future__ import annotations

import operator
from collections.abc import Callable

from weewx_clearskies_api.i18n import t
from weewx_clearskies_api.sse.gfe.thresholds import (
    EXTREME_TEMP_DESCRIPTORS,
    TEMP_BOUNDARY_DICT,
    TEMP_DIFF_THRESHOLD,
    TEMP_EXCEPTION_TABLE,
)

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
}

# Fallback English templates for exception-table phrases (GFE
# `tempPhrase_exceptions`), used only when the locale system has not yet
# been populated with the corresponding `forecast.temp.*` key.
_EXCEPTION_TEMPLATES: dict[str, str] = {
    "around_min": "around {min}",
    "min_to_max": "{min} to {max}",
    "near_zero": "near zero",
    "zero_to_max": "zero to {max}",
    "min_to_zero": "{min} to zero",
    "min_to_max_zero": "{min} to {max} below zero",
    "max_to_min_zero": "{max} to {min} below zero",
}


def _word(key: str, locale: str) -> str:
    """Resolve a single-word temperature vocabulary item via locale lookup.

    Looks up ``forecast.temp.{key}`` (decade positions "lower"/"mid"/
    "upper", special decade names, extreme-temperature descriptors, trend
    directions, time-of-day qualifiers). Falls back to a title-cased
    rendering of *key* (underscores replaced with spaces) when unresolved,
    matching the sky_phrases.py fallback convention.
    """
    dotted = f"forecast.temp.{key}"
    resolved = t(dotted, locale)
    if resolved != dotted:
        return resolved
    return key.replace("_", " ").title()


def _fmt_number(value: float) -> str:
    """Render *value* without a trailing '.0' for whole-number temperatures."""
    if value == int(value):
        return str(int(value))
    return str(value)


def _in_bounds(value: float, bounds: tuple[int, int]) -> bool:
    low, high = bounds
    return low <= value <= high


def _exception_phrase(key: str, locale: str, temp_min: float, temp_max: float) -> str:
    dotted = f"forecast.temp.{key}"
    resolved = t(dotted, locale)
    template = resolved if resolved != dotted else _EXCEPTION_TEMPLATES[key]
    return template.format(min=_fmt_number(temp_min), max=_fmt_number(temp_max))


def _decade_of(value: float) -> int:
    """Return the decade (multiple of 10, floor-divided) containing *value*."""
    return int(value // 10) * 10


def _ones_digit(value: float) -> int:
    """Return the position-within-decade digit (0-9) used by `TEMP_BOUNDARY_DICT`."""
    return int(value) % 10


def _decade_word(decade: int, locale: str) -> str:
    """Return the display word for a decade (GFE `getDecadeStr`).

    Special-cases 0 ("single digits"), 10 ("teens"), and -10 ("teens
    below zero"); otherwise resolves a "{decade}s" template.
    """
    if decade == 0:
        return _word("single_digits", locale)
    if decade == 10:
        return _word("teens", locale)
    if decade == -10:
        return _word("teens_below_zero", locale)

    dotted = "forecast.temp.decade_number"
    resolved = t(dotted, locale)
    template = resolved if resolved != dotted else "{decade}s"
    return template.format(decade=decade)


def temp_phrase(temp_min: float, temp_max: float, is_daytime: bool, locale: str) -> str:
    """Return the full GFE-style temperature phrase for a min/max pair.

    Algorithm (GFE `getTempPhrase`, per lead clarification on precedence):

    1. Check `TEMP_EXCEPTION_TABLE` first (zero-crossing, > 100, exact
       round-decade values).
    2. If min and max fall in the same decade: report decade phrasing.
       When min sits in the "lower" position and max in the "upper"
       position (spanning the full decade), omit the position word
       ("in the 80s"); otherwise use max's position ("in the mid 80s").
    3. If min and max fall in *different* decades and the spread exceeds
       `TEMP_DIFF_THRESHOLD`, report an exact range ("78 to 92").
    4. Otherwise (different decades, spread within threshold): use max's
       decade and position ("in the upper 70s").

    *is_daytime* is accepted for interface symmetry with the other
    generators in this module but does not affect decade phrasing itself
    (day/night only matters for `temp_trend_phrase`'s time-of-day wording).
    """
    for exc in TEMP_EXCEPTION_TABLE:
        if _in_bounds(temp_min, exc.min_bounds) and _in_bounds(temp_max, exc.max_bounds):
            if temp_min == temp_max and exc.equality_key is not None:
                return _exception_phrase(exc.equality_key, locale, temp_min, temp_max)
            return _exception_phrase(exc.range_key, locale, temp_min, temp_max)

    decade_min = _decade_of(temp_min)
    decade_max = _decade_of(temp_max)
    position_min = TEMP_BOUNDARY_DICT[_ones_digit(temp_min)]
    position_max = TEMP_BOUNDARY_DICT[_ones_digit(temp_max)]

    if decade_min == decade_max:
        decade_word = _decade_word(decade_max, locale)
        if position_min == "lower" and position_max == "upper":
            dotted = "forecast.temp.in_the_decade"
            resolved = t(dotted, locale)
            template = resolved if resolved != dotted else "in the {decade}"
            return template.format(decade=decade_word)

        dotted = "forecast.temp.in_the_position_decade"
        resolved = t(dotted, locale)
        template = resolved if resolved != dotted else "in the {position} {decade}"
        return template.format(position=_word(position_max, locale), decade=decade_word)

    if abs(temp_max - temp_min) > TEMP_DIFF_THRESHOLD:
        return _exception_phrase("min_to_max", locale, temp_min, temp_max)

    decade_word = _decade_word(decade_max, locale)
    dotted = "forecast.temp.in_the_position_decade"
    resolved = t(dotted, locale)
    template = resolved if resolved != dotted else "in the {position} {decade}"
    return template.format(position=_word(position_max, locale), decade=decade_word)


def _rule_matches(
    conditions: tuple[tuple[str, str, float], ...], context: dict[str, float | None]
) -> bool:
    for field, op, threshold in conditions:
        value = context.get(field)
        if value is None:
            return False
        if not _OPS[op](value, threshold):
            return False
    return True


def temp_descriptor(
    temp: float,
    heat_index: float | None,
    wind_chill: float | None,
    is_daytime: bool,
    locale: str,
) -> str | None:
    """Return an extreme-temperature descriptor phrase, or None if none applies.

    Evaluates `EXTREME_TEMP_DESCRIPTORS["day"]` or `["night"]` in order;
    the first rule whose ANDed conditions all match wins (GFE
    `extremeTemps_words`). A rule referencing `heat_index` or
    `wind_chill`/`heat_index_minus_temp` never matches when that value is
    unavailable (`None`) — it doesn't silently treat it as passing.
    """
    period = "day" if is_daytime else "night"
    context: dict[str, float | None] = {
        "temp": temp,
        "heat_index": heat_index,
        "heat_index_minus_temp": (heat_index - temp) if heat_index is not None else None,
        "wind_chill": wind_chill,
    }
    for rule in EXTREME_TEMP_DESCRIPTORS[period]:
        if _rule_matches(rule.conditions, context):
            return _word(rule.descriptor, locale)
    return None


def temp_trend_phrase(
    temp_trend: str | None, temp_value: float, is_daytime: bool, locale: str
) -> str | None:
    """Return a temperature-trend phrase, or None when no trend applies.

    *temp_trend* is the trend direction key (e.g. ``"falling"``,
    ``"rising"``); ``None`` means no trend was detected (caller is
    responsible for applying `TEMP_TREND_THRESHOLD` before calling this).
    Composes decade + position phrasing for *temp_value* plus a
    time-of-day qualifier: "in the afternoon" for daytime, "after
    midnight" for nighttime (GFE `temp_trends_words`).
    """
    if temp_trend is None:
        return None

    decade = _decade_of(temp_value)
    position = TEMP_BOUNDARY_DICT[_ones_digit(temp_value)]
    decade_word = _decade_word(decade, locale)
    position_word = _word(position, locale)
    trend_word = _word(temp_trend, locale)
    time_key = "in_the_afternoon" if is_daytime else "after_midnight"
    time_word = _word(time_key, locale)

    dotted = "forecast.temp.trend_phrase"
    resolved = t(dotted, locale)
    template = (
        resolved
        if resolved != dotted
        else "temperatures {trend} into the {position} {decade} {time}"
    )
    return template.format(
        trend=trend_word, position=position_word, decade=decade_word, time=time_word
    )
