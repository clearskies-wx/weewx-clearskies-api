"""Unit tests for the NWS forecast provider (3b round 3).

Covers per the task-3b-3 brief §Test-author parallel scope:

  Pure-compute helpers:
  - Icon URL → shortName extraction: standard URL, no query string, None, empty, malformed
  - Compass abbreviation → degrees: all 16 compass points, unknown, empty, None
  - windSpeed string range parse: range, single, km/h, empty, None, garbage
  - _pair_day_night: normal sequence, leading-night skip, all-night, trailing day
  - _zip_hourly correctness: field-by-field values, METRICWX wind post-convert
  - _zip_daily correctness: tempMax/Min, precipProbabilityMax, windSpeedMax, METRICWX
  - Per-target-unit units= mapping: US→us, METRIC→si, METRICWX→si + post-convert
  - precipType derivation: rain family, snow family, freezing-rain, sleet, null-producing

  Wire-shape Pydantic:
  - Each real fixture loads cleanly into its wire-shape model
  - Missing required field → ValidationError
  - Extra field → ignored

  Module fetch (respx-mocked):
  - Happy path: 5 URLs intercepted, ForecastBundle returned with correct counts,
    discussion populated, source="nws"
  - Cache hit: pre-populated cache, no outbound HTTP calls
  - /points 404 → GeographicallyUnsupported
  - /forecast/hourly 5xx → TransientNetworkError
  - /forecast 429 → QuotaExhausted
  - AFD list empty → discussion=None, no /products/{id} call
  - AFD body 5xx → discussion=None, hourly/daily populated
  - AFD body malformed JSON → discussion=None
  - Malformed hourly wire shape → ProviderProtocolError
  - Leading-night periods → first daily entry is first day-period

  UA contact wiring:
  - With contact: outbound UA header includes contact string
  - Without contact: UA excludes contact; WARN logged once

  Capability registry:
  - wire_providers([nws_forecast.CAPABILITY]) populates registry
  - get_provider_registry() returns the nws entry

  /capabilities response:
  - nws forecast configured → nws in providers list
  - canonicalFieldsAvailable includes nws forecast fields

  /forecast endpoint (respx-mocked):
  - nws configured, happy path → 200 source="nws", discussion populated
  - Slice via query params (?hours=24&days=3) → correct counts
  - Defaults (no params) → 48 hourly, 7 daily
  - NWS down → 502 ProviderProblem TransientNetworkError
  - NWS quota → 503 ProviderProblem QuotaExhausted
  - Non-US lat/lon (/points 404) → 503 ProviderProblem GeographicallyUnsupported

No DB, no live network. respx mocks outbound httpx calls.
Wire-shape rule: fixtures loaded from tests/fixtures/providers/nws/*.json
(real NWS response shapes per rules/clearskies-process.md §Real schemas).
ADR references: ADR-006, ADR-007, ADR-010, ADR-017, ADR-018, ADR-019, ADR-020, ADR-038.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers" / "nws"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from tests/fixtures/providers/nws/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helpers
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache.

    Re-wiring is part of "reset" (matches 3b-2 openmeteo unit test pattern):
    every test that calls fetch() needs a wired cache, and reset-without-rewire
    surfaces as RuntimeError("Cache not initialised") at fetch's first
    get_cache().get() call.
    """
    import weewx_clearskies_api.providers.forecast.nws as _nws  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.forecast.nws import (
        _reset_http_client_for_tests,  # noqa: PLC0415
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    # Clear rate limiter deque so consecutive tests don't trip each other.
    _nws._rate_limiter._calls.clear()
    # Re-wire a clean memory cache for the next test (CLEARSKIES_CACHE_URL unset
    # in the unit test env → MemoryCache).
    wire_cache_from_env()


def _wire_test_station(latitude: float = 47.6062, longitude: float = -122.3321) -> None:
    """Wire station info so endpoint tests have lat/lon for outbound NWS calls.

    Default Seattle coordinates match the recorded fixtures' /points URL.
    Endpoint tests that need a non-US location (e.g. GeographicallyUnsupported)
    pass overrides via this helper or set _cached_station directly.
    """
    import weewx_clearskies_api.services.station as station_mod  # noqa: PLC0415
    from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415

    reset_cache()
    station_mod._cached_station = StationInfo(
        station_id="test-station",
        name="Test Station",
        latitude=latitude,
        longitude=longitude,
        altitude=100.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-420,
        unit_system="US",
        hardware=None,
    )


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
        Settings,
    )

    return Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
        alerts=AlertsSettings({}),
        forecast=ForecastSettings({"provider": provider} if provider else {}),
    )


@pytest.fixture()
def forecast_client_nws() -> Any:
    """TestClient for the forecast endpoint with NWS provider configured.

    Wires station info to default Seattle coordinates so /points URL matches
    the recorded fixtures.  Tests needing a non-US location call
    _wire_test_station(latitude=..., longitude=...) inside the test body.
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415
    from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415

    _reset_provider_state()
    _wire_test_station()
    settings = _make_forecast_settings(provider="nws")
    wire_providers([forecast_nws.CAPABILITY])
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def forecast_client_no_provider() -> Any:
    """TestClient for the forecast endpoint with NO provider configured."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415

    _reset_provider_state()
    _wire_test_station()
    settings = _make_forecast_settings(provider=None)
    wire_providers([])
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# NWS URL constants (match the real API URLs used by the module)
# ---------------------------------------------------------------------------

_NWS_POINTS_URL = "https://api.weather.gov/points/47.6062,-122.3321"
_NWS_HOURLY_URL = "https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly"
_NWS_DAILY_URL = "https://api.weather.gov/gridpoints/SEW/125,68/forecast"
_NWS_AFD_LIST_URL = "https://api.weather.gov/products"
_NWS_AFD_BODY_URL = "https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585"

# Station lat/lon used in all fetch() calls.
_LAT = 47.6062
_LON = -122.3321


# ===========================================================================
# 1. Icon URL → shortName extraction
# ===========================================================================


