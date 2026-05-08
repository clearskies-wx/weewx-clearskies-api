"""Unit tests for the forecast provider domain (3b round 2).

Covers per the task-3b-2 brief:
  - WMO code → text mapping (all documented codes; unknown code → None)
  - WMO code → precipType mapping (rain/freezing-rain/snow families; else → None)
  - Local ISO → UTC conversion (_local_iso_to_utc_iso8601)
  - Per-target-unit param mapping (US/METRIC/METRICWX; unknown → ProviderProtocolError)
  - _zip_hourly correctness (3-hour mini fixture → 3 HourlyForecastPoint records)
  - _zip_daily correctness (daily block → DailyForecastPoint records)
  - Wire-shape Pydantic (real fixture loads cleanly; missing required field → ValidationError;
    extra field ignored)
  - ForecastQueryParams (extra="forbid"; negative hours/days → 422; hours>384 → 422;
    days>16 → 422; defaults; missing-both OK)
  - Hourly/daily slice behaviour (bundle.hourly[:hours], bundle.daily[:days])
  - Module fetch — happy path (respx-mocked 200 with recorded fixture)
  - Module fetch — cache hit (memory + fakeredis; no outbound HTTP call)
  - Module fetch — 5xx → TransientNetworkError
  - Module fetch — 429 → QuotaExhausted
  - Module fetch — 400 with error envelope → ProviderProtocolError
  - Module fetch — malformed wire shape → ProviderProtocolError
  - Module fetch — hourly: null → bundle with hourly=[]
  - Capability registry — forecast module wired alongside alerts
  - /capabilities response — forecast configured; canonicalFieldsAvailable is union
  - /forecast endpoint — no provider configured → 200 source="none"
  - /forecast endpoint — Open-Meteo configured (respx-mocked) → 200 source="openmeteo"
  - /forecast endpoint — slice via query params (?hours=24&days=3)
  - /forecast endpoint — defaults (no params → 48 hourly, 7 daily)
  - /forecast endpoint — invalid query (?nuke=1, ?hours=-1, ?hours=999999, ?days=20) → 422
  - /forecast endpoint — Open-Meteo down → 502 ProviderProblem TransientNetworkError
  - /forecast endpoint — Open-Meteo quota exhausted → 503 ProviderProblem QuotaExhausted

No DB, no live network. respx mocks outbound httpx calls.

Wire-shape rule: fixtures loaded from tests/fixtures/providers/openmeteo/*.json
(real Open-Meteo response shape per rules/clearskies-process.md §Real schemas).
ADR references: ADR-007, ADR-010, ADR-017, ADR-018, ADR-019, ADR-020, ADR-038.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import respx
import httpx

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "openmeteo"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/openmeteo/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, and rate limiter to a clean state between tests."""
    from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.openmeteo import _reset_http_client_for_tests  # noqa: PLC0415
    import weewx_clearskies_api.providers.forecast.openmeteo as _om  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear the sliding-window rate limiter deque so consecutive tests don't
    # trip each other.  The deque is internal state on the module-level singleton.
    _om._rate_limiter._calls.clear()


# ---------------------------------------------------------------------------
# App fixture helpers
# ---------------------------------------------------------------------------


def _make_forecast_settings(provider: str | None = None) -> Any:
    """Build a Settings instance with a ForecastSettings block."""
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        AlertsSettings,
        ApiSettings,
        DatabaseSettings,
        ForecastSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
        Settings,
    )

    return Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({}),
        forecast=ForecastSettings({"provider": provider} if provider else {}),
    )


@pytest.fixture()
def forecast_client_no_provider() -> Any:
    """TestClient for the forecast endpoint with NO provider configured."""
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415

    _reset_provider_state()
    settings = _make_forecast_settings(provider=None)
    wire_cache_from_env()
    wire_providers([])
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def forecast_client_openmeteo() -> Any:
    """TestClient for the forecast endpoint with Open-Meteo configured."""
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

    _reset_provider_state()
    settings = _make_forecast_settings(provider="openmeteo")
    wire_cache_from_env()
    wire_providers([openmeteo.CAPABILITY])
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. WMO code → weatherText mapping
# ===========================================================================


