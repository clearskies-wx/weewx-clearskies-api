"""Unit tests for the OpenAQ v3 AQI provider module (ADR-066, ADR-038).

Covers per the T9.3 brief:

  CAPABILITY declaration:
  - provider_id="openaq", domain="aqi", is_observed_source=True.
  - auth_required=("api_key",).
  - wire_providers([CAPABILITY]) → registry entry for ("aqi", "openaq").

  Wire-shape Pydantic validation:
  - openaq_locations.json validates against _OpenAQLocationsResponse.
  - openaq_latest.json validates against _OpenAQLatestResponse.
  - Extra fields (coordinates, summary, coverage, datetimeFirst, datetimeLast, etc.) ignored.

  _wire_to_canonical:
  - PM2.5 value mapped to pollutantPM25, PM10 to pollutantPM10.
  - aqi=None (OpenAQ does not compute composite AQI index).
  - aqiScale=None, aqiCategory=None, aqiMainPollutant=None.
  - aqiLocation = location name from resolved sensor state.
  - observedAt = UTC Z form from datetime.utc field.
  - source = "openaq".
  - Gas fields (O3, NO2, SO2, CO) all None.

  _build_cache_key:
  - Same lat/lon → same key (deterministic, 64-char SHA-256 hex).
  - Different lat/lon → different key.
  - Credentials NOT in key signature (LC7).

  fetch() — two-step flow:
  - Cache hit → canonical reconstruction; no HTTP call.
  - Cache hit with _no_reading sentinel → None; no HTTP call.
  - Cache miss: resolves location (GET /v3/locations), then fetches latest
    (GET /v3/locations/{id}/latest) → canonical AQIReading.
  - Cache miss with second fetch → location already resolved, skips step 1.
  - Empty /latest results → None + sentinel cached.

  Error handling (L2 carry-forward — canonical exceptions propagate bare):
  - 401 → KeyInvalid.
  - 429 → QuotaExhausted.
  - 5xx → TransientNetworkError.

No DB, no live network. respx mocks outbound httpx calls.
Fixtures: tests/fixtures/providers/aqi/openaq_locations.json, openaq_latest.json.
ADR references: ADR-017, ADR-038, ADR-059, ADR-066.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Constants matching the openaq.py source
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "aqi"

_OPENAQ_BASE_URL = "https://api.openaq.org"
_OPENAQ_LOCATIONS_URL = f"{_OPENAQ_BASE_URL}/v3/locations"

# Coordinates for all tests (Seattle area — matches fixture data)
_LAT = 47.6062
_LON = -122.3321
_LAT6 = round(_LAT, 6)
_LON6 = round(_LON, 6)

_TEST_API_KEY = "TEST_OPENAQ_API_KEY_123"

# Resolved location from fixture: first location in openaq_locations.json
_FIXTURE_LOCATION_ID = 12345
_FIXTURE_LOCATION_NAME = "Seattle-Beacon Hill"
_FIXTURE_PM25_SENSOR_ID = 678
_FIXTURE_PM10_SENSOR_ID = 679
_FIXTURE_LATEST_URL = f"{_OPENAQ_BASE_URL}/v3/locations/{_FIXTURE_LOCATION_ID}/latest"


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/aqi/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helper (mirrors test_aeris.py pattern)
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, sensor state, and HTTP client."""
    import weewx_clearskies_api.providers.aqi.openaq as _openaq  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
        _reset_http_client_for_tests,
        _reset_sensor_state_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _reset_sensor_state_for_tests()
    _openaq._rate_limiter._calls.clear()
    wire_cache_from_env()


# ===========================================================================
# 1. Fixture loading — Pydantic wire-shape validation
# ===========================================================================