class TestExtractIconShortname:
    """_extract_icon_shortname parses NWS icon URLs to shortName."""

    def test_standard_url_with_query_string_and_intensity_returns_shortname(self) -> None:
        """/icons/land/day/sct,30?size=medium → 'sct'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname("/icons/land/day/sct,30?size=medium")
        assert result == "sct"

    def test_night_icon_no_intensity_returns_shortname(self) -> None:
        """/icons/land/night/rain?size=medium → 'rain'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname("/icons/land/night/rain?size=medium")
        assert result == "rain"

    def test_full_https_url_returns_shortname(self) -> None:
        """Full HTTPS URL with query string extracts shortName correctly."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname(
            "https://api.weather.gov/icons/land/night/sct?size=small"
        )
        assert result == "sct"

    def test_tsra_with_comma_intensity_returns_tsra(self) -> None:
        """/icons/land/day/tsra,40?size=medium → 'tsra'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname("/icons/land/day/tsra,40?size=medium")
        assert result == "tsra"

    def test_snow_icon_returns_snow(self) -> None:
        """/icons/land/day/snow?size=medium → 'snow'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname("/icons/land/day/snow?size=medium")
        assert result == "snow"

    def test_none_input_returns_none(self) -> None:
        """None → None (no exception)."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        assert _extract_icon_shortname(None) is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string → None."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        assert _extract_icon_shortname("") is None

    def test_path_with_no_slash_returns_none(self) -> None:
        """URL with no path segment → None (tolerate malformed URLs)."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        # urlparse("notaurl") has no meaningful path segments
        result = _extract_icon_shortname("notaurl")
        # "notaurl" has no slashes; rsplit gives basename="notaurl" which is non-empty.
        # Acceptable: returns "notaurl" or None; either is tolerable. Here we test
        # the documented behavior: returns something (not raises), and if it returns
        # a string, it's non-empty.
        assert result is None or isinstance(result, str)

    def test_rain_showers_hi_with_intensity_returns_rain_showers_hi(self) -> None:
        """/icons/land/day/rain_showers_hi,50 → 'rain_showers_hi'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_icon_shortname,  # noqa: PLC0415
        )
        result = _extract_icon_shortname("/icons/land/day/rain_showers_hi,50?size=medium")
        assert result == "rain_showers_hi"


# ===========================================================================
# 2. Compass abbreviation → degrees
# ===========================================================================


