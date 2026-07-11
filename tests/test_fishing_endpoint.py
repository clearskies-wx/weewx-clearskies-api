"""Basic tests for endpoints/fishing.py (T5.4).

Scope, per brief: keep it simple.

  1. Module imports cleanly.
  2. wire_fishing_config() accepts arbitrary settings without crashing, and
     matches the wire_marine_config() contract (bare MarineConfig,
     settings-like object with .marine_config, or anything else -> None).
  3. 404 when fishing is not configured (list + detail, and detail with an
     unknown location_id, and detail for a location without "fishing" in
     its activities).
  4. List endpoint returns one card per configured fishing location.
  5. Tide-state classification helper logic (_tide_state_at).
  6. Period-building helper logic (_build_periods).

Live provider calls (NDBC / CO-OPS / solunar Skyfield computation via
GET /fishing/{location_id} for a *found* location) are intentionally not
exercised here -- those are Phase 8 integration tests (PROVIDER-MANUAL §11
"No live-network tests in CI").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo


def _wire_test_station() -> None:
    station_mod.reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-fishing-station",
        name="Test Fishing Station",
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
    """Reset fishing module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.fishing as fishing

    fishing._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    fishing._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()


def _make_location(
    location_id: str = "offshore_reef", activities: list[str] | None = None
) -> MarineLocation:
    location = MarineLocation(
        location_id,
        {
            "name": "Offshore Reef",
            "lat": "34.2085",
            "lon": "-77.7964",
            "activities": activities or ["fishing"],
            "ndbc_station_ids": ["41110"],
            "coops_station_ids": ["8658163"],
        },
    )
    location.validate()
    return location


# ---------------------------------------------------------------------------
# 1. Module imports cleanly
# ---------------------------------------------------------------------------


def test_module_imports_cleanly():
    import weewx_clearskies_api.endpoints.fishing as fishing

    assert hasattr(fishing, "router")
    assert hasattr(fishing, "wire_fishing_config")
    assert hasattr(fishing, "list_fishing_locations")
    assert hasattr(fishing, "get_fishing")


# ---------------------------------------------------------------------------
# 2. wire_fishing_config() wiring contract
# ---------------------------------------------------------------------------


def test_wire_fishing_config_accepts_bare_object_without_crash():
    import weewx_clearskies_api.endpoints.fishing as fishing

    fishing.wire_fishing_config(object())
    assert fishing._marine_config is None


def test_wire_fishing_config_accepts_marine_config_instance():
    import weewx_clearskies_api.endpoints.fishing as fishing

    config = MarineConfig(locations=[_make_location()])
    fishing.wire_fishing_config(config)
    assert fishing._marine_config is config


def test_wire_fishing_config_accepts_settings_with_marine_config_attribute():
    import weewx_clearskies_api.endpoints.fishing as fishing

    config = MarineConfig(locations=[_make_location()])

    class _FakeSettings:
        marine_config = config

    fishing.wire_fishing_config(_FakeSettings())
    assert fishing._marine_config is config


# ---------------------------------------------------------------------------
# 3. List endpoint
# ---------------------------------------------------------------------------


def test_list_fishing_locations_returns_card_per_location():
    import weewx_clearskies_api.endpoints.fishing as fishing

    _wire_test_station()
    _wire_test_units("US")
    loc_a = _make_location("offshore_reef")
    loc_b = _make_location("inlet_flats")
    fishing.wire_fishing_config(MarineConfig(locations=[loc_a, loc_b]))

    response = fishing.list_fishing_locations()

    assert "data" in response
    assert "stationClock" in response
    assert "freshness" in response
    assert "generatedAt" in response

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"offshore_reef", "inlet_flats"}


def test_list_fishing_locations_excludes_locations_without_fishing_activity():
    import weewx_clearskies_api.endpoints.fishing as fishing

    _wire_test_station()
    _wire_test_units("US")
    fishing_loc = _make_location("offshore_reef", activities=["fishing"])
    surf_only_loc = _make_location("wrightsville_beach", activities=["surf"])
    fishing.wire_fishing_config(MarineConfig(locations=[fishing_loc, surf_only_loc]))

    response = fishing.list_fishing_locations()

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"offshore_reef"}


# ---------------------------------------------------------------------------
# 4. 404 behavior
# ---------------------------------------------------------------------------


def test_list_fishing_locations_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.fishing as fishing

    fishing._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        fishing.list_fishing_locations()
    assert exc_info.value.status_code == 404


