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

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from weewx_clearskies_api.config.marine_config import MarineConfig, MarineLocation, SurfSpotConfig
from weewx_clearskies_api.services import station as station_mod
from weewx_clearskies_api.services import units as units_mod
from weewx_clearskies_api.services.station import StationInfo
from weewx_clearskies_api.services.surf_1d_pipeline import PipelineResult
from weewx_clearskies_api.services.surfbeat_runner import SurfBeatResult


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


# ---------------------------------------------------------------------------
# 5. _determine_model_status() — the four truthful pipeline outcomes (T4A.4)
#
# Unit-tested directly against the extracted classifier rather than through
# a full get_surf() call, since this test file deliberately does not
# exercise live provider data (module docstring above) — the classifier is
# a pure function of PipelineResult, so it is fully testable in isolation.
# ---------------------------------------------------------------------------


def _make_pipeline_result(
    *,
    best_peak_face_height_m: float = 0.0,
    degraded: bool = False,
    has_transects: bool = True,
) -> PipelineResult:
    return PipelineResult(
        best_peak_face_height_m=best_peak_face_height_m,
        spot_average_face_height_m=best_peak_face_height_m,
        peel_angle_deg=None,
        peel_classification=None,
        peel_direction=None,
        transect_count=4,
        open_transect_count=4 if has_transects else 0,
        per_transect=["placeholder"] if has_transects else [],
        per_partition_breaks=[],
        degraded=degraded,
    )


def test_model_status_unavailable_when_pipeline_result_is_none():
    import weewx_clearskies_api.endpoints.surf as surf

    assert surf._determine_model_status(None) == "unavailable"


def test_model_status_unavailable_on_total_failure():
    """_degraded_result() sentinel: degraded=True AND per_transect=[] (no transects succeeded)."""
    import weewx_clearskies_api.endpoints.surf as surf

    result = _make_pipeline_result(best_peak_face_height_m=0.0, degraded=True, has_transects=False)
    assert surf._determine_model_status(result) == "unavailable"


def test_model_status_degraded_bulk_takes_priority_over_breaking_presence():
    """Bulk fallback (SPECOUT missing) maps to degraded_bulk regardless of face height."""
    import weewx_clearskies_api.endpoints.surf as surf

    flat_bulk = _make_pipeline_result(best_peak_face_height_m=0.0, degraded=True)
    breaking_bulk = _make_pipeline_result(best_peak_face_height_m=1.5, degraded=True)
    assert surf._determine_model_status(flat_bulk) == "degraded_bulk"
    assert surf._determine_model_status(breaking_bulk) == "degraded_bulk"


def test_model_status_ok_when_full_spectral_and_breaking():
    import weewx_clearskies_api.endpoints.surf as surf

    result = _make_pipeline_result(best_peak_face_height_m=1.2, degraded=False)
    assert surf._determine_model_status(result) == "ok"


def test_model_status_no_breaking_when_full_spectral_and_flat():
    import weewx_clearskies_api.endpoints.surf as surf

    result = _make_pipeline_result(best_peak_face_height_m=0.0, degraded=False)
    assert surf._determine_model_status(result) == "no_breaking"


# ---------------------------------------------------------------------------
# 6. No SWAN CURVE face-height computation remains (Adversarial Audit item 3)
# ---------------------------------------------------------------------------


def test_no_swan_curve_face_height_call_in_surf_module():
    """hsig_to_face_height() must not be called from endpoints/surf.py (T4A.4 Do step 1).

    The 1D (SwellTrack) pipeline is the sole source of breaking wave
    heights. Static grep-equivalent check kept as a test so a regression
    fails CI, not just an auditor's manual grep.
    """
    import inspect

    import weewx_clearskies_api.endpoints.surf as surf

    source = inspect.getsource(surf)
    assert "hsig_to_face_height" not in source
    assert '"degraded"' not in source
    assert "entry[\"degraded\"]" not in source


# ---------------------------------------------------------------------------
# 7. _resolve_timestep_wind() — shared ADR-094 wind-source rule (T4A.8)
#
# Extracted so the per-timestep loop (Step 5) and the SurfBeat IG-strip
# precomputation -- which runs BEFORE that loop and needs the wind at its
# own selected timestep (_best_ts) -- apply the identical rule without the
# SurfBeat block referencing the loop's ts_wind_speed/ts_wind_direction
# before they exist. That was the latent UnboundLocalError/F821 (a
# NameError subclass) T4A.8 fixes: a pre-existing defect, confirmed present
# at pre-round commit 0d87b28, not introduced this round.
# ---------------------------------------------------------------------------