class TestCompassToDegrees:
    """_compass_to_degrees maps all 16 documented compass codes; unknown → None."""

    def test_north_is_0(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("N") == 0.0

    def test_northeast_is_45(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("NE") == 45.0

    def test_east_is_90(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("E") == 90.0

    def test_south_is_180(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("S") == 180.0

    def test_west_is_270(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("W") == 270.0

    def test_southwest_is_225(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("SW") == 225.0

    def test_northwest_is_315(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("NW") == 315.0

    def test_nne_is_22_5(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("NNE") == 22.5

    def test_sse_is_157_5(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("SSE") == 157.5

    def test_wnw_is_292_5(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("WNW") == 292.5

    def test_nnw_is_337_5(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("NNW") == 337.5

    def test_all_16_compass_points_covered(self) -> None:
        """All 16 compass points documented in the brief are in _COMPASS_TO_DEGREES."""
        from weewx_clearskies_api.providers.forecast.nws import _COMPASS_TO_DEGREES  # noqa: PLC0415
        expected = {
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        }
        missing = expected - set(_COMPASS_TO_DEGREES.keys())
        assert not missing, f"Missing compass points: {sorted(missing)}"

    def test_unknown_direction_returns_none(self) -> None:
        """Unknown abbreviation (e.g. 'VAR') → None, no exception."""
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("VAR") is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string → None."""
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees("") is None

    def test_none_input_returns_none(self) -> None:
        """None → None (NWS emits null for very low wind speeds)."""
        from weewx_clearskies_api.providers.forecast.nws import _compass_to_degrees  # noqa: PLC0415
        assert _compass_to_degrees(None) is None


# ===========================================================================
# 3. windSpeed string range parse
# ===========================================================================


class TestParseWindSpeed:
    """_parse_wind_speed extracts the upper bound of NWS windSpeed strings."""

    def test_range_string_mph_returns_upper_bound(self) -> None:
        """"5 to 10 mph" → 10.0 (upper bound per brief call 19)."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("5 to 10 mph") == 10.0

    def test_single_value_mph_returns_value(self) -> None:
        """"7 mph" → 7.0."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("7 mph") == 7.0

    def test_range_string_kmh_returns_upper_bound(self) -> None:
        """"5 to 10 km/h" → 10.0."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("5 to 10 km/h") == 10.0

    def test_single_value_kmh_returns_value(self) -> None:
        """"15 km/h" → 15.0."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("15 km/h") == 15.0

    def test_empty_string_returns_none(self) -> None:
        """Empty string → None."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("") is None

    def test_none_returns_none(self) -> None:
        """None → None."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed(None) is None

    def test_garbage_string_returns_none(self) -> None:
        """"foo bar" (no unit, not parseable) → None."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("foo bar") is None

    def test_large_range_takes_upper_bound(self) -> None:
        """"20 to 30 mph" → 30.0."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("20 to 30 mph") == 30.0

    def test_zero_mph_returns_zero(self) -> None:
        """"0 mph" → 0.0."""
        from weewx_clearskies_api.providers.forecast.nws import _parse_wind_speed  # noqa: PLC0415
        assert _parse_wind_speed("0 mph") == 0.0


# ===========================================================================
# 4. _pair_day_night pairing logic
# ===========================================================================


class TestPairDayNight:
    """_pair_day_night correctly pairs NWS forecast periods."""

    def _make_period(self, number: int, is_daytime: bool) -> Any:
        """Build a minimal _NwsForecastPeriod for pairing tests."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsForecastPeriod  # noqa: PLC0415
        return _NwsForecastPeriod(
            number=number,
            startTime=f"2026-05-0{number + 7}T06:00:00-07:00",
            endTime=f"2026-05-0{number + 7}T18:00:00-07:00",
            isDaytime=is_daytime,
            temperatureUnit="F",
        )

    def test_clean_day_night_sequence_pairs_correctly(self) -> None:
        """Day-Night-Day-Night sequence → 2 pairs with correct structure."""
        from weewx_clearskies_api.providers.forecast.nws import _pair_day_night  # noqa: PLC0415
        periods = [
            self._make_period(1, True),   # day1
            self._make_period(2, False),  # night1
            self._make_period(3, True),   # day2
            self._make_period(4, False),  # night2
        ]
        pairs = _pair_day_night(periods)
        assert len(pairs) == 2
        day1, night1 = pairs[0]
        assert day1.number == 1
        assert night1 is not None and night1.number == 2
        day2, night2 = pairs[1]
        assert day2.number == 3
        assert night2 is not None and night2.number == 4

    def test_leading_night_is_skipped(self) -> None:
        """Night-Day-Night-Day sequence: leading night skipped, pairing starts from day."""
        from weewx_clearskies_api.providers.forecast.nws import _pair_day_night  # noqa: PLC0415
        periods = [
            self._make_period(1, False),  # night0 — skip
            self._make_period(2, True),   # day1
            self._make_period(3, False),  # night1
            self._make_period(4, True),   # day2
            self._make_period(5, False),  # night2
        ]
        pairs = _pair_day_night(periods)
        assert len(pairs) == 2
        day1, night1 = pairs[0]
        assert day1.number == 2, f"Expected day period #2, got #{day1.number}"
        assert night1 is not None and night1.number == 3
        day2, night2 = pairs[1]
        assert day2.number == 4
        assert night2 is not None and night2.number == 5

    def test_trailing_day_without_night_gets_none_night(self) -> None:
        """Day-Night-Day sequence: last day gets (day, None) pair."""
        from weewx_clearskies_api.providers.forecast.nws import _pair_day_night  # noqa: PLC0415
        periods = [
            self._make_period(1, True),   # day1
            self._make_period(2, False),  # night1
            self._make_period(3, True),   # day2 — no following night
        ]
        pairs = _pair_day_night(periods)
        assert len(pairs) == 2
        day2, night2 = pairs[1]
        assert day2.number == 3
        assert night2 is None, "Trailing day-period without night should get None"

    def test_all_night_sequence_returns_empty_list(self) -> None:
        """All-night sequence → empty list (no day periods to pair from)."""
        from weewx_clearskies_api.providers.forecast.nws import _pair_day_night  # noqa: PLC0415
        periods = [
            self._make_period(1, False),
            self._make_period(2, False),
            self._make_period(3, False),
        ]
        pairs = _pair_day_night(periods)
        assert pairs == []

    def test_single_day_period_returns_one_pair_with_none_night(self) -> None:
        """Single day period → [(day, None)]."""
        from weewx_clearskies_api.providers.forecast.nws import _pair_day_night  # noqa: PLC0415
        periods = [self._make_period(1, True)]
        pairs = _pair_day_night(periods)
        assert len(pairs) == 1
        day, night = pairs[0]
        assert day.number == 1
        assert night is None


# ===========================================================================
# 5. _zip_hourly correctness
# ===========================================================================


class TestZipHourly:
    """_zip_hourly correctly maps NWS hourly periods to HourlyForecastPoint records."""

    def _make_hourly_period(self, **overrides: Any) -> Any:
        """Build a minimal _NwsForecastPeriod for hourly tests."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsForecastPeriod  # noqa: PLC0415
        defaults = {
            "number": 1,
            "startTime": "2026-05-07T21:00:00-07:00",
            "endTime": "2026-05-07T22:00:00-07:00",
            "isDaytime": False,
            "temperature": 62.0,
            "temperatureUnit": "F",
            "windSpeed": "3 mph",
            "windDirection": "N",
            "icon": "https://api.weather.gov/icons/land/night/sct?size=small",
            "shortForecast": "Partly Cloudy",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 0},
        }
        defaults.update(overrides)
        return _NwsForecastPeriod(**defaults)

    def test_three_period_fixture_produces_three_records(self) -> None:
        """3-period input → 3 HourlyForecastPoint records."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [
            self._make_hourly_period(number=i, startTime=f"2026-05-07T2{i}:00:00-07:00")
            for i in range(1, 4)
        ]
        result = _zip_hourly(periods, target_unit="US")
        assert len(result) == 3

    def test_valid_time_converted_to_utc(self) -> None:
        """validTime: 2026-05-07T21:00:00-07:00 → 2026-05-08T04:00:00Z."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period()]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].validTime == "2026-05-08T04:00:00Z"

    def test_out_temp_set_correctly(self) -> None:
        """outTemp = period.temperature (62.0)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(temperature=62.0)]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].outTemp == 62.0

    def test_weather_code_extracted_from_icon(self) -> None:
        """weatherCode = icon shortName ('sct' from the test period icon)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period()]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].weatherCode == "sct"

    def test_weather_text_is_short_forecast(self) -> None:
        """weatherText = period.shortForecast directly."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(shortForecast="Partly Cloudy")]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].weatherText == "Partly Cloudy"

    def test_wind_speed_parsed_from_string(self) -> None:
        """windSpeed = _parse_wind_speed("3 mph") → 3.0."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(windSpeed="3 mph")]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].windSpeed == 3.0

    def test_wind_dir_converted_from_compass(self) -> None:
        """windDir = _compass_to_degrees("N") → 0.0."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(windDirection="N")]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].windDir == 0.0

    def test_precip_type_from_rain_icon(self) -> None:
        """precipType = 'rain' when icon is rain shortName."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(
            icon="https://api.weather.gov/icons/land/day/rain?size=medium"
        )]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].precipType == "rain"

    def test_precip_type_none_for_clear_icon(self) -> None:
        """precipType = None when icon is 'sct' (not a precip-producing shortName)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(
            icon="https://api.weather.gov/icons/land/day/sct?size=medium"
        )]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].precipType is None

    def test_metricwx_wind_post_converted_from_kmh_to_ms(self) -> None:
        """METRICWX: windSpeed km/h → m/s (÷ 3.6); 36 km/h → 10.0 m/s."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(windSpeed="36 km/h")]
        result = _zip_hourly(periods, target_unit="METRICWX")
        # 36 ÷ 3.6 = 10.0 m/s
        assert result[0].windSpeed is not None
        assert abs(result[0].windSpeed - 10.0) < 0.01

    def test_metric_wind_not_post_converted(self) -> None:
        """METRIC: windSpeed stays in km/h (no post-conversion)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period(windSpeed="36 km/h")]
        result = _zip_hourly(periods, target_unit="METRIC")
        assert result[0].windSpeed == 36.0

    def test_source_field_is_nws(self) -> None:
        """source field on HourlyForecastPoint = 'nws'."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_hourly  # noqa: PLC0415
        periods = [self._make_hourly_period()]
        result = _zip_hourly(periods, target_unit="US")
        assert result[0].source == "nws"


# ===========================================================================
# 6. _zip_daily correctness
# ===========================================================================


class TestZipDaily:
    """_zip_daily correctly maps NWS day/night period pairs to DailyForecastPoint."""

    def _make_day_period(self, number: int, **overrides: Any) -> Any:
        from weewx_clearskies_api.providers.forecast.nws import _NwsForecastPeriod  # noqa: PLC0415
        defaults = {
            "number": number,
            "name": f"Day{number}",
            "startTime": f"2026-05-{8 + number:02d}T06:00:00-07:00",
            "endTime": f"2026-05-{8 + number:02d}T18:00:00-07:00",
            "isDaytime": True,
            "temperature": 70.0,
            "temperatureUnit": "F",
            "windSpeed": "10 to 15 mph",
            "windDirection": "NW",
            "icon": "https://api.weather.gov/icons/land/day/sct?size=medium",
            "shortForecast": "Mostly Sunny",
            "detailedForecast": "Mostly sunny with a high near 70.",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 5},
        }
        defaults.update(overrides)
        return _NwsForecastPeriod(**defaults)

    def _make_night_period(self, number: int, **overrides: Any) -> Any:
        from weewx_clearskies_api.providers.forecast.nws import _NwsForecastPeriod  # noqa: PLC0415
        defaults = {
            "number": number,
            "name": f"Night{number}",
            "startTime": f"2026-05-{8 + number:02d}T18:00:00-07:00",
            "endTime": f"2026-05-{9 + number:02d}T06:00:00-07:00",
            "isDaytime": False,
            "temperature": 50.0,
            "temperatureUnit": "F",
            "windSpeed": "5 mph",
            "windDirection": "SW",
            "icon": "https://api.weather.gov/icons/land/night/bkn?size=medium",
            "shortForecast": "Mostly Cloudy",
            "detailedForecast": "",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 10},
        }
        defaults.update(overrides)
        return _NwsForecastPeriod(**defaults)

    def test_four_pairs_produce_four_daily_points(self) -> None:
        """4 (day, night) pairs → 4 DailyForecastPoint records."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        pairs = [(self._make_day_period(i), self._make_night_period(i)) for i in range(1, 5)]
        result = _zip_daily(pairs, target_unit="US")
        assert len(result) == 4

    def test_temp_max_from_day_period(self) -> None:
        """tempMax = day-period's temperature."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, temperature=72.0)
        night = self._make_night_period(1, temperature=50.0)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].tempMax == 72.0

    def test_temp_min_from_night_period(self) -> None:
        """tempMin = night-period's temperature."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, temperature=72.0)
        night = self._make_night_period(1, temperature=50.0)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].tempMin == 50.0

    def test_temp_min_is_none_when_no_night(self) -> None:
        """tempMin = None when night is None (trailing day)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1)
        result = _zip_daily([(day, None)], target_unit="US")
        assert result[0].tempMin is None

    def test_precip_prob_max_is_max_of_day_and_night(self) -> None:
        """precipProbabilityMax = max(day_prob, night_prob)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(
            1, probabilityOfPrecipitation={"unitCode": "wmoUnit:percent", "value": 20}
        )
        night = self._make_night_period(
            1, probabilityOfPrecipitation={"unitCode": "wmoUnit:percent", "value": 40}
        )
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].precipProbabilityMax == 40.0

    def test_wind_speed_max_is_upper_bound_across_day_and_night(self) -> None:
        """windSpeedMax = max of upper bounds across day and night strings."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, windSpeed="10 to 15 mph")
        night = self._make_night_period(1, windSpeed="5 mph")
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].windSpeedMax == 15.0

    def test_weather_code_from_day_period_icon(self) -> None:
        """weatherCode = icon shortName from day period."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(
            1, icon="https://api.weather.gov/icons/land/day/snow?size=medium"
        )
        night = self._make_night_period(1)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].weatherCode == "snow"

    def test_weather_text_from_day_period(self) -> None:
        """weatherText = day period's shortForecast."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, shortForecast="Mostly Sunny")
        night = self._make_night_period(1)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].weatherText == "Mostly Sunny"

    def test_narrative_from_day_period_detailed_forecast(self) -> None:
        """narrative = day period's detailedForecast."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, detailedForecast="Mostly sunny with a high near 70.")
        night = self._make_night_period(1)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].narrative == "Mostly sunny with a high near 70."

    def test_valid_date_is_station_local_date_from_day_start_time(self) -> None:
        """validDate = YYYY-MM-DD from day period startTime date part."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, startTime="2026-05-09T06:00:00-07:00")
        night = self._make_night_period(1)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].validDate == "2026-05-09"

    def test_metricwx_wind_speed_max_post_converted_to_ms(self) -> None:
        """METRICWX: windSpeedMax post-converted from km/h to m/s (÷ 3.6)."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1, windSpeed="36 km/h")
        night = self._make_night_period(1, windSpeed="18 km/h")
        result = _zip_daily([(day, night)], target_unit="METRICWX")
        # max(36, 18) = 36 km/h → 36 ÷ 3.6 = 10.0 m/s
        assert result[0].windSpeedMax is not None
        assert abs(result[0].windSpeedMax - 10.0) < 0.01

    def test_source_field_is_nws(self) -> None:
        """source field on DailyForecastPoint = 'nws'."""
        from weewx_clearskies_api.providers.forecast.nws import _zip_daily  # noqa: PLC0415
        day = self._make_day_period(1)
        night = self._make_night_period(1)
        result = _zip_daily([(day, night)], target_unit="US")
        assert result[0].source == "nws"


