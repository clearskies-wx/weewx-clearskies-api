"""Integration tests for the fishing enrichment pipeline (T8.1).

Exercises the full scoring chain documented in API-MANUAL.md §17 ("Fishing
scorer") for a Wrightsville Beach, NC saltwater-inshore fishing spot:

  1. Realistic environmental data (pressure trend, tide state, water
     temperature, wind) plus solunar data (from
     ``enrichment/solunar.py``'s ``compute_solunar()`` — Skyfield-computed,
     no external API) fed through ``enrichment/fishing_scorer.py``'s
     ``score_fishing()`` — verifies component scores and the composite
     ``overallScore`` are 0-100 integers, and per-species classifications
     are present.
  2. A live-network variant: real NDBC buoy data (pressure tendency, water
     temp, wind) fetched for a Wrightsville Beach-area buoy is fed into the
     same scorer.

Per API-MANUAL.md §17 and config/marine_config.py's FishingSpotConfig schema.
"""

from __future__ import annotations

from datetime import date

import pytest

from weewx_clearskies_api.config.marine_config import FishingSpotConfig
from weewx_clearskies_api.enrichment.fishing_scorer import score_fishing
from weewx_clearskies_api.enrichment.solunar import compute_solunar
from weewx_clearskies_api.models.responses import FishingForecast, SolunarTimes
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.buoy import ndbc

pytestmark = pytest.mark.integration

# Per-species `status` resolves through i18n.t() against
# "fishing.species_status.<value>" locale keys, which are documented in
# fishing_scorer.py's module docstring as authoritative-but-not-yet-wired
# into locales/*.json. Until wired, i18n.t() falls back to echoing the key
# itself (documented, safe v1 behavior) -- accept either form here so this
# suite doesn't regress once the locale files catch up.
_SPECIES_STATUSES = {"active", "less active", "inactive"}
_SPECIES_STATUSES |= {
    "fishing.species_status.active",
    "fishing.species_status.less_active",
    "fishing.species_status.inactive",
}

_WRIGHTSVILLE_LAT = 34.2085
_WRIGHTSVILLE_LON = -77.7964
_STATION_TZ = "America/New_York"

# Species matching NC's Atlantic Southeast biogeographic region
# (fishing_species.py BIOGEOGRAPHIC_REGIONS "atlantic_se": lat 25-41, lon -82..-71).
_WRIGHTSVILLE_SPECIES = ["Redfish", "Speckled Trout", "Flounder"]


def _wrightsville_fishing_config() -> FishingSpotConfig:
    config = FishingSpotConfig(
        {
            "target_category": "saltwater_inshore",
            "species": _WRIGHTSVILLE_SPECIES,
            "biogeographic_region": "atlantic_se",
        }
    )
    config.validate("wrightsville_beach")
    return config


