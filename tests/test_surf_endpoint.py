"""Basic tests for endpoints/surf.py (T5.3, updated for SWAN+TruShore).

Scope, per brief: keep it simple.

  1. Module imports cleanly.
  2. wire_surf_config() accepts arbitrary settings without crashing, and
     matches the wire_marine_config() contract (bare MarineConfig,
     settings-like object with .marine_config, or anything else -> None).
  3. 404 when surf is not configured (list + detail, and detail with an
     unknown location_id, and detail for a location without "surf" in its
     activities).
  4. List endpoint returns one card per configured surf location.

Live provider calls (SWAN+TruShore / NDBC / CO-OPS / NWS SRF via
GET /surf/{location_id} for a *found* location) are intentionally not
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
        station_id="test-surf-station",
        name="Test Surf Station",
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
    """Reset surf module state + station/units caches around each test."""
    import weewx_clearskies_api.endpoints.surf as surf

    surf._marine_config = None
    station_mod.reset_cache()
    units_mod.reset_cache()
    yield
    surf._marine_config = None
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
            "activities": activities or ["surf", "beach_safety", "fishing"],
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
    import weewx_clearskies_api.endpoints.surf as surf

    assert hasattr(surf, "router")
    assert hasattr(surf, "wire_surf_config")
    assert hasattr(surf, "list_surf_locations")
    assert hasattr(surf, "get_surf")


# ---------------------------------------------------------------------------
# 2. wire_surf_config() wiring contract
# ---------------------------------------------------------------------------


def test_wire_surf_config_accepts_bare_object_without_crash():
    import weewx_clearskies_api.endpoints.surf as surf

    surf.wire_surf_config(object())
    assert surf._marine_config is None


def test_wire_surf_config_accepts_marine_config_instance():
    import weewx_clearskies_api.endpoints.surf as surf

    config = MarineConfig(locations=[_make_location()])
    surf.wire_surf_config(config)
    assert surf._marine_config is config


def test_wire_surf_config_accepts_settings_with_marine_config_attribute():
    import weewx_clearskies_api.endpoints.surf as surf

    config = MarineConfig(locations=[_make_location()])

    class _FakeSettings:
        marine_config = config

    surf.wire_surf_config(_FakeSettings())
    assert surf._marine_config is config


# ---------------------------------------------------------------------------
# 3. List endpoint returns one card per configured surf location
# ---------------------------------------------------------------------------


def test_list_surf_locations_returns_card_per_location():
    import weewx_clearskies_api.endpoints.surf as surf

    _wire_test_station()
    _wire_test_units("US")
    loc_a = _make_location("wrightsville_beach")
    loc_b = _make_location("carolina_beach")
    surf.wire_surf_config(MarineConfig(locations=[loc_a, loc_b]))

    response = surf.list_surf_locations()

    assert "data" in response
    assert "stationClock" in response
    assert "freshness" in response
    assert "generatedAt" in response

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"wrightsville_beach", "carolina_beach"}
    card = response["data"][0]
    assert card["qualityStars"] is None
    assert card["conditionsText"] is None


def test_list_surf_locations_excludes_locations_without_surf_activity():
    import weewx_clearskies_api.endpoints.surf as surf

    _wire_test_station()
    _wire_test_units("US")
    surf_loc = _make_location("wrightsville_beach", activities=["surf"])
    fishing_only_loc = _make_location("offshore_reef", activities=["fishing"])
    surf.wire_surf_config(MarineConfig(locations=[surf_loc, fishing_only_loc]))

    response = surf.list_surf_locations()

    ids = {entry["locationId"] for entry in response["data"]}
    assert ids == {"wrightsville_beach"}


# ---------------------------------------------------------------------------
# 4. 404 behavior
# ---------------------------------------------------------------------------


def test_list_surf_locations_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.surf as surf

    surf._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        surf.list_surf_locations()
    assert exc_info.value.status_code == 404


def test_list_surf_locations_404_when_no_surf_locations_configured():
    import weewx_clearskies_api.endpoints.surf as surf

    non_surf_loc = _make_location("offshore_reef", activities=["fishing"])
    surf.wire_surf_config(MarineConfig(locations=[non_surf_loc]))

    with pytest.raises(HTTPException) as exc_info:
        surf.list_surf_locations()
    assert exc_info.value.status_code == 404


def test_get_surf_404_when_config_is_none():
    import weewx_clearskies_api.endpoints.surf as surf

    surf._marine_config = None

    with pytest.raises(HTTPException) as exc_info:
        surf.get_surf("wrightsville_beach")
    assert exc_info.value.status_code == 404


def test_get_surf_404_when_location_id_unknown():
    import weewx_clearskies_api.endpoints.surf as surf

    surf.wire_surf_config(MarineConfig(locations=[_make_location()]))

    with pytest.raises(HTTPException) as exc_info:
        surf.get_surf("nonexistent_spot")
    assert exc_info.value.status_code == 404


def test_get_surf_404_when_location_lacks_surf_activity():
    import weewx_clearskies_api.endpoints.surf as surf

    fishing_only_loc = _make_location("offshore_reef", activities=["fishing"])
    surf.wire_surf_config(MarineConfig(locations=[fishing_only_loc]))

    with pytest.raises(HTTPException) as exc_info:
        surf.get_surf("offshore_reef")
    assert exc_info.value.status_code == 404


def test_get_surf_404_when_no_surf_spot_config():
    import weewx_clearskies_api.endpoints.surf as surf

    # Location has "surf" activity but no [[surf]] sub-block configured.
    location = _make_location("wrightsville_beach", activities=["surf"])
    surf.wire_surf_config(MarineConfig(locations=[location], surf_spots={}))

    with pytest.raises(HTTPException) as exc_info:
        surf.get_surf("wrightsville_beach")
    assert exc_info.value.status_code == 404