class TestOpenAQFixturesValidate:
    """Real fixture files validate against the OpenAQ Pydantic wire models."""

    def test_locations_fixture_loads_as_valid_json(self) -> None:
        """openaq_locations.json is parseable JSON."""
        data = _load_fixture("openaq_locations.json")
        assert isinstance(data, dict)

    def test_latest_fixture_loads_as_valid_json(self) -> None:
        """openaq_latest.json is parseable JSON."""
        data = _load_fixture("openaq_latest.json")
        assert isinstance(data, dict)

    def test_locations_fixture_validates_against_wire_model(self) -> None:
        """openaq_locations.json validates cleanly against _OpenAQLocationsResponse."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLocationsResponse,
        )
        data = _load_fixture("openaq_locations.json")
        result = _OpenAQLocationsResponse.model_validate(data)
        assert len(result.results) == 2

    def test_locations_fixture_extra_fields_ignored(self) -> None:
        """Locations fixture has extra fields (country, owner, coordinates) — all ignored."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLocationsResponse,
        )
        data = _load_fixture("openaq_locations.json")
        # Should not raise even though fixture has many extra fields
        result = _OpenAQLocationsResponse.model_validate(data)
        assert result is not None

    def test_locations_fixture_first_result_has_pm25_sensor(self) -> None:
        """First location in fixture has a PM2.5 sensor (id=678, name='pm25')."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLocationsResponse,
        )
        data = _load_fixture("openaq_locations.json")
        result = _OpenAQLocationsResponse.model_validate(data)
        first = result.results[0]
        assert first.id == _FIXTURE_LOCATION_ID
        sensor_names = [s.name for s in first.sensors]
        assert "pm25" in sensor_names

    def test_latest_fixture_validates_against_wire_model(self) -> None:
        """openaq_latest.json validates cleanly against _OpenAQLatestResponse."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLatestResponse,
        )
        data = _load_fixture("openaq_latest.json")
        result = _OpenAQLatestResponse.model_validate(data)
        assert len(result.results) == 3

    def test_latest_fixture_extra_fields_ignored(self) -> None:
        """Latest fixture has extra fields (coordinates, summary, coverage) — all ignored."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLatestResponse,
        )
        data = _load_fixture("openaq_latest.json")
        result = _OpenAQLatestResponse.model_validate(data)
        assert result is not None

    def test_latest_fixture_pm25_entry_has_correct_value(self) -> None:
        """Fixture PM2.5 result has value=12.5 µg/m³."""
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLatestResponse,
        )
        data = _load_fixture("openaq_latest.json")
        result = _OpenAQLatestResponse.model_validate(data)
        pm25_results = [r for r in result.results if r.parameter.name == "pm25"]
        assert len(pm25_results) == 1
        assert pm25_results[0].value == 12.5


# ===========================================================================
# 2. _wire_to_canonical — happy path from fixture
# ===========================================================================


class TestWireToCanonicalHappyPath:
    """_wire_to_canonical translates /latest results to correct canonical AQIReading."""

    def _setup_resolved_state(self) -> None:
        """Inject module-level resolved sensor state matching the fixture."""
        import weewx_clearskies_api.providers.aqi.openaq as _openaq  # noqa: PLC0415
        _openaq._resolved_location_id = _FIXTURE_LOCATION_ID
        _openaq._resolved_location_name = _FIXTURE_LOCATION_NAME
        _openaq._resolved_sensor_pm25_id = _FIXTURE_PM25_SENSOR_ID
        _openaq._resolved_sensor_pm10_id = _FIXTURE_PM10_SENSOR_ID

    def setup_method(self) -> None:
        _reset_provider_state()
        self._setup_resolved_state()

    def _get_results_from_fixture(self) -> list[Any]:
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _OpenAQLatestResponse,
        )
        data = _load_fixture("openaq_latest.json")
        return _OpenAQLatestResponse.model_validate(data).results

    def test_fixture_produces_canonical_aqi_reading(self) -> None:
        """_wire_to_canonical returns AQIReading (not None) for the real fixture."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None, "_wire_to_canonical must return AQIReading for valid fixture"

    def test_fixture_aqi_is_none(self) -> None:
        """aqi=None (OpenAQ does not compute composite AQI — canonical spec)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.aqi is None, f"Expected aqi=None for OpenAQ, got {record.aqi!r}"

    def test_fixture_aqi_scale_is_none(self) -> None:
        """aqiScale=None (OpenAQ does not use a composite AQI scale)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.aqiScale is None

    def test_fixture_aqi_category_is_none(self) -> None:
        """aqiCategory=None (no composite AQI category from OpenAQ)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.aqiCategory is None

    def test_fixture_aqi_main_pollutant_is_none(self) -> None:
        """aqiMainPollutant=None (no dominant pollutant derivable without composite AQI)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.aqiMainPollutant is None

    def test_fixture_pm25_maps_to_pollutant_pm25(self) -> None:
        """PM2.5 value 12.5 µg/m³ → pollutantPM25=12.5."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.pollutantPM25 == 12.5, (
            f"Expected pollutantPM25=12.5, got {record.pollutantPM25!r}"
        )

    def test_fixture_pm10_maps_to_pollutant_pm10(self) -> None:
        """PM10 value 18.3 µg/m³ → pollutantPM10=18.3."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.pollutantPM10 == 18.3, (
            f"Expected pollutantPM10=18.3, got {record.pollutantPM10!r}"
        )

    def test_fixture_gas_fields_are_none(self) -> None:
        """Gas fields (O3, NO2, SO2, CO) are all None — OpenAQ only provides PM."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.pollutantO3 is None
        assert record.pollutantNO2 is None
        assert record.pollutantSO2 is None
        assert record.pollutantCO is None

    def test_fixture_source_is_openaq(self) -> None:
        """source='openaq' (provider_id literal on canonical record)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.source == "openaq", f"Expected source='openaq', got {record.source!r}"

    def test_fixture_aqi_location_is_station_name(self) -> None:
        """aqiLocation = location name passed to _wire_to_canonical."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.aqiLocation == _FIXTURE_LOCATION_NAME

    def test_fixture_observed_at_is_utc_z(self) -> None:
        """observedAt ends with Z (UTC ISO-8601 per ADR-020)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.observedAt is not None
        assert record.observedAt.endswith("Z"), (
            f"observedAt must end with Z, got {record.observedAt!r}"
        )

    def test_fixture_observed_at_is_correct_utc_timestamp(self) -> None:
        """observedAt = '2026-06-22T10:00:00Z' (from PM2.5 datetime.utc in fixture)."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=_FIXTURE_LOCATION_NAME)
        assert record is not None
        assert record.observedAt == "2026-06-22T10:00:00Z", (
            f"Expected '2026-06-22T10:00:00Z', got {record.observedAt!r}"
        )

    def test_none_location_name_yields_none_aqi_location(self) -> None:
        """location_name=None → aqiLocation=None on canonical record."""
        from weewx_clearskies_api.providers.aqi.openaq import _wire_to_canonical  # noqa: PLC0415
        results = self._get_results_from_fixture()
        record = _wire_to_canonical(results, location_name=None)
        assert record is not None
        assert record.aqiLocation is None


# ===========================================================================
# 3. _build_cache_key — determinism and privacy
# ===========================================================================


class TestBuildCacheKey:
    """_build_cache_key is deterministic, rounds lat/lon, excludes credentials."""

    def test_same_lat_lon_produces_same_key(self) -> None:
        """Same lat/lon → identical cache key (deterministic)."""
        from weewx_clearskies_api.providers.aqi.openaq import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(_LAT, _LON)
        key2 = _build_cache_key(_LAT, _LON)
        assert key1 == key2

    def test_different_lat_lon_produces_different_key(self) -> None:
        """Different lat/lon → different cache key."""
        from weewx_clearskies_api.providers.aqi.openaq import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.6062, -122.3321)
        key2 = _build_cache_key(40.7128, -74.0060)
        assert key1 != key2

    def test_key_is_64_char_hex_string(self) -> None:
        """Cache key is a 64-character lowercase hexadecimal SHA-256 string."""
        from weewx_clearskies_api.providers.aqi.openaq import _build_cache_key  # noqa: PLC0415
        key = _build_cache_key(_LAT, _LON)
        assert len(key) == 64, f"Expected 64-char key, got {len(key)!r}"
        assert all(c in "0123456789abcdef" for c in key), "Key must be lowercase hex"

    def test_lat_lon_rounded_to_4_decimal_places(self) -> None:
        """Coordinates identical at 4dp → same cache key (LC7 precision rule)."""
        from weewx_clearskies_api.providers.aqi.openaq import _build_cache_key  # noqa: PLC0415
        key1 = _build_cache_key(47.60620001, -122.33210001)
        key2 = _build_cache_key(47.60620009, -122.33210009)
        assert key1 == key2

    def test_credentials_not_in_cache_key_signature(self) -> None:
        """_build_cache_key accepts only lat/lon — no api_key parameter (LC7)."""
        import inspect  # noqa: PLC0415

        from weewx_clearskies_api.providers.aqi.openaq import _build_cache_key  # noqa: PLC0415
        sig = inspect.signature(_build_cache_key)
        param_names = list(sig.parameters.keys())
        assert "api_key" not in param_names, (
            "_build_cache_key must not accept api_key (credentials not in cache key)"
        )


# ===========================================================================
# 4. fetch() — cache hit paths
# ===========================================================================


class TestFetchCacheHit:
    """fetch() returns cached canonical record without making HTTP calls."""

    def setup_method(self) -> None:
        _reset_provider_state()

    def test_cache_hit_returns_canonical_reading_without_http_call(self) -> None:
        """Cache hit → canonical AQIReading returned; no outbound HTTP call."""
        from weewx_clearskies_api.models.responses import AQIReading  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _build_cache_key,
            fetch,
        )

        reading = AQIReading(
            aqi=None,
            aqiScale=None,
            aqiCategory=None,
            aqiMainPollutant=None,
            aqiLocation=_FIXTURE_LOCATION_NAME,
            pollutantPM25=12.5,
            pollutantPM10=18.3,
            pollutantO3=None,
            pollutantNO2=None,
            pollutantSO2=None,
            pollutantCO=None,
            observedAt="2026-06-22T10:00:00Z",
            source="openaq",
        )
        cache_key = _build_cache_key(_LAT, _LON)
        get_cache().set(cache_key, reading.model_dump(), ttl_seconds=3600)

        with respx.mock(assert_all_called=False) as mock:
            result = fetch(
                lat=_LAT,
                lon=_LON,
                api_key=_TEST_API_KEY,
            )
            assert len(mock.calls) == 0, "No HTTP calls expected on cache hit"

        assert result is not None
        assert result.pollutantPM25 == 12.5
        assert result.source == "openaq"
        assert result.aqi is None

    def test_cache_hit_sentinel_returns_none_without_http_call(self) -> None:
        """Cache hit with _no_reading sentinel → None; no outbound HTTP call."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _build_cache_key,
            fetch,
        )

        cache_key = _build_cache_key(_LAT, _LON)
        get_cache().set(cache_key, {"_no_reading": True}, ttl_seconds=3600)

        with respx.mock(assert_all_called=False) as mock:
            result = fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)
            assert len(mock.calls) == 0

        assert result is None