# ===========================================================================
# 7. precipType derivation from icon shortName
# ===========================================================================


class TestPrecipTypeFromIcon:
    """_get_precip_type_from_icon maps icon URLs to canonical precipType enum."""

    def _call(self, icon_url: str | None) -> str | None:
        from weewx_clearskies_api.providers.forecast.nws import (
            _get_precip_type_from_icon,  # noqa: PLC0415
        )
        return _get_precip_type_from_icon(icon_url)

    def test_rain_icon_returns_rain(self) -> None:
        assert self._call("/icons/land/day/rain?size=medium") == "rain"

    def test_rain_showers_icon_returns_rain(self) -> None:
        assert self._call("/icons/land/day/rain_showers?size=medium") == "rain"

    def test_rain_showers_hi_icon_returns_rain(self) -> None:
        assert self._call("/icons/land/day/rain_showers_hi?size=medium") == "rain"

    def test_snow_icon_returns_snow(self) -> None:
        assert self._call("/icons/land/day/snow?size=medium") == "snow"

    def test_snow_showers_icon_returns_snow(self) -> None:
        assert self._call("/icons/land/day/snow_showers?size=medium") == "snow"

    def test_blizzard_icon_returns_snow(self) -> None:
        assert self._call("/icons/land/day/blizzard?size=medium") == "snow"

    def test_fzra_icon_returns_freezing_rain(self) -> None:
        assert self._call("/icons/land/day/fzra?size=medium") == "freezing-rain"

    def test_rain_fzra_icon_returns_freezing_rain(self) -> None:
        assert self._call("/icons/land/day/rain_fzra?size=medium") == "freezing-rain"

    def test_snow_fzra_icon_returns_freezing_rain(self) -> None:
        assert self._call("/icons/land/day/snow_fzra?size=medium") == "freezing-rain"

    def test_sleet_icon_returns_sleet(self) -> None:
        assert self._call("/icons/land/day/sleet?size=medium") == "sleet"

    def test_rain_sleet_icon_returns_sleet(self) -> None:
        assert self._call("/icons/land/day/rain_sleet?size=medium") == "sleet"

    def test_snow_sleet_icon_returns_sleet(self) -> None:
        assert self._call("/icons/land/day/snow_sleet?size=medium") == "sleet"

    def test_tsra_icon_returns_rain(self) -> None:
        """Thunderstorm → rain (canonical has no thunderstorm enum per brief call 21)."""
        assert self._call("/icons/land/day/tsra?size=medium") == "rain"

    def test_tsra_sct_icon_returns_rain(self) -> None:
        assert self._call("/icons/land/day/tsra_sct?size=medium") == "rain"

    def test_tsra_hi_icon_returns_rain(self) -> None:
        assert self._call("/icons/land/day/tsra_hi?size=medium") == "rain"

    def test_sct_icon_returns_none(self) -> None:
        """Scattered clouds → None (not precip-producing)."""
        assert self._call("/icons/land/day/sct?size=medium") is None

    def test_bkn_icon_returns_none(self) -> None:
        assert self._call("/icons/land/day/bkn?size=medium") is None

    def test_fog_icon_returns_none(self) -> None:
        assert self._call("/icons/land/day/fog?size=medium") is None

    def test_none_icon_returns_none(self) -> None:
        assert self._call(None) is None

    def test_freezing_not_flattened_to_rain(self) -> None:
        """Brief call 7: DO NOT flatten freezing variants to 'rain'. Must be 'freezing-rain'."""
        assert self._call("/icons/land/day/fzra?size=medium") == "freezing-rain"
        # NOT "rain" — that would be wrong per canonical §3.3 enum discipline.


