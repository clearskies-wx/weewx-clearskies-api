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

**Unit-system awareness (ADR-082 T7.1 gap closure, 2026-07-06):**
`snow_phrase()`, `descriptive_snow()`, and `ice_phrase()` accept a
``unit_system`` argument (``"US"`` / ``"METRIC"`` / ``"METRICWX"``, default
``"US"``, matching the pattern `sse/gfe/wind_phrases.py` uses). The ported
GFE threshold tables (`SNOW_ACCUMULATION_TIERS`, `DESCRIPTIVE_SNOW_TIERS`,
`ICE_ACCUMULATION_TIERS`) are all inch-calibrated. `ForecastPeriod` values
are NOT guaranteed inches for metric operators — per
`sse/forecast_model.py` and `providers/forecast/aeris.py`:

- `snow_amount` arrives in **centimeters** for METRIC/METRICWX (Xweather's
  `snowCM` daily field; Open-Meteo's `snowfall_sum` under the
  `precipitation_unit=mm` request param, which Open-Meteo reports in cm per
  its documented snowfall unit quirk).
- `ice_accumulation` arrives in **millimeters** for METRIC/METRICWX
  (Xweather's `iceaccumMM` daily field — the only provider that supplies
  this field at all; Open-Meteo and OWM do not).

These are two DIFFERENT units, not a shared cm/inch pair — confirmed with
the lead 2026-07-06 before implementing (an initial reading of the task
brief assumed both were centimeters, which would have applied a 10x-wrong
factor to ice accumulation). `snow_phrase()`/`descriptive_snow()` convert
cm -> inches for threshold comparison via the canonical conversion registry
(`units/conversion.py`); `ice_phrase()` converts mm -> inches. Both render
the ORIGINAL (unconverted) metric value at display time, with a "cm" or
"mm" unit label respectively, rather than the inches value used only for
tier selection. US callers get the identity behavior (no conversion, no
unit suffix change) that predates this change.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    DESCRIPTIVE_SNOW_TIERS,
    ICE_ACCUMULATION_TIERS,
    POP_SNOW_LOWER_THRESHOLD,
    SNOW_ACCUMULATION_TIERS,
)
from weewx_clearskies_api.units.conversion import convert as _convert_units

# Unit systems that carry metric-scale ForecastPeriod values (see module
# docstring). Everything else (just "US") is treated as already-inches.
_METRIC_SYSTEMS = ("METRIC", "METRICWX")


def _format_amount(value: float, locale: str) -> str:
    """Format a snow/ice amount value with locale-correct decimal separator.

    Whole numbers render with no decimal places; fractional values render
    with one decimal place (GFE snow/ice amounts are reported to the
    nearest tenth of an inch, matching `increment_nlValue_dict["SnowAmt"]`
    in `thresholds.ROUNDING_INCREMENTS`). Unit-agnostic — the same rounding
    convention is applied to cm/mm display values as to inches, since no
    GFE source rounding rule exists for those units.
    """
    decimals = 0 if float(value).is_integer() else 1
    return i18n.format_number(value, decimals, locale)


def _snow_amount_to_inches(amount: float, unit_system: str) -> float:
    """Convert a snow accumulation *amount* to inches for GFE threshold checks.

    METRIC/METRICWX values arrive in centimeters (see module docstring); US
    values are already inches and pass through unchanged.
    """
    if unit_system in _METRIC_SYSTEMS:
        return _convert_units(amount, "cm", "inch")
    return amount


def _ice_amount_to_inches(amount: float, unit_system: str) -> float:
    """Convert an ice accumulation *amount* to inches for GFE threshold checks.

    METRIC/METRICWX values arrive in millimeters (see module docstring) —
    NOT centimeters, unlike `snow_amount`. US values are already inches and
    pass through unchanged.
    """
    if unit_system in _METRIC_SYSTEMS:
        return _convert_units(amount, "mm", "inch")
    return amount