# ===========================================================================
# 5. fetch() — cache miss + HTTP paths (happy path)
# ===========================================================================


class TestFetchCacheMissHappyPath:
    """fetch() cache miss: resolves location then fetches latest data."""

    def setup_method(self) -> None:
        _reset_provider_state()

    def test_cache_miss_happy_path_two_step_flow_returns_canonical(self) -> None:
        """Cache miss: GET /v3/locations → GET /v3/locations/{id}/latest → AQIReading."""
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        locations_data = _load_fixture("openaq_locations.json")
        latest_data = _load_fixture("openaq_latest.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(200, json=latest_data)
            )
            result = fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

        assert result is not None
        assert result.pollutantPM25 == 12.5
        assert result.pollutantPM10 == 18.3
        assert result.source == "openaq"
        assert result.aqi is None
        assert result.observedAt == "2026-06-22T10:00:00Z"

    def test_cache_miss_happy_path_result_cached(self) -> None:
        """Cache miss happy path → result written to cache for subsequent reads."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _build_cache_key,
            fetch,
        )

        locations_data = _load_fixture("openaq_locations.json")
        latest_data = _load_fixture("openaq_latest.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(200, json=latest_data)
            )
            fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

        cache_key = _build_cache_key(_LAT, _LON)
        cached = get_cache().get(cache_key)
        assert cached is not None, "Successful fetch must populate cache"

    def test_empty_latest_results_returns_none_and_caches_sentinel(self) -> None:
        """Empty /latest results → None returned + _no_reading sentinel cached."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import (  # noqa: PLC0415
            _build_cache_key,
            fetch,
        )

        locations_data = _load_fixture("openaq_locations.json")
        empty_latest = {"meta": {}, "results": []}

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(200, json=empty_latest)
            )
            result = fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

        assert result is None, "Empty results must return None"
        cache_key = _build_cache_key(_LAT, _LON)
        cached = get_cache().get(cache_key)
        assert cached == {"_no_reading": True}, (
            "Empty results must cache _no_reading sentinel"
        )

    def test_second_fetch_skips_location_resolution(self) -> None:
        """Second fetch() reuses resolved sensor state — only one /latest call."""
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        locations_data = _load_fixture("openaq_locations.json")
        latest_data = _load_fixture("openaq_latest.json")

        with respx.mock(assert_all_called=False) as mock:
            # Step 1: Both calls needed on first fetch
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(200, json=latest_data)
            )
            fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

        # Reset cache only (keep resolved sensor state)
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        import weewx_clearskies_api.providers.aqi.openaq as _openaq  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import _reset_http_client_for_tests  # noqa: PLC0415

        reset_cache_for_tests()
        wire_cache_from_env()
        _reset_http_client_for_tests()
        _openaq._rate_limiter._calls.clear()

        with respx.mock(assert_all_called=False) as mock2:
            # Second fetch: location already resolved → should NOT call /v3/locations
            locations_route = mock2.get(_OPENAQ_LOCATIONS_URL)
            mock2.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(200, json=latest_data)
            )
            result2 = fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)
            assert not locations_route.called, (
                "Second fetch must not call /v3/locations again (sensor state already resolved)"
            )

        assert result2 is not None