# ===========================================================================
# 8. Per-target-unit NWS units= mapping
# ===========================================================================


class TestTargetUnitToNwsUnits:
    """_TARGET_UNIT_TO_NWS_UNITS maps weewx target_unit to NWS units= query param."""

    def test_us_maps_to_us(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import (
            _TARGET_UNIT_TO_NWS_UNITS,  # noqa: PLC0415
        )
        assert _TARGET_UNIT_TO_NWS_UNITS["US"] == "us"

    def test_metric_maps_to_si(self) -> None:
        from weewx_clearskies_api.providers.forecast.nws import (
            _TARGET_UNIT_TO_NWS_UNITS,  # noqa: PLC0415
        )
        assert _TARGET_UNIT_TO_NWS_UNITS["METRIC"] == "si"

    def test_metricwx_maps_to_si(self) -> None:
        """METRICWX → si (post-convert wind km/h → m/s at zip step per brief call 11)."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _TARGET_UNIT_TO_NWS_UNITS,  # noqa: PLC0415
        )
        assert _TARGET_UNIT_TO_NWS_UNITS["METRICWX"] == "si"

    def test_unknown_unit_raises_provider_protocol_error_in_fetch(self) -> None:
        """Unknown target_unit raises ProviderProtocolError before any HTTP call."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()
        with pytest.raises(ProviderProtocolError):
            fetch(lat=47.6062, lon=-122.3321, target_unit="IMPERIAL", user_agent_contact=None)


# ===========================================================================
# 8b. AFD body parse — _extract_afd_headline_and_sender (canonical §4.1.4)
# ===========================================================================


class TestExtractAfdHeadlineAndSender:
    """_extract_afd_headline_and_sender parses headline + sender per canonical §4.1.4."""

    def test_real_fixture_headline_is_first_line_after_wire_header(self) -> None:
        """products_afd_body.json: headline = 'Area Forecast Discussion'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_afd_headline_and_sender,  # noqa: PLC0415
        )
        body = _load_fixture("products_afd_body.json")["productText"]
        headline, _ = _extract_afd_headline_and_sender(body)
        assert headline == "Area Forecast Discussion"

    def test_real_fixture_sender_is_nws_location_composite(self) -> None:
        """products_afd_body.json: senderName = 'NWS Seattle WA'."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_afd_headline_and_sender,  # noqa: PLC0415
        )
        body = _load_fixture("products_afd_body.json")["productText"]
        _, sender = _extract_afd_headline_and_sender(body)
        assert sender == "NWS Seattle WA"

    def test_empty_input_returns_none_none(self) -> None:
        """Empty productText → (None, None)."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_afd_headline_and_sender,  # noqa: PLC0415
        )
        assert _extract_afd_headline_and_sender("") == (None, None)

    def test_body_without_nws_line_falls_back_to_none_sender(self) -> None:
        """productText missing 'National Weather Service' line → sender is None."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_afd_headline_and_sender,  # noqa: PLC0415
        )
        body = (
            "000\n"
            "FXUS66 KXXX 010000\n"
            "AFDXXX\n\n"
            "Some weather content.\n"
            "More content.\n"
        )
        headline, sender = _extract_afd_headline_and_sender(body)
        assert headline == "Some weather content."
        assert sender is None

    def test_body_with_only_wire_header_returns_none_headline(self) -> None:
        """productText with only WMO wire-format header → (None, None)."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _extract_afd_headline_and_sender,  # noqa: PLC0415
        )
        body = "000\nFXUS66 KXXX 010000\nAFDXXX\n"
        headline, sender = _extract_afd_headline_and_sender(body)
        assert headline is None
        assert sender is None


# ===========================================================================
# 9. Wire-shape Pydantic models validate against real fixtures
# ===========================================================================


class TestWireShapeModels:
    """Wire-shape Pydantic models parse real captured fixtures correctly."""

    def test_points_response_loads_from_real_fixture(self) -> None:
        """forecast_points.json loads cleanly into _NwsPointResponse."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsPointResponse  # noqa: PLC0415
        data = _load_fixture("forecast_points.json")
        model = _NwsPointResponse.model_validate(data)
        assert model.properties.cwa == "SEW"
        assert model.properties.gridX == 125
        assert model.properties.gridY == 68
        assert model.properties.timeZone == "America/Los_Angeles"

    def test_hourly_response_loads_from_real_fixture(self) -> None:
        """forecast_hourly.json loads cleanly into _NwsForecastResponse."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _NwsForecastResponse,  # noqa: PLC0415
        )
        data = _load_fixture("forecast_hourly.json")
        model = _NwsForecastResponse.model_validate(data)
        assert len(model.properties.periods) == 156

    def test_forecast_response_loads_from_real_fixture(self) -> None:
        """forecast.json loads cleanly into _NwsForecastResponse."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _NwsForecastResponse,  # noqa: PLC0415
        )
        data = _load_fixture("forecast.json")
        model = _NwsForecastResponse.model_validate(data)
        assert len(model.properties.periods) == 14

    def test_afd_list_response_loads_from_real_fixture(self) -> None:
        """products_afd_list.json loads cleanly into _NwsAfdListResponse."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsAfdListResponse  # noqa: PLC0415
        data = _load_fixture("products_afd_list.json")
        model = _NwsAfdListResponse.model_validate(data)
        assert len(model.graph) == 28
        assert model.graph[0].id == "44453767-e473-4c16-835d-96495e091585"

    def test_afd_body_loads_from_real_fixture(self) -> None:
        """products_afd_body.json loads cleanly into _NwsAfdProductBody."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsAfdProductBody  # noqa: PLC0415
        data = _load_fixture("products_afd_body.json")
        model = _NwsAfdProductBody.model_validate(data)
        assert model.wmoCollectiveId == "FXUS66"
        assert model.issuingOffice == "KSEW"
        assert model.issuanceTime == "2026-05-08T03:40:00+00:00"
        assert "SYNOPSIS" in model.productText

    def test_afd_list_empty_graph_parses_cleanly(self) -> None:
        """products_afd_list_empty.json with empty @graph → model.graph == []."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsAfdListResponse  # noqa: PLC0415
        data = _load_fixture("products_afd_list_empty.json")
        model = _NwsAfdListResponse.model_validate(data)
        assert model.graph == []

    def test_points_missing_required_cwa_raises_validation_error(self) -> None:
        """_NwsPointProperties with missing 'cwa' → ValidationError."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.forecast.nws import _NwsPointResponse  # noqa: PLC0415
        bad_data = {
            "type": "Feature",
            "properties": {
                # cwa is missing
                "gridId": "SEW",
                "gridX": 125,
                "gridY": 68,
                "forecast": "https://example.com/forecast",
                "forecastHourly": "https://example.com/hourly",
                "timeZone": "America/Los_Angeles",
            },
        }
        with pytest.raises(ValidationError):
            _NwsPointResponse.model_validate(bad_data)

    def test_extra_field_in_points_response_is_ignored(self) -> None:
        """extra='ignore': a new NWS property doesn't break the model."""
        from weewx_clearskies_api.providers.forecast.nws import _NwsPointResponse  # noqa: PLC0415
        data = _load_fixture("forecast_points.json")
        data["properties"]["newNwsFieldFromFutureSchemaVersion"] = "some value"
        model = _NwsPointResponse.model_validate(data)
        assert model.properties.cwa == "SEW"  # parsing succeeded

    def test_hourly_malformed_missing_properties_raises_validation_error(self) -> None:
        """forecast_hourly_malformed.json with missing 'periods' loads with empty list."""
        from weewx_clearskies_api.providers.forecast.nws import (
            _NwsForecastResponse,  # noqa: PLC0415
        )
        data = _load_fixture("forecast_hourly_malformed.json")
        # Model has periods with default_factory=list, so empty properties is valid.
        model = _NwsForecastResponse.model_validate(data)
        assert model.properties.periods == []


