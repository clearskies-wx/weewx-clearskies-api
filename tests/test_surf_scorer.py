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

import pytest

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import SurfSpotConfig
from weewx_clearskies_api.enrichment import surf_scorer
from weewx_clearskies_api.enrichment.surf_scorer import score_surf
from weewx_clearskies_api.models.responses import SurfForecast

_ALL_OPEN = {d: True for d in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")}


def _make_spot_config(
    beach_facing_degrees: float, exposure: dict[str, bool] | None = None
) -> SurfSpotConfig:
    return SurfSpotConfig(
        {
            "beach_facing_degrees": beach_facing_degrees,
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
