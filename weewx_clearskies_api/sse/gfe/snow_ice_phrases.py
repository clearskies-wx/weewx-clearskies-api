"""Snow and ice accumulation phrasing (ADR-082, GFE §9).

Ported faithfully from the NWS Graphical Forecast Editor (GFE) text
formatter source (public domain, 17 USC S105), `ScalarPhrases.py`
`snow_words` through `iceAccumulation_words`. See
`docs/reference/nws-text-system/gfe-source-code-analysis.md` §9 for the
full source analysis and `sse/gfe/thresholds.py` for the ported threshold
tables this module consumes.

All display strings resolve through `i18n.t()` — this module holds no
hardcoded English literals. Locale keys used here (``gfe.snow.*``,
``gfe.descriptive_snow.*``, ``gfe.ice.*``) fall back to the raw key string
via `i18n.t()`'s resolution chain until locale JSON entries are populated
in a later I18N task.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    DESCRIPTIVE_SNOW_TIERS,
    ICE_ACCUMULATION_TIERS,
    POP_SNOW_LOWER_THRESHOLD,
    SNOW_ACCUMULATION_TIERS,
)


def _format_inches(value: float, locale: str) -> str:
    """Format an inches value with locale-correct decimal separator.

    Whole numbers render with no decimal places; fractional values render
    with one decimal place (GFE snow/ice amounts are reported to the
    nearest tenth of an inch, matching `increment_nlValue_dict["SnowAmt"]`
    in `thresholds.ROUNDING_INCREMENTS`).
    """
    decimals = 0 if float(value).is_integer() else 1
    return i18n.format_number(value, decimals, locale)


def snow_phrase(max_inches: float, min_inches: float, pop: float, locale: str) -> str | None:
    """Snow accumulation phrasing (GFE `snow_words`).

    Requires *pop* (probability of precipitation, percent) at or above
    `POP_SNOW_LOWER_THRESHOLD`; below that, snow accumulation is not
    reported and this returns ``None``.

    Iterates `SNOW_ACCUMULATION_TIERS` in order — the first tier whose
    conditions match *max_inches*/*min_inches* wins:
      - max < 0.5           -> "no snow accumulation"
      - max < 1             -> "little or no snow accumulation"
      - min < 1, max < 3     -> "up to {max} inches"
      - max - min < 2        -> "around {max} inches"
      - otherwise            -> "{min} to {max} inches"
    """
    if pop < POP_SNOW_LOWER_THRESHOLD:
        return None

    for tier in SNOW_ACCUMULATION_TIERS:
        if tier.max_lt is not None and not (max_inches < tier.max_lt):
            continue
        if tier.min_lt is not None and not (min_inches < tier.min_lt):
            continue
        if tier.delta_lt is not None and not ((max_inches - min_inches) < tier.delta_lt):
            continue
        return _render_snow_phrase(tier.phrase_key, min_inches, max_inches, locale)

    return None  # pragma: no cover - final tier has no conditions and always matches


def _render_snow_phrase(
    phrase_key: str, min_inches: float, max_inches: float, locale: str
) -> str:
    """Render the matched snow accumulation tier to display text."""
    if phrase_key in ("no_accumulation", "little_or_no_accumulation"):
        return i18n.t(f"gfe.snow.{phrase_key}", locale)

    if phrase_key == "up_to_max":
        template = i18n.t("gfe.snow.up_to_max", locale)
        return template.format(max=_format_inches(max_inches, locale))

    if phrase_key == "around_max":
        template = i18n.t("gfe.snow.around_max", locale)
        return template.format(max=_format_inches(max_inches, locale))

    # "min_to_max" — the otherwise/fallback tier.
    template = i18n.t("gfe.snow.min_to_max", locale)
    return template.format(
        min=_format_inches(min_inches, locale),
        max=_format_inches(max_inches, locale),
    )


def descriptive_snow(max_inches: float, locale: str) -> str | None:
    """Descriptive snow category (GFE `descriptive_snow_words`).

    Iterates `DESCRIPTIVE_SNOW_TIERS` in order — the first tier whose
    exclusive upper bound *max_inches* falls under wins. Returns ``None``
    when the matched tier has no descriptor (accumulations under 1 inch).
    """
    for tier in DESCRIPTIVE_SNOW_TIERS:
        if tier.max_upper is None or max_inches < tier.max_upper:
            if tier.phrase_key is None:
                return None
            return i18n.t(f"gfe.descriptive_snow.{tier.phrase_key}", locale)

    return None  # pragma: no cover - final tier (max_upper=None) always matches


def ice_phrase(inches: float, locale: str) -> str:
    """Ice accumulation phrasing (GFE `iceAccumulation_words`).

    Iterates `ICE_ACCUMULATION_TIERS` in order — the first tier whose
    exclusive upper bound *inches* falls under wins. The final tier
    (``upper_bound=None``) rounds *inches* to the nearest whole integer,
    matching the GFE source's behavior at and above 1.8 inches.
    """
    for tier in ICE_ACCUMULATION_TIERS:
        if tier.upper_bound is None:
            return _render_ice_integer(inches, locale)
        if inches < tier.upper_bound:
            return i18n.t(f"gfe.ice.{tier.phrase_key}", locale)

    return _render_ice_integer(inches, locale)  # pragma: no cover - unreachable


def _render_ice_integer(inches: float, locale: str) -> str:
    """Render the integer-rounded ice accumulation phrase."""
    rounded = round(inches)
    template = i18n.t("gfe.ice.integer_rounded", locale)
    return template.format(value=i18n.format_number(rounded, 0, locale))
