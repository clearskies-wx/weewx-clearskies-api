"""Surf quality scoring processor (API-MANUAL.md §17 "Surf quality scorer").

Registered against the surf endpoint pipeline. Runs after
``enrichment/wave_transform.py`` has applied the NWPS bathymetric/structure/
topographic supplements (API-MANUAL.md §17 "NWPS supplement processor") —
this module scores whatever wave data it is handed and does not itself
correct for shoaling, refraction, or site-specific breaker physics.

Four weighted scoring factors combine into a 1-5 star rating:

    wave height   (0.35) — larger, within a rideable range, scores higher
    wave period   (0.35) — longer period = cleaner, more powerful waves
    wind quality  (0.20) — offshore wind holds the wave face up; onshore
                            wind blows it down
    swell dominance (0.10) — a clean, single dominant swell system scores
                              higher than a confused wind-chop sea

The composite score is then multiplied by three independent filters: beach
angle alignment (how directly the swell hits the beach), the operator's
directional exposure config (some directions are physically blocked by
headlands/bathymetry and score near zero), and an optional dawn/afternoon
time-of-day adjustment.

**i18n (API-MANUAL.md §17 "Marine i18n"):** every human-readable output
field resolves through ``i18n.t()`` — quality label, wind quality label, and
the composed conditions text. Per the manual, the output function accepts
``locale: str | None = None`` and resolves internally via
``locale or i18n.get_active_locale()``. The quality/wind-quality keys
below (``_LOCALE_KEYS``) and the ``surf.conditions.*`` composition
template keys used by ``_compose_conditions_text()`` (T4.4) are wired into
all 13 ``locales/*.json`` files; non-English locales currently carry
English placeholder text pending translation (I18N T3.1 skeleton
convention).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import SurfSpotConfig
from weewx_clearskies_api.models.responses import SpectralWaveComponent, SurfForecast
from weewx_clearskies_api.units.conversion import convert

# ---------------------------------------------------------------------------
# Locale keys referenced by this module (API-MANUAL.md §17 "Marine i18n").
#
# Documented here as the authoritative v1 English source; wired into
# locales/*.json (see "surf" section). Not modified by this module — a
# separate mirror kept for at-a-glance reference alongside the scoring
# logic that consumes each key. Composition template keys
# ("surf.conditions.*", T4.4) live only in locales/*.json, not duplicated
# here, since they are read via string templates rather than fixed keys.
# ---------------------------------------------------------------------------

_LOCALE_KEYS: dict[str, str] = {
    "surf.quality.1": "Poor",
    "surf.quality.2": "Fair",
    "surf.quality.3": "Good",
    "surf.quality.4": "Very Good",
    "surf.quality.5": "Epic",
    "surf.wind_quality.offshore": "Offshore",
    "surf.wind_quality.cross_offshore": "Cross-offshore",
    "surf.wind_quality.cross": "Cross-shore",
    "surf.wind_quality.cross_onshore": "Cross-onshore",
    "surf.wind_quality.onshore": "Onshore",
    "surf.wind_quality.glassy": "Glassy",
}

# ---------------------------------------------------------------------------
# Scoring weights (API-MANUAL.md §17 "Surf quality scorer")
# ---------------------------------------------------------------------------

_WEIGHT_HEIGHT = 0.35
_WEIGHT_PERIOD = 0.35
_WEIGHT_WIND = 0.20
_WEIGHT_SWELL = 0.10

# ---------------------------------------------------------------------------
# Wave height component — range lookup, height in feet (upper bound inclusive)
# ---------------------------------------------------------------------------

_WAVE_HEIGHT_RANGES_FT: list[tuple[float, float]] = [
    (0.5, 0.1),
    (1.0, 0.3),
    (1.5, 0.5),
    (3.0, 0.8),
    (6.0, 1.0),
    (10.0, 0.8),
    (15.0, 0.6),
    (math.inf, 0.2),
]

# ---------------------------------------------------------------------------
# Wave period component — range lookup, period in seconds (upper inclusive)
# ---------------------------------------------------------------------------

_WAVE_PERIOD_RANGES_S: list[tuple[float, float]] = [
    (6.0, 0.2),
    (8.0, 0.4),
    (10.0, 0.6),
    (12.0, 0.8),
    (16.0, 1.0),
    (18.0, 0.9),
    (math.inf, 0.8),
]

_PERIOD_MULTIPLIER_LONG_S = 18.0  # 18+s -> boost
_PERIOD_MULTIPLIER_SHORT_S = 8.0  # <8s -> heavy penalty
_PERIOD_MULTIPLIER_LONG = 1.5
_PERIOD_MULTIPLIER_SHORT = 0.1
_PERIOD_MULTIPLIER_DEFAULT = 1.0  # includes the documented 12-14s x1.0 case

# ---------------------------------------------------------------------------
# Wind quality component (API-MANUAL.md §17)
# ---------------------------------------------------------------------------

_WIND_LIGHT_MPH = 10.0
_WIND_STRONG_MPH = 20.0
_WIND_GLASSY_MPH = 5.0

_WIND_OFFSHORE_LIGHT = 1.2
_WIND_OFFSHORE_MODERATE = 1.0
_WIND_OFFSHORE_STRONG = 0.7
_WIND_CROSS_SHORE = 0.8
_WIND_ONSHORE_LIGHT = 0.7
_WIND_ONSHORE_MODERATE = 0.5
_WIND_ONSHORE_STRONG = 0.3
_WIND_GLASSY = 1.1
_WIND_NO_DATA = 0.5  # neutral fallback when wind speed/direction unavailable

# Wind-angle bands (degrees between wind direction and beach-facing direction,
# normalized to [0, 180]).  135-180 = offshore (wind blows from land to sea,
# i.e. FROM roughly the opposite compass point of the beach-facing direction).
# 0-45 = onshore.  45-135 = cross-shore, subdivided into thirds for the
# cross_offshore / cross / cross_onshore labels.
_ANGLE_OFFSHORE_MIN = 135.0
_ANGLE_ONSHORE_MAX = 45.0
_ANGLE_CROSS_OFFSHORE_MIN = 105.0
_ANGLE_CROSS_ONSHORE_MAX = 75.0

# ---------------------------------------------------------------------------
# Swell dominance component
# ---------------------------------------------------------------------------

_SWELL_PERIOD_THRESHOLD_S = 10.0  # component period > this counts as "swell"
_SWELL_DOMINANCE_PURE_RATIO = 0.8
_SWELL_DOMINANCE_MIXED_RATIO = 0.5
_SWELL_DOMINANCE_PURE_SCORE = 1.0
_SWELL_DOMINANCE_MIXED_SCORE = 0.6
_SWELL_DOMINANCE_CHOP_SCORE = 0.2
_SWELL_DOMINANCE_DEFAULT = 0.5  # no spectral data available

# ---------------------------------------------------------------------------
# Multi-swell integration thresholds
# ---------------------------------------------------------------------------

_PRIMARY_DOMINANT_RATIO = 0.75  # primary > this fraction of total energy
_SECONDARY_SIGNIFICANT_RATIO = 0.5  # secondary > this fraction of primary energy

# ---------------------------------------------------------------------------
# Beach angle alignment (angle between swell direction and beach-facing
# direction, normalized to [0, 180]; upper bound inclusive)
# ---------------------------------------------------------------------------

_BEACH_ALIGNMENT_RANGES: list[tuple[float, float]] = [
    (15.0, 1.0),
    (30.0, 0.8),
    (45.0, 0.6),
    (60.0, 0.3),
    (math.inf, 0.1),
]

# ---------------------------------------------------------------------------
# Directional exposure filter
# ---------------------------------------------------------------------------

_DIRECTIONAL_FILTER_BLOCKED = 0.1
_DIRECTIONAL_FILTER_OPEN = 1.0
_COMPASS_DIRECTIONS: tuple[str, ...] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# ---------------------------------------------------------------------------
# Time-of-day adjustment
# ---------------------------------------------------------------------------

_DAWN_WINDOW_HOURS = 1.0
_DAWN_MULTIPLIER = 1.1
_AFTERNOON_MULTIPLIER = 0.9
_TIME_MULTIPLIER_DEFAULT = 1.0
_AFTERNOON_START_HOUR = 14  # 2pm
_AFTERNOON_END_HOUR = 17  # 5pm


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _range_lookup(value: float, ranges: list[tuple[float, float]]) -> float:
    """Return the score for the first bracket whose upper bound >= *value*."""
    for upper, score in ranges:
        if value <= upper:
            return score
    return ranges[-1][1]


def _score_wave_height(height_m: float) -> float:
    """Score wave height (meters in, lookup performed in feet)."""
    height_ft = convert(height_m, "meter", "foot") or 0.0
    return _range_lookup(height_ft, _WAVE_HEIGHT_RANGES_FT)


def _period_multiplier(period_s: float) -> float:
    if period_s >= _PERIOD_MULTIPLIER_LONG_S:
        return _PERIOD_MULTIPLIER_LONG
    if period_s < _PERIOD_MULTIPLIER_SHORT_S:
        return _PERIOD_MULTIPLIER_SHORT
    return _PERIOD_MULTIPLIER_DEFAULT


def _score_wave_period(period_s: float) -> float:
    base = _range_lookup(period_s, _WAVE_PERIOD_RANGES_S)
    return base * _period_multiplier(period_s)


def _normalize_angle_diff(a: float, b: float) -> float:
    """Circular difference between two compass bearings, in [0, 180]."""
    diff = abs(a - b) % 360.0
    return 360.0 - diff if diff > 180.0 else diff


def _wind_quality(
    wind_speed: float | None,
    wind_direction: float | None,
    beach_facing_degrees: float,
) -> tuple[float, str]:
    """Score wind quality and return (score, label_key).

    label_key is one of: offshore, cross_offshore, cross, cross_onshore,
    onshore, glassy — the suffix of the ``surf.wind_quality.*`` locale keys.
    """
    if wind_speed is None or wind_direction is None:
        return _WIND_NO_DATA, "cross"

    speed_mph = convert(wind_speed, "meter_per_second", "mile_per_hour") or 0.0

    if speed_mph < _WIND_GLASSY_MPH:
        return _WIND_GLASSY, "glassy"

    angle = _normalize_angle_diff(wind_direction, beach_facing_degrees)

    if angle >= _ANGLE_OFFSHORE_MIN:
        if speed_mph < _WIND_LIGHT_MPH:
            return _WIND_OFFSHORE_LIGHT, "offshore"
        if speed_mph <= _WIND_STRONG_MPH:
            return _WIND_OFFSHORE_MODERATE, "offshore"
        return _WIND_OFFSHORE_STRONG, "offshore"

    if angle <= _ANGLE_ONSHORE_MAX:
        if speed_mph < _WIND_LIGHT_MPH:
            return _WIND_ONSHORE_LIGHT, "onshore"
        if speed_mph <= _WIND_STRONG_MPH:
            return _WIND_ONSHORE_MODERATE, "onshore"
        return _WIND_ONSHORE_STRONG, "onshore"

    # Cross-shore band (45-135deg), subdivided into thirds for the label.
    if angle >= _ANGLE_CROSS_OFFSHORE_MIN:
        label = "cross_offshore"
    elif angle <= _ANGLE_CROSS_ONSHORE_MAX:
        label = "cross_onshore"
    else:
        label = "cross"
    return _WIND_CROSS_SHORE, label


def _total_energy(components: list[dict[str, Any]]) -> float:
    return sum(float(c.get("energy", 0.0)) for c in components)


def _swell_dominance(spectral_components: list[dict[str, Any]] | None) -> float:
    if not spectral_components:
        return _SWELL_DOMINANCE_DEFAULT

    total = _total_energy(spectral_components)
    if total <= 0:
        return _SWELL_DOMINANCE_DEFAULT

    swell_energy = sum(
        float(c.get("energy", 0.0))
        for c in spectral_components
        if float(c.get("period", 0.0)) > _SWELL_PERIOD_THRESHOLD_S
    )
    ratio = swell_energy / total

    if ratio > _SWELL_DOMINANCE_PURE_RATIO:
        return _SWELL_DOMINANCE_PURE_SCORE
    if ratio >= _SWELL_DOMINANCE_MIXED_RATIO:
        return _SWELL_DOMINANCE_MIXED_SCORE
    return _SWELL_DOMINANCE_CHOP_SCORE


def _effective_swell(
    wave_height: float,
    wave_period: float,
    wave_direction: float,
    spectral_components: list[dict[str, Any]] | None,
) -> tuple[float, float, float]:
    """Resolve the (height_m, period_s, direction_deg) to score against.

    Multi-swell integration (API-MANUAL.md §17):
      - primary swell > 75% of total energy -> use primary swell alone.
      - secondary swell > 50% of primary energy -> energy superposition
        (H_combined = sqrt(H1^2 + H2^2), T_combined = energy-weighted mean).
      - otherwise (or with < 2 components / no spectral data) -> fall back
        to the caller-supplied dominant-swell values as-is.
    """
    if not spectral_components or len(spectral_components) < 2:
        return wave_height, wave_period, wave_direction

    ordered = sorted(spectral_components, key=lambda c: float(c.get("energy", 0.0)), reverse=True)
    primary, secondary = ordered[0], ordered[1]
    total = _total_energy(ordered)
    if total <= 0:
        return wave_height, wave_period, wave_direction

    primary_energy = float(primary.get("energy", 0.0))
    primary_ratio = primary_energy / total
    if primary_ratio > _PRIMARY_DOMINANT_RATIO:
        return (
            float(primary.get("height", wave_height)),
            float(primary.get("period", wave_period)),
            float(primary.get("direction", wave_direction)),
        )

    secondary_energy = float(secondary.get("energy", 0.0))
    if primary_energy > 0 and (secondary_energy / primary_energy) > _SECONDARY_SIGNIFICANT_RATIO:
        h1 = float(primary.get("height", 0.0))
        h2 = float(secondary.get("height", 0.0))
        t1 = float(primary.get("period", wave_period))
        t2 = float(secondary.get("period", wave_period))
        combined_energy = primary_energy + secondary_energy
        h_combined = math.sqrt(h1**2 + h2**2)
        t_combined = (primary_energy * t1 + secondary_energy * t2) / combined_energy
        d1 = float(primary.get("direction", wave_direction))
        d2 = float(secondary.get("direction", wave_direction))
        d_combined = (primary_energy * d1 + secondary_energy * d2) / combined_energy
        return h_combined, t_combined, d_combined

    # Neither dominant nor comparable enough for superposition: use primary.
    return (
        float(primary.get("height", wave_height)),
        float(primary.get("period", wave_period)),
        float(primary.get("direction", wave_direction)),
    )


def _beach_alignment(swell_direction: float, beach_facing_degrees: float) -> float:
    angle = _normalize_angle_diff(swell_direction, beach_facing_degrees)
    return _range_lookup(angle, _BEACH_ALIGNMENT_RANGES)


def _nearest_compass(direction_degrees: float) -> str:
    idx = round((direction_degrees % 360.0) / 45.0) % 8
    return _COMPASS_DIRECTIONS[idx]


def _directional_filter(swell_direction: float, spot_config: SurfSpotConfig) -> float:
    compass = _nearest_compass(swell_direction)
    if not spot_config.directional_exposure.get(compass, False):
        return _DIRECTIONAL_FILTER_BLOCKED
    return _DIRECTIONAL_FILTER_OPEN


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_of_day_adjustment(
    time_utc: str | None,
    sunrise_utc: str | None,
    sunset_utc: str | None,  # noqa: ARG001 - reserved for future dusk handling
) -> float:
    """Optional time-of-day adjustment; 1.0 (no-op) when time data is unavailable.

    Dawn detection compares two UTC timestamps directly (no timezone needed).
    Afternoon (2-5pm local) detection is skipped: this function only receives
    UTC timestamps, and converting to station-local hour would require a UTC
    offset that is not part of this function's inputs. Per the brief this
    adjustment is optional, so afternoon defaults to the neutral multiplier
    rather than guessing a timezone.
    """
    now = _parse_iso(time_utc)
    if now is None:
        return _TIME_MULTIPLIER_DEFAULT

    sunrise = _parse_iso(sunrise_utc)
    if sunrise is not None and abs(now - sunrise) <= timedelta(hours=_DAWN_WINDOW_HOURS):
        return _DAWN_MULTIPLIER

    return _TIME_MULTIPLIER_DEFAULT


def _format_range(low: float, high: float, locale: str) -> str:
    return f"{i18n.format_number(low, 0, locale)}-{i18n.format_number(high, 0, locale)}"


def _compose_conditions_text(
    *,
    height_ft: float,
    period_s: float,
    direction_deg: float,
    wind_label: str,
    wind_speed_mph: float | None,
    swell_score: float,
    locale: str,
) -> str:
    """Compose a human-readable conditions summary (API-MANUAL.md §17).

    Example (en): "3-4 ft at 12 seconds from the SSW. Offshore winds
    5-10 mph. Clean conditions with long-period swell."
    Sentence templates resolve through i18n.t() (``surf.conditions.*``
    keys, API-MANUAL.md §17 "Marine i18n", T4.4) and are filled in with
    ``str.format()``; numbers through i18n.format_number(). *wind_label*
    is the already-resolved (i18n.t()) wind quality label. The compass
    abbreviation itself (N/NE/E/...) is not locale-resolved — these are
    the same terse letters used cross-locale in marine/aviation notation.
    """
    compass = _nearest_compass(direction_deg)
    height_low = max(0.0, height_ft - 1.0)
    height_high = height_ft + 1.0
    wave_part = i18n.t("surf.conditions.wave_summary", locale).format(
        range=_format_range(height_low, height_high, locale),
        period=i18n.format_number(period_s, 0, locale),
        compass=compass,
    )

    if wind_speed_mph is not None:
        wind_low = max(0.0, wind_speed_mph - 2.5)
        wind_high = wind_speed_mph + 2.5
        wind_part = i18n.t("surf.conditions.wind_with_speed", locale).format(
            wind_label=wind_label,
            range=_format_range(wind_low, wind_high, locale),
        )
    else:
        wind_part = i18n.t("surf.conditions.wind_no_speed", locale).format(
            wind_label=wind_label,
        )

    if swell_score >= _SWELL_DOMINANCE_PURE_SCORE:
        summary_part = i18n.t("surf.conditions.swell_clean", locale)
    elif swell_score >= _SWELL_DOMINANCE_MIXED_SCORE:
        summary_part = i18n.t("surf.conditions.swell_mixed", locale)
    else:
        summary_part = i18n.t("surf.conditions.swell_chop", locale)

    return f"{wave_part} {wind_part} {summary_part}"


def _build_multi_swell(
    spectral_components: list[dict[str, Any]] | None,
) -> list[SpectralWaveComponent] | None:
    if not spectral_components:
        return None
    try:
        return [SpectralWaveComponent(**c) for c in spectral_components]
    except (ValidationError, TypeError):
        # Upstream (NDBC decomposition) is expected to supply well-formed
        # components; malformed input degrades to "no multi-swell detail"
        # rather than failing the whole surf score.
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def score_surf(
    wave_height: float,
    wave_period: float,
    wave_direction: float,
    wind_speed: float | None,
    wind_direction: float | None,
    spectral_components: list[dict[str, Any]] | None,
    spot_config: SurfSpotConfig,
    time_utc: str | None = None,
    sunrise_utc: str | None = None,
    sunset_utc: str | None = None,
    locale: str | None = None,
) -> SurfForecast:
    """Score surf quality and produce a SurfForecast model instance.

    Args:
        wave_height: meters; post-supplement wave height (dominant swell).
        wave_period: seconds; dominant period.
        wave_direction: degrees true north; dominant swell direction.
        wind_speed: m/s; from NDBC or WaveWatch III. None if unavailable.
        wind_direction: degrees true north. None if unavailable.
        spectral_components: NDBC spectral swell systems (dicts shaped like
            SpectralWaveComponent), or None if unavailable.
        spot_config: surf spot configuration (beach facing, directional
            exposure) from config/marine_config.py.
        time_utc: ISO-8601 UTC; for the optional time-of-day adjustment.
        sunrise_utc: ISO-8601 UTC; for dawn detection.
        sunset_utc: ISO-8601 UTC; reserved for dusk detection.
        locale: BCP-47 locale code. When None, resolves to
            i18n.get_active_locale() (API-MANUAL.md §17 "Marine i18n").
    """
    loc = locale or i18n.get_active_locale()

    eff_height, eff_period, eff_direction = _effective_swell(
        wave_height, wave_period, wave_direction, spectral_components
    )

    height_score = _score_wave_height(eff_height)
    period_score = _score_wave_period(eff_period)
    wind_score, wind_label_key = _wind_quality(
        wind_speed, wind_direction, spot_config.beach_facing_degrees
    )
    swell_score = _swell_dominance(spectral_components)

    beach_alignment = _beach_alignment(eff_direction, spot_config.beach_facing_degrees)
    directional_filter = _directional_filter(eff_direction, spot_config)
    time_adjustment = _time_of_day_adjustment(time_utc, sunrise_utc, sunset_utc)

    overall = (
        (
            height_score * _WEIGHT_HEIGHT
            + period_score * _WEIGHT_PERIOD
            + wind_score * _WEIGHT_WIND
            + swell_score * _WEIGHT_SWELL
        )
        * beach_alignment
        * directional_filter
        * time_adjustment
    )

    stars = max(1, min(5, round(overall * 5)))
    quality_label = i18n.t(f"surf.quality.{stars}", loc)
    # windQuality is a (locale) field per API-MANUAL.md §17 "Marine i18n" —
    # resolved through i18n.t(), same as qualityLabel.
    wind_quality_label = i18n.t(f"surf.wind_quality.{wind_label_key}", loc)

    height_ft = convert(eff_height, "meter", "foot") or 0.0
    wind_speed_mph = (
        convert(wind_speed, "meter_per_second", "mile_per_hour") if wind_speed is not None else None
    )

    conditions_text = _compose_conditions_text(
        height_ft=height_ft,
        period_s=eff_period,
        direction_deg=eff_direction,
        wind_label=wind_quality_label,
        wind_speed_mph=wind_speed_mph,
        swell_score=swell_score,
        locale=loc,
    )

    return SurfForecast(
        time=time_utc or "",
        waveHeightAtBreak=eff_height,
        period=eff_period,
        direction=eff_direction,
        qualityStars=stars,
        qualityLabel=quality_label,
        conditionsText=conditions_text,
        windQuality=wind_quality_label,
        swellDominance=swell_score,
        multiSwell=_build_multi_swell(spectral_components),
    )
