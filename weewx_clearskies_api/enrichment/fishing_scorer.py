"""Fishing conditions scoring processor (API-MANUAL.md §17 "Fishing scorer",
Phase 4 T4.2).

Registered against the fishing endpoint pipeline. Produces a
``FishingForecast`` for one spot for one time period from already-resolved
inputs (pressure trend, tide state, water temperature, solunar intensity —
all sourced upstream by their own providers/processors per API-MANUAL.md
§17). This module does not fetch data itself; it is a pure scoring
function plus a small habitat-feature helper.

**Four-component weighted scoring** (T6.2, 2026-07-11 — water temperature
removed from this shared base; see below):

    pressure trend (0.375) — 3-hour barometric delta
    tide state     (0.3125) — position in the tidal cycle
    solunar intensity (0.1875) — major/minor period + moon-phase intensity
    time of day    (0.125) — dawn/dusk/morning/night/midday

``overallScore`` is the weighted sum of these four components only — it is
species-agnostic (one score per period, not per species) and carries no
temperature signal. Per-species adjustment (pressure sensitivity,
temperature preference, tide preference, time-of-day preference, seasonal
behavior) is applied independently to each entry in ``speciesScores``,
starting from the same weighted base score. This split was confirmed with
the lead 2026-07-10: API-MANUAL.md §17's single-sentence formula ("Final
score = Σ(component × weight) × species_modifier × seasonal_multiplier")
describes the *per-species* computation, not a modifier on the shared
``overallScore``.

**Water temperature (T6.2, 2026-07-11):** Formerly a fifth component
(weight 0.20) scored against the target category's typical range and baked
into ``weighted_base`` — this double-counted temperature alongside the
per-species ``_species_temp_multiplier()`` adjustment below, and the
category-level ranges were coarser than the per-species profiles.
Temperature scoring now lives *only* in per-species scoring
(``fishing_species.SPECIES_PROFILES`` optimal/good/marginal ranges); the
former weight was redistributed proportionally across the remaining four
components (see ``_WEIGHT_*`` constants below). ``FishingForecast
.waterTempScore`` is always ``None`` in the response — it no longer has a
single species-agnostic value.

**Habitat features:** ``bathymetry.py`` (Phase 3, T3.1) already implements
``identify_habitat_features()`` against a ``list[BathymetryPoint]`` —
dropoff/ledge/reef/channel/pinnacle detection. This module does not
duplicate that logic; ``get_habitat_features()`` below is a thin wrapper
that accepts the wire-shaped ``list[dict]`` profile ``score_fishing()``
receives and normalizes the output to ``{"type", "distance_m", "depth_m"}``
per the brief. Note: ``FishingForecast`` (models/responses.py) has no
habitat-features field — habitat annotations are spot-level (static),
not period-level, so they don't belong on a per-period forecast object.
``get_habitat_features()`` is exposed as a standalone, independently
callable/testable function; ``score_fishing()`` accepts
``bathymetric_profile`` for signature fidelity with the brief but does not
thread it into the returned ``FishingForecast`` (flagged to the lead as an
FYI — if habitat annotations need a home on a response model, that's a
``models/responses.py`` change out of this task's scope).

**i18n (API-MANUAL.md §17 "Marine i18n"):** ``periodLabel`` and
``speciesScores[].status`` resolve through ``i18n.t()``; ``conditionsText``
is composed via locale-aware ``fishing.conditions.*`` templates (T4.4) —
per-species ``note`` remains plain English text (not marked "(locale)" in
the API-MANUAL.md field table, unlike ``status``, and out of this task's
scope). Locale keys referenced below (``_LOCALE_KEYS``) and the
``fishing.conditions.*`` composition template keys are wired into all 13
``locales/*.json`` files; non-English locales currently carry English
placeholder text pending translation (I18N T3.1 skeleton convention).
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Any

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import BathymetryPoint
from weewx_clearskies_api.enrichment.bathymetry import identify_habitat_features
from weewx_clearskies_api.enrichment.fishing_species import (
    SEASONAL_BEHAVIOR,
    SPECIES_PROFILES,
    SpeciesProfile,
)
from weewx_clearskies_api.models.responses import FishingForecast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locale keys referenced by this module (API-MANUAL.md §17 "Marine i18n").
#
# Documented here as the authoritative v1 English source; wired into
# locales/*.json (see "fishing" section). Not modified by this module — a
# separate mirror kept for at-a-glance reference alongside the scoring
# logic that consumes each key. Composition template keys
# ("fishing.conditions.*", T4.4) live only in locales/*.json, not
# duplicated here, since they are read via string templates rather than
# fixed keys.
# ---------------------------------------------------------------------------

_LOCALE_KEYS: dict[str, str] = {
    "fishing.period.early_morning": "Early Morning",
    "fishing.period.morning": "Morning",
    "fishing.period.midday": "Midday",
    "fishing.period.afternoon": "Afternoon",
    "fishing.period.evening": "Evening",
    "fishing.period.night": "Night",
    "fishing.species_status.active": "active",
    "fishing.species_status.less_active": "less active",
    "fishing.species_status.inactive": "inactive",
    "fishing.habitat.dropoff": "Drop-off",
    "fishing.habitat.ledge": "Ledge",
    "fishing.habitat.reef": "Reef",
    "fishing.habitat.channel": "Channel",
    "fishing.habitat.pinnacle": "Pinnacle",
    "fishing.solunar_caveat": (
        "Solunar theory suggests feeding activity correlates with moon "
        "position. Scientific evidence is mixed; environmental conditions "
        "(pressure, temperature, tides) have stronger research support."
    ),
}

# ---------------------------------------------------------------------------
# Component weights (API-MANUAL.md §17 "Fishing scorer").
#
# Water temperature is no longer a component of the shared weighted_base —
# it is scored purely per-species (via _species_temp_multiplier(), using
# each species' own SPECIES_PROFILES temp ranges) to avoid double-counting
# temperature once at the category level and again per species. The former
# _WEIGHT_WATER_TEMP (0.20) has been redistributed proportionally across
# the remaining four weights so they still sum to 1.0:
#   original: pressure .30, tide .25, solunar .15, time_of_day .10 (sum .80)
#   factor = 1.0 / .80 = 1.25
#   pressure .30*1.25=.375, tide .25*1.25=.3125, solunar .15*1.25=.1875,
#   time_of_day .10*1.25=.125 (sum 1.0)
# ---------------------------------------------------------------------------

_WEIGHT_PRESSURE = 0.375
_WEIGHT_TIDE = 0.3125
_WEIGHT_SOLUNAR = 0.1875
_WEIGHT_TIME_OF_DAY = 0.125

# ---------------------------------------------------------------------------
# Pressure trend component
# ---------------------------------------------------------------------------

_PRESSURE_RAPID_DROP_THRESHOLD_HPA = -3.0
_PRESSURE_FALLING_THRESHOLD_HPA = -1.0
_PRESSURE_STABLE_THRESHOLD_HPA = 1.0
_PRESSURE_RISING_THRESHOLD_HPA = 3.0

_PRESSURE_SCORE_RAPID_DROP = 100
_PRESSURE_SCORE_FALLING = 80
_PRESSURE_SCORE_STABLE = 50
_PRESSURE_SCORE_RISING_SLOW = 30
_PRESSURE_SCORE_RISING_RAPID = 20
_PRESSURE_SCORE_NEUTRAL = 50  # None/unknown


def _score_pressure(trend_hpa_3hr: float | None) -> int:
    """Score the 3-hour pressure trend (magnitude thresholds, both signs).

    Boundary convention (v1, this module): a magnitude exactly at a
    threshold falls into the *less extreme* bucket (e.g. exactly -3.0 hPa
    is "falling", not "rapid drop") — consistent with the brief's ">3
    hPa/3hr" strict-inequality wording for the rapid buckets.
    """
    if trend_hpa_3hr is None:
        return _PRESSURE_SCORE_NEUTRAL
    if trend_hpa_3hr < _PRESSURE_RAPID_DROP_THRESHOLD_HPA:
        return _PRESSURE_SCORE_RAPID_DROP
    if trend_hpa_3hr < _PRESSURE_FALLING_THRESHOLD_HPA:
        return _PRESSURE_SCORE_FALLING
    if trend_hpa_3hr <= _PRESSURE_STABLE_THRESHOLD_HPA:
        return _PRESSURE_SCORE_STABLE
    if trend_hpa_3hr <= _PRESSURE_RISING_THRESHOLD_HPA:
        return _PRESSURE_SCORE_RISING_SLOW
    return _PRESSURE_SCORE_RISING_RAPID


# ---------------------------------------------------------------------------
# Tide state component
# ---------------------------------------------------------------------------

_TIDE_SCORES: dict[str, int] = {
    "outgoing": 100,
    "incoming": 80,
    "peak_flow": 70,
    "slack_high": 30,
    "slack_low": 20,
}
_TIDE_SCORE_DEFAULT = 50  # unrecognized tide_state; neutral fallback


def _score_tide(tide_state: str) -> int:
    if tide_state not in _TIDE_SCORES:
        logger.warning(
            "fishing_scorer: unrecognized tide_state=%r; using neutral score.", tide_state
        )
        return _TIDE_SCORE_DEFAULT
    return _TIDE_SCORES[tide_state]


# ---------------------------------------------------------------------------
# Water temperature is intentionally NOT a component here. It was formerly
# scored at the category level via _score_water_temp()/_CATEGORY_TEMP_RANGES_F
# and baked into weighted_base — that double-counted temperature alongside
# the per-species _species_temp_multiplier() below, and the category-level
# ranges were coarser than the per-species profiles. Removed (T6.2,
# 2026-07-11): temperature scoring now lives solely in _score_one_species()
# via fishing_species.SPECIES_PROFILES per-species temp ranges.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Solunar component
# ---------------------------------------------------------------------------

_SOLUNAR_PEAK_INTENSITY_THRESHOLD = 1.0  # new/full moon intensity
_SOLUNAR_SCORE_MAJOR_PEAK = 100
_SOLUNAR_SCORE_MAJOR_OTHER = 80
_SOLUNAR_SCORE_MINOR = 60
_SOLUNAR_SCORE_NONE = 30


def _score_solunar(
    is_during_major_period: bool, is_during_minor_period: bool, solunar_intensity: float
) -> int:
    if is_during_major_period:
        if solunar_intensity >= _SOLUNAR_PEAK_INTENSITY_THRESHOLD:
            return _SOLUNAR_SCORE_MAJOR_PEAK
        return _SOLUNAR_SCORE_MAJOR_OTHER
    if is_during_minor_period:
        return _SOLUNAR_SCORE_MINOR
    return _SOLUNAR_SCORE_NONE


# ---------------------------------------------------------------------------
# Time-of-day component
# ---------------------------------------------------------------------------

_DAWN_DUSK_WINDOW_HOURS = 1.0
_NIGHT_BUFFER_HOURS = 2.0
_NOON_HOUR = 12.0

_TIME_SCORE_DAWN = 100
_TIME_SCORE_DUSK = 90
_TIME_SCORE_MORNING = 70
_TIME_SCORE_NIGHT = 50
_TIME_SCORE_MIDDAY = 30
_TIME_SCORE_UNKNOWN = 50  # no sunrise/sunset data available

_TIME_BUCKET_SCORES: dict[str, int] = {
    "dawn": _TIME_SCORE_DAWN,
    "dusk": _TIME_SCORE_DUSK,
    "morning": _TIME_SCORE_MORNING,
    "night": _TIME_SCORE_NIGHT,
    "midday": _TIME_SCORE_MIDDAY,
    "unknown": _TIME_SCORE_UNKNOWN,
}


def _parse_hour_fraction(iso_value: str | None) -> float | None:
    """Extract the fractional UTC hour (0.0-23.999...) from an ISO-8601 timestamp."""
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def _circular_hour_diff(a: float, b: float) -> float:
    """Shortest distance between two hours on a 24-hour clock."""
    diff = abs(a - b) % 24.0
    return min(diff, 24.0 - diff)


def _hour_in_window(hour: float, start: float, end: float) -> bool:
    """Whether *hour* falls in [start, end] on a 24-hour clock, wrapping past midnight."""
    s, e, h = start % 24.0, end % 24.0, hour % 24.0
    if s <= e:
        return s <= h <= e
    return h >= s or h <= e


def _time_bucket(hour_utc: int, sunrise_utc: str | None, sunset_utc: str | None) -> str:
    """Classify *hour_utc* into dawn/dusk/morning/night/midday/unknown.

    "unknown" when sunrise/sunset data is unavailable — the neutral time
    score is used in that case, matching the None/unknown fallback pattern
    used by the pressure and water-temperature components.
    """
    sunrise_h = _parse_hour_fraction(sunrise_utc)
    sunset_h = _parse_hour_fraction(sunset_utc)
    if sunrise_h is None or sunset_h is None:
        return "unknown"

    if _circular_hour_diff(hour_utc, sunrise_h) <= _DAWN_DUSK_WINDOW_HOURS:
        return "dawn"
    if _circular_hour_diff(hour_utc, sunset_h) <= _DAWN_DUSK_WINDOW_HOURS:
        return "dusk"
    if _hour_in_window(hour_utc, sunrise_h + 1.0, _NOON_HOUR):
        return "morning"
    if _hour_in_window(hour_utc, sunset_h + _NIGHT_BUFFER_HOURS, sunrise_h - _NIGHT_BUFFER_HOURS):
        return "night"
    return "midday"  # catch-all: noon-to-(sunset-3) plus any residual gap hours


def _score_time_of_day(hour_utc: int, sunrise_utc: str | None, sunset_utc: str | None) -> int:
    return _TIME_BUCKET_SCORES[_time_bucket(hour_utc, sunrise_utc, sunset_utc)]


# ---------------------------------------------------------------------------
# Period label (API-MANUAL.md §17 "fishing.period.<value>", 6 labels)
# ---------------------------------------------------------------------------


def _period_label_key(hour_utc: int) -> str:
    h = hour_utc % 24
    if 5 <= h <= 7:
        return "early_morning"
    if 8 <= h <= 10:
        return "morning"
    if 11 <= h <= 14:
        return "midday"
    if 15 <= h <= 17:
        return "afternoon"
    if 18 <= h <= 20:
        return "evening"
    return "night"  # 21-23, 0-4


# ---------------------------------------------------------------------------
# Species scoring
# ---------------------------------------------------------------------------

_DEFAULT_SPECIES_PROFILE: SpeciesProfile = {
    "pressure_sensitivity": 0.5,
    "temp_optimal": (60.0, 75.0),
    "temp_good": (52.0, 82.0),
    "temp_marginal": (45.0, 88.0),
    "tide_preference": "any",
    "tide_multiplier": 1.0,
    "time_preference": "any",
    "time_multiplier": 1.0,
}

_SPECIES_STATUS_ACTIVE_THRESHOLD = 60
_SPECIES_STATUS_LESS_ACTIVE_THRESHOLD = 30

_SPECIES_TEMP_MULTIPLIER_OPTIMAL = 1.0
_SPECIES_TEMP_MULTIPLIER_GOOD = 0.8
_SPECIES_TEMP_MULTIPLIER_MARGINAL = 0.5
_SPECIES_TEMP_MULTIPLIER_INACTIVE = 0.1
_SPECIES_TEMP_MULTIPLIER_UNKNOWN = 1.0  # no adjustment when water_temp_f is None


def _get_profile(species_name: str) -> SpeciesProfile:
    return SPECIES_PROFILES.get(species_name.strip().lower(), _DEFAULT_SPECIES_PROFILE)


def _species_temp_multiplier(profile: SpeciesProfile, water_temp_f: float | None) -> float:
    if water_temp_f is None:
        return _SPECIES_TEMP_MULTIPLIER_UNKNOWN
    lo, hi = profile["temp_optimal"]
    if lo <= water_temp_f <= hi:
        return _SPECIES_TEMP_MULTIPLIER_OPTIMAL
    lo, hi = profile["temp_good"]
    if lo <= water_temp_f <= hi:
        return _SPECIES_TEMP_MULTIPLIER_GOOD
    lo, hi = profile["temp_marginal"]
    if lo <= water_temp_f <= hi:
        return _SPECIES_TEMP_MULTIPLIER_MARGINAL
    return _SPECIES_TEMP_MULTIPLIER_INACTIVE


def _species_tide_multiplier(profile: SpeciesProfile, tide_state: str) -> float:
    pref = profile["tide_preference"]
    if pref == "any":
        return 1.0
    slack_states = ("slack_high", "slack_low")
    matched = (tide_state in slack_states) if pref == "slack" else (tide_state == pref)
    return profile["tide_multiplier"] if matched else 1.0


def _species_time_multiplier(profile: SpeciesProfile, time_bucket: str) -> float:
    pref = profile["time_preference"]
    if pref == "any" or time_bucket != pref:
        return 1.0
    return profile["time_multiplier"]


def _closed_season_note(species_name: str, month: int) -> str:
    """Compose "Closed season (Jun-Aug)" from the contiguous closed months."""
    entries = SEASONAL_BEHAVIOR.get(species_name, {})
    closed_months = sorted(m for m, e in entries.items() if e.get("closed"))
    if not closed_months:
        return "Closed season"
    if closed_months == list(range(closed_months[0], closed_months[-1] + 1)):
        start, end = calendar.month_abbr[closed_months[0]], calendar.month_abbr[closed_months[-1]]
        return f"Closed season ({start}-{end})" if start != end else f"Closed season ({start})"
    names = ", ".join(calendar.month_abbr[m] for m in closed_months)
    return f"Closed season ({names})"


def _seasonal_adjustment(species_name: str, month: int) -> tuple[float, str | None]:
    """Return (multiplier, note) for *species_name* in *month*.

    multiplier: 0.0 if the season is closed (regulatory closure — score
    zeroed regardless of environmental conditions), spawning/pre-spawn
    multiplier if configured, else 1.0 (no adjustment, no note).
    """
    entry = SEASONAL_BEHAVIOR.get(species_name, {}).get(month)
    if not entry:
        return 1.0, None
    if entry.get("closed"):
        return 0.0, _closed_season_note(species_name, month)
    if "spawning_multiplier" in entry:
        return entry["spawning_multiplier"], "Spawning run — peak activity"
    if "pre_spawn_multiplier" in entry:
        return entry["pre_spawn_multiplier"], "Pre-spawn — building activity"
    return 1.0, None


def _species_status_key(score: int) -> str:
    if score >= _SPECIES_STATUS_ACTIVE_THRESHOLD:
        return "active"
    if score >= _SPECIES_STATUS_LESS_ACTIVE_THRESHOLD:
        return "less_active"
    return "inactive"


def _score_one_species(
    species_name: str,
    weighted_base_score: float,
    water_temp_f: float | None,
    tide_state: str,
    time_bucket: str,
    month: int,
    locale: str,
) -> dict[str, Any]:
    """Score one species: weighted base * (pressure sensitivity * temp *
    tide preference * time preference * seasonal) multiplier chain,
    clamped to 0-100 (API-MANUAL.md §17 species profile scoring).
    """
    profile = _get_profile(species_name)
    species_key = species_name.strip().lower()

    score = weighted_base_score
    score *= profile["pressure_sensitivity"]
    score *= _species_temp_multiplier(profile, water_temp_f)
    score *= _species_tide_multiplier(profile, tide_state)
    score *= _species_time_multiplier(profile, time_bucket)
    seasonal_mult, note = _seasonal_adjustment(species_key, month)
    score *= seasonal_mult

    final_score = max(0, min(100, round(max(0.0, min(100.0, score)))))
    status_key = _species_status_key(final_score)

    result: dict[str, Any] = {
        "name": species_name.strip().title(),
        "score": final_score,
        "status": i18n.t(f"fishing.species_status.{status_key}", locale),
    }
    if note is not None:
        result["note"] = note
    return result


# ---------------------------------------------------------------------------
# Habitat features (delegates to bathymetry.identify_habitat_features —
# see module docstring for why this is a standalone function rather than a
# FishingForecast field)
# ---------------------------------------------------------------------------


def get_habitat_features(bathymetric_profile: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Detect fishing-relevant seafloor features from a bathymetric profile.

    Thin wrapper around ``bathymetry.identify_habitat_features()``:
    converts the wire-shaped ``list[dict]`` profile into
    ``list[BathymetryPoint]`` and normalizes the output to
    ``{"type", "distance_m", "depth_m"}`` per the brief. Returns ``[]`` for
    ``None``/empty input.
    """
    if not bathymetric_profile:
        return []
    points = [BathymetryPoint(p) for p in bathymetric_profile]
    features = identify_habitat_features(points)
    return [
        {"type": f["type"], "distance_m": f["distance_m"], "depth_m": f["depth_m"]}
        for f in features
    ]


