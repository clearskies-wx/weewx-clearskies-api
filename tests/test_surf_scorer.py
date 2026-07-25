"""Unit tests for the surf quality scoring processor (API-MANUAL.md §17,
Phase 3 T3.3).

Module under test: weewx_clearskies_api/enrichment/surf_scorer.py

Covers the three weighted scoring factors (wave height, wave period, wave
organization), multi-swell energy superposition, beach angle alignment,
the directional exposure filter, and the full score_surf() ->
SurfForecast pipeline (locale wiring, output shape, non-empty conditions
text).
"""

from __future__ import annotations

import math

import pytest

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import SurfSpotConfig
from weewx_clearskies_api.enrichment import surf_scorer
from weewx_clearskies_api.enrichment.surf_scorer import score_surf
from weewx_clearskies_api.models.responses import SurfForecast

_ALL_OPEN = {d: True for d in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")}

# Fixed reference point + segment length for the geodesic destination-point
# helper below (arbitrary — SoCal coast, 200 m segment).
_SEG_START_LAT = 34.0
_SEG_START_LON = -118.5
_SEG_LENGTH_M = 200.0
_EARTH_RADIUS_M = 6_371_000.0


def _destination_point(
    lat1: float, lon1: float, bearing_deg: float, dist_m: float
) -> tuple[float, float]:
    """Great-circle destination point (lat, lon) from (lat1, lon1) along bearing_deg.

    Standard spherical direct geodesic formula — used to build a shoreline
    segment whose *perpendicular* bearing (see
    ``marine_config._perpendicular_bearing()``) comes out to a chosen
    ``beach_facing_degrees``, since ``SurfSpotConfig.beach_facing_degrees``
    is computed from segment geometry and cannot be set directly (T2.1).
    """
    lat1_r = math.radians(lat1)
    lon1_r = math.radians(lon1)
    bearing_r = math.radians(bearing_deg)
    d_r = dist_m / _EARTH_RADIUS_M
    lat2_r = math.asin(
        math.sin(lat1_r) * math.cos(d_r) + math.cos(lat1_r) * math.sin(d_r) * math.cos(bearing_r)
    )
    lon2_r = lon1_r + math.atan2(
        math.sin(bearing_r) * math.sin(d_r) * math.cos(lat1_r),
        math.cos(d_r) - math.sin(lat1_r) * math.sin(lat2_r),
    )
    return math.degrees(lat2_r), math.degrees(lon2_r)


def _make_spot_config(
    beach_facing_degrees: float, exposure: dict[str, bool] | None = None
) -> SurfSpotConfig:
    """Build a SurfSpotConfig whose *computed* beach_facing_degrees matches the arg.

    SurfSpotConfig derives ``beach_facing_degrees`` from
    ``segment_start/end_lat/lon`` (perpendicular to the shoreline segment,
    T2.1) — it is a read-only property, not a constructor field. Passing
    ``"beach_facing_degrees"`` directly in the section dict (the previous
    version of this helper) is silently ignored; this constructs a shoreline
    segment whose perpendicular bearing equals *beach_facing_degrees* instead.
    """
    seg_bearing = (beach_facing_degrees - 90.0) % 360.0
    end_lat, end_lon = _destination_point(
        _SEG_START_LAT, _SEG_START_LON, seg_bearing, _SEG_LENGTH_M
    )
    return SurfSpotConfig(
        {
            "segment_start_lat": str(_SEG_START_LAT),
            "segment_start_lon": str(_SEG_START_LON),
            "segment_end_lat": str(end_lat),
            "segment_end_lon": str(end_lon),
            "directional_exposure": dict(exposure) if exposure is not None else dict(_ALL_OPEN),
        }
    )


# ---------------------------------------------------------------------------
# 1. Wave height score ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("height_ft", "expected"),
    [
        (0.25, 0.1),  # 0-0.6
        (0.9, 0.3),  # 0.6-1.2
        (1.5, 0.5),  # 1.2-1.8
        (2.5, 0.8),  # 1.8-3.5
        (5.0, 1.0),  # 3.5-7.0
        (9.0, 0.8),  # 7.0-12.0
        (15.0, 0.6),  # 12.0-18.0
        (20.0, 0.2),  # 18.0+
    ],
)
def test_wave_height_score_ranges(height_ft, expected):
    assert surf_scorer._range_lookup(height_ft, surf_scorer._WAVE_HEIGHT_RANGES_FT) == expected