class TestFishingPipelineWithRealisticEnvironmentalData:
    """Realistic pressure/tide/water-temp/solunar inputs -> score_fishing()."""

    def test_incoming_tide_falling_pressure_produces_0_to_100_period_scores(self) -> None:
        fishing_config = _wrightsville_fishing_config()
        solunar = compute_solunar(
            date(2026, 7, 11), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _STATION_TZ
        )
        assert isinstance(solunar, SolunarTimes)

        forecast = score_fishing(
            pressure_hpa=1015.0,
            pressure_trend_hpa_3hr=-2.5,  # falling
            tide_state="incoming",
            water_temp_f=72.0,  # within saltwater_inshore optimal range (65-80)
            hour_utc=11,  # ~7am local (America/New_York, summer UTC-4)
            sunrise_utc="2026-07-11T10:15:00Z",
            sunset_utc="2026-07-12T00:30:00Z",
            solunar_intensity=solunar.intensity,
            is_during_major_period=False,
            is_during_minor_period=True,
            species=fishing_config.species,
            target_category=fishing_config.target_categories[0],
            month=7,
            wind_speed=4.0,
            wind_direction=200.0,
            period_start_utc="2026-07-11T10:00:00Z",
            period_end_utc="2026-07-11T12:00:00Z",
        )

        assert isinstance(forecast, FishingForecast)
        for score in (
            forecast.overallScore,
            forecast.pressureScore,
            forecast.tideScore,
            forecast.solunarScore,
            forecast.timeofdayScore,
        ):
            assert isinstance(score, int)
            assert 0 <= score <= 100
        # waterTempScore is no longer a shared-base component (T6.2,
        # 2026-07-11) -- temperature is scored per-species only.
        assert forecast.waterTempScore is None

    def test_species_scores_present_with_0_to_100_int_scores_and_status(self) -> None:
        fishing_config = _wrightsville_fishing_config()
        solunar = compute_solunar(
            date(2026, 7, 11), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _STATION_TZ
        )

        forecast = score_fishing(
            pressure_hpa=1015.0,
            pressure_trend_hpa_3hr=-2.5,
            tide_state="outgoing",
            water_temp_f=72.0,
            hour_utc=11,
            sunrise_utc="2026-07-11T10:15:00Z",
            sunset_utc="2026-07-12T00:30:00Z",
            solunar_intensity=solunar.intensity,
            is_during_major_period=True,
            is_during_minor_period=False,
            species=fishing_config.species,
            target_category=fishing_config.target_categories[0],
            month=7,
        )

        assert forecast.speciesScores is not None
        assert len(forecast.speciesScores) == len(fishing_config.species)

        scored_names = {s["name"] for s in forecast.speciesScores}
        assert scored_names == {"Redfish", "Speckled Trout", "Flounder"}

        for species_score in forecast.speciesScores:
            assert isinstance(species_score["score"], int)
            assert 0 <= species_score["score"] <= 100
            assert species_score["status"] in _SPECIES_STATUSES

    def test_conditions_text_and_period_label_are_non_empty(self) -> None:
        fishing_config = _wrightsville_fishing_config()
        solunar = compute_solunar(
            date(2026, 7, 11), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _STATION_TZ
        )

        forecast = score_fishing(
            pressure_hpa=1015.0,
            pressure_trend_hpa_3hr=-4.0,  # rapid drop -- peak conditions
            tide_state="outgoing",
            water_temp_f=72.0,
            hour_utc=10,  # dawn window
            sunrise_utc="2026-07-11T10:15:00Z",
            sunset_utc="2026-07-12T00:30:00Z",
            solunar_intensity=solunar.intensity,
            is_during_major_period=True,
            is_during_minor_period=False,
            species=fishing_config.species,
            target_category=fishing_config.target_categories[0],
            month=7,
        )

        assert forecast.conditionsText
        assert forecast.periodLabel


@pytest.mark.live_network
class TestFishingPipelineWithLiveNdbcData:
    """Real NDBC buoy data (live NOAA) fed through score_fishing()."""

    @pytest.fixture(autouse=True)
    def _reset_ndbc_state(self):
        reset_cache_for_tests()
        ndbc._reset_http_client_for_tests()
        yield
        reset_cache_for_tests()
        ndbc._reset_http_client_for_tests()

    @pytest.mark.xfail(
        reason="NDBC station may be temporarily offline or missing pressure "
        "tendency / water temp fields for the current observation window",
        strict=False,
    )
    def test_live_ndbc_pressure_and_water_temp_produce_a_valid_forecast(self) -> None:
        fishing_config = _wrightsville_fishing_config()
        solunar = compute_solunar(date.today(), _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _STATION_TZ)

        result = ndbc.fetch(station_id="41110", include_spectral=False)
        observation = result["observation"]
        assert observation is not None, "Expected a live NDBC observation for station 41110"

        water_temp_f = (
            observation.waterTemp * 9.0 / 5.0 + 32.0 if observation.waterTemp is not None else None
        )

        forecast = score_fishing(
            pressure_hpa=observation.pressure,
            pressure_trend_hpa_3hr=observation.pressureTendency,
            tide_state="incoming",
            water_temp_f=water_temp_f,
            hour_utc=15,
            sunrise_utc=None,
            sunset_utc=None,
            solunar_intensity=solunar.intensity,
            is_during_major_period=False,
            is_during_minor_period=False,
            species=fishing_config.species,
            target_category=fishing_config.target_categories[0],
            month=date.today().month,
            wind_speed=observation.windSpeed,
            wind_direction=observation.windDirection,
        )

        assert isinstance(forecast, FishingForecast)
        assert 0 <= forecast.overallScore <= 100
