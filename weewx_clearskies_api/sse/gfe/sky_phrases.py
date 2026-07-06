"""Sky coverage phrase generation (GFE SS1 port, ADR-082).

Ports the sky-coverage phrasing logic from the NWS Graphical Forecast
Editor (GFE) text formatter (`ScalarPhrases.py`, `sky_valueList()` /
`similarSkyWords_list()` / `pop_sky_lower_threshold()`). Threshold data
lives in `thresholds.py`; this module only composes display strings from
those tables via `weewx_clearskies_api.i18n.t()`.

Full source analysis: `docs/reference/nws-text-system/gfe-source-code-analysis.md`
section 1. Decision record: ADR-082.
"""

from __future__ import annotations

from weewx_clearskies_api.i18n import t
from weewx_clearskies_api.sse.gfe.thresholds import (
    POP_SKY_LOWER_THRESHOLD,
    SIMILAR_SKY_WORDS_DAY,
    SIMILAR_SKY_WORDS_NIGHT,
    SKY_VALUE_LIST,
)


def _label(key: str, locale: str) -> str:
    """Resolve a sky-coverage vocabulary *key* to a display string.

    Looks up ``forecast.sky.{key}`` via :func:`weewx_clearskies_api.i18n.t`.
    Falls back to a title-cased rendering of *key* (underscores replaced
    with spaces) when the locale system has not yet been populated with
    the corresponding string — see design note in the T4.1/T4.2 task
    brief: locale JSON files do not carry `forecast.*` keys yet.
    """
    dotted = f"forecast.sky.{key}"
    resolved = t(dotted, locale)
    if resolved != dotted:
        return resolved
    return key.replace("_", " ").title()


def sky_phrase(sky_percent: float, is_daytime: bool, locale: str) -> str:
    """Return the sky coverage phrase for *sky_percent* (0-100).

    Iterates the 6-bucket `SKY_VALUE_LIST` table (GFE `sky_valueList()`)
    and returns the first bucket whose inclusive upper bound is greater
    than or equal to *sky_percent*, using the daytime or nighttime key per
    *is_daytime*.
    """
    for bucket in SKY_VALUE_LIST:
        if sky_percent <= bucket.upper_pct:
            key = bucket.day_key if is_daytime else bucket.night_key
            return _label(key, locale)

    # Defensive fallback for out-of-range input (> 100): use the cloudiest
    # (final) bucket rather than raising, since callers may pass unclamped
    # provider data.
    last = SKY_VALUE_LIST[-1]
    key = last.day_key if is_daytime else last.night_key
    return _label(key, locale)


def sky_trend_phrase(prev_sky: str, curr_sky: str, is_daytime: bool, locale: str) -> str | None:
    """Return a trend phrase transitioning from *prev_sky* to *curr_sky*.

    *prev_sky* and *curr_sky* are sky-coverage keys (e.g. ``"sunny"``,
    ``"mostly_cloudy"``), not display strings. Returns ``None`` when the
    pair is listed in `SIMILAR_SKY_WORDS_DAY`/`_NIGHT` (GFE
    `similarSkyWords_list()`) — the transition is too similar to report,
    checked in either direction since the suppression is about textual
    similarity, not direction of change.
    """
    similar_pairs = SIMILAR_SKY_WORDS_DAY if is_daytime else SIMILAR_SKY_WORDS_NIGHT
    if (prev_sky, curr_sky) in similar_pairs or (curr_sky, prev_sky) in similar_pairs:
        return None

    curr_label = _label(curr_sky, locale)
    dotted = "forecast.sky.trend_then_becoming"
    resolved = t(dotted, locale)
    template = resolved if resolved != dotted else "then becoming {sky}"
    return template.format(sky=curr_label)


def sky_pop_suppression(pop: float) -> bool:
    """Return True when high probability of precipitation should suppress the sky phrase.

    GFE `pop_sky_lower_threshold()`: when PoP is at or above this value,
    precipitation dominates the forecast and the sky phrase is omitted.
    """
    return pop >= POP_SKY_LOWER_THRESHOLD