# ---------------------------------------------------------------------------
# 2. Wave period score ranges (base lookup, before the period multiplier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_s", "expected"),
    [
        (3.0, 0.2),  # 0-6
        (7.0, 0.4),  # 6-8
        (9.0, 0.6),  # 8-10
        (11.0, 0.8),  # 10-12
        (14.0, 1.0),  # 12-16
        (17.0, 0.9),  # 16-18
        (19.0, 0.8),  # 18+
    ],
)
def test_wave_period_score_ranges(period_s, expected):
    assert surf_scorer._range_lookup(period_s, surf_scorer._WAVE_PERIOD_RANGES_S) == expected


# ---------------------------------------------------------------------------
# 3-5. Wind quality
# ---------------------------------------------------------------------------


def test_wind_quality_offshore_light():
    # Beach faces N (0deg); wind blowing from due S (180deg) is offshore.
    score, label = surf_scorer._wind_quality(3.0, 180.0, 0.0)  # ~6.7 mph
    assert label == "offshore"
    assert score == pytest.approx(1.2)


def test_wind_quality_onshore_strong():
    # Beach faces N (0deg); wind blowing from due N (0deg) is onshore.
    score, label = surf_scorer._wind_quality(12.0, 0.0, 0.0)  # ~26.8 mph
    assert label == "onshore"
    assert score == pytest.approx(0.3)


def test_wind_quality_glassy():
    score, label = surf_scorer._wind_quality(1.0, 90.0, 0.0)  # ~2.2 mph
    assert label == "glassy"
    assert score == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# 6-8. Swell dominance
# ---------------------------------------------------------------------------


def test_swell_dominance_pure():
    components = [
        {"period": 14.0, "energy": 0.9},
        {"period": 5.0, "energy": 0.1},
    ]
    assert surf_scorer._swell_dominance(components) == pytest.approx(1.0)


def test_swell_dominance_chop():
    components = [
        {"period": 14.0, "energy": 0.3},
        {"period": 5.0, "energy": 0.7},
    ]
    assert surf_scorer._swell_dominance(components) == pytest.approx(0.2)


def test_swell_dominance_no_spectral():
    assert surf_scorer._swell_dominance(None) == pytest.approx(0.5)
    assert surf_scorer._swell_dominance([]) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 9-10. Multi-swell integration
# ---------------------------------------------------------------------------


def test_multi_swell_primary_dominant():
    # _effective_swell() removed per ADR-096 — replaced by DSPR/cross-swell scoring
    score = surf_scorer._directional_spread_score(10.0)
    assert score == pytest.approx(1.0)  # < 15° = clean, tight spread


def test_directional_spread_wide():
    score = surf_scorer._directional_spread_score(30.0)
    assert score == pytest.approx(0.4)  # 25-35° = messy


def test_cross_swell_no_interference():
    components = [
        {"height": 6.0, "period": 14.0, "direction": 200.0, "energy": 80.0},
        {"height": 3.0, "period": 9.0, "direction": 210.0, "energy": 20.0},
    ]
    score = surf_scorer._cross_swell_score(components)
    assert score == pytest.approx(1.0)  # secondary < 50% primary energy OR < 30° angle diff


# ---------------------------------------------------------------------------
# 11-12. Beach angle alignment
# ---------------------------------------------------------------------------


def test_beach_alignment_direct():
    assert surf_scorer._beach_alignment(10.0, 0.0) == pytest.approx(1.0)


def test_beach_alignment_oblique():
    assert surf_scorer._beach_alignment(100.0, 0.0) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# 13-14. Directional exposure filter
# ---------------------------------------------------------------------------


def test_directional_exposure_blocked():
    config = _make_spot_config(0.0, exposure={"N": False})
    assert surf_scorer._directional_filter(0.0, config) == pytest.approx(0.1)


def test_directional_exposure_open():
    config = _make_spot_config(0.0, exposure={"N": True})
    assert surf_scorer._directional_filter(0.0, config) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 15-16. Full pipeline: perfect vs terrible conditions
# ---------------------------------------------------------------------------


def test_perfect_conditions():
    config = _make_spot_config(180.0)  # beach faces S; swell direct hit from S
    forecast = score_surf(
        wave_height=1.6764,  # ~5.5 ft, well inside the 3-6 ft (score 1.0) bracket
        wave_period=14.0,  # 12-16s bracket, score 1.0, no multiplier adjustment
        wave_direction=180.0,  # direct hit on a S-facing beach
        wind_speed=3.0,  # ~6.7 mph, light
        wind_direction=0.0,  # wind FROM the N == offshore for a S-facing beach
        spectral_components=None,
        spot_config=config,
        multi_swell=[{"period": 14.0, "energy": 0.9, "direction": 180.0, "height": 1.8}],
    )
    assert isinstance(forecast, SurfForecast)
    assert forecast.qualityStars in (4, 5)


