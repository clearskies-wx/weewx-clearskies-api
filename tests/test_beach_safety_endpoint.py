"""Basic tests for endpoints/beach_safety.py (T5.5).

Scope, per brief: keep it simple.

  1. Module imports cleanly.
  2. wire_beach_safety_config() accepts arbitrary settings without crashing,
     and matches the wire_marine_config() contract (bare MarineConfig,
     settings-like object with .marine_config, or anything else -> None).
  3. Safety classification logic (classify_sea_state):
       height=1ft period=12s -> "safe"; height=4ft -> "dangerous".
  4. Water temp comfort logic (classify_water_comfort):
       80F -> "comfortable"; 50F -> "dangerous".
  5. 404 when beach-safety is not configured (list + detail, and detail
     with an unknown location_id, and detail for a location without
     "beach_safety" in its activities).
  6. Alert filter helper (_filter_beach_safety_alerts) — ADR-090 event types.

Live provider calls (NWPS / NDBC / NWS SRF / CO-OPS / NWS alerts via
GET /beach-safety/{location_id} or the list endpoint for a *found*
location) are intentionally not exercised here -- those are Phase 8
integration tests (PROVIDER-MANUAL §11 "No live-network tests in CI").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo


def _wire_test_station() -> None:
    station_mod.reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-beach-safety-station",
        name="Test Beach Safety Station",
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
    """Reset beach_safety module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    beach_safety._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    beach_safety._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()


def _make_location(
    location_id: str = "wrightsville_beach", activities: list[str] | None = None
) -> MarineLocation:
    location = MarineLocation(
        location_id,
        {
            "name": "Wrightsville Beach",
            "lat": "34.2085",
            "lon": "-77.7964",
            "activities": activities or ["beach_safety"],
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
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert hasattr(beach_safety, "router")
    assert hasattr(beach_safety, "wire_beach_safety_config")
    assert hasattr(beach_safety, "list_beach_safety_locations")
    assert hasattr(beach_safety, "get_beach_safety")
    assert hasattr(beach_safety, "classify_sea_state")
    assert hasattr(beach_safety, "classify_water_comfort")


# ---------------------------------------------------------------------------
# 2. wire_beach_safety_config() wiring contract
# ---------------------------------------------------------------------------


def test_wire_beach_safety_config_accepts_bare_object_without_crash():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    beach_safety.wire_beach_safety_config(object())
    assert beach_safety._marine_config is None


def test_wire_beach_safety_config_accepts_marine_config_instance():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    config = MarineConfig(locations=[_make_location()])
    beach_safety.wire_beach_safety_config(config)
    assert beach_safety._marine_config is config


def test_wire_beach_safety_config_accepts_settings_with_marine_config_attribute():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    config = MarineConfig(locations=[_make_location()])

    class _FakeSettings:
        marine_config = config

    beach_safety.wire_beach_safety_config(_FakeSettings())
    assert beach_safety._marine_config is config


# ---------------------------------------------------------------------------
# 3. Sea-state safety classification (task brief thresholds)
# ---------------------------------------------------------------------------


def test_classify_sea_state_safe():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(1.0, 12.0) == "safe"


def test_classify_sea_state_dangerous_by_height():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(4.0, 12.0) == "dangerous"


def test_classify_sea_state_dangerous_by_short_period():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(1.0, 4.0) == "dangerous"


def test_classify_sea_state_caution_by_height():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(2.5, 12.0) == "caution"


def test_classify_sea_state_caution_by_period():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(1.0, 7.0) == "caution"


def test_classify_sea_state_none_when_data_missing():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_sea_state(None, 12.0) is None
    assert beach_safety.classify_sea_state(1.0, None) is None


# ---------------------------------------------------------------------------
# 4. Water temperature comfort classification (task brief thresholds)
# ---------------------------------------------------------------------------


def test_classify_water_comfort_comfortable():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_water_comfort(80.0) == "comfortable"


def test_classify_water_comfort_dangerous():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_water_comfort(50.0) == "dangerous"


def test_classify_water_comfort_cool():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_water_comfort(70.0) == "cool"


def test_classify_water_comfort_cold():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_water_comfort(60.0) == "cold"


def test_classify_water_comfort_none_when_missing():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety.classify_water_comfort(None) is None


# ---------------------------------------------------------------------------
# 5. 404 behavior
# ---------------------------------------------------------------------------


def test_list_beach_safety_locations_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    beach_safety._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        beach_safety.list_beach_safety_locations()
    assert exc_info.value.status_code == 404


def test_list_beach_safety_locations_404_when_none_configured():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    non_beach_safety_loc = _make_location("offshore_reef", activities=["fishing"])
    beach_safety.wire_beach_safety_config(MarineConfig(locations=[non_beach_safety_loc]))

    with pytest.raises(HTTPException) as exc_info:
        beach_safety.list_beach_safety_locations()
    assert exc_info.value.status_code == 404


def test_get_beach_safety_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    beach_safety._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        beach_safety.get_beach_safety("wrightsville_beach")
    assert exc_info.value.status_code == 404


def test_get_beach_safety_404_when_location_id_unknown():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    beach_safety.wire_beach_safety_config(MarineConfig(locations=[_make_location()]))

    with pytest.raises(HTTPException) as exc_info:
        beach_safety.get_beach_safety("nonexistent_spot")
    assert exc_info.value.status_code == 404


def test_get_beach_safety_404_when_location_lacks_activity():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    fishing_only_loc = _make_location("offshore_reef", activities=["fishing"])
    beach_safety.wire_beach_safety_config(MarineConfig(locations=[fishing_only_loc]))

    with pytest.raises(HTTPException) as exc_info:
        beach_safety.get_beach_safety("offshore_reef")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 6. Alert filter (ADR-090)
# ---------------------------------------------------------------------------


def test_filter_beach_safety_alerts_keeps_matching_events():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    alerts = [
        SimpleNamespace(event="High Surf Advisory", headline="High Surf Advisory issued"),
        SimpleNamespace(event="Rip Current Statement", headline="Rip Current Statement issued"),
        SimpleNamespace(event="Tornado Warning", headline="Tornado Warning issued"),
    ]

    filtered = beach_safety._filter_beach_safety_alerts(alerts)

    events = {a.event for a in filtered}
    assert events == {"High Surf Advisory", "Rip Current Statement"}


def test_filter_beach_safety_alerts_empty_input():
    import weewx_clearskies_api.endpoints.beach_safety as beach_safety

    assert beach_safety._filter_beach_safety_alerts([]) == []
