"""Unit tests for _suggest_group heuristic patterns in endpoints/setup.py.

Verifies positive and negative pattern matches for AQI pollutant columns,
weather observation columns, and edge cases where patterns must not over-match.
"""

from __future__ import annotations

import base64
import json
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from weewx_clearskies_api.endpoints.setup import _suggest_group

# ---------------------------------------------------------------------------
# PM2.5 variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["pm25", "pm_25", "PM2.5", "pm_2_5"])
def test_pm25_variants(col: str) -> None:
    assert _suggest_group(col) == "group_concentration"


# ---------------------------------------------------------------------------
# PM10
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["pm10", "PM10", "pm_10"])
def test_pm10(col: str) -> None:
    assert _suggest_group(col) == "group_concentration"


# ---------------------------------------------------------------------------
# PM1 (must not match PM10)
# ---------------------------------------------------------------------------

def test_pm1_suggests_concentration() -> None:
    assert _suggest_group("pm1") == "group_concentration"


def test_pm1_not_pm10() -> None:
    result_pm1 = _suggest_group("pm1")
    result_pm10 = _suggest_group("pm10")
    assert result_pm1 == "group_concentration"
    assert result_pm10 == "group_concentration"


# ---------------------------------------------------------------------------
# Gas pollutants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["no2", "so2", "o3", "co", "nh3"])
def test_gas_pollutants(col: str) -> None:
    assert _suggest_group(col) == "group_fraction"


# ---------------------------------------------------------------------------
# CO negative patterns (must not match cool, count, conf)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["cool", "count", "conf"])
def test_co_not_cool_or_count(col: str) -> None:
    assert _suggest_group(col) != "group_fraction"


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["myTemp", "indoor_temp_1"])
def test_temperature_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_temperature"


# ---------------------------------------------------------------------------
# Humidity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["extraHumid1", "soil_humidity"])
def test_humidity_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_percent"


# ---------------------------------------------------------------------------
# Pressure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["seaLevelPressure", "barometer_trend"])
def test_pressure_pattern(col: str) -> None:
    assert _suggest_group(col) == "group_pressure"


# ---------------------------------------------------------------------------
# Rain (positive) and not rainbow (negative)
# ---------------------------------------------------------------------------

def test_rain_total_suggests_rain() -> None:
    assert _suggest_group("rain_total") == "group_rain"


def test_rainbow_does_not_suggest_rain() -> None:
    assert _suggest_group("rainbow") != "group_rain"


# ---------------------------------------------------------------------------
# Wind speed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["windSpeed_2", "wind_gust_max"])
def test_wind_speed(col: str) -> None:
    assert _suggest_group(col) == "group_speed"


# ---------------------------------------------------------------------------
# Wind direction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("col", ["windDir_avg", "wind_direction"])
def test_wind_direction(col: str) -> None:
    assert _suggest_group(col) == "group_direction"


# ---------------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------------

def test_no_match() -> None:
    assert _suggest_group("fooBarBaz") is None


# ---------------------------------------------------------------------------
# Fishing pressure compatibility (Phase 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_id", "location_specific"),
    [
        ("aeris", False),
        ("openmeteo", False),
        ("openweathermap", False),
        ("nws", True),
    ],
)
def test_forecast_capabilities_declare_fishing_pressure_contract(
    provider_id: str, location_specific: bool
) -> None:
    """Every supported forecast provider declares its pressure behavior."""
    from weewx_clearskies_api.providers._common.dispatch import get_provider_module

    capability = get_provider_module(domain="forecast", provider_id=provider_id).CAPABILITY

    assert capability.fishing_pressure is not None
    assert capability.fishing_pressure.supported is True
    assert capability.fishing_pressure.location_specific is location_specific
    assert "pressure" in capability.supplied_canonical_fields
    assert "pressureSource" in capability.supplied_canonical_fields