# ===========================================================================
# 6. fetch() — error handling (L2 carry-forward)
# ===========================================================================


class TestFetchErrorHandling:
    """Error responses propagate as canonical exception types (L2 carry-forward)."""

    def setup_method(self) -> None:
        _reset_provider_state()

    def test_locations_401_raises_key_invalid(self) -> None:
        """HTTP 401 on /v3/locations → KeyInvalid (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(401, json={"detail": "Invalid API key"})
            )
            with pytest.raises(KeyInvalid):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

    def test_locations_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 on /v3/locations → QuotaExhausted (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": "60"},
                )
            )
            with pytest.raises(QuotaExhausted):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

    def test_locations_500_raises_transient_network_error(self) -> None:
        """HTTP 500 on /v3/locations → TransientNetworkError (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import (  # noqa: PLC0415
            TransientNetworkError,
        )
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(500, json={"detail": "Internal server error"})
            )
            with pytest.raises(TransientNetworkError):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

    def test_latest_401_raises_key_invalid(self) -> None:
        """HTTP 401 on /v3/locations/{id}/latest → KeyInvalid (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        locations_data = _load_fixture("openaq_locations.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(401, json={"detail": "Invalid API key"})
            )
            with pytest.raises(KeyInvalid):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

    def test_latest_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 on /v3/locations/{id}/latest → QuotaExhausted (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        locations_data = _load_fixture("openaq_locations.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(
                    429,
                    json={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": "30"},
                )
            )
            with pytest.raises(QuotaExhausted):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)

    def test_latest_500_raises_transient_network_error(self) -> None:
        """HTTP 500 on /v3/locations/{id}/latest → TransientNetworkError (L2 carry-forward)."""
        from weewx_clearskies_api.providers._common.errors import (  # noqa: PLC0415
            TransientNetworkError,
        )
        from weewx_clearskies_api.providers.aqi.openaq import fetch  # noqa: PLC0415

        locations_data = _load_fixture("openaq_locations.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OPENAQ_LOCATIONS_URL).mock(
                return_value=httpx.Response(200, json=locations_data)
            )
            mock.get(_FIXTURE_LATEST_URL).mock(
                return_value=httpx.Response(500, json={"detail": "Internal server error"})
            )
            with pytest.raises(TransientNetworkError):
                fetch(lat=_LAT, lon=_LON, api_key=_TEST_API_KEY)


# ===========================================================================
# 7. CAPABILITY declaration
# ===========================================================================


class TestCapabilityDeclaration:
    """CAPABILITY symbol declares the correct provider metadata."""

    def test_capability_provider_id_is_openaq(self) -> None:
        """CAPABILITY.provider_id = 'openaq'."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.provider_id == "openaq"

    def test_capability_domain_is_aqi(self) -> None:
        """CAPABILITY.domain = 'aqi'."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.domain == "aqi"

    def test_capability_is_observed_source_is_true(self) -> None:
        """CAPABILITY.is_observed_source=True (government reference monitors — haze-eligible)."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.is_observed_source is True, (
            "OpenAQ must be is_observed_source=True (government reference monitors, ADR-066)"
        )

    def test_capability_auth_required_includes_api_key(self) -> None:
        """CAPABILITY.auth_required = ('api_key',) (X-API-Key header pattern)."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert "api_key" in CAPABILITY.auth_required, (
            "OpenAQ CAPABILITY.auth_required must include 'api_key'"
        )

    def test_capability_geographic_coverage_is_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global' (141+ countries per ADR-066)."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_supplied_fields_includes_pm25_and_pm10(self) -> None:
        """CAPABILITY.supplied_canonical_fields includes pollutantPM25 and pollutantPM10."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        supplied = set(CAPABILITY.supplied_canonical_fields)
        assert "pollutantPM25" in supplied, "CAPABILITY must declare pollutantPM25"
        assert "pollutantPM10" in supplied, "CAPABILITY must declare pollutantPM10"

    def test_capability_default_poll_interval_is_3600_seconds(self) -> None:
        """CAPABILITY.default_poll_interval_seconds = 3600 (1 hour; data lag makes shorter TTL wasteful)."""
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415
        assert CAPABILITY.default_poll_interval_seconds == 3600, (
            f"Expected 3600s poll interval (1 hour), got {CAPABILITY.default_poll_interval_seconds!r}"
        )

    def test_wire_providers_registers_openaq_aqi_capability(self) -> None:
        """wire_providers([openaq.CAPABILITY]) → registry entry for ('aqi', 'openaq')."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.aqi.openaq import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(p.provider_id == "openaq" and p.domain == "aqi" for p in registry), (
            "wire_providers must register openaq aqi capability in registry"
        )