def test_resolve_timestep_wind_t0_uses_station_wind_when_present():
    import weewx_clearskies_api.endpoints.surf as surf

    speed, direction, source = surf._resolve_timestep_wind(
        is_first_ts=True,
        valid_time="2026-07-25T00:00:00Z",
        wind_speed_station=5.5,
        wind_direction_station=180.0,
        wind_source_station="station",
        hrrr_field=None,
        lat=34.2,
        lon=-77.8,
    )
    assert speed == 5.5
    assert direction == 180.0
    assert source == "station"


def test_resolve_timestep_wind_t0_falls_back_to_hrrr_when_station_missing():
    import weewx_clearskies_api.endpoints.surf as surf

    hrrr_field = {
        "grids": [
            {
                "forecast_hour": 0,
                "nj": 2,
                "ni": 2,
                "lat_first": 33.0,
                "lat_last": 35.0,
                "lon_first": -79.0,
                "lon_last": -76.0,
                "u_earth": [[3.0, 3.0], [3.0, 3.0]],
                "v_earth": [[4.0, 4.0], [4.0, 4.0]],
            }
        ]
    }
    speed, direction, source = surf._resolve_timestep_wind(
        is_first_ts=True,
        valid_time="2026-07-25T00:00:00Z",
        wind_speed_station=None,
        wind_direction_station=None,
        wind_source_station="station",
        hrrr_field=hrrr_field,
        lat=34.2,
        lon=-77.8,
    )
    assert speed == pytest.approx(5.0)  # sqrt(3^2 + 4^2)
    assert source == "hrrr"


def test_resolve_timestep_wind_non_t0_uses_hrrr_at_its_own_valid_time():
    """Regression: a forecast timestep must resolve HRRR at ITS OWN
    valid_time, never at t=0's, and must ignore station wind even when
    station wind happens to be present (station wind is t=0-only, ADR-094)."""
    import weewx_clearskies_api.endpoints.surf as surf

    hrrr_field = {
        "grids": [
            {
                "valid_time": "2026-07-25T03:00:00Z",
                "nj": 2,
                "ni": 2,
                "lat_first": 33.0,
                "lat_last": 35.0,
                "lon_first": -79.0,
                "lon_last": -76.0,
                "u_earth": [[6.0, 6.0], [6.0, 6.0]],
                "v_earth": [[0.0, 0.0], [0.0, 0.0]],
            }
        ]
    }
    speed, direction, source = surf._resolve_timestep_wind(
        is_first_ts=False,
        valid_time="2026-07-25T03:00:00Z",
        wind_speed_station=99.0,  # must be ignored -- not t=0
        wind_direction_station=99.0,
        wind_source_station="station",
        hrrr_field=hrrr_field,
        lat=34.2,
        lon=-77.8,
    )
    assert speed == pytest.approx(6.0)
    assert direction == pytest.approx(270.0)
    assert source == "hrrr"


def test_resolve_timestep_wind_non_t0_no_hrrr_field_returns_none():
    import weewx_clearskies_api.endpoints.surf as surf

    speed, direction, source = surf._resolve_timestep_wind(
        is_first_ts=False,
        valid_time="2026-07-25T03:00:00Z",
        wind_speed_station=None,
        wind_direction_station=None,
        wind_source_station="station",
        hrrr_field=None,
        lat=34.2,
        lon=-77.8,
    )
    assert speed is None
    assert direction is None
    assert source == "hrrr"


# ---------------------------------------------------------------------------
# 8. SurfBeat IG-strip precomputation regression (T4A.8)
#
# Exercises the actual precomputation block inside get_surf() (not just the
# extracted helper) with surfbeat_enabled=True and cadence hours that match
# real SWAN timesteps -- the exact precondition the pre-fix code raised
# UnboundLocalError/F821 under. All external providers are monkeypatched to
# deterministic in-process fakes; no live network is exercised (PROVIDER-
# MANUAL §11).
# ---------------------------------------------------------------------------

_SB_T0_ISO = "2026-07-25T00:00:00Z"
_SB_T3_ISO = "2026-07-25T03:00:00Z"

# Station wind (t=0 rule, ADR-094) -- distinct from the HRRR values below so
# a test that mixed them up would be caught.
_SB_STATION_WIND_SPEED = 8.0
_SB_STATION_WIND_DIRECTION = 150.0

# HRRR field: constant 6.0 m/s from due north (u=6, v=0) at valid_time=_SB_T3_ISO
# only -- used for the non-t0 cadence hour.
_SB_HRRR_FIELD = {
    "grids": [
        {
            "valid_time": _SB_T3_ISO,
            "nj": 2,
            "ni": 2,
            "lat_first": 33.0,
            "lat_last": 35.0,
            "lon_first": -79.0,
            "lon_last": -76.0,
            "u_earth": [[6.0, 6.0], [6.0, 6.0]],
            "v_earth": [[0.0, 0.0], [0.0, 0.0]],
        }
    ]
}


