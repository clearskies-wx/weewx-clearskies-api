"""Sub-phrase connector strategies (ADR-082, GFE §5.2).

Ported faithfully from the NWS Graphical Forecast Editor (GFE) text
formatter source (public domain, 17 USC S105), `PhraseBuilder.py`
`scalarConnector`, `vectorConnector`, `wxConnector` plus
`ConfigVariables.py` `phrase_connector_dict`. See
`docs/reference/nws-text-system/gfe-source-code-analysis.md` §5.2 for the
full source analysis.

Connectors join adjacent sub-phrases within a single period's forecast
sentence (e.g. "Winds NW at 10 to 15 mph, becoming N in the afternoon").
All display strings resolve through `i18n.t()` — this module holds no
hardcoded English literals. Locale keys used here (``gfe.connector.*``)
fall back to the raw key string via `i18n.t()`'s resolution chain until
locale JSON entries are populated in a later I18N task.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n

# Wind magnitude (mph) at or above which a direction+magnitude change is
# treated as "high wind" for vector connector purposes (GFE
# `highValue_threshold_dict["Wind"]`, thresholds.py does not carry marine
# tables for this — kept local since it is connector-specific wiring, not
# a threshold table consumed elsewhere).
_HIGH_WIND_THRESHOLD: float = 45.0


def scalar_connector(prev_value: float, curr_value: float, element: str, locale: str) -> str:
    """Scalar connector between two adjacent sub-phrases (GFE `scalarConnector`).

    Resolves to "then", "increasing to", or "decreasing to" based on the
    comparison of *prev_value* and *curr_value*, with per-element overrides
    from `ConfigVariables.phrase_connector_dict`:
      - ``element == "Sky"``: always "then becoming" regardless of trend.
      - ``element == "WaveHeight"``: "building to" / "subsiding to" instead
        of "increasing to" / "decreasing to".
      - otherwise: "then" / "increasing to" / "decreasing to".
    """
    if element == "Sky":
        return i18n.t("gfe.connector.then_becoming", locale)

    if curr_value > prev_value:
        key = (
            "gfe.connector.building_to"
            if element == "WaveHeight"
            else "gfe.connector.increasing_to"
        )
        return i18n.t(key, locale)

    if curr_value < prev_value:
        key = (
            "gfe.connector.subsiding_to"
            if element == "WaveHeight"
            else "gfe.connector.decreasing_to"
        )
        return i18n.t(key, locale)

    return i18n.t("gfe.connector.then", locale)


def vector_connector(
    prev_dir: str, prev_mag: float, curr_dir: str, curr_mag: float, locale: str
) -> str:
    """Vector connector for wind (GFE `vectorConnector`), direction-aware.

    - Same direction, different magnitude: "increasing to" / "decreasing to".
    - Same magnitude, different direction: "shifting to the".
    - Both different, high wind (either magnitude >= `_HIGH_WIND_THRESHOLD`):
      "becoming {dir} and increasing/decreasing to" (direction interpolated
      by the caller into the returned template's ``{dir}`` placeholder).
    - Both different, normal wind: "increasing to" / "decreasing to" when
      magnitude changed, else "becoming".
    """
    same_dir = prev_dir == curr_dir
    same_mag = prev_mag == curr_mag

    if same_dir and not same_mag:
        key = (
            "gfe.connector.increasing_to"
            if curr_mag > prev_mag
            else "gfe.connector.decreasing_to"
        )
        return i18n.t(key, locale)

    if same_mag and not same_dir:
        return i18n.t("gfe.connector.shifting_to_the", locale)

    if not same_dir and not same_mag:
        high_wind = prev_mag >= _HIGH_WIND_THRESHOLD or curr_mag >= _HIGH_WIND_THRESHOLD
        if high_wind:
            key = (
                "gfe.connector.becoming_dir_and_increasing_to"
                if curr_mag > prev_mag
                else "gfe.connector.becoming_dir_and_decreasing_to"
            )
            return i18n.t(key, locale).format(dir=curr_dir)
        if curr_mag != prev_mag:
            key = (
                "gfe.connector.increasing_to"
                if curr_mag > prev_mag
                else "gfe.connector.decreasing_to"
            )
            return i18n.t(key, locale)
        return i18n.t("gfe.connector.becoming", locale)

    # same_dir and same_mag: no change to connect.
    return i18n.t("gfe.connector.then", locale)


def weather_connector(locale: str) -> str:
    """Weather sub-phrase connector (GFE `wxConnector`).

    Returns the ", then" connector used between weather sub-phrases within
    a period (the period-separating ". " form is a sentence-assembly
    concern handled by the caller, not by this per-connector lookup).
    """
    return i18n.t("gfe.connector.weather_then", locale)