def test_aeris_pressure_maps_to_provider_neutral_hourly_field() -> None:
    from weewx_clearskies_api.providers.forecast.aeris import (
        _AerisHourlyPeriod,
        _hourly_period_to_point,
    )

    point = _hourly_period_to_point(
        _AerisHourlyPeriod(
            timestamp=1788782400,
            dateTimeISO="2026-09-07T12:00:00-07:00",
            pressureMB=1012.4,
        ),
        "METRICWX",
    )

    assert point.validTime == "2026-09-07T19:00:00Z"
    assert point.pressure == 1012.4
    assert point.pressureSource == "aeris"


def test_openmeteo_pressure_maps_by_matching_hour_index() -> None:
    from weewx_clearskies_api.providers.forecast.openmeteo import (
        _OpenMeteoHourlyBlock,
        _zip_hourly,
    )

    points = _zip_hourly(
        _OpenMeteoHourlyBlock(
            time=["2026-09-07T12:00", "2026-09-07T13:00"],
            pressure_msl=[1008.1, None],
        ),
        0,
    )

    assert [point.pressure for point in points] == [1008.1, None]
    assert points[0].pressureSource == "openmeteo"
    assert points[1].pressureSource is None


def test_openweathermap_pressure_preserves_hpa_and_source() -> None:
    from weewx_clearskies_api.providers.forecast.openweathermap import (
        _owm_to_hourly_point,
        _OWMHourlyPeriod,
    )

    point = _owm_to_hourly_point(
        _OWMHourlyPeriod(dt=1788782400, pressure=1006.7),
        target_unit="METRICWX",
    )

    assert point.pressure == 1006.7
    assert point.pressureSource == "openweathermap"


def test_nws_pressure_uses_raw_grid_validity_interval_and_unit() -> None:
    from weewx_clearskies_api.providers.forecast.nws import (
        _NwsForecastPeriod,
        _NwsGridLayer,
        _NwsGridValue,
        _zip_hourly,
    )

    periods = [
        _NwsForecastPeriod(
            number=1,
            startTime="2026-09-07T12:00:00Z",
            endTime="2026-09-07T13:00:00Z",
            isDaytime=True,
            temperatureUnit="F",
        ),
        _NwsForecastPeriod(
            number=2,
            startTime="2026-09-07T13:00:00Z",
            endTime="2026-09-07T14:00:00Z",
            isDaytime=True,
            temperatureUnit="F",
        ),
    ]
    layer = _NwsGridLayer(
        uom="wmoUnit:Pa",
        values=[
            _NwsGridValue(
                validTime="2026-09-07T11:00:00Z/PT3H",
                value=100900.0,
            )
        ],
    )

    points = _zip_hourly(periods, target_unit="US", pressure_layer=layer)

    assert [point.pressure for point in points] == [1009.0, 1009.0]
    assert [point.pressureSource for point in points] == ["nws", "nws"]


def test_served_forecast_provider_provenance_uses_forecast_source_type() -> None:
    from weewx_clearskies_api.models.responses import HourlyForecastPoint
    from weewx_clearskies_api.services.marine_enrichment import (
        _provider_observation_updates,
    )

    previous = HourlyForecastPoint(
        validTime="2026-09-07T09:00:00Z",
        outTemp=18.0,
        windSpeed=3.0,
        pressure=1015.0,
        source="aeris",
    )
    current = HourlyForecastPoint(
        validTime="2026-09-07T12:00:00Z",
        outTemp=19.0,
        windSpeed=4.0,
        pressure=1013.5,
        source="aeris",
    )

    updates = _provider_observation_updates(
        current, [previous, current], "aeris"
    )

    assert updates["source"] == "aeris"
    assert updates["provenance"]["conditions"]["sourceType"] == "forecast"
    assert updates["provenance"]["pressure"]["sourceType"] == "forecast"
    assert updates["provenance"]["pressure"]["available"] is True
    assert updates["provenance"]["pressure"]["validTime"] == current.validTime