def test_list_fishing_locations_404_when_no_fishing_locations_configured():
    import weewx_clearskies_api.endpoints.fishing as fishing

    non_fishing_loc = _make_location("wrightsville_beach", activities=["surf"])
    fishing.wire_fishing_config(MarineConfig(locations=[non_fishing_loc]))

    with pytest.raises(HTTPException) as exc_info:
        fishing.list_fishing_locations()
    assert exc_info.value.status_code == 404


def test_get_fishing_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.fishing as fishing

    fishing._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        fishing.get_fishing("offshore_reef")
    assert exc_info.value.status_code == 404


def test_get_fishing_404_when_location_id_unknown():
    import weewx_clearskies_api.endpoints.fishing as fishing

    fishing.wire_fishing_config(MarineConfig(locations=[_make_location()]))

    with pytest.raises(HTTPException) as exc_info:
        fishing.get_fishing("nonexistent_spot")
    assert exc_info.value.status_code == 404


def test_get_fishing_404_when_location_lacks_fishing_activity():
    import weewx_clearskies_api.endpoints.fishing as fishing

    surf_only_loc = _make_location("wrightsville_beach", activities=["surf"])
    fishing.wire_fishing_config(MarineConfig(locations=[surf_only_loc]))

    with pytest.raises(HTTPException) as exc_info:
        fishing.get_fishing("wrightsville_beach")
    assert exc_info.value.status_code == 404


def test_get_fishing_404_when_no_fishing_spot_config():
    import weewx_clearskies_api.endpoints.fishing as fishing

    # Location has "fishing" activity but no [[fishing]] sub-block configured.
    location = _make_location("offshore_reef", activities=["fishing"])
    fishing.wire_fishing_config(MarineConfig(locations=[location], fishing_spots={}))

    with pytest.raises(HTTPException) as exc_info:
        fishing.get_fishing("offshore_reef")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 5. Tide-state classification helper
# ---------------------------------------------------------------------------


def test_tide_state_at_returns_incoming_when_no_predictions():
    import weewx_clearskies_api.endpoints.fishing as fishing

    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    assert fishing._tide_state_at([], now) == "incoming"


def test_tide_state_at_slack_high_near_high_tide():
    import weewx_clearskies_api.endpoints.fishing as fishing

    high_time = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    low_time = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
    earlier_low_time = high_time - timedelta(hours=6)
    predictions = [
        {"time": earlier_low_time.isoformat().replace("+00:00", "Z"), "height": 0.2, "type": "low"},
        {"time": high_time.isoformat().replace("+00:00", "Z"), "height": 1.8, "type": "high"},
        {"time": low_time.isoformat().replace("+00:00", "Z"), "height": 0.1, "type": "low"},
    ]
    now = high_time + timedelta(minutes=10)
    assert fishing._tide_state_at(predictions, now) == "slack_high"


def test_tide_state_at_outgoing_between_high_and_low():
    import weewx_clearskies_api.endpoints.fishing as fishing

    high_time = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    low_time = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
    predictions = [
        {"time": high_time.isoformat().replace("+00:00", "Z"), "height": 1.8, "type": "high"},
        {"time": low_time.isoformat().replace("+00:00", "Z"), "height": 0.1, "type": "low"},
    ]
    # 1 hour after high tide, well clear of the slack window and the
    # midpoint peak-flow window -- should read as "outgoing".
    now = high_time + timedelta(hours=1)
    assert fishing._tide_state_at(predictions, now) == "outgoing"


# ---------------------------------------------------------------------------
# 6. Period-building helper
# ---------------------------------------------------------------------------


def test_build_periods_returns_six_periods_in_order():
    import weewx_clearskies_api.endpoints.fishing as fishing

    sunrise = datetime(2026, 7, 10, 10, 30, tzinfo=UTC)  # ~6:30am EDT
    sunset = datetime(2026, 7, 11, 0, 15, tzinfo=UTC)  # ~8:15pm EDT

    periods = fishing._build_periods(sunrise, sunset)

    assert [p[0] for p in periods] == [
        "dawn",
        "morning",
        "midday",
        "afternoon",
        "evening",
        "night",
    ]
    for _label, start, end in periods:
        assert end > start


def test_build_periods_dawn_straddles_sunrise():
    import weewx_clearskies_api.endpoints.fishing as fishing

    sunrise = datetime(2026, 7, 10, 10, 30, tzinfo=UTC)
    sunset = datetime(2026, 7, 11, 0, 15, tzinfo=UTC)

    periods = fishing._build_periods(sunrise, sunset)
    dawn_start, dawn_end = periods[0][1], periods[0][2]
    assert dawn_start < sunrise < dawn_end