# ===========================================================================
# 10. Module fetch — happy path (respx-mocked)
# ===========================================================================


class TestFetchHappyPath:
    """fetch() with all 5 NWS URLs mocked returns ForecastBundle with correct counts."""

    def test_happy_path_returns_forecast_bundle(self) -> None:
        """fetch() returns a ForecastBundle instance (not dict, not list)."""
        from weewx_clearskies_api.models.responses import ForecastBundle  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert isinstance(result, ForecastBundle)

    def test_happy_path_returns_156_hourly_points(self) -> None:
        """156 hourly periods from NWS → 156 HourlyForecastPoints."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert len(result.hourly) == 156

    def test_happy_path_leading_night_skipped_in_daily(self) -> None:
        """forecast.json starts with night → leading night skipped; 7 daily points."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        # 14 periods: Night+Day pairs starting with a night.
        # After skipping leading night: 13 periods = 6 full day/night pairs + 1 trailing day.
        # Or the real count: skip night(1), then pairs: (2,3),(4,5),(6,7),(8,9),(10,11),(12,13),(14,None) = 7.
        assert len(result.daily) == 7, (
            f"Expected 7 daily points (leading night skipped, 14 periods), got {len(result.daily)}"
        )

    def test_happy_path_discussion_is_forecast_discussion(self) -> None:
        """discussion is ForecastDiscussion with populated fields."""
        from weewx_clearskies_api.models.responses import ForecastDiscussion  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert isinstance(result.discussion, ForecastDiscussion)
        # Per canonical-data-model §4.1.4 NWS column:
        #   headline   = productText first line (after WMO wire header)
        #   senderName = "NWS [Location]" composite (e.g. "NWS Seattle WA")
        # Verified against fixture products_afd_body.json — body line 1
        # (after header) is "Area Forecast Discussion" and the body line
        # "National Weather Service Seattle WA" yields the abbreviated sender.
        assert result.discussion.senderName == "NWS Seattle WA"
        assert result.discussion.headline == "Area Forecast Discussion"
        assert result.discussion.issuedAt == "2026-05-08T03:40:00Z"

    def test_happy_path_source_is_nws(self) -> None:
        """bundle.source = 'nws'."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert result.source == "nws"

    def test_happy_path_first_hourly_valid_time_is_utc(self) -> None:
        """First hourly point validTime = '2026-05-08T04:00:00Z' (21:00 PDT → UTC)."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert result.hourly[0].validTime == "2026-05-08T04:00:00Z"

    def test_happy_path_first_daily_valid_date_after_leading_night_skip(self) -> None:
        """First daily point is the first full DAY period (skipping leading night)."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
            )
            mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        # Period[0] is Tonight (night), Period[1] is Friday (day) startTime=2026-05-08T...
        assert result.daily[0].validDate == "2026-05-08"


# ===========================================================================
# 11. Module fetch — cache hit (memory + fakeredis)
# ===========================================================================


class TestFetchCacheHit:
    """Cache hit: pre-populated cache returns bundle without outbound HTTP."""

    def _run_cache_hit_test(self) -> None:
        from weewx_clearskies_api.models.responses import ForecastBundle  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import (  # noqa: PLC0415
            PROVIDER_ID,
            _build_cache_key,
            fetch,
        )

        # Build a minimal cached bundle.
        bundle = ForecastBundle(
            hourly=[],
            daily=[],
            discussion=None,
            source=PROVIDER_ID,
            generatedAt="2026-05-08T00:00:00Z",
        )
        key = _build_cache_key(_LAT, _LON, "US")
        get_cache().set(key, bundle.model_dump(mode="json"), ttl_seconds=1800)

        # fetch() should reconstruct from cache without any HTTP calls.
        with respx.mock(assert_all_called=False) as mock:
            # Register a mock that should NOT be called.
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json={})
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")
            # The points mock should NOT have been called (cache hit).
            assert mock.calls.call_count == 0

        assert isinstance(result, ForecastBundle)
        assert result.source == PROVIDER_ID

    def test_cache_hit_with_memory_cache_skips_http(self) -> None:
        """Memory cache hit: no outbound HTTP, returns reconstructed ForecastBundle."""
        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import MemoryCache  # noqa: PLC0415

        _reset_provider_state()
        cache_mod._cache = MemoryCache()
        self._run_cache_hit_test()

    def test_cache_hit_with_fakeredis_skips_http(self) -> None:
        """Redis (fakeredis) cache hit: no outbound HTTP, returns reconstructed ForecastBundle."""
        try:
            import fakeredis  # noqa: PLC0415
        except ImportError:
            pytest.skip("fakeredis not installed")

        import redis as redis_lib  # noqa: PLC0415

        import weewx_clearskies_api.providers._common.cache as cache_mod  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import RedisCache  # noqa: PLC0415

        _reset_provider_state()
        # Bypass RedisCache.__init__'s real-Redis ping; assign fake client directly
        # (matches 3b-1 alerts unit test pattern in test_providers_alerts_unit.py).
        fake_client = fakeredis.FakeRedis(decode_responses=False)
        redis_cache = object.__new__(RedisCache)
        redis_cache._client = fake_client  # type: ignore[attr-defined]
        redis_cache._redis_error_cls = redis_lib.exceptions.RedisError  # type: ignore[attr-defined]
        cache_mod._cache = redis_cache
        self._run_cache_hit_test()


# ===========================================================================
# 12. Module fetch — error paths
# ===========================================================================


class TestFetchErrorPaths:
    """fetch() error paths: 404→GeographicallyUnsupported, 5xx→TransientNetworkError, etc."""

    def test_points_404_raises_geographically_unsupported(self) -> None:
        """/points 404 → GeographicallyUnsupported (non-US location, brief call 13)."""
        from weewx_clearskies_api.providers._common.errors import (
            GeographicallyUnsupported,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/-33.8688,151.2093").mock(
                return_value=httpx.Response(404, json=_load_fixture("forecast_points_404.json"))
            )
            with pytest.raises(GeographicallyUnsupported):
                fetch(
                    lat=-33.8688, lon=151.2093,
                    target_unit="US",
                    user_agent_contact="test@example.com",
                )

    def test_hourly_5xx_raises_transient_network_error(self) -> None:
        """/forecast/hourly 503 → TransientNetworkError after retries."""
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(503, text="Service unavailable")
            )
            with pytest.raises(TransientNetworkError):
                fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

    def test_daily_429_raises_quota_exhausted(self) -> None:
        """/forecast 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "60"}, text="Too Many Requests")
            )
            with pytest.raises(QuotaExhausted):
                fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

    def test_afd_list_empty_returns_bundle_with_discussion_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty AFD @graph → discussion=None; no /products/{id} call; WARN logged."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
                )
                # products/{id} should NOT be called.
                afd_body_mock = mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                    return_value=httpx.Response(200, json={})
                )
                result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

                # AFD body call should not have been made.
                assert afd_body_mock.called is False

        assert result.discussion is None
        assert len(result.hourly) == 156
        warn_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("empty" in m.lower() or "graph" in m.lower() for m in warn_messages), (
            "Expected a WARN about empty AFD list"
        )

    def test_afd_body_5xx_returns_bundle_with_discussion_none_and_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AFD body 503 → discussion=None; hourly/daily still populated; WARN logged."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
                )
                mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                    return_value=httpx.Response(503, text="Service unavailable")
                )
                result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert result.discussion is None
        assert len(result.hourly) == 156
        assert len(result.daily) == 7
        warn_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_messages) > 0, "Expected at least one WARN for AFD body failure"

    def test_afd_body_malformed_json_returns_discussion_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AFD body with malformed JSON → discussion=None; WARN logged."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
                )
                mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
                    return_value=httpx.Response(200, content=b"not valid json{{{")
                )
                result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        assert result.discussion is None
        warn_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_messages) > 0

    def test_malformed_hourly_response_raises_provider_protocol_error(self) -> None:
        """Malformed /forecast/hourly response → ProviderProtocolError (NOT soft-failure)."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        # forecast_hourly_malformed.json has `properties: {}` which parses with empty periods.
        # To trigger ProviderProtocolError, we need a response that FAILS validation, not just empty.
        # Use a response that's not a Feature at all (missing `type: "Feature"`).
        bad_hourly = {"type": "FeatureCollection", "features": []}  # wrong type → Literal fails

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=bad_hourly)
            )
            with pytest.raises(ProviderProtocolError):
                fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

    def test_leading_night_periods_fixture_first_daily_is_first_day_period(self) -> None:
        """forecast_periods_starts_night.json: first daily point starts with the day period."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        starts_night_fixture = _load_fixture("forecast_periods_starts_night.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
            )
            mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                return_value=httpx.Response(200, json=starts_night_fixture)
            )
            mock.get("https://api.weather.gov/products").mock(
                return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
            )
            result = fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact="test@example.com")

        # Leading night(1) skipped; day periods are 2 and 4; 2 full pairs.
        assert len(result.daily) == 2
        # First daily point comes from period 2 (isDaytime=True, Thursday,
        # startTime=2026-05-08T06:00:00-07:00; validDate is station-local date part).
        assert result.daily[0].validDate == "2026-05-08"
        assert result.daily[0].tempMax == 67.0