def _snow_unit_label(unit_system: str, locale: str) -> str:
    """Resolve the display unit word for snow accumulation phrases.

    US (default): the locale's own word for "inches"
    (``gfe.snow.unit_inches``), matching each locale's pre-existing
    hardcoded template wording exactly — preserves current output
    byte-for-byte for US callers. METRIC/METRICWX: "cm"
    (``unit_labels.cm``, stripped of its leading-space number-suffix
    convention — see `wind_phrases._wind_unit_label` for the same
    stripping pattern).
    """
    if unit_system in _METRIC_SYSTEMS:
        return i18n.t("unit_labels.cm", locale).strip()
    return i18n.t("gfe.snow.unit_inches", locale)


def _ice_unit_label(unit_system: str, locale: str) -> str:
    """Resolve the display unit word for ice accumulation phrases.

    US (default): the locale's own word for "inches" (``gfe.ice.unit_inches``)
    — kept as a SEPARATE key from ``gfe.snow.unit_inches`` because at least
    one locale (Russian) uses a different grammatical case/number form in
    the ice integer-rounded template than in the snow templates ("дюйма"
    singular genitive vs. "дюймов" plural genitive). METRIC/METRICWX: "mm"
    (``unit_labels.mm``, stripped) — ice_accumulation arrives in
    millimeters, not centimeters, for both metric unit systems (see module
    docstring).
    """
    if unit_system in _METRIC_SYSTEMS:
        return i18n.t("unit_labels.mm", locale).strip()
    return i18n.t("gfe.ice.unit_inches", locale)


def snow_phrase(
    max_amount: float,
    min_amount: float,
    pop: float,
    locale: str,
    unit_system: str = "US",
) -> str | None:
    """Snow accumulation phrasing (GFE `snow_words`).

    Requires *pop* (probability of precipitation, percent) at or above
    `POP_SNOW_LOWER_THRESHOLD`; below that, snow accumulation is not
    reported and this returns ``None``.

    *max_amount*/*min_amount* are expressed in *unit_system*'s unit (inches
    for "US", centimeters for "METRIC"/"METRICWX" — see module docstring).
    Threshold comparisons always run in inches — `SNOW_ACCUMULATION_TIERS`
    is inch-calibrated GFE data — so metric values are converted for tier
    selection only; the rendered phrase uses the original metric value with
    a "cm" unit label.

    Iterates `SNOW_ACCUMULATION_TIERS` in order — the first tier whose
    conditions match wins:
      - max < 0.5 in            -> "no snow accumulation"
      - max < 1 in              -> "little or no snow accumulation"
      - min < 1 in, max < 3 in  -> "up to {max} {unit}"
      - max - min < 2 in        -> "around {max} {unit}"
      - otherwise                -> "{min} to {max} {unit}"
    """
    if pop < POP_SNOW_LOWER_THRESHOLD:
        return None

    max_inches = _snow_amount_to_inches(max_amount, unit_system)
    min_inches = _snow_amount_to_inches(min_amount, unit_system)

    for tier in SNOW_ACCUMULATION_TIERS:
        if tier.max_lt is not None and not (max_inches < tier.max_lt):
            continue
        if tier.min_lt is not None and not (min_inches < tier.min_lt):
            continue
        if tier.delta_lt is not None and not ((max_inches - min_inches) < tier.delta_lt):
            continue
        return _render_snow_phrase(tier.phrase_key, min_amount, max_amount, unit_system, locale)

    return None  # pragma: no cover - final tier has no conditions and always matches


