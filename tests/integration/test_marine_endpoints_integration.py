"""Live-network, full-stack integration tests for the marine endpoints (T8.1).

Existing unit tests (tests/test_marine_endpoint.py, tests/test_surf_endpoint.py,
tests/test_fishing_endpoint.py, tests/test_beach_safety_endpoint.py,
tests/test_solunar.py) already cover config-wiring contracts, 404 shapes, and
list-view card shapes — each explicitly documents that live provider calls for
a *found* location are deferred to "Phase 8 integration tests" (see their
module docstrings, e.g. test_marine_endpoint.py: "Live provider calls (NDBC /
WaveWatch III / NWS marine text via GET /marine/{location_id} for a *found*
location) are intentionally not exercised here").

This module fills that gap: it wires a real Wrightsville Beach, NC marine
location (matching the ndbc/coops/nws_marine_zone_id ids already used
across the existing unit-test fixtures) and calls each endpoint's
detail-view function directly against the real NDBC/CO-OPS/WaveWatch
III/NWS marine/NWS SRF/NWS alerts services — actual provider calls ->
enrichment -> endpoint response, end to end. It intentionally does NOT
duplicate the 404 / config-wiring / list-shape assertions already covered.

Endpoints exercised:
  GET /api/v1/marine/{locationId}        -> endpoints/marine.py
  GET /api/v1/surf/{locationId}          -> endpoints/surf.py
  GET /api/v1/fishing/{locationId}       -> endpoints/fishing.py
  GET /api/v1/beach-safety/{locationId}  -> endpoints/beach_safety.py
  GET /api/v1/almanac/solunar            -> endpoints/almanac.py (not marine-gated)

Each endpoint wraps every provider call in its own try/except (graceful
degradation, per each endpoint module's docstring) — a single upstream NOAA
outage degrades one field to None rather than failing the request. These
tests therefore assert on envelope shape and "at least the parts backed by a
provider that responded" rather than requiring every optional field to be
populated.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.config.marine_config import (
    BeachSafetyConfig,
    FishingSpotConfig,
    MarineConfig,
    MarineLocation,
    SurfSpotConfig,
)
from weewx_clearskies_api.models.params import SolunarQueryParams
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

_LOCATION_ID = "wrightsville_beach"
_LAT = 34.2085
_LON = -77.7964

_SURF_SECTION = {
    "beach_facing_degrees": "135",
    "bottom_type": "sand",
    "topographic_feature": "straight_beach",
    "directional_exposure": {
        "N": "false",
        "NE": "false",
        "E": "true",
        "SE": "true",
        "S": "true",
        "SW": "true",
        "W": "false",
        "NW": "false",
    },
}


def _wire_test_station() -> None:
    station_mod.reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="integration-marine-station",
        name="Integration Marine Test Station",
        latitude=_LAT,
        longitude=_LON,
        altitude=10.0,
        timezone="America/New_York",
        timezone_offset_minutes=-300,
        unit_system="US",
        hardware=None,
    )


def _wire_test_units(target_unit: str = "US") -> None:
    units_mod.reset_cache()
    units_mod.set_units_block({}, target_unit)


def _make_wrightsville_config() -> MarineConfig:
    location = MarineLocation(
        _LOCATION_ID,
        {
            "name": "Wrightsville Beach",
            "lat": str(_LAT),
            "lon": str(_LON),
            "activities": ["marine", "surf", "fishing", "beach_safety"],
            "ndbc_station_ids": ["41110"],
            "coops_station_ids": ["8658163"],
            "nws_marine_zone_id": "AMZ250",
        },
    )
    location.validate()

    surf_config = SurfSpotConfig(_SURF_SECTION)
    surf_config.validate(_LOCATION_ID)

    fishing_config = FishingSpotConfig(
        {
            "target_category": "saltwater_inshore",
            "species": ["Redfish", "Speckled Trout", "Flounder"],
            "biogeographic_region": "atlantic_se",
        }
    )
    fishing_config.validate(_LOCATION_ID)

    beach_safety_config = BeachSafetyConfig({})

    return MarineConfig(
        locations=[location],
        surf_spots={_LOCATION_ID: surf_config},
        fishing_spots={_LOCATION_ID: fishing_config},
        beach_safety={_LOCATION_ID: beach_safety_config},
    )


@pytest.fixture(autouse=True)
def _reset_state():
    """Wire station/units + cold provider caches; unwire marine config after."""
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety
    import weewx_clearskies_api.endpoints.fishing as fishing
    import weewx_clearskies_api.endpoints.marine as marine
    import weewx_clearskies_api.endpoints.surf as surf

    _wire_test_station()
    _wire_test_units("US")
    reset_cache_for_tests()

    yield

    marine._marine_config = None
    surf._marine_config = None
    fishing._marine_config = None
    beach_safety._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    reset_cache_for_tests()


class TestMarineEndpointFullStack:
    """GET /marine/{locationId} -> real NDBC + WaveWatch III + NWS marine text."""

    def test_get_marine_location_returns_full_envelope(self) -> None:
        import weewx_clearskies_api.endpoints.marine as marine

        marine.wire_marine_config(_make_wrightsville_config())
        response = marine.get_marine_location(_LOCATION_ID)

        for key in ("data", "units", "stationClock", "freshness", "generatedAt"):
            assert key in response

        bundle = response["data"]
        assert bundle["locationId"] == _LOCATION_ID
        assert bundle["source"] == "ndbc+wavewatch+nws_marine"

    @pytest.mark.xfail(
        reason="WaveWatch III (ERDDAP) may be temporarily unavailable; the "
        "endpoint degrades gracefully to an empty forecast list rather than "
        "failing, so this asserts the happy path when ERDDAP responds",
        strict=False,
    )
    def test_wavewatch_forecast_is_populated(self) -> None:
        import weewx_clearskies_api.endpoints.marine as marine

        marine.wire_marine_config(_make_wrightsville_config())
        response = marine.get_marine_location(_LOCATION_ID)

        assert len(response["data"]["forecast"]) > 0


class TestSurfEndpointFullStack:
    """GET /surf/{locationId} -> real NWPS/WaveWatch III + NDBC + CO-OPS + NWS SRF."""

    def test_get_surf_returns_full_envelope(self) -> None:
        import weewx_clearskies_api.endpoints.surf as surf

        surf.wire_surf_config(_make_wrightsville_config())
        response = surf.get_surf(_LOCATION_ID)

        for key in ("data", "units", "stationClock", "freshness", "generatedAt"):
            assert key in response

        bundle = response["data"]
        assert bundle["locationId"] == _LOCATION_ID
        assert bundle["source"] == "nwps+wavewatch+ndbc+coops+nws_srf"

    @pytest.mark.xfail(
        reason="NWPS and its WaveWatch III fallback may both be temporarily "
        "unavailable, in which case the endpoint returns an empty forecast "
        "list rather than failing (graceful degradation)",
        strict=False,
    )
    def test_surf_quality_stars_present_when_wave_data_available(self) -> None:
        import weewx_clearskies_api.endpoints.surf as surf

        surf.wire_surf_config(_make_wrightsville_config())
        response = surf.get_surf(_LOCATION_ID)

        forecast_entries = response["data"]["forecast"]
        assert len(forecast_entries) > 0
        assert 1 <= forecast_entries[0]["qualityStars"] <= 5

    def test_tide_predictions_present_from_live_coops(self) -> None:
        import weewx_clearskies_api.endpoints.surf as surf

        surf.wire_surf_config(_make_wrightsville_config())
        response = surf.get_surf(_LOCATION_ID)

        assert len(response["data"]["tidePredictions"]) > 0


class TestFishingEndpointFullStack:
    """GET /fishing/{locationId} -> real NDBC + CO-OPS + Skyfield solunar."""

    def test_get_fishing_returns_full_envelope(self) -> None:
        import weewx_clearskies_api.endpoints.fishing as fishing

        fishing.wire_fishing_config(_make_wrightsville_config())
        response = fishing.get_fishing(_LOCATION_ID)

        for key in ("data", "units", "stationClock", "freshness", "generatedAt"):
            assert key in response

        bundle = response["data"]
        assert bundle["locationId"] == _LOCATION_ID
        assert bundle["source"] == "ndbc+coops+solunar"

    def test_three_days_of_periods_are_returned(self) -> None:
        import weewx_clearskies_api.endpoints.fishing as fishing

        fishing.wire_fishing_config(_make_wrightsville_config())
        response = fishing.get_fishing(_LOCATION_ID)

        days = response["data"]["days"]
        assert len(days) == 3
        for day in days:
            assert "solunar" in day
            assert day["solunar"]["moonPhase"]

    def test_period_scores_are_0_to_100_ints_when_periods_are_built(self) -> None:
        import weewx_clearskies_api.endpoints.fishing as fishing

        fishing.wire_fishing_config(_make_wrightsville_config())
        response = fishing.get_fishing(_LOCATION_ID)

        any_period_checked = False
        for day in response["data"]["days"]:
            for period in day["periods"]:
                any_period_checked = True
                assert isinstance(period["overallScore"], int)
                assert 0 <= period["overallScore"] <= 100
                assert period["speciesScores"] is not None
                species_names = {s["name"] for s in period["speciesScores"]}
                assert species_names == {"Redfish", "Speckled Trout", "Flounder"}
        assert any_period_checked, "Expected at least one scored period across 3 days"


class TestBeachSafetyEndpointFullStack:
    """GET /beach-safety/{locationId} -> real NDBC + NWS SRF + CO-OPS + NWS alerts."""

    def test_get_beach_safety_returns_full_envelope(self) -> None:
        import weewx_clearskies_api.endpoints.beach_safety as beach_safety

        beach_safety.wire_beach_safety_config(_make_wrightsville_config())
        response = beach_safety.get_beach_safety(_LOCATION_ID)

        for key in ("data", "units", "stationClock", "freshness", "generatedAt"):
            assert key in response

        bundle = response["data"]
        assert bundle["locationId"] == _LOCATION_ID
        assert bundle["source"] == "ndbc+nws_srf+coops+nws_alerts"

    def test_assessment_fields_present_in_response(self) -> None:
        import weewx_clearskies_api.endpoints.beach_safety as beach_safety

        beach_safety.wire_beach_safety_config(_make_wrightsville_config())
        response = beach_safety.get_beach_safety(_LOCATION_ID)

        assessment = response["data"]["assessment"]
        for key in (
            "safetyLevel",
            "ripCurrentRisk",
            "comfortLevel",
            "uvIndex",
            "activeAlerts",
        ):
            assert key in assessment

        if assessment["safetyLevel"] is not None:
            assert assessment["safetyLevel"] in ("safe", "caution", "dangerous")
        if assessment["comfortLevel"] is not None:
            assert assessment["comfortLevel"] in ("comfortable", "cool", "cold", "dangerous")
        if assessment["ripCurrentRisk"] is not None:
            assert assessment["ripCurrentRisk"] in ("low", "moderate", "high")

    def test_active_alerts_is_a_list(self) -> None:
        import weewx_clearskies_api.endpoints.beach_safety as beach_safety

        beach_safety.wire_beach_safety_config(_make_wrightsville_config())
        response = beach_safety.get_beach_safety(_LOCATION_ID)

        assert isinstance(response["data"]["assessment"]["activeAlerts"], list)


class TestSolunarEndpointFullStack:
    """GET /almanac/solunar -> Skyfield-computed solunar times (not marine-gated)."""

    def test_get_solunar_returns_solunar_times_for_wrightsville_beach(self) -> None:
        from weewx_clearskies_api.endpoints.almanac import get_solunar

        params = SolunarQueryParams(lat=_LAT, lon=_LON, days=1)
        response = get_solunar(params)

        for key in ("data", "generatedAt", "stationClock", "freshness"):
            assert key in response

        data = response["data"]
        assert data["moonPhase"] in (
            "new",
            "waxing_crescent",
            "first_quarter",
            "waxing_gibbous",
            "full",
            "waning_gibbous",
            "last_quarter",
            "waning_crescent",
        )
        assert 0.0 <= data["moonIllumination"] <= 1.0
        assert len(data["majorPeriods"]) == 2
        assert len(data["minorPeriods"]) <= 2

    def test_get_solunar_multi_day_returns_a_list(self) -> None:
        from weewx_clearskies_api.endpoints.almanac import get_solunar

        params = SolunarQueryParams(lat=_LAT, lon=_LON, days=3)
        response = get_solunar(params)

        assert isinstance(response["data"], list)
        assert len(response["data"]) == 3