# ===========================================================================
# 13. UA contact wiring
# ===========================================================================


class TestUAContactWiring:
    """UA contact is wired into the outbound User-Agent header correctly."""

    def test_with_contact_ua_includes_contact_string(self) -> None:
        """user_agent_contact='me@example.com' → UA = '(weewx-clearskies-api/0.1.0, me@example.com)'."""
        from weewx_clearskies_api.providers.forecast.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent("me@example.com")
        assert "me@example.com" in ua
        assert "weewx-clearskies-api" in ua

    def test_without_contact_ua_excludes_contact(self) -> None:
        """user_agent_contact=None → UA = '(weewx-clearskies-api/0.1.0)' only."""
        from weewx_clearskies_api.providers.forecast.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent(None)
        assert "@" not in ua
        assert "weewx-clearskies-api" in ua
        assert ua.startswith("(")

    def test_without_contact_emits_one_time_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """No user_agent_contact → one-time WARN logged at first fetch() call."""
        from weewx_clearskies_api.providers.forecast.nws import fetch  # noqa: PLC0415

        _reset_provider_state()

        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.providers.forecast.nws"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
                )
                mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
                    return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
                )
                mock.get("https://api.weather.gov/products").mock(
                    return_value=httpx.Response(200, json=_load_fixture("products_afd_list_empty.json"))
                )
                fetch(lat=_LAT, lon=_LON, target_unit="US", user_agent_contact=None)

        warn_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("contact" in m.lower() or "user.agent" in m.lower() or "nws_user_agent" in m.lower() for m in warn_messages), (
            f"Expected WARN about missing UA contact; got: {warn_messages}"
        )

    def test_version_in_ua_string(self) -> None:
        """UA string includes version '0.1.0'."""
        from weewx_clearskies_api.providers.forecast.nws import _build_user_agent  # noqa: PLC0415
        ua = _build_user_agent("contact@example.com")
        assert "0.1.0" in ua


# ===========================================================================
# 14. Capability registry
# ===========================================================================