def test_browser_scoring_inputs_are_removed_and_replaced_by_server_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public query parameters cannot inject the private Fishing handoff."""
    from starlette.requests import Request

    import weewx_clearskies_api.services.companion_proxy as companion_proxy

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        companion_proxy,
        "_build_fishing_scoring_payload",
        lambda state, location_id: ("server-generated-inputs", []),
    )

    def capture_fetch(state, resolved_upstream, query_params):
        captured.update(dict(query_params))
        return 404, {"detail": "unknown location"}

    monkeypatch.setattr(companion_proxy, "_fetch_upstream", capture_fetch)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/fishing/harbor",
            "query_string": b"scoringInputs=attacker-payload&hours=24",
            "headers": [],
            "path_params": {"location_id": "harbor"},
        }
    )
    state = companion_proxy.CompanionProxyState(
        service_url="https://marine.example.test"
    )
    manifest = {
        "path": "/fishing/{location_id}",
        "upstream": "/fishing/{location_id}",
        "cache_ttl": 3600,
    }
    from weewx_clearskies_api.providers._common.cache import wire_cache_from_env

    wire_cache_from_env()

    response = companion_proxy._proxy_request(request, state, manifest)

    assert response.status_code == 404
    assert captured["hours"] == "24"
    assert captured["scoringInputs"] == "server-generated-inputs"
    assert captured["scoringInputs"] != "attacker-payload"


def test_fishing_transport_excludes_dense_tide_chart_but_retains_it_for_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dense chart cannot consume the scorer handoff's 64 KiB budget."""
    import weewx_clearskies_api.services.companion_proxy as companion_proxy
    import weewx_clearskies_api.services.marine_enrichment as marine_enrichment

    start = datetime(2026, 9, 7, tzinfo=UTC)
    end = start + timedelta(hours=6)
    tide_predictions = [
        {
            "time": (start + timedelta(minutes=index * 6)).isoformat().replace("+00:00", "Z"),
            "height": 1.0 + index / 1000,
            "type": "high" if index % 2 else "low",
        }
        for index in range(721)
    ]

    monkeypatch.setattr(marine_enrichment, "build_fishing_weather_inputs", lambda location_id: [])
    monkeypatch.setattr(
        companion_proxy,
        "_fishing_periods",
        lambda location_id: [
            (
                start.isoformat().replace("+00:00", "Z"),
                end.isoformat().replace("+00:00", "Z"),
                start + (end - start) / 2,
            )
        ],
    )

    def fetch(state, resolved_upstream, query_params):
        if resolved_upstream == "/tides/harbor":
            return 200, {"predictions": tide_predictions}
        if resolved_upstream == "/marine/harbor":
            return 200, {"forecast": []}
        raise AssertionError(f"unexpected upstream request: {resolved_upstream}")

    monkeypatch.setattr(companion_proxy, "_fetch_upstream", fetch)
    transport, public_tide_predictions = companion_proxy._build_fishing_scoring_payload(
        companion_proxy.CompanionProxyState(service_url="https://marine.example.test"), "harbor"
    )

    assert transport is not None
    padded = transport + "=" * (-len(transport) % 4)
    decoded = zlib.decompress(base64.urlsafe_b64decode(padded.encode("ascii")))
    payload = json.loads(decoded)
    assert len(decoded) <= 64 * 1024
    assert "tidePredictions" not in payload
    assert public_tide_predictions == tide_predictions