# ---------------------------------------------------------------------------
# Conditions text composition
# ---------------------------------------------------------------------------

_OVERALL_GREAT_THRESHOLD = 70
_OVERALL_GOOD_THRESHOLD = 50
_OVERALL_FAIR_THRESHOLD = 30


def _overall_label(overall_score: int, locale: str) -> str:
    if overall_score >= _OVERALL_GREAT_THRESHOLD:
        key = "fishing.conditions.overall_great"
    elif overall_score >= _OVERALL_GOOD_THRESHOLD:
        key = "fishing.conditions.overall_good"
    elif overall_score >= _OVERALL_FAIR_THRESHOLD:
        key = "fishing.conditions.overall_fair"
    else:
        key = "fishing.conditions.overall_poor"
    return i18n.t(key, locale)


def _pressure_phrase(trend_hpa_3hr: float | None, locale: str) -> str:
    if trend_hpa_3hr is None:
        return i18n.t("fishing.conditions.pressure_unavailable", locale)
    score = _score_pressure(trend_hpa_3hr)
    key = {
        _PRESSURE_SCORE_RAPID_DROP: "fishing.conditions.pressure_rapid_drop",
        _PRESSURE_SCORE_FALLING: "fishing.conditions.pressure_falling",
        _PRESSURE_SCORE_STABLE: "fishing.conditions.pressure_stable",
        _PRESSURE_SCORE_RISING_SLOW: "fishing.conditions.pressure_rising_slow",
        _PRESSURE_SCORE_RISING_RAPID: "fishing.conditions.pressure_rising_rapid",
    }[score]
    return i18n.t(key, locale)


