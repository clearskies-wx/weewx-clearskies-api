"""Surf quality scoring processor (API-MANUAL.md §17 "Surf quality scorer").

Registered against the surf endpoint pipeline. Runs after the 1D
(SwellTrack) per-partition, multi-transect pipeline (``services/
surf_1d_pipeline.py``) has produced (or failed to produce) a breaking face
height — this module scores whatever wave data it is handed and does not
itself correct for shoaling, refraction, or site-specific breaker physics.
As of T4A.4, there is no SWAN CURVE fallback: ``wave_height`` is the
SwellTrack face height or ``None`` (see ``score_surf()`` docstring).

Three weighted scoring factors combine into a 1-5 star rating (T4.1):

    wave height       (0.35, max 35 pts) — larger, within a rideable range, scores higher
    wave period       (0.35, max 35 pts) — longer period = cleaner, more powerful waves
    wave organization (0.30, max 30 pts) — composite of four sub-factors:
        wind effect           (50% of 30 = 15 pts effective)
            offshore wind holds the wave face up; onshore wind blows it down
        swell dominance       (25% of 30 = 7.5 pts effective)
            clean single dominant swell scores higher than confused wind-chop
        directional spread    (15% of 30 = 4.5 pts effective, SWAN DSPR)
            tight spread = organized waves; wide spread = messy surf
        cross-swell           (10% of 30 = 3 pts effective, SWAN SPECOUT)
            multiple systems at conflicting angles create interference

Three signed adjustments then surface all penalty/bonus factors (T4.2):

    beach alignment      — angle between swell and beach (0 = direct hit)
    directional exposure — operator config (0 = open, negative = blocked)
    time of day          — dawn bonus, afternoon penalty, 0 otherwise

Additive identity guaranteed by construction:
    total = waveHeight + wavePeriod + waveOrganization
            + beachAlignment + directionalExposure + timeOfDay

All scoring uses SWAN values directly. NDBC spectral data is NOT used
(``spectral_components`` parameter retained for backward compatibility
but is deprecated and ignored as of T4.1 / ADR-096).

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import SurfSpotConfig
from weewx_clearskies_api.models.responses import SpectralWaveComponent, SurfForecast, SurfScoringBreakdown
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
# Scoring weights (API-MANUAL.md §17 "Surf quality scorer", ADR-096)
# Three top-level weighted factors (T4.1)
# ---------------------------------------------------------------------------

_WEIGHT_HEIGHT = 0.35        # max 35 pts
_WEIGHT_PERIOD = 0.35        # max 35 pts
_WEIGHT_ORGANIZATION = 0.30  # max 30 pts — composite of four sub-factors

# Organization composite sub-weights (must sum to 1.0)
_ORG_WEIGHT_WIND = 0.50             # wind effect: 50% of 30 = 15 pts effective
_ORG_WEIGHT_SWELL_DOMINANCE = 0.25  # swell dominance: 25% of 30 = 7.5 pts effective
_ORG_WEIGHT_DSPR = 0.15             # directional spread: 15% of 30 = 4.5 pts effective
_ORG_WEIGHT_CROSS_SWELL = 0.10      # cross-swell: 10% of 30 = 3 pts effective

# ---------------------------------------------------------------------------
# Directional spread scoring thresholds (SWAN TABLE DSPR in degrees)
# ---------------------------------------------------------------------------

_DSPR_TIGHT_DEG = 15.0     # < 15° = tight (organized, clean)
_DSPR_MODERATE_DEG = 25.0  # 15–25° = moderate spread
_DSPR_WIDE_DEG = 35.0      # 25–35° = wide spread
# ≥ 35° = messy
_DSPR_TIGHT_SCORE = 1.0    # < 15°: perfectly organized
_DSPR_MODERATE_SCORE = 0.7  # 15–25°: decent organization
_DSPR_WIDE_SCORE = 0.4     # 25–35°: messy
_DSPR_MESSY_SCORE = 0.2    # ≥ 35°: very disorganized
_DSPR_NO_DATA = 0.5        # neutral fallback when DSPR unavailable

# ---------------------------------------------------------------------------
# Cross-swell interference scoring thresholds (SWAN SPECOUT)
# ---------------------------------------------------------------------------

_CROSS_SWELL_ENERGY_RATIO = 0.5  # secondary > this fraction of primary = significant
_CROSS_SWELL_ANGLE_DEG = 30.0    # angle difference threshold for interference
_CROSS_SWELL_CLEAN = 1.0         # no cross-swell interference
_CROSS_SWELL_INTERFERENCE = 0.4  # significant cross-swell detected
_CROSS_SWELL_NO_DATA = 0.5       # neutral fallback when SPECOUT unavailable

# ---------------------------------------------------------------------------
# Wave height component — range lookup, height in feet (upper bound inclusive)
# ---------------------------------------------------------------------------

_WAVE_HEIGHT_RANGES_FT: list[tuple[float, float]] = [
    # T2.6 recalibration: thresholds shifted +17% because scorer now receives
    # breakingFaceHeight (trough-to-crest) instead of raw waveHeightAtBreak
    # (post-supplement Hsig).  K-G amplification at typical surf wave sizes
    # (1-3m Hsig, 10-16s) is ~1.5-1.7×, but the effective display-height
    # jump surfers perceive is ~15-20% above the Hsig they once saw.
    # Threshold progression (previous → new):
    #   0.5 → 0.6 ft   1.0 → 1.2 ft   1.5 → 1.8 ft   3.0 → 3.5 ft
    #   6.0 → 7.0 ft  10.0 → 12.0 ft  15.0 → 18.0 ft   inf → inf
    (0.6, 0.1),
    (1.2, 0.3),
    (1.8, 0.5),
    (3.5, 0.8),
    (7.0, 1.0),
    (12.0, 0.8),
    (18.0, 0.6),
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


def _directional_spread_score(dspr: float | None) -> float:
    """Score directional spread from SWAN TABLE DSPR output (T4.1, ADR-096).

    Tight spread = organized, clean waves; wide spread = messy, disorganized surf.
    Returns neutral 0.5 when DSPR is unavailable (no SWAN TABLE DSPR column).

    Thresholds from nearshore DSPR literature and KEWL Mermaid precedent:
        < 15°  → 1.0 (tightly organized groundswell)
        15–25° → 0.7 (decent organization)
        25–35° → 0.4 (messy)
        ≥ 35°  → 0.2 (very disorganized)
    """
    if dspr is None:
        return _DSPR_NO_DATA
    if dspr < _DSPR_TIGHT_DEG:
        return _DSPR_TIGHT_SCORE
    if dspr < _DSPR_MODERATE_DEG:
        return _DSPR_MODERATE_SCORE
    if dspr < _DSPR_WIDE_DEG:
        return _DSPR_WIDE_SCORE
    return _DSPR_MESSY_SCORE


def _cross_swell_score(multi_swell: list[dict[str, Any]] | None) -> float:
    """Score cross-swell interference from SWAN SPECOUT decomposition (T4.1, ADR-096).

    Checks whether a secondary swell system is both energetically significant
    (> 50% of primary energy) and at a significantly different direction
    (> 30° angle difference). When both conditions are met, wave interference
    creates messy, unpredictable surf.

    Returns:
        0.5  — neutral (no SPECOUT data available)
        1.0  — no cross-swell (only one system, or no secondary meets threshold)
        0.4  — significant cross-swell interference detected
    """
    if not multi_swell:
        return _CROSS_SWELL_NO_DATA
    if len(multi_swell) < 2:
        return _CROSS_SWELL_CLEAN

    ordered = sorted(multi_swell, key=lambda c: float(c.get("energy", 0.0)), reverse=True)
    primary = ordered[0]
    primary_energy = float(primary.get("energy", 0.0))
    if primary_energy <= 0:
        return _CROSS_SWELL_NO_DATA

    primary_dir = float(primary.get("direction", 0.0))
    for secondary in ordered[1:]:
        secondary_energy = float(secondary.get("energy", 0.0))
        if (secondary_energy / primary_energy) > _CROSS_SWELL_ENERGY_RATIO:
            secondary_dir = float(secondary.get("direction", 0.0))
            angle_diff = _normalize_angle_diff(primary_dir, secondary_dir)
            if angle_diff > _CROSS_SWELL_ANGLE_DEG:
                return _CROSS_SWELL_INTERFERENCE

    return _CROSS_SWELL_CLEAN


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
    station_tz: str | None = None,
) -> float:
    """Optional time-of-day adjustment; 1.0 (no-op) when inputs are unavailable.

    Dawn bonus (``_DAWN_MULTIPLIER = 1.1``): applied when ``time_utc`` falls
    within ``_DAWN_WINDOW_HOURS`` (±1 h) of ``sunrise_utc``.  Dawn detection
    compares two UTC timestamps directly — no timezone conversion needed.

    Afternoon penalty (``_AFTERNOON_MULTIPLIER = 0.9``): applied when local
    time falls between ``_AFTERNOON_START_HOUR`` (14:00) and
    ``_AFTERNOON_END_HOUR`` (17:00) exclusive.  Requires ``station_tz`` — an
    IANA timezone identifier (e.g. ``"America/Los_Angeles"``).  Skipped
    gracefully when ``station_tz`` is ``None`` or unrecognised.

    Returns ``_TIME_MULTIPLIER_DEFAULT`` (1.0) when ``time_utc`` is absent or
    unparseable, or when neither condition is met.
    """
    now = _parse_iso(time_utc)
    if now is None:
        return _TIME_MULTIPLIER_DEFAULT

    sunrise = _parse_iso(sunrise_utc)
    if sunrise is not None and abs(now - sunrise) <= timedelta(hours=_DAWN_WINDOW_HOURS):
        return _DAWN_MULTIPLIER

    if station_tz is not None:
        try:
            local_now = now.astimezone(ZoneInfo(station_tz))
            if _AFTERNOON_START_HOUR <= local_now.hour < _AFTERNOON_END_HOUR:
                return _AFTERNOON_MULTIPLIER
        except ZoneInfoNotFoundError:
            pass

    return _TIME_MULTIPLIER_DEFAULT


def _format_range(low: float, high: float, locale: str) -> str:
    return f"{i18n.format_number(low, 0, locale)}-{i18n.format_number(high, 0, locale)}"


def _compose_conditions_text(
    *,
    height_display: float,
    height_unit_label: str,
    period_s: float,
    direction_deg: float,
    wind_label: str,
    wind_speed_display: float | None,
    wind_unit_label: str,
    swell_score: float,
    locale: str,
) -> str:
    """Compose a human-readable conditions summary (API-MANUAL.md §17).

    Example (en): "3-4 ft at 12 seconds from the SSW. Offshore winds
    5-10 mph. Clean conditions with long-period swell."

    Uses the operator's configured units for height and wind speed
    (SURF-10 fix). ``height_display`` and ``wind_speed_display`` are
    already converted to the operator's display units by the caller.
    """
    compass = _nearest_compass(direction_deg)
    height_low = max(0.0, height_display - 1.0)
    height_high = height_display + 1.0
    wave_part = i18n.t("surf.conditions.wave_summary", locale).format(
        range=_format_range(height_low, height_high, locale),
        unit=height_unit_label,
        period=i18n.format_number(period_s, 0, locale),
        compass=compass,
    )

    if wind_speed_display is not None:
        wind_low = max(0.0, wind_speed_display - 2.5)
        wind_high = wind_speed_display + 2.5
        wind_part = i18n.t("surf.conditions.wind_with_speed", locale).format(
            wind_label=wind_label,
            range=_format_range(wind_low, wind_high, locale),
            unit=wind_unit_label,
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
    wave_height: float | None,
    wave_period: float,
    wave_direction: float,
    wind_speed: float | None,
    wind_direction: float | None,
    spectral_components: list[dict[str, Any]] | None,  # deprecated — ignored (T4.1/ADR-096)
    spot_config: SurfSpotConfig,
    time_utc: str | None = None,
    sunrise_utc: str | None = None,
    sunset_utc: str | None = None,
    station_tz: str | None = None,
    locale: str | None = None,
    wind_source: str = "station",
    directional_spread: float | None = None,
    multi_swell: list[dict[str, Any]] | None = None,
    height_unit: str = "foot",
    height_unit_label: str = "ft",
    wind_unit: str = "mile_per_hour",
    wind_unit_label: str = "mph",
) -> SurfForecast:
    """Score surf quality and produce a SurfForecast model instance.

    Args:
        wave_height: meters; SwellTrack breaking face height
            (``best_peak_face_height_m`` from the 1D pipeline), or ``None``
            when the pipeline is unavailable (T4A.4, LC-17). ``None`` is NOT
            the same as ``0.0``: ``0.0`` means the model ran and found
            genuinely flat conditions (a valid rating — "Flat"/0 stars);
            ``None`` means the model failed or never ran, so no rating can
            be computed — the caller must not paper over that with a
            defaulted score. ``_WAVE_HEIGHT_RANGES_FT`` thresholds are
            calibrated for the trough-to-crest face height, not raw Hsig
            (+17%, T2.6).
        wave_period: seconds; dominant period.
        wave_direction: degrees true north; dominant swell direction.
        wind_speed: m/s; at-beach wind (station hardware for t=0, HRRR
            forecast wind for t>0 per ADR-094). None if unavailable.
        wind_direction: degrees true north. None if unavailable.
        spectral_components: deprecated; not used. Pass None. Previously held
            NDBC spectral swell systems. NDBC data is no longer passed to the
            scorer (T3.5/T4.1/ADR-096). Retained in signature for backward
            compatibility with existing callers and tests.
        spot_config: surf spot configuration (beach facing, directional
            exposure) from config/marine_config.py.
        time_utc: ISO-8601 UTC; for the optional time-of-day adjustment.
        sunrise_utc: ISO-8601 UTC; for dawn detection.
        sunset_utc: ISO-8601 UTC; reserved for dusk detection.
        station_tz: IANA timezone identifier (e.g. ``"America/Los_Angeles"``);
            used to convert ``time_utc`` to station-local time for the
            afternoon penalty (14:00–17:00 local).  When ``None``, the
            afternoon penalty is skipped; dawn detection still works
            using UTC timestamps only.
        locale: BCP-47 locale code. When None, resolves to
            i18n.get_active_locale() (API-MANUAL.md §17 "Marine i18n").
        wind_source: metadata field indicating the wind data source for this
            timestep. One of ``"station"``, ``"forecast_provider"``, or
            ``"hrrr"`` (ADR-094). Set on the response entry by the endpoint,
            not directly in SurfForecast.
        directional_spread: degrees; SWAN DSPR output at ~10m depth (T4.1).
            Feeds the directional spread sub-factor of the organization
            composite. None when SWAN TABLE does not include DSPR (neutral 0.5
            fallback applies).
        multi_swell: SWAN SPECOUT spectral decomposition for this timestep
            (T4.1). List of dicts shaped like SpectralWaveComponent. Feeds
            the swell dominance and cross-swell sub-factors of the organization
            composite. None when SPECOUT is unavailable (neutral fallback).
    """
    loc = locale or i18n.get_active_locale()

    if wave_height is None:
        # T4A.4/LC-17: the 1D pipeline is unavailable — no face height, no
        # quality rating. windQuality and swellDominance are independent of
        # face height (wind is a real observation; swell dominance is a
        # spectral-energy ratio) and remain meaningful, so they are still
        # computed rather than nulled out. qualityStars/qualityLabel/scoring
        # would require the wave-height component of the composite score,
        # which cannot be computed truthfully without a face height — they
        # are None rather than a defaulted "Poor"/0-star rating (LC-17: a
        # missing rating is not the same lie as a confident wrong one).
        # conditionsText must still resolve through the locale file (rules
        # §6.2) — it is a "forecast unavailable" sentence, never empty.
        wind_score, wind_label_key = _wind_quality(
            wind_speed, wind_direction, spot_config.beach_facing_degrees
        )
        wind_quality_label = i18n.t(f"surf.wind_quality.{wind_label_key}", loc)
        swell_dom_score = _swell_dominance(multi_swell)
        return SurfForecast(
            time=time_utc or "",
            waveHeightAtBreak=None,
            period=wave_period,
            direction=wave_direction,
            qualityStars=None,
            qualityLabel=None,
            conditionsText=i18n.t("surf.conditions.unavailable", loc),
            windQuality=wind_quality_label,
            swellDominance=swell_dom_score,
            multiSwell=_build_multi_swell(multi_swell),
            scoring=None,
        )

    # Scorer uses SWAN height/period/direction directly (no _effective_swell()
    # NDBC override — removed in T4.1/ADR-096).
    height_score = _score_wave_height(wave_height)
    period_score = _score_wave_period(wave_period)

    # Wind quality sub-factor (unchanged logic, now part of organization composite)
    wind_score, wind_label_key = _wind_quality(
        wind_speed, wind_direction, spot_config.beach_facing_degrees
    )

    # Organization composite sub-factors (T4.1)
    swell_dom_score = _swell_dominance(multi_swell)   # SWAN SPECOUT swell dominance
    dspr_score = _directional_spread_score(directional_spread)  # SWAN TABLE DSPR
    cross_swell_score_val = _cross_swell_score(multi_swell)     # SWAN SPECOUT cross-swell

    org_score = (
        wind_score * _ORG_WEIGHT_WIND
        + swell_dom_score * _ORG_WEIGHT_SWELL_DOMINANCE
        + dspr_score * _ORG_WEIGHT_DSPR
        + cross_swell_score_val * _ORG_WEIGHT_CROSS_SWELL
    )

    # -----------------------------------------------------------------------
    # Convert factor scores to integer point breakdown (T4.1, T4.2)
    # -----------------------------------------------------------------------

    # Top-level factor points (max 35/35/30 for score ≤ 1.0; multipliers
    # such as the period long-period boost and offshore wind score can push
    # individual factors above their nominal max — same as the old 4-factor
    # model, where `overall` could exceed 1.0 for exceptional conditions).
    wave_height_pts = round(height_score * 35)
    wave_period_pts = round(period_score * 35)
    wave_org_pts = round(org_score * 30)
    sub_total = wave_height_pts + wave_period_pts + wave_org_pts

    # Beach alignment (T4.2): signed integer delta showing how much the beach
    # angle degrades the sub-total.  beach_mult ∈ [0.1, 1.0] so delta ≤ 0.
    beach_mult = _beach_alignment(wave_direction, spot_config.beach_facing_degrees)
    post_beach = round(sub_total * beach_mult)
    beach_alignment_delta = post_beach - sub_total

    # Directional exposure (T4.2): 0 when open, large negative when blocked.
    dir_mult = _directional_filter(wave_direction, spot_config)
    post_dir = round(post_beach * dir_mult)
    directional_exposure_delta = post_dir - post_beach

    # Time-of-day adjustment (T4.2): positive at dawn, negative in afternoon.
    time_mult = _time_of_day_adjustment(time_utc, sunrise_utc, sunset_utc, station_tz)
    post_time_float = post_dir * time_mult
    total_score = round(post_time_float)
    time_of_day_delta = total_score - post_dir

    # Verify additive identity: total = waveHeight + wavePeriod + waveOrganization
    #   + beachAlignment + directionalExposure + timeOfDay
    # (holds by construction — no independent rounding between stages)

    # Organization sub-factor point contributions (for breakdown display)
    org_wind_pts = round(wind_score * _ORG_WEIGHT_WIND * 30, 1)
    org_swell_pts = round(swell_dom_score * _ORG_WEIGHT_SWELL_DOMINANCE * 30, 1)
    org_dspr_pts = round(dspr_score * _ORG_WEIGHT_DSPR * 30, 1)
    org_cross_pts = round(cross_swell_score_val * _ORG_WEIGHT_CROSS_SWELL * 30, 1)

    # Stars: 1-5 scale from total_score (0-100 nominal range)
    # round(total_score / 20) maps 0→0, 20→1, 40→2, 60→3, 80→4, 100→5.
    stars = max(1, min(5, round(total_score / 20)))
    quality_label = i18n.t(f"surf.quality.{stars}", loc)
    wind_quality_label = i18n.t(f"surf.wind_quality.{wind_label_key}", loc)

    height_display = convert(wave_height, "meter", height_unit) or 0.0
    wind_speed_display = (
        convert(wind_speed, "meter_per_second", wind_unit) if wind_speed is not None else None
    )

    conditions_text = _compose_conditions_text(
        height_display=height_display,
        height_unit_label=height_unit_label,
        period_s=wave_period,
        direction_deg=wave_direction,
        wind_label=wind_quality_label,
        wind_speed_display=wind_speed_display,
        wind_unit_label=wind_unit_label,
        swell_score=swell_dom_score,
        locale=loc,
    )

    scoring = SurfScoringBreakdown(
        waveHeight=wave_height_pts,
        wavePeriod=wave_period_pts,
        waveOrganization=wave_org_pts,
        organizationWind=org_wind_pts,
        organizationSwellDominance=org_swell_pts,
        organizationDirectionalSpread=org_dspr_pts,
        organizationCrossSwell=org_cross_pts,
        beachAlignment=beach_alignment_delta,
        directionalExposure=directional_exposure_delta,
        timeOfDay=time_of_day_delta,
    )

    return SurfForecast(
        time=time_utc or "",
        waveHeightAtBreak=wave_height,
        period=wave_period,
        direction=wave_direction,
        qualityStars=stars,
        qualityLabel=quality_label,
        conditionsText=conditions_text,
        windQuality=wind_quality_label,
        swellDominance=swell_dom_score,
        multiSwell=_build_multi_swell(multi_swell),
        scoring=scoring,
    )