def _render_snow_phrase(
    phrase_key: str,
    min_amount: float,
    max_amount: float,
    unit_system: str,
    locale: str,
) -> str:
    """Render the matched snow accumulation tier to display text."""
    if phrase_key in ("no_accumulation", "little_or_no_accumulation"):
        return i18n.t(f"gfe.snow.{phrase_key}", locale)

    unit = _snow_unit_label(unit_system, locale)

    if phrase_key == "up_to_max":
        template = i18n.t("gfe.snow.up_to_max", locale)
        return template.format(max=_format_amount(max_amount, locale), unit=unit)

    if phrase_key == "around_max":
        template = i18n.t("gfe.snow.around_max", locale)
        return template.format(max=_format_amount(max_amount, locale), unit=unit)

    # "min_to_max" — the otherwise/fallback tier.
    template = i18n.t("gfe.snow.min_to_max", locale)
    return template.format(
        min=_format_amount(min_amount, locale),
        max=_format_amount(max_amount, locale),
        unit=unit,
    )


def descriptive_snow(max_amount: float, locale: str, unit_system: str = "US") -> str | None:
    """Descriptive snow category (GFE `descriptive_snow_words`).

    *max_amount* is expressed in *unit_system*'s unit (inches for "US",
    centimeters for "METRIC"/"METRICWX"); converted to inches before
    consulting `DESCRIPTIVE_SNOW_TIERS`, which is inch-calibrated GFE data.
    This function returns a categorical descriptor with no embedded
    numeral/unit, so no unit label rendering is needed.

    Iterates `DESCRIPTIVE_SNOW_TIERS` in order — the first tier whose
    exclusive upper bound *max_amount* falls under wins. Returns ``None``
    when the matched tier has no descriptor (accumulations under 1 inch).
    """
    max_inches = _snow_amount_to_inches(max_amount, unit_system)

    for tier in DESCRIPTIVE_SNOW_TIERS:
        if tier.max_upper is None or max_inches < tier.max_upper:
            if tier.phrase_key is None:
                return None
            return i18n.t(f"gfe.descriptive_snow.{tier.phrase_key}", locale)

    return None  # pragma: no cover - final tier (max_upper=None) always matches


def ice_phrase(amount: float, locale: str, unit_system: str = "US") -> str:
    """Ice accumulation phrasing (GFE `iceAccumulation_words`).

    *amount* is expressed in *unit_system*'s unit (inches for "US",
    **millimeters** for "METRIC"/"METRICWX" — NOT centimeters, unlike
    `snow_phrase()`'s `snow_amount`; see module docstring). Converted to
    inches for tier selection against `ICE_ACCUMULATION_TIERS` (inch-
    calibrated GFE data); the final (open-ended) tier's integer-rounded
    render uses the original metric value with an "mm" unit label rather
    than rounding the converted inches value.

    Iterates `ICE_ACCUMULATION_TIERS` in order — the first tier whose
    exclusive upper bound the inches-converted *amount* falls under wins.
    The final tier (``upper_bound=None``) rounds *amount* (in its own
    *unit_system* unit) to the nearest whole integer, matching the GFE
    source's behavior at and above 1.8 inches. For METRIC/METRICWX, the
    fractional-inch descriptive phrases ("one quarter of an inch") have no
    natural metric equivalent, so this bypasses them entirely and renders
    the integer-rounded mm phrase instead — see `_render_ice_integer`.
    """
    inches = _ice_amount_to_inches(amount, unit_system)

    if unit_system in _METRIC_SYSTEMS:
        return _render_ice_integer(amount, unit_system, locale)

    for tier in ICE_ACCUMULATION_TIERS:
        if tier.upper_bound is None:
            return _render_ice_integer(amount, unit_system, locale)
        if inches < tier.upper_bound:
            return i18n.t(f"gfe.ice.{tier.phrase_key}", locale)

    return _render_ice_integer(amount, unit_system, locale)  # pragma: no cover - unreachable


def _render_ice_integer(amount: float, unit_system: str, locale: str) -> str:
    """Render the integer-rounded ice accumulation phrase.

    *amount* is rounded in its own *unit_system* unit (inches or mm) — no
    unit conversion happens here, only display rounding.
    """
    rounded = round(amount)
    unit = _ice_unit_label(unit_system, locale)
    template = i18n.t("gfe.ice.integer_rounded", locale)
    return template.format(value=i18n.format_number(rounded, 0, locale), unit=unit)