_TIDE_PHRASE_KEYS: dict[str, str] = {
    "outgoing": "fishing.conditions.tide_outgoing",
    "incoming": "fishing.conditions.tide_incoming",
    "peak_flow": "fishing.conditions.tide_peak_flow",
    "slack_high": "fishing.conditions.tide_slack_high",
    "slack_low": "fishing.conditions.tide_slack_low",
}


def _tide_phrase(tide_state: str, locale: str) -> str:
    key = _TIDE_PHRASE_KEYS.get(tide_state, "fishing.conditions.tide_uncertain")
    return i18n.t(key, locale)


def _join_names(names: list[str], locale: str) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        connector = i18n.t("fishing.conditions.list_and", locale)
        return f"{names[0]}{connector}{names[1]}"
    separator = i18n.t("fishing.conditions.list_separator", locale)
    final_connector = i18n.t("fishing.conditions.list_final_and", locale)
    return separator.join(names[:-1]) + f"{final_connector}{names[-1]}"


def _compose_conditions_text(
    *,
    overall_score: int,
    pressure_trend_hpa_3hr: float | None,
    tide_state: str,
    is_during_major_period: bool,
    is_during_minor_period: bool,
    active_species_names: list[str],
    locale: str,
) -> str:
    """Compose a natural-language conditions summary (API-MANUAL.md §17).

    Example: "Good conditions — falling pressure with outgoing tide during
    a major solunar period. Redfish and Flounder active."
    Sentence templates resolve through i18n.t() (``fishing.conditions.*``
    keys, API-MANUAL.md §17 "Marine i18n", T4.4) and are filled in with
    ``str.format()`` — overall/pressure/tide phrase lookups and list
    connectors all route through i18n.t() via the helpers above; only the
    interpolation slots (phrase substitution, species names) remain in the
    composed string.
    """
    if is_during_major_period:
        solunar_clause = i18n.t("fishing.conditions.solunar_major", locale)
    elif is_during_minor_period:
        solunar_clause = i18n.t("fishing.conditions.solunar_minor", locale)
    else:
        solunar_clause = ""

    text = i18n.t("fishing.conditions.summary", locale).format(
        overall_label=_overall_label(overall_score, locale),
        pressure_phrase=_pressure_phrase(pressure_trend_hpa_3hr, locale),
        tide_phrase=_tide_phrase(tide_state, locale),
        solunar_clause=solunar_clause,
    )
    if active_species_names:
        text += " " + i18n.t("fishing.conditions.species_active", locale).format(
            names=_join_names(active_species_names, locale)
        )
    return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def score_fishing(
    *,
    pressure_hpa: float | None,
    pressure_trend_hpa_3hr: float | None,
    tide_state: str,
    water_temp_f: float | None,
    hour_utc: int,
    sunrise_utc: str | None,
    sunset_utc: str | None,
    solunar_intensity: float,
    is_during_major_period: bool,
    is_during_minor_period: bool,
    species: list[str],
    target_category: str,
    month: int,
    bathymetric_profile: list[dict[str, Any]] | None = None,
    wind_speed: float | None = None,
    wind_direction: float | None = None,
    wind_gust: float | None = None,
    swell_height: float | None = None,
    swell_period: float | None = None,
    period_start_utc: str | None = None,
    period_end_utc: str | None = None,
    locale: str | None = None,
) -> FishingForecast:
    """Score fishing conditions and produce a FishingForecast model instance.

    Args:
        pressure_hpa: current barometric pressure; informational only (not
            an input to any component score — only the 3-hour trend is
            scored).
        pressure_trend_hpa_3hr: signed 3-hour pressure delta in hPa;
            negative = falling. None if unavailable (neutral score).
        tide_state: one of "incoming", "outgoing", "slack_high",
            "slack_low", "peak_flow".
        water_temp_f: water temperature in degrees Fahrenheit, or None.
        hour_utc: UTC hour (0-23) of the forecast period.
        sunrise_utc, sunset_utc: ISO-8601 UTC timestamps for the forecast
            day; used only for their time-of-day (hour) component.
        solunar_intensity: 0.0-1.0, from enrichment/solunar.py.
        is_during_major_period, is_during_minor_period: solunar period
            flags, from enrichment/solunar.py.
        species: species names to score individually (matched
            case-insensitively against fishing_species.SPECIES_PROFILES;
            species without a profile get a neutral default).
        target_category: one of "saltwater_inshore", "bottom_fish",
            "freshwater_sport", "salmonids" (config/marine_config.py
            FishingSpotConfig.target_categories). Retained for signature
            compatibility; not used in scoring — temperature is per-species
            via SPECIES_PROFILES (T6.2, 2026-07-11).
        month: 1-12; drives per-species seasonal adjustment.
        bathymetric_profile: accepted for signature fidelity with the
            brief; not threaded into the returned FishingForecast (see
            module docstring). Use get_habitat_features() directly.
        wind_speed, wind_direction, wind_gust, swell_height, swell_period:
            informational passthrough fields, not scored.
        period_start_utc, period_end_utc: ISO-8601 UTC period bounds. The
            brief's score_fishing() signature had no date/period-window
            input (only a bare hour_utc), so these were added as optional
            kwargs (confirmed with lead 2026-07-10) — default to "" so the
            function remains testable standalone without a caller
            supplying real timestamps; FishingForecast.periodStart/End are
            non-nullable str.
        locale: BCP-47 locale code. When None, resolves to
            i18n.get_active_locale() (API-MANUAL.md §17 "Marine i18n").
    """
    loc = locale or i18n.get_active_locale()

    pressure_score = _score_pressure(pressure_trend_hpa_3hr)
    tide_score = _score_tide(tide_state)
    solunar_score = _score_solunar(
        is_during_major_period, is_during_minor_period, solunar_intensity
    )
    time_score = _score_time_of_day(hour_utc, sunrise_utc, sunset_utc)

    weighted_base = (
        pressure_score * _WEIGHT_PRESSURE
        + tide_score * _WEIGHT_TIDE
        + solunar_score * _WEIGHT_SOLUNAR
        + time_score * _WEIGHT_TIME_OF_DAY
    )
    overall_score = max(0, min(100, round(weighted_base)))

    time_bucket = _time_bucket(hour_utc, sunrise_utc, sunset_utc)

    species_scores = [
        _score_one_species(name, weighted_base, water_temp_f, tide_state, time_bucket, month, loc)
        for name in species
    ]

    active_names = [
        s["name"] for s in species_scores if s["score"] >= _SPECIES_STATUS_ACTIVE_THRESHOLD
    ]
    conditions_text = _compose_conditions_text(
        overall_score=overall_score,
        pressure_trend_hpa_3hr=pressure_trend_hpa_3hr,
        tide_state=tide_state,
        is_during_major_period=is_during_major_period,
        is_during_minor_period=is_during_minor_period,
        active_species_names=active_names,
        locale=loc,
    )

    period_label = i18n.t(f"fishing.period.{_period_label_key(hour_utc)}", loc)

    return FishingForecast(
        periodStart=period_start_utc or "",
        periodEnd=period_end_utc or "",
        periodLabel=period_label,
        overallScore=overall_score,
        pressureScore=pressure_score,
        tideScore=tide_score,
        solunarScore=solunar_score,
        waterTempScore=None,
        timeofdayScore=time_score,
        speciesScores=species_scores or None,
        conditionsText=conditions_text,
        windSpeed=wind_speed,
        windDirection=wind_direction,
        windGust=wind_gust,
        swellHeight=swell_height,
        swellPeriod=swell_period,
    )