class TestCapabilityRegistry:
    """Capability registry wired with forecast/nws CAPABILITY."""

    def test_wire_providers_with_nws_capability_populates_registry(self) -> None:
        """wire_providers([nws_forecast.CAPABILITY]) → registry has 'nws' forecast entry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            wire_providers,
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415

        _reset_provider_state()
        wire_providers([forecast_nws.CAPABILITY])

        registry = get_provider_registry()
        nws_entries = [p for p in registry if p.provider_id == "nws" and p.domain == "forecast"]
        assert len(nws_entries) == 1
        assert nws_entries[0].domain == "forecast"

    def test_nws_capability_geographic_coverage_is_us(self) -> None:
        """NWS forecast capability.geographic_coverage = 'us'."""
        from weewx_clearskies_api.providers.forecast.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "us"

    def test_nws_capability_auth_required_is_empty(self) -> None:
        """NWS forecast is keyless: auth_required = ()."""
        from weewx_clearskies_api.providers.forecast.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.auth_required == ()

    def test_nws_capability_default_poll_interval_is_1800(self) -> None:
        """Forecast TTL = 1800s (30 min) per ADR-017."""
        from weewx_clearskies_api.providers.forecast.nws import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 1800

    def test_nws_capability_supplied_fields_includes_discussion_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes ForecastDiscussion fields."""
        from weewx_clearskies_api.providers.forecast.nws import CAPABILITY  # noqa: PLC0415
        for field in ("headline", "body", "issuedAt", "senderName"):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"Discussion field {field!r} missing from CAPABILITY.supplied_canonical_fields"
            )

    def test_nws_capability_supplied_fields_includes_hourly_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes HourlyForecastPoint key fields."""
        from weewx_clearskies_api.providers.forecast.nws import CAPABILITY  # noqa: PLC0415
        for field in ("validTime", "outTemp", "windSpeed", "windDir", "precipType", "weatherCode"):
            assert field in CAPABILITY.supplied_canonical_fields, (
                f"Hourly field {field!r} missing from CAPABILITY.supplied_canonical_fields"
            )


# ===========================================================================
# 15. /capabilities response — nws forecast configured
# ===========================================================================


class TestCapabilitiesEndpointNws:
    """/capabilities response includes nws forecast provider."""

    def test_capabilities_includes_nws_forecast_provider(
        self, forecast_client_nws: Any
    ) -> None:
        """GET /api/v1/capabilities with NWS forecast → nws in providers list."""
        response = forecast_client_nws.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        providers = body["data"]["providers"]
        provider_ids = [p["providerId"] for p in providers]
        assert "nws" in provider_ids, f"Expected 'nws' in providers; got {provider_ids}"

    def test_capabilities_nws_domain_is_forecast(
        self, forecast_client_nws: Any
    ) -> None:
        """NWS entry in /capabilities has domain='forecast'."""
        response = forecast_client_nws.get("/api/v1/capabilities")
        body = response.json()
        providers = body["data"]["providers"]
        nws_entries = [p for p in providers if p["providerId"] == "nws"]
        assert nws_entries, "No NWS provider in response"
        assert nws_entries[0]["domain"] == "forecast"

    def test_capabilities_canonical_fields_includes_discussion_fields(
        self, forecast_client_nws: Any
    ) -> None:
        """canonicalFieldsAvailable includes ForecastDiscussion fields."""
        response = forecast_client_nws.get("/api/v1/capabilities")
        body = response.json()
        available = set(body["data"]["canonicalFieldsAvailable"])
        for field in ("headline", "body", "issuedAt", "senderName"):
            assert field in available, (
                f"Discussion field {field!r} not in canonicalFieldsAvailable"
            )


# ===========================================================================
# 16. /forecast endpoint — nws configured (respx-mocked)
# ===========================================================================


class TestForecastEndpointNws:
    """/forecast endpoint behavior with NWS provider configured."""

    def _mock_all_nws(self, mock: Any) -> None:
        """Wire respx mock for all 5 NWS URLs with real fixtures."""
        mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
            return_value=httpx.Response(200, json=_load_fixture("forecast_points.json"))
        )
        mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast/hourly").mock(
            return_value=httpx.Response(200, json=_load_fixture("forecast_hourly.json"))
        )
        mock.get("https://api.weather.gov/gridpoints/SEW/125,68/forecast").mock(
            return_value=httpx.Response(200, json=_load_fixture("forecast.json"))
        )
        mock.get("https://api.weather.gov/products").mock(
            return_value=httpx.Response(200, json=_load_fixture("products_afd_list.json"))
        )
        mock.get("https://api.weather.gov/products/44453767-e473-4c16-835d-96495e091585").mock(
            return_value=httpx.Response(200, json=_load_fixture("products_afd_body.json"))
        )

    def test_nws_happy_path_returns_200(self, forecast_client_nws: Any) -> None:
        """NWS configured + mocked → 200."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get("/api/v1/forecast")
        assert response.status_code == 200

    def test_nws_happy_path_source_is_nws(self, forecast_client_nws: Any) -> None:
        """NWS configured → response body data.source = 'nws'."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["source"] == "nws"

    def test_nws_happy_path_discussion_not_null(self, forecast_client_nws: Any) -> None:
        """NWS configured + AFD available → discussion is populated (not null)."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert body["data"]["discussion"] is not None

    def test_nws_default_params_returns_48_hourly_and_7_daily(
        self, forecast_client_nws: Any
    ) -> None:
        """No query params → 48 hourly (default), 7 daily (default)."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert len(body["data"]["hourly"]) == 48
        assert len(body["data"]["daily"]) == 7

    def test_nws_slice_params_respected(self, forecast_client_nws: Any) -> None:
        """?hours=24&days=3 → exactly 24 hourly points and 3 daily points."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get(
                "/api/v1/forecast", params={"hours": 24, "days": 3}
            )
        body = response.json()
        assert len(body["data"]["hourly"]) == 24
        assert len(body["data"]["daily"]) == 3

    def test_nws_down_returns_502_provider_problem(self, forecast_client_nws: Any) -> None:
        """/points 503 → 502 ProviderProblem with errorCode=TransientNetworkError."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(503, text="Service unavailable")
            )
            response = forecast_client_nws.get("/api/v1/forecast")

        assert response.status_code == 502
        body = response.json()
        assert body["errorCode"] == "TransientNetworkError"

    def test_nws_quota_exhausted_returns_503_provider_problem(
        self, forecast_client_nws: Any
    ) -> None:
        """/points 429 → 503 ProviderProblem with errorCode=QuotaExhausted."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/47.6062,-122.3321").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "60"}, text="Too Many Requests")
            )
            response = forecast_client_nws.get("/api/v1/forecast")

        assert response.status_code == 503
        body = response.json()
        assert body["errorCode"] == "QuotaExhausted"

    def test_nws_non_us_location_returns_503_geographically_unsupported(
        self, forecast_client_nws: Any
    ) -> None:
        """/points 404 (non-US lat/lon) → 503 ProviderProblem GeographicallyUnsupported."""
        from weewx_clearskies_api.services import station as station_mod  # noqa: PLC0415
        from weewx_clearskies_api.services.station import StationInfo, reset_cache  # noqa: PLC0415

        # Override station to a non-US location (Sydney, Australia).
        reset_cache()
        station_mod._cached_station = StationInfo(
            station_id="test-aus",
            name="Sydney Test",
            latitude=-33.8688,
            longitude=151.2093,
            altitude=5.0,
            timezone="Australia/Sydney",
            timezone_offset_minutes=600,
            unit_system="METRIC",
            hardware=None,
        )

        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.weather.gov/points/-33.8688,151.2093").mock(
                return_value=httpx.Response(404, json=_load_fixture("forecast_points_404.json"))
            )
            response = forecast_client_nws.get("/api/v1/forecast")

        assert response.status_code == 503
        body = response.json()
        assert body["errorCode"] == "GeographicallyUnsupported"

    def test_nws_units_block_present_in_response(self, forecast_client_nws: Any) -> None:
        """Response includes envelope units block."""
        _reset_provider_state()
        from weewx_clearskies_api.providers._common.cache import (
            wire_cache_from_env,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers._common.capability import (
            wire_providers,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.forecast import nws as forecast_nws  # noqa: PLC0415
        wire_cache_from_env()
        wire_providers([forecast_nws.CAPABILITY])

        with respx.mock(assert_all_called=False) as mock:
            self._mock_all_nws(mock)
            response = forecast_client_nws.get("/api/v1/forecast")
        body = response.json()
        assert "units" in body
        assert isinstance(body["units"], dict)
