"""Unit tests for the fishing conditions scoring processor (API-MANUAL.md
§17, Phase 4 T4.2).

Modules under test: weewx_clearskies_api/enrichment/fishing_scorer.py,
weewx_clearskies_api/enrichment/fishing_species.py.

Covers the five weighted environmental components (pressure, tide, water
temperature, solunar, time of day), the per-species multiplier chain
(pressure sensitivity, temperature/tide/time preference, seasonal
behavior/regulatory closures), biogeographic region classification,
habitat feature detection (delegated to bathymetry.py), and the full
score_fishing() -> FishingForecast pipeline (locale wiring, score
scaling/clamping, "great day"/"bad day" integration).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api import i18n
from weewx_clearskies_api.enrichment import fishing_scorer
from weewx_clearskies_api.enrichment.fishing_scorer import get_habitat_features, score_fishing
from weewx_clearskies_api.enrichment.fishing_species import (
    SEASONAL_BEHAVIOR,
    SPECIES_PROFILES,
    classify_region,
)
from weewx_clearskies_api.models.responses import FishingForecast

# ---------------------------------------------------------------------------
# 1. Pressure trend scoring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trend_hpa_3hr", "expected"),
    [
        (-4.0, 100),  # rapid drop (> 3 hPa/3hr)
        (-3.0, 80),  # boundary: exactly 3 -> falling bucket (not rapid)
        (-2.0, 80),  # falling
        (-1.0, 50),  # boundary: exactly 1 -> stable bucket
        (0.0, 50),  # stable
        (1.0, 50),  # stable, upper boundary
        (2.0, 30),  # rising slowly
        (3.0, 30),  # rising slowly, upper boundary
        (4.0, 20),  # rising rapidly (> 3 hPa/3hr)
        (None, 50),  # unknown -> neutral
    ],
)
def test_pressure_scoring(trend_hpa_3hr, expected):
    assert fishing_scorer._score_pressure(trend_hpa_3hr) == expected


# ---------------------------------------------------------------------------
# 2. Tide state scoring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tide_state", "expected"),
    [
        ("outgoing", 100),
        ("incoming", 80),
        ("peak_flow", 70),
        ("slack_high", 30),
        ("slack_low", 20),
        ("nonsense", 50),  # unrecognized -> neutral fallback
    ],
)
def test_tide_state_scoring(tide_state, expected):
    assert fishing_scorer._score_tide(tide_state) == expected


# ---------------------------------------------------------------------------
# 3. Species temperature multiplier transitions at range boundaries
# ---------------------------------------------------------------------------


def test_species_temp_multiplier_boundaries():
    profile = SPECIES_PROFILES["redfish"]  # optimal (68,80) good (60,85) marginal (50,90)
    assert fishing_scorer._species_temp_multiplier(profile, 74.0) == 1.0  # inside optimal
    assert fishing_scorer._species_temp_multiplier(profile, 68.0) == 1.0  # optimal lower bound
    assert fishing_scorer._species_temp_multiplier(profile, 80.0) == 1.0  # optimal upper bound
    assert fishing_scorer._species_temp_multiplier(profile, 60.0) == 0.8  # good lower (not optimal)
    assert fishing_scorer._species_temp_multiplier(profile, 85.0) == 0.8  # good upper bound
    assert fishing_scorer._species_temp_multiplier(profile, 50.0) == 0.5  # marginal lower bound
    assert fishing_scorer._species_temp_multiplier(profile, 90.0) == 0.5  # marginal upper bound
    assert fishing_scorer._species_temp_multiplier(profile, 40.0) == 0.1  # outside
    assert fishing_scorer._species_temp_multiplier(profile, 95.0) == 0.1  # outside


def test_species_temp_multiplier_unknown_temp_is_neutral():
    profile = SPECIES_PROFILES["redfish"]
    assert fishing_scorer._species_temp_multiplier(profile, None) == 1.0


# ---------------------------------------------------------------------------
# 4. Seasonal behavior
# ---------------------------------------------------------------------------


def test_redfish_october_spawning_multiplier():
    assert SEASONAL_BEHAVIOR["redfish"][10]["spawning_multiplier"] == 2.5
    multiplier, note = fishing_scorer._seasonal_adjustment("redfish", 10)
    assert multiplier == 2.5
    assert note == "Spawning run — peak activity"


def test_snook_june_closed_season():
    assert SEASONAL_BEHAVIOR["snook"][6]["closed"] is True
    multiplier, note = fishing_scorer._seasonal_adjustment("snook", 6)
    assert multiplier == 0.0
    assert "Closed season" in note
    assert "Jun" in note and "Aug" in note


# ---------------------------------------------------------------------------
# 5. Biogeographic region classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "expected_region"),
    [
        (43.66, -70.25, "atlantic_ne"),  # Portland, ME
        (27.95, -82.46, "gulf"),  # Tampa, FL
        (32.72, -117.16, "pacific_sw"),  # San Diego, CA
        (47.60, -122.33, "pacific_nw"),  # Seattle, WA
        (61.20, -149.90, "alaska"),  # Anchorage, AK
        (21.30, -157.86, "hawaii"),  # Honolulu, HI
        (41.88, -87.63, "great_lakes"),  # Chicago, IL
        (18.47, -66.11, "caribbean"),  # San Juan, PR
        (13.47, 144.75, "pacific_territories"),  # Guam
    ],
)
def test_classify_region(lat, lon, expected_region):
    assert classify_region(lat, lon) == expected_region


def test_classify_region_fallback():
    # Middle of the North Atlantic, outside every known bounding box.
    assert classify_region(35.0, -40.0) == "atlantic_se"


# ---------------------------------------------------------------------------
# 6. Score scale: 0-100 int, rounding and clamping
# ---------------------------------------------------------------------------


def test_component_scores_are_ints_in_0_100_range():
    assert fishing_scorer._score_pressure(-5.0) == 100
    assert isinstance(fishing_scorer._score_pressure(-5.0), int)
    for score in (
        fishing_scorer._score_pressure(-2.0),
        fishing_scorer._score_tide("incoming"),
        fishing_scorer._score_solunar(True, False, 1.0),
        fishing_scorer._score_time_of_day(6, "2026-07-10T06:00:00Z", "2026-07-10T19:00:00Z"),
    ):
        assert isinstance(score, int)
        assert 0 <= score <= 100


def test_overall_score_rounds_and_clamps():
    forecast = score_fishing(
        pressure_hpa=1013.0,
        pressure_trend_hpa_3hr=-2.0,  # falling -> 80
        tide_state="peak_flow",  # 70
        water_temp_f=70.0,
        hour_utc=14,
        sunrise_utc="2026-07-10T06:00:00Z",
        sunset_utc="2026-07-10T19:00:00Z",
        solunar_intensity=0.5,
        is_during_major_period=False,
        is_during_minor_period=True,  # 60
        species=[],
        target_category="saltwater_inshore",
        month=7,
    )
    # Four-component weighted base (T6.2 weights: pressure .375, tide .3125,
    # solunar .1875, time_of_day .125 — water temperature no longer scored
    # at the category level, see fishing_scorer.py module docstring):
    # 80*.375 + 70*.3125 + 60*.1875 + 30*.125 = 30+21.875+11.25+3.75 = 66.875 -> 67
    assert isinstance(forecast.overallScore, int)
    assert forecast.overallScore == 67
    assert 0 <= forecast.overallScore <= 100


# ---------------------------------------------------------------------------
# 7. "Great day" integration
# ---------------------------------------------------------------------------


def test_great_day_overall_score_at_least_70():
    forecast = score_fishing(
        pressure_hpa=1005.0,
        pressure_trend_hpa_3hr=-4.0,  # rapid drop -> 100
        tide_state="outgoing",  # 100
        water_temp_f=72.0,  # within saltwater_inshore optimal (65-80) -> 100
        hour_utc=6,  # dawn (== sunrise) -> 100
        sunrise_utc="2026-07-10T06:00:00Z",
        sunset_utc="2026-07-10T19:00:00Z",
        solunar_intensity=1.0,
        is_during_major_period=True,  # major + peak intensity -> 100
        is_during_minor_period=False,
        species=["redfish", "flounder"],
        target_category="saltwater_inshore",
        month=7,
    )
    assert forecast.overallScore >= 70
    assert forecast.pressureScore == 100
    assert forecast.tideScore == 100
    assert forecast.waterTempScore is None
    assert forecast.solunarScore == 100
    assert forecast.timeofdayScore == 100


# ---------------------------------------------------------------------------
# 8. "Bad day" integration
# ---------------------------------------------------------------------------


def test_bad_day_overall_score_at_most_40():
    forecast = score_fishing(
        pressure_hpa=1015.0,
        pressure_trend_hpa_3hr=0.0,  # stable -> 50
        tide_state="slack_low",  # 20
        water_temp_f=50.0,  # marginal for saltwater_inshore (45-90, not good 55-85) -> 50
        hour_utc=14,  # midday -> 30
        sunrise_utc="2026-07-10T06:00:00Z",
        sunset_utc="2026-07-10T19:00:00Z",
        solunar_intensity=0.0,
        is_during_major_period=False,
        is_during_minor_period=False,  # outside any period -> 30
        species=[],
        target_category="saltwater_inshore",
        month=7,
    )
    assert forecast.overallScore <= 40


# ---------------------------------------------------------------------------
# 9. Habitat features
# ---------------------------------------------------------------------------


def test_habitat_features_detects_dropoff():
    profile = [
        {"distance_m": 0.0, "depth_m": 2.0},
        {"distance_m": 100.0, "depth_m": 12.0},  # 10m change over 100m -> dropoff
        {"distance_m": 300.0, "depth_m": 13.0},
    ]
    features = get_habitat_features(profile)
    assert any(f["type"] == "dropoff" for f in features)
    dropoff = next(f for f in features if f["type"] == "dropoff")
    assert dropoff["distance_m"] == 100.0
    assert dropoff["depth_m"] == 12.0


def test_habitat_features_empty_for_no_profile():
    assert get_habitat_features(None) == []
    assert get_habitat_features([]) == []


# ---------------------------------------------------------------------------
# 10. Closed season produces species score 0
# ---------------------------------------------------------------------------


def test_snook_july_closed_season_species_score_zero():
    result = fishing_scorer._score_one_species(
        "Snook",
        weighted_base_score=100.0,  # best-case environmental conditions
        water_temp_f=80.0,  # within snook's optimal range -> would otherwise score high
        tide_state="incoming",  # matches snook's tide preference
        time_bucket="night",  # matches snook's time preference
        month=7,  # closed season (Jun-Aug)
        locale="en",
    )
    assert result["score"] == 0
    assert result["status"] == i18n.t("fishing.species_status.inactive", "en")
    assert "Closed season" in result["note"]


def test_species_score_full_pipeline_via_score_fishing():
    forecast = score_fishing(
        pressure_hpa=1005.0,
        pressure_trend_hpa_3hr=-4.0,
        tide_state="outgoing",
        water_temp_f=72.0,
        hour_utc=6,
        sunrise_utc="2026-07-10T06:00:00Z",
        sunset_utc="2026-07-10T19:00:00Z",
        solunar_intensity=1.0,
        is_during_major_period=True,
        is_during_minor_period=False,
        species=["Redfish", "Snook"],
        target_category="saltwater_inshore",
        month=7,  # snook closed, redfish not seasonally adjusted in July
    )
    assert forecast.speciesScores is not None
    by_name = {s["name"]: s for s in forecast.speciesScores}
    assert by_name["Snook"]["score"] == 0
    assert "Closed season" in by_name["Snook"]["note"]
    assert by_name["Redfish"]["score"] > 0


# ---------------------------------------------------------------------------
# Locale wiring (surf_scorer.py precedent: _LOCALE_KEYS documents the v1
# English source; i18n.t() falls back to the key itself until locale files
# are wired — assert equality against t(), not a hardcoded literal string)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "fishing.period.early_morning",
        "fishing.period.morning",
        "fishing.period.midday",
        "fishing.period.afternoon",
        "fishing.period.evening",
        "fishing.period.night",
        "fishing.species_status.active",
        "fishing.species_status.less_active",
        "fishing.species_status.inactive",
    ],
)
def test_locale_keys_documented(key):
    assert key in fishing_scorer._LOCALE_KEYS
    assert fishing_scorer._LOCALE_KEYS[key]


def test_period_label_resolves_through_i18n():
    forecast = score_fishing(
        pressure_hpa=1013.0,
        pressure_trend_hpa_3hr=0.0,
        tide_state="incoming",
        water_temp_f=70.0,
        hour_utc=6,  # early_morning bucket
        sunrise_utc=None,
        sunset_utc=None,
        solunar_intensity=0.5,
        is_during_major_period=False,
        is_during_minor_period=False,
        species=[],
        target_category="saltwater_inshore",
        month=1,
    )
    assert forecast.periodLabel == i18n.t("fishing.period.early_morning")


# ---------------------------------------------------------------------------
# Output shape sanity
# ---------------------------------------------------------------------------


def test_score_fishing_returns_fishing_forecast_with_expected_shape():
    forecast = score_fishing(
        pressure_hpa=1013.0,
        pressure_trend_hpa_3hr=-2.0,
        tide_state="incoming",
        water_temp_f=70.0,
        hour_utc=6,
        sunrise_utc="2026-07-10T06:00:00Z",
        sunset_utc="2026-07-10T19:00:00Z",
        solunar_intensity=0.5,
        is_during_major_period=False,
        is_during_minor_period=True,
        species=["walleye"],
        target_category="freshwater_sport",
        month=4,
        period_start_utc="2026-07-10T05:00:00Z",
        period_end_utc="2026-07-10T07:00:00Z",
    )
    assert isinstance(forecast, FishingForecast)
    assert forecast.periodStart == "2026-07-10T05:00:00Z"
    assert forecast.periodEnd == "2026-07-10T07:00:00Z"
    assert isinstance(forecast.periodLabel, str) and forecast.periodLabel
    for field in (
        forecast.overallScore,
        forecast.pressureScore,
        forecast.tideScore,
        forecast.solunarScore,
        forecast.timeofdayScore,
    ):
        assert isinstance(field, int)
        assert 0 <= field <= 100
    assert forecast.waterTempScore is None
    assert isinstance(forecast.conditionsText, str) and forecast.conditionsText
    assert forecast.speciesScores is not None
    assert forecast.speciesScores[0]["name"] == "Walleye"


def test_score_fishing_no_period_bounds_defaults_to_empty_string():
    forecast = score_fishing(
        pressure_hpa=1013.0,
        pressure_trend_hpa_3hr=0.0,
        tide_state="incoming",
        water_temp_f=70.0,
        hour_utc=6,
        sunrise_utc=None,
        sunset_utc=None,
        solunar_intensity=0.5,
        is_during_major_period=False,
        is_during_minor_period=False,
        species=[],
        target_category="saltwater_inshore",
        month=1,
    )
    assert forecast.periodStart == ""
    assert forecast.periodEnd == ""
