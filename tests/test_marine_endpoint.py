"""Basic tests for endpoints/marine.py (T5.1).

Scope, per brief: keep it simple.

  1. Module imports cleanly.
  2. wire_marine_config() accepts arbitrary "settings" without crashing.
  3. MarineLocationSummary construction from config (list endpoint).
  4. 404 when marine is not configured (list + detail, and detail with an
     unknown location_id).

Live provider calls (NDBC / WaveWatch III / NWS marine text via
GET /marine/{location_id} for a *found* location) are intentionally not
exercised here -- those are Phase 8 integration tests (PROVIDER-MANUAL §11
"No live-network tests in CI").
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo


def _wire_test_station() -> None:
    station_mod.reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-marine-station",
        name="Test Marine Station",
        latitude=34.2,
        longitude=-77.8,
        altitude=10.0,
        timezone="America/New_York",
        timezone_offset_minutes=-300,
        unit_system="US",
        hardware=None,
    )


def _wire_test_units(target_unit: str = "US") -> None:
    units_mod.reset_cache()
    units_mod.set_units_block({}, target_unit)


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset marine module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.marine as marine

    marine._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    marine._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()


def _make_location(location_id: str = "wrightsville_beach") -> MarineLocation:
    location = MarineLocation(
        location_id,
        {
            "name": "Wrightsville Beach",
            "lat": "34.2085",
            "lon": "-77.7964",
            "activities": ["surf", "beach_safety", "fishing"],
            "ndbc_station_ids": ["41110"],
            "coops_station_ids": ["8658163"],
            "nws_marine_zone_id": "AMZ250",
        },
    )
    location.validate()
    return location


# ---------------------------------------------------------------------------
# 1. Module imports cleanly
# ---------------------------------------------------------------------------


def test_module_imports_cleanly():
    import weewx_clearskies_api.endpoints.marine as marine

    assert hasattr(marine, "router")
    assert hasattr(marine, "wire_marine_config")
    assert hasattr(marine, "list_marine_locations")
    assert hasattr(marine, "get_marine_location")


# ---------------------------------------------------------------------------
# 2. wire_marine_config() accepts arbitrary settings without crashing
# ---------------------------------------------------------------------------


def test_wire_marine_config_accepts_bare_object_without_crash():
    import weewx_clearskies_api.endpoints.marine as marine

    marine.wire_marine_config(object())
    assert marine._marine_config is None


def test_wire_marine_config_accepts_marine_config_instance():
    import weewx_clearskies_api.endpoints.marine as marine

    config = MarineConfig(locations=[_make_location()])
    marine.wire_marine_config(config)
    assert marine._marine_config is config


def test_wire_marine_config_accepts_settings_with_marine_config_attribute():
    import weewx_clearskies_api.endpoints.marine as marine

    config = MarineConfig(locations=[_make_location()])

    class _FakeSettings:
        marine_config = config

    marine.wire_marine_config(_FakeSettings())
    assert marine._marine_config is config


# ---------------------------------------------------------------------------
# 3. MarineLocationSummary construction from config (list endpoint)
# ---------------------------------------------------------------------------


def test_list_marine_locations_returns_summary_per_location():
    import weewx_clearskies_api.endpoints.marine as marine

    _wire_test_station()
    _wire_test_units("US")
    location = _make_location()
    marine.wire_marine_config(MarineConfig(locations=[location]))

    response = marine.list_marine_locations()

    assert "data" in response
    assert "units" in response
    assert "stationClock" in response
    assert "freshness" in response
    assert "generatedAt" in response

    assert len(response["data"]) == 1
    summary = response["data"][0]
    assert summary["locationId"] == "wrightsville_beach"
    assert summary["name"] == "Wrightsville Beach"
    assert summary["coordinates"] == {"lat": 34.2085, "lon": -77.7964}
    assert set(summary["activities"]) == {"surf", "beach_safety", "fishing"}
    # Deferred population -- see endpoints/marine.py module docstring.
    assert summary["currentConditions"] is None
    assert summary["currentTide"] is None


def test_list_marine_locations_returns_one_summary_per_configured_location():
    import weewx_clearskies_api.endpoints.marine as marine

    _wire_test_station()
    _wire_test_units("US")
    loc_a = _make_location("wrightsville_beach")
    loc_b = _make_location("carolina_beach")
    marine.wire_marine_config(MarineConfig(locations=[loc_a, loc_b]))

    response = marine.list_marine_locations()

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"wrightsville_beach", "carolina_beach"}


# ---------------------------------------------------------------------------
# 4. 404 when marine is not configured
# ---------------------------------------------------------------------------


def test_list_marine_locations_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.marine as marine

    marine._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        marine.list_marine_locations()
    assert exc_info.value.status_code == 404


def test_list_marine_locations_404_when_no_locations_configured():
    import weewx_clearskies_api.endpoints.marine as marine

    marine.wire_marine_config(MarineConfig(locations=[]))

    with pytest.raises(HTTPException) as exc_info:
        marine.list_marine_locations()
    assert exc_info.value.status_code == 404


def test_get_marine_location_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.marine as marine

    marine._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        marine.get_marine_location("wrightsville_beach")
    assert exc_info.value.status_code == 404


def test_get_marine_location_404_when_location_id_unknown():
    import weewx_clearskies_api.endpoints.marine as marine

    marine.wire_marine_config(MarineConfig(locations=[_make_location()]))

    with pytest.raises(HTTPException) as exc_info:
        marine.get_marine_location("nonexistent_spot")
    assert exc_info.value.status_code == 404