def test_fishing_proxy_restores_public_tide_chart_after_marine_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded private handoff must not remove the public chart field."""
    from starlette.requests import Request

    import weewx_clearskies_api.services.companion_proxy as companion_proxy

    tide_predictions = [{"time": "2026-09-07T12:00:00Z", "height": 1.1, "type": "high"}]
    monkeypatch.setattr(
        companion_proxy,
        "_build_fishing_scoring_payload",
        lambda state, location_id: ("server-generated-inputs", tide_predictions),
    )
    monkeypatch.setattr(
        companion_proxy,
        "_fetch_upstream",
        lambda state, resolved_upstream, query_params: (200, {"tidePredictions": [], "days": []}),
    )
    monkeypatch.setattr(
        companion_proxy,
        "_apply_response_transform",
        lambda body, *, manifest_entry: body,
    )

    class EmptyCache:
        def get(self, key):
            return None

        def set(self, key, value, ttl):
            return None

    monkeypatch.setattr(companion_proxy, "get_cache", EmptyCache)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/fishing/harbor",
            "query_string": b"",
            "headers": [],
            "path_params": {"location_id": "harbor"},
        }
    )
    state = companion_proxy.CompanionProxyState(service_url="https://marine.example.test")
    manifest = {
        "path": "/fishing/{location_id}",
        "upstream": "/fishing/{location_id}",
        "cache_ttl": 3600,
    }

    response = companion_proxy._proxy_request(request, state, manifest)

    assert response.status_code == 200
    assert json.loads(response.body)["tidePredictions"] == tide_predictions


def test_fishing_depth_temperature_candidates_exclude_nonlocal_ndbc_source() -> None:
    from weewx_clearskies_api.services.companion_proxy import (
        _fishing_depth_temperature_candidates,
    )

    body = {
        "forecast": [
            {
                "time": "2026-09-07T12:00:00Z",
                "waterTemp": 18.0,
                "waterTempProvenance": {
                    "available": True,
                    "source": "ofs:cbofs",
                    "sourceType": "modeled",
                    "validTime": "2026-09-07T12:00:00Z",
                    "coverageTier": "ofs",
                    "depthM": 25.0,
                },
            },
            {
                "time": "2026-09-07T12:00:00Z",
                "waterTemp": 18.0,
                "waterTempProvenance": {
                    "available": True,
                    "source": "ndbc:46253",
                    "sourceType": "observed",
                    "validTime": "2026-09-07T12:00:00Z",
                    "coverageTier": "observed",
                    "depthM": 25.0,
                },
            },
        ]
    }

    result = _fishing_depth_temperature_candidates(body)

    assert len(result) == 1
    assert result[0]["provenance"]["source"] == "ofs:cbofs"


def test_fishing_enrichment_exposes_selected_status_without_generic_phrase() -> None:
    from weewx_clearskies_api.services.marine_enrichment import _enrich_fishing_entry

    entry = {
        "selectedSpecies": "Black Sea Bass",
        "status": "active",
        "conditionsTextParts": {
            "overallLabelKey": "fishing.conditions.overall_great",
            "pressurePhraseKey": "fishing.conditions.pressure_stable",
            "tidePhraseKey": "fishing.conditions.tide_incoming",
            "activeSpeciesNames": ["Black Sea Bass"],
        },
    }

    _enrich_fishing_entry(entry, "en")

    assert entry["conditionsText"] == "Black Sea Bass: active"
    assert "conditionsTextParts" not in entry
    assert "overallLabelKey" not in entry
    assert "great conditions" not in entry["conditionsText"].lower()


def test_unavailable_fishing_enrichment_has_no_generic_conditions_phrase() -> None:
    from weewx_clearskies_api.services.marine_enrichment import _enrich_fishing_entry

    entry = {
        "selectedSpecies": "Black Sea Bass",
        "status": None,
        "score": None,
        "conditionsTextParts": {
            "overallLabelKey": "fishing.conditions.overall_great",
            "pressurePhraseKey": "fishing.conditions.pressure_unavailable",
            "tidePhraseKey": "fishing.conditions.tide_uncertain",
        },
    }

    _enrich_fishing_entry(entry, "en")

    assert "conditionsTextParts" not in entry
    assert "conditionsText" not in entry
    assert "overallLabelKey" not in entry
    assert all("condition" not in str(value).lower() for value in entry.values())



def test_cwf_labels_get_bounds_from_regular_forecast_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matched CWF labels get UTC bounds; unknown labels remain unaligned."""
    from zoneinfo import ZoneInfo

    import weewx_clearskies_api.services.companion_proxy as companion_proxy
    from weewx_clearskies_api.services.companion_proxy import (
        _join_regional_marine_additions,
    )

    # Fix the test's configured station timezone to UTC so the expected
    # regular-column boundaries are deterministic and directly inspectable.
    monkeypatch.setattr(
        companion_proxy,
        "_regular_forecast_timezone",
        lambda: ZoneInfo("UTC"),
    )
    anchor = datetime.now(tz=UTC).date()
    next_date = anchor + timedelta(days=1)

    body = {
        "regularForecast": [
            {
                "validTime": f"{anchor.isoformat()}T12:00:00Z",
                "windSpeed": 4.0,
                "source": "aeris",
            },
            {
                "validTime": f"{anchor.isoformat()}T18:00:00Z",
                "windSpeed": 5.0,
                "source": "aeris",
            },
            {
                "validTime": f"{next_date.isoformat()}T12:00:00Z",
                "windSpeed": 6.0,
                "source": "aeris",
            },
        ],
        "textForecast": [
            {
                "periodName": "TODAY",
                "periodStart": None,
                "periodEnd": None,
                "wind": "W winds 10 kt.",
                "seas": "Seas 2 ft.",
                "visibility": None,
                "weather": "A chance of showers.",
                "text": "W winds 10 kt. Seas 2 ft. A chance of showers.",
            },
            {
                "periodName": "TONIGHT",
                "periodStart": None,
                "periodEnd": None,
                "wind": "E winds 5 kt.",
                "seas": "Seas 1 ft.",
            },
            {
                "periodName": "UNRECOGNIZED LABEL",
                "periodStart": None,
                "periodEnd": None,
                "wind": "N winds 5 kt.",
                "seas": "Seas 1 ft.",
            },
        ],
    }

    result = _join_regional_marine_additions(
        body, manifest_path="/marine/{location_id}"
    )

    today_period = result["textForecast"][0]
    tonight_period = result["textForecast"][1]
    unknown_period = result["textForecast"][2]
    assert today_period["periodStart"] == f"{anchor.isoformat()}T06:00:00Z"
    assert today_period["periodEnd"] == f"{anchor.isoformat()}T18:00:00Z"
    assert tonight_period["periodStart"] == f"{anchor.isoformat()}T18:00:00Z"
    assert tonight_period["periodEnd"] == f"{next_date.isoformat()}T06:00:00Z"
    assert unknown_period["periodStart"] is None
    assert unknown_period["periodEnd"] is None

    assert result["regularForecast"][0]["windSpeed"] == 4.0
    assert result["regularForecast"][0]["source"] == "aeris"
    assert result["regularForecast"][0]["marineAdditions"] == {
        "source": "nws_cwf",
        "validTime": f"{anchor.isoformat()}T12:00:00Z",
        "periodStart": f"{anchor.isoformat()}T06:00:00Z",
        "periodEnd": f"{anchor.isoformat()}T18:00:00Z",
        "periodName": "TODAY",
        "issuanceTime": None,
        "wind": "W winds 10 kt.",
        "seas": "Seas 2 ft.",
        "visibility": None,
        "weather": "A chance of showers.",
        "text": "W winds 10 kt. Seas 2 ft. A chance of showers.",
    }
    assert result["regularForecast"][1]["marineAdditions"]["periodName"] == "TONIGHT"
    assert "marineAdditions" not in result["regularForecast"][2]


