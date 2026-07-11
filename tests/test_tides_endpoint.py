"""Basic tests for endpoints/tides.py (T5.2).

Scope, per brief: keep it simple (mirrors tests/test_marine_endpoint.py).

  1. Module imports cleanly.
  2. wire_tides_config() accepts arbitrary "settings" without crashing.
  3. MarineLocationSummary construction from config (list endpoint),
     filtered to tide-capable (has a CO-OPS station) locations.
  4. 404 when tides are not configured (list + detail, and detail with an
     unknown / non-tide-capable location_id).

Live provider calls (CO-OPS via GET /tides/{location_id} for a *found*
location) are intentionally not exercised here -- Phase 8 integration
tests (PROVIDER-MANUAL §11 "No live-network tests in CI").
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
        station_id="test-tides-station",
        name="Test Tides Station",
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
    """Reset tides module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.tides as tides

    tides._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    tides._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()


def _make_location(
    location_id: str = "wrightsville_beach", *, with_coops: bool = True
) -> MarineLocation:
    section = {
        "name": "Wrightsville Beach",
        "lat": "34.2085",
        "lon": "-77.7964",
        "activities": ["surf", "beach_safety", "fishing"],
        "ndbc_station_ids": ["41110"],
        "nws_marine_zone_id": "AMZ250",
    }
    if with_coops:
        section["coops_station_ids"] = ["8658163"]
    location = MarineLocation(location_id, section)
    location.validate()
    return location


# ---------------------------------------------------------------------------
# 1. Module imports cleanly
# ---------------------------------------------------------------------------


def test_module_imports_cleanly():
    import weewx_clearskies_api.endpoints.tides as tides

    assert hasattr(tides, "router")
    assert hasattr(tides, "wire_tides_config")
    assert hasattr(tides, "list_tide_locations")
    assert hasattr(tides, "get_tide_location")


# ---------------------------------------------------------------------------
# 2. wire_tides_config() accepts arbitrary settings without crashing
# ---------------------------------------------------------------------------


def test_wire_tides_config_accepts_bare_object_without_crash():
    import weewx_clearskies_api.endpoints.tides as tides

    tides.wire_tides_config(object())
    assert tides._marine_config is None


def test_wire_tides_config_accepts_marine_config_instance():
    import weewx_clearskies_api.endpoints.tides as tides

    config = MarineConfig(locations=[_make_location()])
    tides.wire_tides_config(config)
    assert tides._marine_config is config


def test_wire_tides_config_accepts_settings_with_marine_config_attribute():
    import weewx_clearskies_api.endpoints.tides as tides

    config = MarineConfig(locations=[_make_location()])

    class _FakeSettings:
        marine_config = config

    tides.wire_tides_config(_FakeSettings())
    assert tides._marine_config is config


# ---------------------------------------------------------------------------
# 3. MarineLocationSummary construction from config, tide-capable filter
# ---------------------------------------------------------------------------


def test_list_tide_locations_returns_summary_for_coops_location():
    import weewx_clearskies_api.endpoints.tides as tides

    _wire_test_station()
    _wire_test_units("US")
    location = _make_location()
    tides.wire_tides_config(MarineConfig(locations=[location]))

    response = tides.list_tide_locations()

    assert "data" in response
    assert "units" in response
    assert "stationClock" in response
    assert "freshness" in response
    assert "generatedAt" in response

    assert len(response["data"]) == 1
    summary = response["data"][0]
    assert summary["locationId"] == "wrightsville_beach"
    assert summary["coordinates"] == {"lat": 34.2085, "lon": -77.7964}
    assert summary["currentTide"] is None


def test_list_tide_locations_excludes_locations_without_coops_station():
    import weewx_clearskies_api.endpoints.tides as tides

    _wire_test_station()
    _wire_test_units("US")
    tide_location = _make_location("wrightsville_beach", with_coops=True)
    no_tide_location = _make_location("inland_spot", with_coops=False)
    tides.wire_tides_config(MarineConfig(locations=[tide_location, no_tide_location]))

    response = tides.list_tide_locations()

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"wrightsville_beach"}


# ---------------------------------------------------------------------------
# 4. 404 when tides are not configured
# ---------------------------------------------------------------------------


def test_list_tide_locations_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.tides as tides

    tides._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        tides.list_tide_locations()
    assert exc_info.value.status_code == 404


def test_list_tide_locations_404_when_no_location_has_coops_station():
    import weewx_clearskies_api.endpoints.tides as tides

    no_tide_location = _make_location("inland_spot", with_coops=False)
    tides.wire_tides_config(MarineConfig(locations=[no_tide_location]))

    with pytest.raises(HTTPException) as exc_info:
        tides.list_tide_locations()
    assert exc_info.value.status_code == 404


def test_get_tide_location_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.tides as tides

    tides._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        tides.get_tide_location("wrightsville_beach")
    assert exc_info.value.status_code == 404


def test_get_tide_location_404_when_location_id_unknown():
    import weewx_clearskies_api.endpoints.tides as tides

    tides.wire_tides_config(MarineConfig(locations=[_make_location()]))

    with pytest.raises(HTTPException) as exc_info:
        tides.get_tide_location("nonexistent_spot")
    assert exc_info.value.status_code == 404


def test_get_tide_location_404_when_location_has_no_coops_station():
    import weewx_clearskies_api.endpoints.tides as tides

    no_tide_location = _make_location("inland_spot", with_coops=False)
    tides.wire_tides_config(MarineConfig(locations=[no_tide_location]))

    with pytest.raises(HTTPException) as exc_info:
        tides.get_tide_location("inland_spot")
    assert exc_info.value.status_code == 404