def test_terrible_conditions():
    config = _make_spot_config(180.0)
    forecast = score_surf(
        wave_height=0.2438,  # ~0.8 ft, inside the 0.5-1 ft (score 0.3) bracket
        wave_period=6.0,  # 0-6s bracket, score 0.2, x0.1 short-period penalty
        wave_direction=180.0,  # still a direct hit, isolating the other factors
        wind_speed=12.0,  # ~26.8 mph, strong
        wind_direction=180.0,  # wind FROM the S == onshore for a S-facing beach
        spectral_components=None,
        spot_config=config,
        multi_swell=[
            {"period": 5.0, "energy": 0.7, "direction": 180.0, "height": 0.2},
            {"period": 4.0, "energy": 0.3, "direction": 160.0, "height": 0.1},
        ],  # pure wind chop (all periods < 10s)
    )
    assert isinstance(forecast, SurfForecast)
    assert forecast.qualityStars == 1


# ---------------------------------------------------------------------------
# 17. Additive identity
# ---------------------------------------------------------------------------


def test_additive_identity():
    """Scoring breakdown fields add up to the score that produces qualityStars.

    API-MANUAL §17 guarantees:
        total = waveHeight + wavePeriod + waveOrganization
                + beachAlignment + directionalExposure + timeOfDay
    and qualityStars = max(1, min(5, round(total / 20))).
    Verify by reconstructing stars from the breakdown and asserting equality.
    """
    config = _make_spot_config(180.0)
    forecast = score_surf(
        wave_height=1.6764,
        wave_period=14.0,
        wave_direction=180.0,
        wind_speed=3.0,
        wind_direction=0.0,
        spectral_components=None,
        spot_config=config,
        multi_swell=[{"period": 14.0, "energy": 0.9, "direction": 180.0, "height": 1.8}],
    )
    assert forecast.scoring is not None
    s = forecast.scoring
    total = (
        s.waveHeight + s.wavePeriod + s.waveOrganization
        + s.beachAlignment + s.directionalExposure + s.timeOfDay
    )
    expected_stars = max(1, min(5, round(total / 20)))
    assert forecast.qualityStars == expected_stars


# ---------------------------------------------------------------------------
# 18-19. Output shape
# ---------------------------------------------------------------------------


def test_score_output_type():
    config = _make_spot_config(180.0)
    forecast = score_surf(
        wave_height=1.5,
        wave_period=12.0,
        wave_direction=190.0,
        wind_speed=5.0,
        wind_direction=10.0,
        spectral_components=None,
        spot_config=config,
        time_utc="2026-07-10T12:00:00Z",
    )
    assert isinstance(forecast, SurfForecast)
    assert forecast.time == "2026-07-10T12:00:00Z"
    assert 1 <= forecast.qualityStars <= 5
    assert isinstance(forecast.qualityLabel, str) and forecast.qualityLabel
    assert isinstance(forecast.windQuality, str) and forecast.windQuality
    assert 0.0 <= forecast.swellDominance <= 1.0
    assert isinstance(forecast.waveHeightAtBreak, float)
    assert isinstance(forecast.period, float)
    assert isinstance(forecast.direction, float)


def test_conditions_text_not_empty():
    config = _make_spot_config(180.0)
    forecast = score_surf(
        wave_height=1.5,
        wave_period=12.0,
        wave_direction=190.0,
        wind_speed=5.0,
        wind_direction=10.0,
        spectral_components=None,
        spot_config=config,
    )
    assert isinstance(forecast.conditionsText, str)
    assert len(forecast.conditionsText) > 0


# ---------------------------------------------------------------------------
# 19. Quality labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stars", [1, 2, 3, 4, 5])
def test_quality_labels(stars):
    key = f"surf.quality.{stars}"
    assert key in surf_scorer._LOCALE_KEYS
    assert surf_scorer._LOCALE_KEYS[key]


def test_quality_label_wiring_matches_i18n_lookup():
    """The qualityLabel field is exactly i18n.t() of the star-derived key."""
    config = _make_spot_config(180.0)
    forecast = score_surf(
        wave_height=1.6764,
        wave_period=14.0,
        wave_direction=180.0,
        wind_speed=3.0,
        wind_direction=0.0,
        spectral_components=[{"period": 14.0, "energy": 1.0}],
        spot_config=config,
    )
    assert forecast.qualityLabel == i18n.t(f"surf.quality.{forecast.qualityStars}")