def _fishing_apply_request(provider_id: str = "nws"):
    from weewx_clearskies_api.endpoints.setup import (
        ApplyRequest,
        DatabaseApplyConfig,
        MarineApplyConfig,
        MarineFishingSpotApplyConfig,
        MarineLocationApplyConfig,
        ProviderConfig,
    )

    return ApplyRequest(
        database=DatabaseApplyConfig(
            kind="mysql", host="db", user="weather", password="secret", name="weewx"
        ),
        providers={
            "forecast": ProviderConfig(
                provider=provider_id,
                nws_user_agent_contact="weather@example.test",
            )
        },
        marine=MarineApplyConfig(
            locations=[
                MarineLocationApplyConfig(
                    id="harbor",
                    name="Harbour",
                    lat=33.65,
                    lon=-118.0,
                    activities=["fishing"],
                    fishing=MarineFishingSpotApplyConfig(
                        target_categories="saltwater_inshore"
                    ),
                )
            ]
        ),
    )


def test_setup_checks_every_nws_fishing_location_before_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from weewx_clearskies_api.endpoints import setup
    from weewx_clearskies_api.providers._common import dispatch
    from weewx_clearskies_api.providers.forecast import nws

    calls: list[tuple[float, float, str | None]] = []

    def check(*, lat: float, lon: float, user_agent_contact: str | None):
        calls.append((lat, lon, user_agent_contact))
        return nws.FishingPressureCheck(
            supported=True,
            provider="nws",
            checked_at="2026-09-07T12:00:00Z",
            valid_from="2026-09-07T12:00:00Z",
            valid_to="2026-09-07T18:00:00Z",
            reason=None,
        )

    monkeypatch.setattr(dispatch, "get_provider_module", lambda **kwargs: nws)
    monkeypatch.setattr(nws, "check_fishing_pressure", check)

    result = setup._check_fishing_pressure_compatibility(
        _fishing_apply_request(), tmp_path
    )

    assert calls == [(33.65, -118.0, "weather@example.test")]
    assert len(result) == 1
    assert result[0].location_id == "harbor"
    assert result[0].supported is True
    assert result[0].provider == "nws"
    assert result[0].valid_from == "2026-09-07T12:00:00Z"
    assert result[0].valid_to == "2026-09-07T18:00:00Z"