def _sb_make_surf_spot_config() -> SurfSpotConfig:
    config = SurfSpotConfig(
        {
            "bottom_type": "sand",
            "topographic_feature": "straight_beach",
            "directional_exposure": {
                "N": "false", "NE": "false", "E": "true", "SE": "true",
                "S": "true", "SW": "true", "W": "false", "NW": "false",
            },
            "surfbeat_enabled": "true",
            "surfbeat_cadence_hours": "3",
        }
    )
    config.validate("wrightsville_beach")
    return config


def _sb_fake_swan_fetch(*, spot_id):
    return {
        "forecast": [
            {
                "time": _SB_T0_ISO,
                "distanceFromShore": 500.0,
                "waveHeight": 1.2,
                "wavePeriod": 9.0,
                "waveDirection": 190.0,
            },
            {
                "time": _SB_T3_ISO,
                "distanceFromShore": 500.0,
                "waveHeight": 1.4,
                "wavePeriod": 9.5,
                "waveDirection": 195.0,
            },
        ],
        "spectral": [],
        "run_time": _SB_T0_ISO,
        "data_age_seconds": 0,
    }


def _sb_fake_compute_spot_transects(**_kwargs):
    return [
        SimpleNamespace(
            is_structure_affected=False,
            bathymetric_profile=[
                {"distance_m": 0.0, "depth_m": 4.0},
                {"distance_m": 100.0, "depth_m": 9.0},
            ],
        )
    ]


class _SBFakeWeatherCache:
    def get_current_conditions(self, lat, lon):
        return {
            "windSpeed": _SB_STATION_WIND_SPEED,
            "windDirection": _SB_STATION_WIND_DIRECTION,
        }


def test_surfbeat_precompute_block_runs_without_nameerror(monkeypatch):
    """T4A.8 Accept: 'A test exercises the previously-unreachable path and
    passes.' surfbeat_enabled=True + two cadence hours (0 and 3) that exactly
    match SWAN timesteps -- the precondition the pre-fix code could not reach
    without raising."""
    import weewx_clearskies_api.endpoints.surf as surf

    _wire_test_station()
    _wire_test_units("US")

    location = _make_location("wrightsville_beach")
    surf_config = _sb_make_surf_spot_config()
    surf.wire_surf_config(
        MarineConfig(locations=[location], surf_spots={"wrightsville_beach": surf_config})
    )

    captured_calls: list[dict] = []

    def _fake_run_surfbeat_strip(**kwargs):
        captured_calls.append(kwargs)
        return SurfBeatResult(hs_ig_shoreline=0.3, set_timing_minutes=None, set_amplitude_m=0.3)

    monkeypatch.setattr(surf, "_compute_spot_transects", _sb_fake_compute_spot_transects)
    monkeypatch.setattr(surf, "run_surfbeat_strip", _fake_run_surfbeat_strip)
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.nearshore.swan.fetch", _sb_fake_swan_fetch
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.wind.hrrr.fetch", lambda **kw: _SB_HRRR_FIELD
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.buoy.ndbc.fetch", lambda **kw: {"spectral": []}
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.services.marine_location_resolver.is_station_served",
        lambda location_id: False,
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.services.marine_weather_cache.get_marine_weather_cache",
        lambda: _SBFakeWeatherCache(),
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.tides.coops.fetch", lambda **kw: {"predictions": []}
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.providers.marine.nws_srf.fetch", lambda **kw: {"forecasts": []}
    )
    monkeypatch.setattr(
        "weewx_clearskies_api.services.ocean_data_resolver.resolve",
        lambda **kw: SimpleNamespace(surface_temp=None),
    )

    # The point of this test: this call must not raise UnboundLocalError/
    # NameError from the SurfBeat block referencing ts_wind_speed /
    # ts_wind_direction before they exist.
    response = surf.get_surf("wrightsville_beach")

    assert "data" in response
    assert response["data"]["locationId"] == "wrightsville_beach"

    # At least the hr=0 (t0/station) and hr=3 (HRRR) cadence calls happened.
    assert len(captured_calls) >= 2

    # hr=0: _best_ts == t0 -> station wind (ADR-094 t=0 rule).
    hr0_call = captured_calls[0]
    assert hr0_call["wind_speed_ms"] == pytest.approx(_SB_STATION_WIND_SPEED)
    assert hr0_call["wind_direction_deg"] == pytest.approx(_SB_STATION_WIND_DIRECTION)

    # hr=3: _best_ts == t+3h (not t0) -> HRRR interpolated at t+3h's
    # valid_time, NOT the t0 station values. A helper that ignored
    # valid_time (e.g. always resolving forecast_hour=0) would fail this.
    hr3_call = captured_calls[1]
    assert hr3_call["wind_speed_ms"] == pytest.approx(6.0)
    assert hr3_call["wind_direction_deg"] == pytest.approx(270.0)
    assert hr3_call["wind_speed_ms"] != pytest.approx(_SB_STATION_WIND_SPEED)