# ---------------------------------------------------------------------------
# 20. Wind quality labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    ["offshore", "cross_offshore", "cross", "cross_onshore", "onshore", "glassy"],
)
def test_wind_quality_labels(suffix):
    key = f"surf.wind_quality.{suffix}"
    assert key in surf_scorer._LOCALE_KEYS
    assert surf_scorer._LOCALE_KEYS[key]


@pytest.mark.parametrize(
    ("wind_speed", "wind_direction", "beach_facing", "expected_label"),
    [
        (3.0, 180.0, 0.0, "offshore"),  # angle 180
        (6.0, 120.0, 0.0, "cross_offshore"),  # angle 120
        (6.0, 90.0, 0.0, "cross"),  # angle 90
        (6.0, 60.0, 0.0, "cross_onshore"),  # angle 60
        (12.0, 0.0, 0.0, "onshore"),  # angle 0
        (1.0, 45.0, 0.0, "glassy"),  # calm overrides direction
    ],
)
def test_wind_quality_labels_from_angle(wind_speed, wind_direction, beach_facing, expected_label):
    _, label = surf_scorer._wind_quality(wind_speed, wind_direction, beach_facing)
    assert label == expected_label


# ---------------------------------------------------------------------------
# 21. Null face height (T4A.4, LC-17) — model unavailable vs. genuinely flat
#
# wave_height=None means the 1D pipeline is unavailable (model failed/never
# ran) — no quality rating is computable and score_surf() must not paper
# over that with a defaulted score. wave_height=0.0 means the model ran and
# found genuinely flat conditions — a valid "Flat"/low rating, NOT null.
# These two must produce visibly different output.
# ---------------------------------------------------------------------------


def _score_with_height(wave_height: float | None) -> SurfForecast:
    config = _make_spot_config(180.0)
    return score_surf(
        wave_height=wave_height,
        wave_period=14.0,
        wave_direction=180.0,
        wind_speed=3.0,
        wind_direction=0.0,
        spectral_components=None,
        spot_config=config,
        multi_swell=[{
            "period": 14.0,
            "energy": 0.9,
            "direction": 180.0,
            "height": 1.8,
            "frequencyRange": [0.05, 0.09],
            "classification": "groundswell",
        }],
    )


def test_score_surf_none_height_yields_null_rating():
    """wave_height=None (model unavailable) — no star rating, no scoring breakdown."""
    forecast = _score_with_height(None)
    assert forecast.qualityStars is None
    assert forecast.qualityLabel is None
    assert forecast.scoring is None
    # waveHeightAtBreak mirrors the None input — set by the caller (surf.py),
    # but score_surf() itself must not synthesize a numeric value either.
    assert forecast.waveHeightAtBreak is None


def test_score_surf_none_height_conditions_text_is_not_a_lie():
    """conditionsText must not read like a confident 0 ft / Poor rating."""
    forecast = _score_with_height(None)
    assert isinstance(forecast.conditionsText, str)
    assert len(forecast.conditionsText) > 0
    assert forecast.conditionsText == i18n.t("surf.conditions.unavailable")
    # Must not contain a fabricated height/quality phrase from the normal
    # wave_summary template (which always starts with a numeric range).
    assert "ft" not in forecast.conditionsText
    assert "0-1" not in forecast.conditionsText


def test_score_surf_none_height_still_computes_wind_and_swell():
    """windQuality/swellDominance are independent of face height and stay populated."""
    forecast = _score_with_height(None)
    assert forecast.windQuality  # non-empty — still a real wind classification
    assert 0.0 <= forecast.swellDominance <= 1.0
    assert forecast.multiSwell is not None  # independent of face height


def test_score_surf_zero_height_is_a_real_rating_not_none():
    """wave_height=0.0 (model ran, genuinely flat) is a valid low rating, not null.

    This is the case that must NOT be confused with wave_height=None — a
    model that ran and found flat water is not the same as a model that
    failed (T4A.4/LC-16 distinction, mirrored here at the scorer level).
    """
    forecast = _score_with_height(0.0)
    assert forecast.qualityStars is not None
    assert 1 <= forecast.qualityStars <= 5
    assert forecast.qualityLabel is not None
    assert forecast.scoring is not None
    assert forecast.conditionsText != i18n.t("surf.conditions.unavailable")