def test_setup_rejects_nws_fishing_location_without_pressure_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from weewx_clearskies_api.endpoints import setup
    from weewx_clearskies_api.providers._common import dispatch
    from weewx_clearskies_api.providers.forecast import nws

    monkeypatch.setattr(dispatch, "get_provider_module", lambda **kwargs: nws)
    monkeypatch.setattr(
        nws,
        "check_fishing_pressure",
        lambda **kwargs: nws.FishingPressureCheck(
            supported=False,
            provider="nws",
            checked_at="2026-09-07T12:00:00Z",
            valid_from="2026-09-07T12:00:00Z",
            valid_to="2026-09-07T13:00:00Z",
            reason="NWS pressure coverage is shorter than the required three-hour trend window",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        setup._check_fishing_pressure_compatibility(_fishing_apply_request(), tmp_path)

    assert exc_info.value.status_code == 422
    assert isinstance(exc_info.value.detail, dict)
    assert "choose a forecast provider" in exc_info.value.detail["message"].lower()
    assert exc_info.value.detail["fishing_pressure_checks"][0]["supported"] is False


def test_setup_rejects_provider_without_declared_pressure_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from weewx_clearskies_api.endpoints import setup
    from weewx_clearskies_api.providers._common import dispatch
    from weewx_clearskies_api.providers._common.capability import ProviderCapability

    unsupported = SimpleNamespace(
        CAPABILITY=ProviderCapability(
            provider_id="unsupported",
            domain="forecast",
            supplied_canonical_fields=(),
            geographic_coverage="global",
            fishing_pressure=None,
        )
    )
    monkeypatch.setattr(dispatch, "get_provider_module", lambda **kwargs: unsupported)

    with pytest.raises(HTTPException) as exc_info:
        setup._check_fishing_pressure_compatibility(
            _fishing_apply_request("unsupported"), tmp_path
        )

    assert exc_info.value.status_code == 422
    assert "hourly pressure" in str(exc_info.value.detail).lower()