class TestWmoCodeToText:
    """_WMO_CODE_TO_TEXT maps all documented codes; unknown code → None."""

    def test_code_0_is_clear_sky(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[0] == "Clear sky"

    def test_code_1_is_mainly_clear(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[1] == "Mainly clear"

    def test_code_2_is_partly_cloudy(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[2] == "Partly cloudy"

    def test_code_3_is_overcast(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[3] == "Overcast"

    def test_code_45_is_fog(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[45] == "Fog"

    def test_code_48_is_depositing_rime_fog(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert _WMO_CODE_TO_TEXT[48] == "Depositing rime fog"

    def test_drizzle_codes_51_53_55_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (51, 53, 55):
            assert code in _WMO_CODE_TO_TEXT, f"Drizzle code {code} missing from _WMO_CODE_TO_TEXT"

    def test_freezing_drizzle_codes_56_57_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (56, 57):
            assert code in _WMO_CODE_TO_TEXT, f"Freezing drizzle code {code} missing"

    def test_rain_codes_61_63_65_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (61, 63, 65):
            assert code in _WMO_CODE_TO_TEXT, f"Rain code {code} missing"

    def test_freezing_rain_codes_66_67_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (66, 67):
            assert code in _WMO_CODE_TO_TEXT, f"Freezing rain code {code} missing"

    def test_snow_codes_71_73_75_77_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (71, 73, 75, 77):
            assert code in _WMO_CODE_TO_TEXT, f"Snow code {code} missing"

    def test_rain_shower_codes_80_81_82_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (80, 81, 82):
            assert code in _WMO_CODE_TO_TEXT, f"Rain shower code {code} missing"

    def test_snow_shower_codes_85_86_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (85, 86):
            assert code in _WMO_CODE_TO_TEXT, f"Snow shower code {code} missing"

    def test_thunderstorm_code_95_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        assert 95 in _WMO_CODE_TO_TEXT

    def test_thunderstorm_hail_codes_96_99_present(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        for code in (96, 99):
            assert code in _WMO_CODE_TO_TEXT, f"Thunderstorm+hail code {code} missing"

    def test_unknown_wmo_code_returns_none(self) -> None:
        """Code 200 is not in the WMO code table → None, no exception."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        result = _WMO_CODE_TO_TEXT.get(200)
        assert result is None, f"Expected None for unknown code 200, got {result!r}"

    def test_all_documented_codes_covered(self) -> None:
        """Every WMO code documented in openmeteo.md is in the table."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_TEXT  # noqa: PLC0415
        # Per api-docs/openmeteo.md WMO codes table
        documented_codes = {
            0, 1, 2, 3,
            45, 48,
            51, 53, 55, 56, 57,
            61, 63, 65, 66, 67,
            71, 73, 75, 77,
            80, 81, 82, 85, 86,
            95, 96, 99,
        }
        missing = documented_codes - set(_WMO_CODE_TO_TEXT.keys())
        assert not missing, f"Missing WMO codes in _WMO_CODE_TO_TEXT: {sorted(missing)}"


# ===========================================================================
# 2. WMO code → precipType mapping
# ===========================================================================


class TestWmoCodeToPrecipType:
    """_WMO_CODE_TO_PRECIP_TYPE maps rain/freezing-rain/snow; else → None."""

    def test_drizzle_51_is_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(51) == "rain"

    def test_drizzle_53_is_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(53) == "rain"

    def test_drizzle_55_is_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(55) == "rain"

    def test_rain_codes_61_63_65_are_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (61, 63, 65):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "rain", (
                f"Rain code {code} should map to 'rain'"
            )

    def test_rain_showers_80_81_82_are_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (80, 81, 82):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "rain", (
                f"Rain shower code {code} should map to 'rain'"
            )

    def test_thunderstorm_95_96_99_are_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (95, 96, 99):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "rain", (
                f"Thunderstorm code {code} should map to 'rain'"
            )

    def test_freezing_drizzle_56_57_are_freezing_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (56, 57):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "freezing-rain", (
                f"Freezing drizzle code {code} should map to 'freezing-rain'"
            )

    def test_freezing_rain_66_67_are_freezing_rain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (66, 67):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "freezing-rain", (
                f"Freezing rain code {code} should map to 'freezing-rain'"
            )

    def test_snow_codes_71_73_75_77_are_snow(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (71, 73, 75, 77):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "snow", (
                f"Snow code {code} should map to 'snow'"
            )

    def test_snow_shower_codes_85_86_are_snow(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        for code in (85, 86):
            assert _WMO_CODE_TO_PRECIP_TYPE.get(code) == "snow", (
                f"Snow shower code {code} should map to 'snow'"
            )

    def test_clear_sky_code_0_returns_none(self) -> None:
        """WMO 0 (Clear sky) has no precipitation → None."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(0) is None

    def test_partly_cloudy_code_2_returns_none(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(2) is None

    def test_fog_code_45_returns_none(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(45) is None

    def test_unknown_code_200_returns_none(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _WMO_CODE_TO_PRECIP_TYPE  # noqa: PLC0415
        assert _WMO_CODE_TO_PRECIP_TYPE.get(200) is None


# ===========================================================================
# 3. Local ISO → UTC conversion
# ===========================================================================


class TestLocalIsoToUtcConversion:
    """_local_iso_to_utc_iso8601 correctly converts station-local times to UTC Z."""

    def test_negative_offset_pdt_converts_to_utc(self) -> None:
        """'2026-04-30T16:00' with offset -25200 (PDT = UTC-7) → '2026-04-30T23:00:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        result = _local_iso_to_utc_iso8601("2026-04-30T16:00", -25200)
        assert result == "2026-04-30T23:00:00Z", (
            f"Expected '2026-04-30T23:00:00Z', got {result!r}"
        )

    def test_zero_offset_utc_is_unchanged(self) -> None:
        """'2026-04-30T16:00' with offset 0 (UTC) → '2026-04-30T16:00:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        result = _local_iso_to_utc_iso8601("2026-04-30T16:00", 0)
        assert result == "2026-04-30T16:00:00Z", (
            f"Expected '2026-04-30T16:00:00Z', got {result!r}"
        )

    def test_positive_offset_tokyo_jst_converts_to_utc(self) -> None:
        """'2026-04-30T09:00' with offset +32400 (JST = UTC+9) → '2026-04-30T00:00:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        result = _local_iso_to_utc_iso8601("2026-04-30T09:00", 32400)
        assert result == "2026-04-30T00:00:00Z", (
            f"Expected '2026-04-30T00:00:00Z', got {result!r}"
        )

    def test_fixture_hourly_index_0_converts_correctly(self) -> None:
        """Fixture first hourly time '2026-05-07T00:00' + offset -25200 → '2026-05-07T07:00:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        # Fixture: time[0] = "2026-05-07T00:00", utc_offset_seconds = -25200
        result = _local_iso_to_utc_iso8601("2026-05-07T00:00", -25200)
        assert result == "2026-05-07T07:00:00Z", (
            f"Expected '2026-05-07T07:00:00Z', got {result!r}"
        )

    def test_fixture_daily_sunrise_converts_correctly(self) -> None:
        """Fixture sunrise '2026-05-07T05:42' + offset -25200 → '2026-05-07T12:42:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        result = _local_iso_to_utc_iso8601("2026-05-07T05:42", -25200)
        assert result == "2026-05-07T12:42:00Z", (
            f"Expected '2026-05-07T12:42:00Z', got {result!r}"
        )

    def test_result_always_ends_with_z_suffix(self) -> None:
        """ADR-020: all UTC times use Z suffix, not +00:00."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _local_iso_to_utc_iso8601  # noqa: PLC0415
        result = _local_iso_to_utc_iso8601("2026-05-07T12:00", 0)
        assert result.endswith("Z"), f"Expected Z suffix, got {result!r}"
        assert "+00:00" not in result, f"Expected no +00:00 suffix, got {result!r}"


# ===========================================================================
# 4. Per-target-unit param mapping
# ===========================================================================


class TestTargetUnitParamMapping:
    """_TARGET_UNIT_TO_OPENMETEO_UNITS maps US/METRIC/METRICWX correctly."""

    def test_us_maps_to_fahrenheit_mph_inch(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _TARGET_UNIT_TO_OPENMETEO_UNITS  # noqa: PLC0415
        params = _TARGET_UNIT_TO_OPENMETEO_UNITS["US"]
        assert params["temperature_unit"] == "fahrenheit"
        assert params["wind_speed_unit"] == "mph"
        assert params["precipitation_unit"] == "inch"

    def test_metric_maps_to_celsius_kmh_mm(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _TARGET_UNIT_TO_OPENMETEO_UNITS  # noqa: PLC0415
        params = _TARGET_UNIT_TO_OPENMETEO_UNITS["METRIC"]
        assert params["temperature_unit"] == "celsius"
        assert params["wind_speed_unit"] == "kmh"
        assert params["precipitation_unit"] == "mm"

    def test_metricwx_maps_to_celsius_ms_mm(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _TARGET_UNIT_TO_OPENMETEO_UNITS  # noqa: PLC0415
        params = _TARGET_UNIT_TO_OPENMETEO_UNITS["METRICWX"]
        assert params["temperature_unit"] == "celsius"
        assert params["wind_speed_unit"] == "ms"
        assert params["precipitation_unit"] == "mm"

    def test_unknown_target_unit_raises_provider_protocol_error(self) -> None:
        """Unknown target_unit (defensive case) → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _reset_http_client_for_tests,
        )
        import weewx_clearskies_api.providers.forecast.openmeteo as _om  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _om._rate_limiter._calls.clear()

        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        with pytest.raises(ProviderProtocolError) as exc_info:
            from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415
            openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="INVALID",
                timezone="America/Los_Angeles",
            )
        assert "INVALID" in str(exc_info.value)


# ===========================================================================
# 5. _zip_hourly correctness
# ===========================================================================


class TestZipHourlyCorrectness:
    """_zip_hourly zips column arrays into HourlyForecastPoint records correctly."""

    def _make_hourly_block(self) -> Any:
        """Build a minimal 3-hour _OpenMeteoHourlyBlock for testing."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _OpenMeteoHourlyBlock  # noqa: PLC0415
        return _OpenMeteoHourlyBlock(
            time=["2026-05-07T00:00", "2026-05-07T01:00", "2026-05-07T02:00"],
            temperature_2m=[52.9, 52.1, 51.8],
            relative_humidity_2m=[90.0, 91.0, 92.0],
            wind_speed_10m=[1.8, 1.5, 1.2],
            wind_direction_10m=[180.0, 175.0, 170.0],
            wind_gusts_10m=[8.1, 7.5, 7.0],
            precipitation_probability=[0.0, 0.0, 0.0],
            precipitation=[0.0, 0.0, 0.0],
            weather_code=[3, 3, 2],
            cloud_cover=[100.0, 95.0, 80.0],
        )

    def test_three_hour_block_produces_three_records(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert len(result) == 3

    def test_first_record_valid_time_is_utc_z(self) -> None:
        """First hourly time '2026-05-07T00:00' + offset -25200 → '2026-05-07T07:00:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert result[0].validTime == "2026-05-07T07:00:00Z", (
            f"Expected UTC Z time, got {result[0].validTime!r}"
        )

    def test_first_record_temperature_matches_fixture(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert result[0].outTemp == 52.9

    def test_first_record_weather_code_is_string_three(self) -> None:
        """WMO code 3 (int) → canonical weatherCode '3' (string)."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert result[0].weatherCode == "3", (
            f"WMO int 3 should produce string '3', got {result[0].weatherCode!r}"
        )

    def test_first_record_weather_text_is_overcast(self) -> None:
        """WMO code 3 → weatherText 'Overcast'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert result[0].weatherText == "Overcast"

    def test_first_record_precip_type_is_none_for_clear_weather(self) -> None:
        """WMO code 3 (Overcast, no precip) → precipType is None."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        assert result[0].precipType is None

    def test_null_companion_array_entry_surfaces_as_none_in_record(self) -> None:
        """Null entry in a companion array appears as None in the canonical record."""
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoHourlyBlock,
            _zip_hourly,
        )
        block = _OpenMeteoHourlyBlock(
            time=["2026-05-07T00:00"],
            temperature_2m=[None],
            relative_humidity_2m=[None],
            wind_speed_10m=[None],
            wind_direction_10m=[None],
            wind_gusts_10m=[None],
            precipitation_probability=[None],
            precipitation=[None],
            weather_code=[None],
            cloud_cover=[None],
        )
        result = _zip_hourly(block, utc_offset_seconds=0)
        assert len(result) == 1
        assert result[0].outTemp is None
        assert result[0].windSpeed is None
        assert result[0].weatherCode is None
        assert result[0].weatherText is None
        assert result[0].precipType is None

    def test_all_records_have_source_openmeteo(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_hourly  # noqa: PLC0415
        block = self._make_hourly_block()
        result = _zip_hourly(block, utc_offset_seconds=-25200)
        for point in result:
            assert point.source == "openmeteo", (
                f"Expected source='openmeteo', got {point.source!r}"
            )


# ===========================================================================
# 6. _zip_daily correctness
# ===========================================================================


class TestZipDailyCorrectness:
    """_zip_daily zips column arrays into DailyForecastPoint records correctly."""

    def _make_daily_block(self) -> Any:
        """Build a minimal 2-day _OpenMeteoDailyBlock for testing."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _OpenMeteoDailyBlock  # noqa: PLC0415
        return _OpenMeteoDailyBlock(
            time=["2026-05-07", "2026-05-08"],
            temperature_2m_max=[61.9, 63.4],
            temperature_2m_min=[50.6, 51.2],
            precipitation_sum=[0.0, 0.0],
            precipitation_probability_max=[0.0, 5.0],
            wind_speed_10m_max=[5.4, 6.1],
            wind_gusts_10m_max=[10.5, 11.2],
            sunrise=["2026-05-07T05:42", "2026-05-08T05:40"],
            sunset=["2026-05-07T20:29", "2026-05-08T20:31"],
            uv_index_max=[5.95, 6.1],
            weather_code=[3, 2],
        )

    def test_two_day_block_produces_two_records(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert len(result) == 2

    def test_first_daily_valid_date_stays_station_local(self) -> None:
        """Daily validDate '2026-05-07' stays as-is (station-local date, per ADR-020)."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert result[0].validDate == "2026-05-07", (
            f"Expected station-local date '2026-05-07', got {result[0].validDate!r}"
        )

    def test_first_daily_temp_max_matches_fixture(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert result[0].tempMax == 61.9

    def test_first_daily_sunrise_converts_to_utc(self) -> None:
        """Sunrise '2026-05-07T05:42' + offset -25200 → '2026-05-07T12:42:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert result[0].sunrise == "2026-05-07T12:42:00Z", (
            f"Expected '2026-05-07T12:42:00Z', got {result[0].sunrise!r}"
        )

    def test_first_daily_sunset_converts_to_utc(self) -> None:
        """Sunset '2026-05-07T20:29' + offset -25200 → '2026-05-08T03:29:00Z'."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert result[0].sunset == "2026-05-08T03:29:00Z", (
            f"Expected '2026-05-08T03:29:00Z', got {result[0].sunset!r}"
        )

    def test_first_daily_narrative_is_none(self) -> None:
        """Open-Meteo doesn't supply narrative; it must be None."""
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        assert result[0].narrative is None

    def test_all_records_have_source_openmeteo(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import _zip_daily  # noqa: PLC0415
        block = self._make_daily_block()
        result = _zip_daily(block, utc_offset_seconds=-25200)
        for point in result:
            assert point.source == "openmeteo"


# ===========================================================================
# 7. Wire-shape Pydantic models
# ===========================================================================


class TestWireShapePydantic:
    """_OpenMeteoForecastResponse validates real fixture cleanly."""

    def test_real_fixture_loads_cleanly(self) -> None:
        """forecast.json (real Open-Meteo response) validates against _OpenMeteoForecastResponse."""
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoForecastResponse,
        )
        fixture = _load_fixture("forecast.json")
        model = _OpenMeteoForecastResponse.model_validate(fixture)
        assert model.latitude == pytest.approx(47.595562, rel=1e-4)
        assert model.utc_offset_seconds == -25200
        assert model.timezone == "America/Los_Angeles"
        assert model.hourly is not None
        assert len(model.hourly.time) == 168
        assert model.daily is not None
        assert len(model.daily.time) == 7

    def test_missing_required_latitude_raises_validation_error(self) -> None:
        """forecast_malformed.json (no latitude) → pydantic ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoForecastResponse,
        )
        fixture = _load_fixture("forecast_malformed.json")
        with pytest.raises(ValidationError):
            _OpenMeteoForecastResponse.model_validate(fixture)

    def test_extra_field_in_wire_response_is_ignored(self) -> None:
        """Future Open-Meteo additions don't break parsing (extra='ignore')."""
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoForecastResponse,
        )
        fixture = _load_fixture("forecast.json")
        fixture["new_field_from_openmeteo_2027"] = "some-future-value"
        # Should not raise
        model = _OpenMeteoForecastResponse.model_validate(fixture)
        assert model.latitude == pytest.approx(47.595562, rel=1e-4)

    def test_no_hourly_fixture_has_null_hourly_block(self) -> None:
        """forecast_no_hourly.json loads cleanly; hourly block is None."""
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoForecastResponse,
        )
        fixture = _load_fixture("forecast_no_hourly.json")
        model = _OpenMeteoForecastResponse.model_validate(fixture)
        assert model.hourly is None
        assert model.daily is not None
        assert len(model.daily.time) == 2

    def test_unknown_wmo_fixture_loads_cleanly(self) -> None:
        """forecast_unknown_wmo_code.json (code=200) loads without error."""
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _OpenMeteoForecastResponse,
        )
        fixture = _load_fixture("forecast_unknown_wmo_code.json")
        model = _OpenMeteoForecastResponse.model_validate(fixture)
        assert model.hourly is not None
        assert model.hourly.weather_code[0] == 200


# ===========================================================================
# 8. ForecastQueryParams
# ===========================================================================


class TestForecastQueryParams:
    """ForecastQueryParams validates hours/days with correct bounds."""

    def test_defaults_when_both_omitted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({})
        assert params.hours == 48
        assert params.days == 7

    def test_valid_hours_and_days_accepted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({"hours": "24", "days": "3"})
        assert params.hours == 24
        assert params.days == 3

    def test_hours_at_max_boundary_384_accepted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({"hours": "384"})
        assert params.hours == 384

    def test_days_at_max_boundary_16_accepted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({"days": "16"})
        assert params.days == 16

    def test_hours_zero_accepted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({"hours": "0"})
        assert params.hours == 0

    def test_days_zero_accepted(self) -> None:
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        params = ForecastQueryParams.model_validate({"days": "0"})
        assert params.days == 0

    def test_negative_hours_raises_validation_error(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        with pytest.raises(ValidationError):
            ForecastQueryParams.model_validate({"hours": "-1"})

    def test_negative_days_raises_validation_error(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        with pytest.raises(ValidationError):
            ForecastQueryParams.model_validate({"days": "-1"})

    def test_hours_above_max_385_raises_validation_error(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        with pytest.raises(ValidationError):
            ForecastQueryParams.model_validate({"hours": "385"})

    def test_days_above_max_17_raises_validation_error(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        with pytest.raises(ValidationError):
            ForecastQueryParams.model_validate({"days": "17"})

    def test_unknown_key_raises_validation_error(self) -> None:
        """extra='forbid' blocks unknown query keys per security-baseline §3.5."""
        from pydantic import ValidationError  # noqa: PLC0415
        from weewx_clearskies_api.models.params import ForecastQueryParams  # noqa: PLC0415
        with pytest.raises(ValidationError):
            ForecastQueryParams.model_validate({"hours": "24", "nuke": "1"})


# ===========================================================================
# 9. Hourly/daily slice behaviour
# ===========================================================================


class TestSliceBehaviour:
    """Slice is applied at the endpoint to the cached full bundle."""

    def _make_bundle_with_n_hourly_and_m_daily(
        self, hourly_count: int, daily_count: int
    ) -> Any:
        """Build a ForecastBundle with the specified hourly/daily point counts."""
        from weewx_clearskies_api.models.responses import (  # noqa: PLC0415
            DailyForecastPoint,
            ForecastBundle,
            HourlyForecastPoint,
        )
        hourly = [
            HourlyForecastPoint(
                validTime=f"2026-05-07T{i:02d}:00:00Z",
                source="openmeteo",
            )
            for i in range(hourly_count)
        ]
        daily = [
            DailyForecastPoint(
                validDate=f"2026-05-{7 + i:02d}",
                source="openmeteo",
            )
            for i in range(daily_count)
        ]
        return ForecastBundle(
            hourly=hourly,
            daily=daily,
            discussion=None,
            source="openmeteo",
            generatedAt="2026-05-07T12:00:00Z",
        )

    def test_hours_24_from_168_returns_24(self) -> None:
        """?hours=24 from a 168-point bundle returns exactly 24 points."""
        bundle = self._make_bundle_with_n_hourly_and_m_daily(168, 7)
        sliced = bundle.hourly[:24]
        assert len(sliced) == 24

    def test_hours_200_from_168_returns_168_full_available(self) -> None:
        """?hours=200 from a 168-point bundle returns all 168 (Python slice cap)."""
        bundle = self._make_bundle_with_n_hourly_and_m_daily(168, 7)
        sliced = bundle.hourly[:200]
        assert len(sliced) == 168

    def test_hours_0_returns_empty(self) -> None:
        """?hours=0 returns empty list."""
        bundle = self._make_bundle_with_n_hourly_and_m_daily(168, 7)
        sliced = bundle.hourly[:0]
        assert sliced == []

    def test_days_3_from_7_returns_3(self) -> None:
        """?days=3 from a 7-point bundle returns exactly 3 points."""
        bundle = self._make_bundle_with_n_hourly_and_m_daily(168, 7)
        sliced = bundle.daily[:3]
        assert len(sliced) == 3

    def test_days_0_returns_empty(self) -> None:
        bundle = self._make_bundle_with_n_hourly_and_m_daily(168, 7)
        sliced = bundle.daily[:0]
        assert sliced == []


# ===========================================================================
# 10. Module fetch — happy path (respx-mocked)
# ===========================================================================


class TestModuleFetchHappyPath:
    """fetch() with respx-mocked 200 returns correct ForecastBundle."""

    def test_fetch_returns_forecast_bundle_type(self) -> None:
        """fetch() returns ForecastBundle (not a list, not a dict — brief §9)."""
        from weewx_clearskies_api.models.responses import ForecastBundle  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert isinstance(result, ForecastBundle), (
            f"fetch() must return ForecastBundle, got {type(result).__name__}"
        )

    def test_fetch_returns_168_hourly_points(self) -> None:
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert len(result.hourly) == 168, (
            f"Expected 168 hourly points, got {len(result.hourly)}"
        )

    def test_fetch_returns_7_daily_points(self) -> None:
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert len(result.daily) == 7, (
            f"Expected 7 daily points, got {len(result.daily)}"
        )

    def test_fetch_discussion_is_none(self) -> None:
        """Open-Meteo never supplies a discussion; bundle.discussion is always None."""
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert result.discussion is None, (
            "Open-Meteo does not supply a discussion; bundle.discussion must be None"
        )

    def test_fetch_source_is_openmeteo(self) -> None:
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert result.source == "openmeteo"

    def test_fetch_first_hourly_valid_time_is_utc(self) -> None:
        """First hourly point validTime is UTC ISO-8601 with Z suffix (ADR-020)."""
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        first = result.hourly[0]
        assert first.validTime.endswith("Z"), (
            f"Expected Z-suffixed UTC time, got {first.validTime!r}"
        )
        # Fixture: time[0]='2026-05-07T00:00', utc_offset=-25200 → UTC 07:00
        assert first.validTime == "2026-05-07T07:00:00Z", (
            f"Expected '2026-05-07T07:00:00Z', got {first.validTime!r}"
        )

    def test_fetch_first_daily_valid_date_stays_station_local(self) -> None:
        """First daily point validDate is station-local date string (ADR-020)."""
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        first_daily = result.daily[0]
        assert first_daily.validDate == "2026-05-07", (
            f"Expected station-local '2026-05-07', got {first_daily.validDate!r}"
        )


# ===========================================================================
# 11. Module fetch — cache hit (no outbound call)
# ===========================================================================


class TestModuleFetchCacheHit:
    """Cache-hit path returns bundle without making an HTTP call."""

    def test_memory_cache_hit_skips_http_call(self) -> None:
        """Pre-populated MemoryCache → fetch() returns bundle; respx call count = 0."""
        from weewx_clearskies_api.models.responses import (  # noqa: PLC0415
            ForecastBundle,
            HourlyForecastPoint,
            DailyForecastPoint,
        )
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            MemoryCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
            fetch,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        import weewx_clearskies_api.providers.forecast.openmeteo as _om  # noqa: PLC0415
        _om._rate_limiter._calls.clear()

        # Inject MemoryCache and pre-populate with a serialized bundle
        cache = MemoryCache()

        cached_bundle = ForecastBundle(
            hourly=[HourlyForecastPoint(validTime="2026-05-07T07:00:00Z", source="openmeteo")],
            daily=[DailyForecastPoint(validDate="2026-05-07", source="openmeteo")],
            discussion=None,
            source="openmeteo",
            generatedAt="2026-05-07T07:00:00Z",
        )
        key = _build_cache_key(47.6062, -122.3321, "US")
        cache.set(key, cached_bundle.model_dump(mode="json"), ttl_seconds=1800)

        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = cache

        with respx.mock(base_url="https://api.open-meteo.com") as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json={})
            )
            result = fetch(lat=47.6062, lon=-122.3321, target_unit="US", timezone="America/Los_Angeles")
            assert mock.call_count == 0, (
                f"Cache hit should skip HTTP call; got {mock.call_count} calls"
            )

        assert isinstance(result, ForecastBundle)
        assert result.source == "openmeteo"

    def test_redis_cache_hit_via_fakeredis_skips_http_call(self) -> None:
        """fakeredis-backed cache hit → fetch() returns bundle; no HTTP call."""
        try:
            import fakeredis  # noqa: PLC0415
        except ImportError:
            pytest.skip("fakeredis not installed")

        from weewx_clearskies_api.models.responses import (  # noqa: PLC0415
            ForecastBundle,
            HourlyForecastPoint,
            DailyForecastPoint,
        )
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.forecast.openmeteo import (  # noqa: PLC0415
            _build_cache_key,
            _reset_http_client_for_tests,
            fetch,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        import weewx_clearskies_api.providers.forecast.openmeteo as _om2  # noqa: PLC0415
        _om2._rate_limiter._calls.clear()

        # Build a fakeredis-backed RedisCache by patching redis.Redis.from_url
        fake_redis = fakeredis.FakeRedis()

        class _FakeRedisCache(RedisCache):
            """RedisCache that uses a FakeRedis client instead of a real one."""

            def __init__(self) -> None:
                self._client = fake_redis
                self._redis_error_cls = __import__("redis").exceptions.RedisError

        cache = _FakeRedisCache()

        cached_bundle = ForecastBundle(
            hourly=[HourlyForecastPoint(validTime="2026-05-07T07:00:00Z", source="openmeteo")],
            daily=[DailyForecastPoint(validDate="2026-05-07", source="openmeteo")],
            discussion=None,
            source="openmeteo",
            generatedAt="2026-05-07T07:00:00Z",
        )
        key = _build_cache_key(47.6062, -122.3321, "US")
        cache.set(key, cached_bundle.model_dump(mode="json"), ttl_seconds=1800)

        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        cache_mod._cache = cache

        with respx.mock(base_url="https://api.open-meteo.com") as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json={})
            )
            result = fetch(lat=47.6062, lon=-122.3321, target_unit="US", timezone="America/Los_Angeles")
            assert mock.call_count == 0, (
                f"Redis cache hit should skip HTTP call; got {mock.call_count} calls"
            )

        assert isinstance(result, ForecastBundle)


# ===========================================================================
# 12. Module fetch — error paths
# ===========================================================================


class TestModuleFetchErrorPaths:
    """fetch() raises canonical ProviderError subclasses for upstream failures."""

    def _setup(self) -> None:
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

    def test_5xx_raises_transient_network_error(self) -> None:
        """Open-Meteo 503 → TransientNetworkError after retries exhausted."""
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        self._setup()

        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(503, text="Service Unavailable")
            )
            with pytest.raises(TransientNetworkError):
                openmeteo.fetch(
                    lat=47.6062,
                    lon=-122.3321,
                    target_unit="US",
                    timezone="America/Los_Angeles",
                )

    def test_429_raises_quota_exhausted(self) -> None:
        """Open-Meteo 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        self._setup()

        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with pytest.raises(QuotaExhausted):
                openmeteo.fetch(
                    lat=47.6062,
                    lon=-122.3321,
                    target_unit="US",
                    timezone="America/Los_Angeles",
                )

    def test_400_with_error_envelope_raises_provider_protocol_error(self) -> None:
        """Open-Meteo 400 + {error:true, reason:...} → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        self._setup()

        error_fixture = _load_fixture("forecast_400_error.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(400, json=error_fixture)
            )
            with pytest.raises(ProviderProtocolError):
                openmeteo.fetch(
                    lat=47.6062,
                    lon=-122.3321,
                    target_unit="US",
                    timezone="America/Los_Angeles",
                )

    def test_malformed_wire_shape_raises_provider_protocol_error(self) -> None:
        """200 response with missing required 'latitude' → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        self._setup()

        malformed_fixture = _load_fixture("forecast_malformed.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=malformed_fixture)
            )
            with pytest.raises(ProviderProtocolError):
                openmeteo.fetch(
                    lat=47.6062,
                    lon=-122.3321,
                    target_unit="US",
                    timezone="America/Los_Angeles",
                )

    def test_no_hourly_in_response_returns_bundle_with_empty_hourly(self) -> None:
        """forecast_no_hourly.json → ForecastBundle.hourly == []."""
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        self._setup()

        no_hourly_fixture = _load_fixture("forecast_no_hourly.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=no_hourly_fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert result.hourly == [], (
            f"No-hourly fixture should produce empty hourly list, got {len(result.hourly)} points"
        )


# ===========================================================================
# 13. Capability registry — forecast module
# ===========================================================================


class TestCapabilityRegistryForecastModule:
    """wire_providers with alerts + forecast populates both."""

    def test_forecast_capability_has_correct_provider_id_and_domain(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.provider_id == "openmeteo"
        assert CAPABILITY.domain == "forecast"

    def test_wire_providers_with_alerts_and_forecast_populates_both(self) -> None:
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.alerts.nws import CAPABILITY as alerts_cap  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.openmeteo import CAPABILITY as forecast_cap  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([alerts_cap, forecast_cap])
        registry = get_provider_registry()

        domains = {p.domain for p in registry}
        assert "alerts" in domains
        assert "forecast" in domains
        assert len(registry) == 2

    def test_forecast_capability_geographic_coverage_is_global(self) -> None:
        from weewx_clearskies_api.providers.forecast.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "global"

    def test_forecast_capability_default_poll_interval_is_1800(self) -> None:
        """30 min cache TTL per ADR-017."""
        from weewx_clearskies_api.providers.forecast.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 1800

    def test_forecast_capability_auth_required_is_empty(self) -> None:
        """Open-Meteo is keyless; auth_required must be empty."""
        from weewx_clearskies_api.providers.forecast.openmeteo import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.auth_required == ()


# ===========================================================================
# 14. /capabilities — forecast configured
# ===========================================================================


class TestCapabilitiesEndpointForecastConfigured:
    """/capabilities returns forecast declaration when Open-Meteo is configured."""

    def test_capabilities_includes_openmeteo_provider(
        self, forecast_client_openmeteo: Any
    ) -> None:
        response = forecast_client_openmeteo.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        provider_ids = [p["providerId"] for p in providers]
        assert "openmeteo" in provider_ids, (
            f"Expected 'openmeteo' in providers list; got {provider_ids}"
        )

    def test_capabilities_forecast_domain_is_forecast(
        self, forecast_client_openmeteo: Any
    ) -> None:
        response = forecast_client_openmeteo.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        om_provider = next((p for p in providers if p["providerId"] == "openmeteo"), None)
        assert om_provider is not None
        assert om_provider["domain"] == "forecast"

    def test_capabilities_canonical_fields_includes_forecast_fields(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """canonicalFieldsAvailable includes forecast-specific fields."""
        response = forecast_client_openmeteo.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        available = body["data"]["canonicalFieldsAvailable"]
        # Key forecast fields from the CAPABILITY declaration
        for field in ("validTime", "outTemp", "precipProbability", "weatherCode"):
            assert field in available, (
                f"canonicalFieldsAvailable should include forecast field {field!r}"
            )

    def test_capabilities_no_providers_when_none_configured(
        self, forecast_client_no_provider: Any
    ) -> None:
        response = forecast_client_no_provider.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        provider_ids = [p["providerId"] for p in providers]
        assert "openmeteo" not in provider_ids


# ===========================================================================
# 15. /forecast endpoint — no provider configured
# ===========================================================================


class TestForecastEndpointNoProvider:
    """/forecast with no provider → 200 empty bundle source='none'."""

    def test_no_provider_returns_200(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        assert response.status_code == 200

    def test_no_provider_source_is_none(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["source"] == "none", (
            f"Expected source='none' when no provider; got {body['data']['source']!r}"
        )
        assert body["source"] == "none"

    def test_no_provider_hourly_is_empty_list(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["hourly"] == []

    def test_no_provider_daily_is_empty_list(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["daily"] == []

    def test_no_provider_discussion_is_null(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is None

    def test_no_provider_generated_at_is_set(self, forecast_client_no_provider: Any) -> None:
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert "generatedAt" in body["data"]
        assert body["data"]["generatedAt"].endswith("Z")

    def test_no_provider_units_block_is_present(self, forecast_client_no_provider: Any) -> None:
        """Even with no provider, the envelope includes the units block."""
        response = forecast_client_no_provider.get("/api/v1/forecast")
        body = response.json()
        assert "units" in body, "units block should always be present in ForecastResponse"
        assert isinstance(body["units"], dict)


# ===========================================================================
# 16. /forecast endpoint — Open-Meteo configured (respx-mocked)
# ===========================================================================


class TestForecastEndpointOpenMeteo:
    """/forecast with Open-Meteo configured + respx-mocked returns 200."""

    def test_openmeteo_configured_returns_200(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        assert response.status_code == 200

    def test_openmeteo_source_is_openmeteo(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["source"] == "openmeteo"
        assert body["source"] == "openmeteo"

    def test_openmeteo_default_48_hourly_points_returned(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """Default ?hours=48 → endpoint slices to 48 points from the 168-point cache."""
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert len(body["data"]["hourly"]) == 48, (
            f"Default should return 48 hourly points, got {len(body['data']['hourly'])}"
        )

    def test_openmeteo_default_7_daily_points_returned(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """Default ?days=7 → endpoint returns 7 daily points."""
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert len(body["data"]["daily"]) == 7, (
            f"Default should return 7 daily points, got {len(body['data']['daily'])}"
        )

    def test_openmeteo_discussion_is_null(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is None

    def test_openmeteo_units_block_is_present(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        body = response.json()
        assert "units" in body
        assert isinstance(body["units"], dict)


# ===========================================================================
# 17. /forecast endpoint — slice via query params
# ===========================================================================


class TestForecastEndpointSlice:
    """?hours=N&days=M returns exactly N hourly and M daily points."""

    def test_hours_24_days_3_returns_correct_counts(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get(
                "/api/v1/forecast", params={"hours": 24, "days": 3}
            )
        body = response.json()
        assert len(body["data"]["hourly"]) == 24, (
            f"Expected 24 hourly points, got {len(body['data']['hourly'])}"
        )
        assert len(body["data"]["daily"]) == 3, (
            f"Expected 3 daily points, got {len(body['data']['daily'])}"
        )

    def test_hours_0_returns_empty_hourly(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get(
                "/api/v1/forecast", params={"hours": 0}
            )
        body = response.json()
        assert body["data"]["hourly"] == []

    def test_days_0_returns_empty_daily(
        self, forecast_client_openmeteo: Any
    ) -> None:
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get(
                "/api/v1/forecast", params={"days": 0}
            )
        body = response.json()
        assert body["data"]["daily"] == []

    def test_hours_above_available_returns_all_available(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """?hours=384 but fixture has 168 → returns all 168."""
        fixture = _load_fixture("forecast.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            response = forecast_client_openmeteo.get(
                "/api/v1/forecast", params={"hours": 384}
            )
        body = response.json()
        # Fixture has 168 hourly points; requesting 384 returns all 168
        assert len(body["data"]["hourly"]) == 168, (
            f"Expected 168 (all available), got {len(body['data']['hourly'])}"
        )


# ===========================================================================
# 18. /forecast endpoint — invalid query params → 422
# ===========================================================================


class TestForecastEndpointInvalidParams:
    """Invalid/unknown query params → 422."""

    def test_unknown_param_nuke_returns_422(
        self, forecast_client_no_provider: Any
    ) -> None:
        """extra='forbid' blocks unknown query key 'nuke' per security-baseline §3.5."""
        response = forecast_client_no_provider.get(
            "/api/v1/forecast", params={"nuke": "1"}
        )
        assert response.status_code in (400, 422), (
            f"Unknown query key should return 400/422, got {response.status_code}"
        )

    def test_negative_hours_returns_422(
        self, forecast_client_no_provider: Any
    ) -> None:
        response = forecast_client_no_provider.get(
            "/api/v1/forecast", params={"hours": -1}
        )
        assert response.status_code in (400, 422)

    def test_hours_above_max_999999_returns_422(
        self, forecast_client_no_provider: Any
    ) -> None:
        response = forecast_client_no_provider.get(
            "/api/v1/forecast", params={"hours": 999999}
        )
        assert response.status_code in (400, 422)

    def test_days_above_max_20_returns_422(
        self, forecast_client_no_provider: Any
    ) -> None:
        response = forecast_client_no_provider.get(
            "/api/v1/forecast", params={"days": 20}
        )
        assert response.status_code in (400, 422)

    def test_negative_days_returns_422(
        self, forecast_client_no_provider: Any
    ) -> None:
        response = forecast_client_no_provider.get(
            "/api/v1/forecast", params={"days": -1}
        )
        assert response.status_code in (400, 422)


# ===========================================================================
# 19. /forecast endpoint — error responses from Open-Meteo
# ===========================================================================


class TestForecastEndpointProviderErrors:
    """Provider failures produce correct HTTP error codes with ProviderProblem."""

    def test_openmeteo_503_returns_502_provider_problem(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """Open-Meteo 503 → 502 with errorCode=TransientNetworkError."""
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(503, text="Service Unavailable")
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        assert response.status_code == 502
        body = response.json()
        # RFC 9457 ProviderProblem
        assert body.get("errorCode") == "TransientNetworkError", (
            f"Expected errorCode='TransientNetworkError', got {body.get('errorCode')!r}"
        )

    def test_openmeteo_429_returns_503_quota_exhausted(
        self, forecast_client_openmeteo: Any
    ) -> None:
        """Open-Meteo 429 → 503 with errorCode=QuotaExhausted."""
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            response = forecast_client_openmeteo.get("/api/v1/forecast")
        assert response.status_code == 503
        body = response.json()
        assert body.get("errorCode") == "QuotaExhausted", (
            f"Expected errorCode='QuotaExhausted', got {body.get('errorCode')!r}"
        )


# ===========================================================================
# 20. Unknown WMO code — no exception, null weatherText and precipType
# ===========================================================================


class TestUnknownWmoCodeHandling:
    """Unknown WMO code → weatherText=null, precipType=null (no exception)."""

    def test_unknown_wmo_code_produces_null_weather_text_and_precip_type(self) -> None:
        """forecast_unknown_wmo_code.json (code=200) → weatherText=None, precipType=None."""
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import wire_cache_from_env  # noqa: PLC0415
        wire_cache_from_env()

        fixture = _load_fixture("forecast_unknown_wmo_code.json")
        with respx.mock(base_url="https://api.open-meteo.com", assert_all_called=False) as mock:
            mock.get("/v1/forecast").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            result = openmeteo.fetch(
                lat=47.6062,
                lon=-122.3321,
                target_unit="US",
                timezone="America/Los_Angeles",
            )

        assert len(result.hourly) == 1
        point = result.hourly[0]
        assert point.weatherText is None, (
            f"Unknown WMO code should produce null weatherText, got {point.weatherText!r}"
        )
        assert point.precipType is None, (
            f"Unknown WMO code should produce null precipType, got {point.precipType!r}"
        )
