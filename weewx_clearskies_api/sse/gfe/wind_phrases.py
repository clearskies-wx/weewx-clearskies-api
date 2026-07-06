"""Wind phrase generation for the GFE forecast text engine (ADR-082).

Composes wind direction, magnitude, and gust phrases from provider hourly
forecast fields using the **hybrid Beaufort/GFE wind scale** (ADR-082,
settled decision #11): Beaufort labels below 30 mph, GFE/NWS descriptors at
and above 30 mph. This replaces the raw GFE ``WIND_SUMMARY_DESCRIPTORS``
table (``sse/gfe/thresholds.py``) for rendering purposes — see
API-MANUAL.md SS8 "Wind" for the full rationale and the canonical threshold
table this module implements.

Ported from the algorithm in `VectorRelatedPhrases.py` SS3.1 (GFE source
analysis, `docs/reference/nws-text-system/gfe-source-code-analysis.md`
SS3), adapted for the hybrid scale.

Two independent public surfaces:

- :func:`wind_descriptor` — a standalone hybrid-scale label for a single
  speed value (usable anywhere a single wind adjective is needed, e.g. a
  current-conditions composer).
- :func:`wind_phrase` — the full GFE-style vector phrase: direction +
  magnitude range + optional gust qualifier. The vector phrase composes
  from direction/magnitude/gust only (matching the GFE source's
  ``vector_summary`` construction); the hybrid descriptor label is not
  merged into it inline.
- :func:`marine_wind_phrase` — marine (gales/storm force/hurricane force)
  descriptor for a knot speed, dormant until a marine provider module ships
  (see PROVIDER-MANUAL.md SS4 "Marine data availability").

All display text resolves through ``i18n.t()`` against the caller-supplied
locale. Locale JSON files carry the ``wind.*`` keys this module introduces
for every supported locale (I18N T6.4); the humanized (title-cased)
rendering of the raw key is retained as a defensive fallback for any
locale that omits an entry, so output is never a raw dotted key string.

**Unit-system awareness (ADR-082 T7.1 gap closure, 2026-07-06):**
:func:`wind_phrase` accepts a ``unit_system`` argument (``"US"`` /
``"METRIC"`` / ``"METRICWX"``, default ``"US"``). The ``wind.magnitude_*``
and ``wind.gust_suffix`` locale templates carry a ``{unit}`` placeholder
resolved from the same ``unit_labels.*`` keys the API's ``units`` response
block uses (e.g. Russian "миль/ч", Chinese "英里/时") — see
:func:`_wind_unit_label`. The GFE mph-calibrated thresholds
(:data:`WIND_NULL_THRESHOLD`, :data:`WIND_GUST_DIFFERENCE`) are converted
forward into the requested unit system via the canonical conversion
registry (``units/conversion.py``) before the calm/gust comparisons run —
see :func:`_wind_threshold`. Callers that already have mph-scale values and
want mph output can omit ``unit_system`` entirely; the default reproduces
the pre-T7.1 behavior exactly.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    MARINE_WIND_DESCRIPTORS,
    WIND_GUST_DIFFERENCE,
    WIND_NULL_THRESHOLD,
)
from weewx_clearskies_api.units.conversion import convert as _convert_units

# Hybrid Beaufort/GFE wind scale (ADR-082, settled decision #11).
# ``upper_bound`` is the exclusive upper bound (mph) of the speed range this
# label applies to; ranges are contiguous starting at 0. See API-MANUAL.md
# SS8 "Wind" for the full mph / m-per-s / label table this encodes.
HYBRID_WIND_SCALE: list[tuple[float, str]] = [
    (1, "calm"),  # Beaufort 0
    (4, "very_light_breeze"),  # Beaufort 1
    (8, "light_breeze"),  # Beaufort 2
    (13, "gentle_breeze"),  # Beaufort 3
    (18, "moderate_breeze"),  # Beaufort 4
    (25, "fresh_breeze"),  # Beaufort 5
    (30, "strong_breeze"),  # Beaufort 6 (partial)
    (40, "windy"),  # GFE
    (50, "very_windy"),  # GFE
    (74, "strong_winds"),  # GFE
    (999, "hurricane_force_winds"),  # GFE
]


def _t(key: str, locale: str) -> str:
    """Resolve *key* through i18n, falling back to a humanized key.

    Used for standalone label lookups (wind descriptors). When no
    translation exists yet, the last dot-segment of the key is title-cased
    (``"wind.very_light_breeze"`` -> ``"Very Light Breeze"``) rather than
    surfacing the raw dotted key.
    """
    resolved = i18n.t(key, locale)
    if resolved == key:
        return key.rsplit(".", 1)[-1].replace("_", " ").title()
    return resolved


def _t_template(key: str, default: str, locale: str) -> str:
    """Resolve *key* through i18n, falling back to a literal *default* template.

    Used for sentence templates containing ``{placeholder}`` tokens, where
    title-casing the key (as :func:`_t` does) would destroy the template.
    """
    resolved = i18n.t(key, locale)
    return default if resolved == key else resolved


def _format_speed(value: float, locale: str) -> str:
    """Render a speed value with locale-correct decimal separator.

    Whole numbers render with no decimal places; fractional values render
    with one decimal place, matching
    :func:`weewx_clearskies_api.sse.gfe.snow_ice_phrases._format_inches`'s
    convention. Delegates to :func:`weewx_clearskies_api.i18n.format_number`
    so the decimal separator follows locale conventions (comma vs period).
    """
    decimals = 0 if float(value).is_integer() else 1
    return i18n.format_number(value, decimals, locale)


# Maps the weewx-style unit_system string ("US" / "METRIC" / "METRICWX") to
# the units/conversion.py unit key used both for threshold conversion and
# for the locale unit_labels.* lookup. Unrecognized values fall back to US.
_WIND_UNIT_KEY_FOR_SYSTEM: dict[str, str] = {
    "US": "mile_per_hour",
    "METRIC": "km_per_hour",
    "METRICWX": "meter_per_second",
}


def _wind_unit_label(unit_system: str, locale: str) -> str:
    """Resolve the locale-correct wind-speed unit label for *unit_system*.

    Looks up ``unit_labels.<key>`` (already populated for all 13 locales —
    the same resource the API's ``units`` response block draws from, e.g.
    Russian "миль/ч", Chinese "英里/时") rather than a hardcoded ASCII
    abbreviation, so the {unit} placeholder renders in the locale's
    existing convention. Falls back to the US (mph) key for any
    unrecognized *unit_system* value.
    """
    unit_key = _WIND_UNIT_KEY_FOR_SYSTEM.get(unit_system, _WIND_UNIT_KEY_FOR_SYSTEM["US"])
    return i18n.t(f"unit_labels.{unit_key}", locale).strip()


def _wind_threshold(threshold_mph: float, unit_system: str) -> float:
    """Convert a GFE mph-calibrated wind threshold into *unit_system*.

    :data:`WIND_NULL_THRESHOLD`/:data:`WIND_GUST_DIFFERENCE` are mph
    constants from the GFE source. Callers passing forecast-period wind
    values that already arrived pre-converted to the operator's configured
    unit system (providers fetch in ``target_unit`` — see
    ``providers/forecast/*.py``) need the threshold converted forward into
    that same unit, rather than converting every value backward to mph.
    Uses the canonical conversion registry (``units/conversion.py``) per
    API-MANUAL.md's conversion-factor-accuracy rule, not a hand-rolled
    factor.
    """
    unit_key = _WIND_UNIT_KEY_FOR_SYSTEM.get(unit_system, _WIND_UNIT_KEY_FOR_SYSTEM["US"])
    return _convert_units(threshold_mph, "mile_per_hour", unit_key)


def wind_descriptor(speed_mph: float, locale: str) -> str:
    """Return the hybrid Beaufort/GFE descriptor for *speed_mph*.

    Looks up the first :data:`HYBRID_WIND_SCALE` row whose ``upper_bound``
    exceeds *speed_mph*; falls back to the final (open-ended) row for any
    value at or above the highest threshold.
    """
    for upper_bound, label_key in HYBRID_WIND_SCALE:
        if speed_mph < upper_bound:
            return _t(f"wind.{label_key}", locale)
    return _t(f"wind.{HYBRID_WIND_SCALE[-1][1]}", locale)


def wind_phrase(
    wind_min: float | None,
    wind_max: float | None,
    wind_gust: float | None,
    wind_dir: str | None,
    locale: str,
    unit_system: str = "US",
) -> str:
    """Compose a full wind phrase: direction + magnitude + optional gust.

    Algorithm (GFE SS3.1, `VectorRelatedPhrases.py`):

    - ``wind_max`` absent or below the (unit-converted) null threshold ->
      the null phrase ("light winds").
    - Magnitude: ``"around {max} {unit}"`` when min == max; ``"up to {max}
      {unit}"`` when min is absent or below the null threshold; otherwise
      ``"{min} to {max} {unit}"``.
    - Direction, when present, prefixes the magnitude phrase.
    - A gust qualifier is appended when the gust exceeds the sustained max
      by more than the (unit-converted) gust-difference threshold.

    *unit_system* (``"US"`` / ``"METRIC"`` / ``"METRICWX"``, default
    ``"US"``) controls both the ``{unit}`` label rendered in the locale
    templates and the unit :data:`WIND_NULL_THRESHOLD`/
    :data:`WIND_GUST_DIFFERENCE` are converted into before comparison —
    see :func:`_wind_unit_label` / :func:`_wind_threshold`. Callers must
    pass *wind_min*/*wind_max*/*wind_gust* already expressed in
    *unit_system*'s unit; the default ``"US"`` reproduces the pre-T7.1
    mph-only behavior exactly (identity conversion, "mph" label).
    """
    null_threshold = _wind_threshold(WIND_NULL_THRESHOLD, unit_system)
    gust_difference = _wind_threshold(WIND_GUST_DIFFERENCE, unit_system)
    unit = _wind_unit_label(unit_system, locale)

    if wind_max is None or wind_max < null_threshold:
        return _t_template("wind.null_phrase", "light winds", locale)

    if wind_min is not None and wind_max == wind_min:
        magnitude = _t_template(
            "wind.magnitude_around", "around {speed} {unit}", locale
        ).format(speed=_format_speed(wind_max, locale), unit=unit)
    elif wind_min is None or wind_min < null_threshold:
        magnitude = _t_template(
            "wind.magnitude_up_to", "up to {speed} {unit}", locale
        ).format(speed=_format_speed(wind_max, locale), unit=unit)
    else:
        magnitude = _t_template(
            "wind.magnitude_range", "{min} to {max} {unit}", locale
        ).format(
            min=_format_speed(wind_min, locale),
            max=_format_speed(wind_max, locale),
            unit=unit,
        )

    if wind_dir:
        phrase = _t_template("wind.with_direction", "{dir} winds {magnitude}", locale).format(
            dir=wind_dir, magnitude=magnitude
        )
    else:
        phrase = magnitude

    if wind_gust is not None and (wind_gust - wind_max) > gust_difference:
        gust_phrase = _t_template(
            "wind.gust_suffix", "with gusts to around {gust} {unit}", locale
        ).format(gust=_format_speed(wind_gust, locale), unit=unit)
        phrase = f"{phrase} {gust_phrase}"

    return phrase


def marine_wind_phrase(speed_kt: float, locale: str) -> str | None:
    """Return a marine wind descriptor (gales/storm force/hurricane force).

    Uses :data:`MARINE_WIND_DESCRIPTORS`, lower-bound inclusive: the
    highest threshold not exceeding *speed_kt* wins. Returns ``None`` below
    the lowest (gale-force) threshold.
    """
    label_key: str | None = None
    for lower_bound, key in MARINE_WIND_DESCRIPTORS:
        if speed_kt >= lower_bound:
            label_key = key
        else:
            break

    if label_key is None:
        return None
    # GFE key stems carry a trailing "_to" (a templating artifact meant to
    # prefix a following number, e.g. "gales to 40 kt") — strip it so the
    # standalone label reads as "Gales" rather than "Gales To".
    display_key = label_key.removesuffix("_to")
    return _t(f"wind.marine_{display_key}", locale)
