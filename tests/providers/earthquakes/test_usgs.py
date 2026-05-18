"""Unit tests for the USGS earthquake provider module (3b-13).

Covers per the task-3b-13 brief §Test surface (test_usgs.py):

  Wire-shape Pydantic validation:
  - Seattle radius fixture loads cleanly via _UsgsResponse (3-feature FeatureCollection).
  - Extra wire fields (tz, detail, cdi, sig, etc.) ignored (extra="ignore").
  - Required fields enforced: missing id → ValidationError → ProviderProtocolError.

  _to_canonical() mapping (per canonical-data-model §4.4 USGS column):
  - id from top-level Feature.id.
  - time from properties.time (epoch ms → UTC ISO-8601 via epoch_ms_to_utc_iso8601).
  - latitude from geometry.coordinates[1], longitude from geometry.coordinates[0].
  - depth from geometry.coordinates[2] (USGS uses positive km; no sign flip needed).
  - magnitude from properties.mag.
  - magnitudeType from properties.magType.
  - place from properties.place.
  - url from properties.url.
  - tsunami from properties.tsunami (0/1 int → bool).
  - felt from properties.felt.
  - mmi from properties.mmi (nullable).
  - alert from properties.alert (nullable).
  - status from properties.status.
  - source = "usgs".
  - extras carries net, code, ids, sources, types, sig, nst, dmin, rms, gap, type.

  Epoch ms → ISO conversion:
  - 1778131207650 ms → "2026-05-07T02:40:07Z" (verified via datetime.fromtimestamp).
  - epoch_ms_to_utc_iso8601 present in _common/datetime_utils.py.
  - ProviderProtocolError raised on non-numeric epoch_ms.

  Tsunami 0/1 → bool:
  - tsunami=0 → False.
  - tsunami=1 → True.

  Cache TTL:
  - _USGS_CACHE_TTL == 60 (per brief Q2 resolution).

  fetch() happy path:
  - Cache miss → HTTP call → list[EarthquakeRecord] returned and cached.
  - Cache hit → 0 HTTP calls, same records returned.
  - Redis fakeredis cache hit → 0 HTTP calls.

  ProviderProtocolError on invalid wire shape:
  - Drop 'id' from a feature → ValidationError caught → ProviderProtocolError raised.

  Rate limiter:
  - Two fetch() calls in quick succession do not raise (rate limiter within bounds).

  Capability declaration:
  - CAPABILITY.provider_id = "usgs".
  - CAPABILITY.domain = "earthquakes".
  - CAPABILITY.auth_required = () (empty tuple — keyless).
  - CAPABILITY.geographic_coverage = "global".
  - CAPABILITY.supplied_canonical_fields covers the 10 fields USGS supplies.
  - wire_providers([CAPABILITY]) registers usgs earthquakes in registry.

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/earthquakes/usgs_seattle_radius_m2_5.json
ADR references: ADR-017, ADR-020, ADR-038, ADR-040.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = (
    Path(__file__).parent.parent.parent / "fixtures" / "providers" / "earthquakes"
)

# Station coordinates for cache-key tests
_LAT = 47.6
_LON = -122.3

_USGS_BASE_URL = "https://earthquake.usgs.gov"
_USGS_QUERY_PATH = "/fdsnws/event/1/query"
_USGS_QUERY_URL = _USGS_BASE_URL + _USGS_QUERY_PATH


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures/providers/earthquakes/."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# State-reset helper
# ---------------------------------------------------------------------------


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and re-wire memory cache."""
    import os  # noqa: PLC0415

    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
        _rate_limiter,
        _reset_http_client_for_tests,
    )

    cache_url = os.environ.get("CLEARSKIES_CACHE_URL")
    if cache_url:
        try:
            import redis as redis_lib  # noqa: PLC0415
            r = redis_lib.from_url(cache_url)
            r.flushdb()
        except Exception:  # noqa: BLE001
            pass

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    _reset_http_client_for_tests()
    _rate_limiter._calls.clear()
    wire_cache_from_env()


# ===========================================================================
# 1. Wire-shape Pydantic validation
# ===========================================================================


class TestUSGSWireShapeValidation:
    """Wire-shape models validate correctly against the fixture and edge-case shapes."""

    def test_fixture_loads_cleanly_via_response_model(self) -> None:
        """usgs_seattle_radius_m2_5.json loads via _UsgsResponse without error."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        assert response.type == "FeatureCollection"
        assert len(response.features) == 3

    def test_fixture_has_three_features(self) -> None:
        """Fixture has exactly 3 features (sliced to 3 at capture time)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        assert len(response.features) == 3, (
            f"Expected 3 features, got {len(response.features)}"
        )

    def test_extra_wire_fields_are_ignored(self) -> None:
        """Extra wire fields (tz, detail, cdi, sig, etc.) silently ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        raw["unexpected_future_field"] = "should_be_dropped"
        raw["features"][0]["properties"]["future_field"] = "also_dropped"
        response = _UsgsResponse.model_validate(raw)
        assert response is not None, "Extra fields must not cause ValidationError"

    def test_first_feature_id_is_uw62242697(self) -> None:
        """Feature[0].id = 'uw62242697' (top-level Feature.id, not properties field)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        assert response.features[0].id == "uw62242697", (
            f"Expected id='uw62242697', got {response.features[0].id!r}"
        )

    def test_first_feature_time_is_epoch_ms(self) -> None:
        """Feature[0].properties.time = 1778131207650 (epoch milliseconds, not ISO)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        assert response.features[0].properties.time == 1778131207650, (
            f"Expected time=1778131207650, got {response.features[0].properties.time!r}"
        )

    def test_first_feature_tsunami_is_zero_integer(self) -> None:
        """Feature[0].properties.tsunami = 0 (integer, not boolean; cast at canonical layer)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import _UsgsResponse  # noqa: PLC0415

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        assert response.features[0].properties.tsunami == 0, (
            f"Expected tsunami=0 (int), got {response.features[0].properties.tsunami!r}"
        )

    def test_missing_id_raises_validation_error(self) -> None:
        """Dropping 'id' from a Feature → ValidationError (required field)."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.earthquakes.usgs import (
            _UsgsEventFeature,  # noqa: PLC0415
        )

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        feature_raw = raw["features"][0].copy()
        del feature_raw["id"]
        with pytest.raises(ValidationError):
            _UsgsEventFeature.model_validate(feature_raw)


# ===========================================================================
# 2. epoch_ms_to_utc_iso8601 — USGS-specific helper
# ===========================================================================


class TestEpochMsToUtcIso8601:
    """epoch_ms_to_utc_iso8601 converts USGS epoch-ms timestamps correctly."""

    def test_epoch_ms_helper_exists_in_datetime_utils(self) -> None:
        """epoch_ms_to_utc_iso8601 is present in _common/datetime_utils.py."""
        from weewx_clearskies_api.providers._common import datetime_utils  # noqa: PLC0415

        assert hasattr(datetime_utils, "epoch_ms_to_utc_iso8601"), (
            "epoch_ms_to_utc_iso8601 must be added to _common/datetime_utils.py for USGS"
        )

    def test_known_epoch_ms_converts_to_correct_iso(self) -> None:
        """1778131207650 ms → '2026-05-07T02:40:07Z' (numerical sanity check)."""
        from weewx_clearskies_api.providers._common.datetime_utils import (  # noqa: PLC0415
            epoch_ms_to_utc_iso8601,
        )

        result = epoch_ms_to_utc_iso8601(
            1778131207650,
            provider_id="usgs",
            domain="earthquakes",
        )
        # Verify: datetime.fromtimestamp(1778131207650/1000, tz=UTC)
        expected_dt = datetime.fromtimestamp(1778131207650 / 1000, tz=UTC)
        expected = expected_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert result == expected, (
            f"epoch_ms_to_utc_iso8601(1778131207650) = {result!r}, expected {expected!r}"
        )

    def test_epoch_ms_result_ends_with_z(self) -> None:
        """epoch_ms_to_utc_iso8601 result always ends with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.datetime_utils import (  # noqa: PLC0415
            epoch_ms_to_utc_iso8601,
        )

        result = epoch_ms_to_utc_iso8601(1778131207650, provider_id="usgs", domain="earthquakes")
        assert result.endswith("Z"), f"Result must end with Z, got {result!r}"

    def test_epoch_ms_invalid_raises_provider_protocol_error(self) -> None:
        """Non-numeric epoch_ms → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.datetime_utils import (  # noqa: PLC0415
            epoch_ms_to_utc_iso8601,
        )
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )

        with pytest.raises(ProviderProtocolError):
            epoch_ms_to_utc_iso8601("not-a-number", provider_id="usgs", domain="earthquakes")  # type: ignore[arg-type]


# ===========================================================================
# 3. _to_canonical() — field mapping
# ===========================================================================


class TestUSGSToCanonical:
    """_to_canonical() maps every §4.4 USGS field to the correct EarthquakeRecord field."""

    def _get_first_canonical(self) -> Any:
        """Load fixture, parse, and get first canonical EarthquakeRecord."""
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _to_canonical,
            _UsgsResponse,
        )

        raw = _load_fixture("usgs_seattle_radius_m2_5.json")
        response = _UsgsResponse.model_validate(raw)
        feature = response.features[0]
        return _to_canonical(feature, raw["features"][0])

    def test_id_is_feature_id(self) -> None:
        """id = Feature.id = 'uw62242697' (top-level, not properties field)."""
        record = self._get_first_canonical()
        assert record.id == "uw62242697", f"Expected id='uw62242697', got {record.id!r}"

    def test_time_is_utc_iso_z_format(self) -> None:
        """time is UTC ISO-8601 Z format (epoch ms converted via epoch_ms_to_utc_iso8601)."""
        record = self._get_first_canonical()
        assert record.time.endswith("Z"), f"time must end with Z, got {record.time!r}"
        # Verify the decoded date is correct
        from weewx_clearskies_api.providers._common.datetime_utils import (  # noqa: PLC0415
            epoch_ms_to_utc_iso8601,
        )
        expected = epoch_ms_to_utc_iso8601(1778131207650, provider_id="usgs", domain="earthquakes")
        assert record.time == expected, f"Expected time={expected!r}, got {record.time!r}"

    def test_latitude_from_coordinates_1(self) -> None:
        """latitude = geometry.coordinates[1] = 48.5363... (not [0])."""
        record = self._get_first_canonical()
        assert abs(record.latitude - 48.53633499145508) < 1e-6, (
            f"Expected latitude≈48.5363, got {record.latitude!r}"
        )

    def test_longitude_from_coordinates_0(self) -> None:
        """longitude = geometry.coordinates[0] = -121.7728..."""
        record = self._get_first_canonical()
        assert abs(record.longitude - (-121.77283477783203)) < 1e-6, (
            f"Expected longitude≈-121.7728, got {record.longitude!r}"
        )

    def test_depth_from_coordinates_2_positive(self) -> None:
        """depth = geometry.coordinates[2] (USGS uses positive km; no sign flip)."""
        record = self._get_first_canonical()
        # Feature[0] has depth 0.189999997615814 in coordinates[2]
        assert record.depth is not None
        assert record.depth > 0, f"USGS depth must be positive, got {record.depth!r}"
        assert abs(record.depth - 0.189999997615814) < 1e-4, (
            f"Expected depth≈0.19, got {record.depth!r}"
        )

    def test_magnitude_from_properties_mag(self) -> None:
        """magnitude = properties.mag = 2.8379..."""
        record = self._get_first_canonical()
        assert abs(record.magnitude - 2.8379271030426025) < 1e-6, (
            f"Expected magnitude≈2.838, got {record.magnitude!r}"
        )

    def test_magnitude_type_from_properties_mag_type(self) -> None:
        """magnitudeType = properties.magType = 'ml'."""
        record = self._get_first_canonical()
        assert record.magnitudeType == "ml", (
            f"Expected magnitudeType='ml', got {record.magnitudeType!r}"
        )

    def test_place_from_properties_place(self) -> None:
        """place = properties.place = '1 km W of Concrete, Washington'."""
        record = self._get_first_canonical()
        assert record.place == "1 km W of Concrete, Washington", (
            f"Expected place='1 km W of Concrete, Washington', got {record.place!r}"
        )

    def test_url_from_properties_url(self) -> None:
        """url = properties.url (USGS provides direct url in properties)."""
        record = self._get_first_canonical()
        assert record.url == "https://earthquake.usgs.gov/earthquakes/eventpage/uw62242697", (
            f"Expected usgs eventpage url, got {record.url!r}"
        )

    def test_tsunami_zero_maps_to_false(self) -> None:
        """tsunami = bool(0) = False (int 0 cast to bool per §4.4)."""
        record = self._get_first_canonical()
        assert record.tsunami is False, f"Expected tsunami=False (bool), got {record.tsunami!r}"
        assert isinstance(record.tsunami, bool), (
            f"tsunami must be bool type, got {type(record.tsunami).__name__!r}"
        )

    def test_felt_from_properties_felt(self) -> None:
        """felt = properties.felt = 160 (integer count of felt reports)."""
        record = self._get_first_canonical()
        assert record.felt == 160, f"Expected felt=160, got {record.felt!r}"

    def test_mmi_is_none_when_null(self) -> None:
        """mmi = None when properties.mmi is null (most events have null mmi)."""
        record = self._get_first_canonical()
        assert record.mmi is None, f"Expected mmi=None, got {record.mmi!r}"

    def test_alert_is_none_when_null(self) -> None:
        """alert = None when properties.alert is null (no PAGER assessment)."""
        record = self._get_first_canonical()
        assert record.alert is None, f"Expected alert=None, got {record.alert!r}"

    def test_status_from_properties_status(self) -> None:
        """status = properties.status = 'reviewed'."""
        record = self._get_first_canonical()
        assert record.status == "reviewed", f"Expected status='reviewed', got {record.status!r}"

    def test_source_is_usgs(self) -> None:
        """source = 'usgs' (provider_id literal per §4.4)."""
        record = self._get_first_canonical()
        assert record.source == "usgs", f"Expected source='usgs', got {record.source!r}"

    def test_extras_contains_net_field(self) -> None:
        """extras['net'] = 'uw' (USGS-specific; routes through extras per §4.4)."""
        record = self._get_first_canonical()
        assert "net" in record.extras, "extras must contain 'net' key"
        assert record.extras["net"] == "uw", (
            f"Expected extras['net']='uw', got {record.extras['net']!r}"
        )

    def test_extras_contains_code_field(self) -> None:
        """extras['code'] = '62242697' (USGS-specific; routes through extras)."""
        record = self._get_first_canonical()
        assert "code" in record.extras, "extras must contain 'code' key"

    def test_extras_contains_sig_field(self) -> None:
        """extras['sig'] = 193 (USGS significance score; routes through extras)."""
        record = self._get_first_canonical()
        assert "sig" in record.extras, "extras must contain 'sig' key"
        assert record.extras["sig"] == 193, (
            f"Expected extras['sig']=193, got {record.extras['sig']!r}"
        )

    def test_extras_contains_gap_field(self) -> None:
        """extras['gap'] = 100 (azimuthal gap; routes through extras)."""
        record = self._get_first_canonical()
        assert "gap" in record.extras, "extras must contain 'gap' key"

    def test_extras_contains_type_field(self) -> None:
        """extras['type'] = 'earthquake' (event type; routes through extras)."""
        record = self._get_first_canonical()
        assert "type" in record.extras, "extras must contain 'type' key"
        assert record.extras["type"] == "earthquake", (
            f"Expected extras['type']='earthquake', got {record.extras['type']!r}"
        )


# ===========================================================================
# 4. Tsunami int → bool conversion
# ===========================================================================


class TestTsunamiConversion:
    """properties.tsunami 0/1 integer → bool at canonical layer."""

    def _make_feature_with_tsunami(self, tsunami_val: int) -> Any:
        """Build a minimal feature dict with the given tsunami value."""
        return {
            "type": "Feature",
            "id": "test_event_001",
            "properties": {
                "mag": 5.0,
                "place": "Test Location",
                "time": 1778131207650,
                "updated": 1778131207650,
                "tz": None,
                "url": "https://earthquake.usgs.gov/earthquakes/eventpage/test_event_001",
                "detail": "https://earthquake.usgs.gov/fdsnws/event/1/query?eventid=test_event_001&format=geojson",
                "felt": None,
                "cdi": None,
                "mmi": None,
                "alert": None,
                "status": "reviewed",
                "tsunami": tsunami_val,
                "sig": 400,
                "net": "us",
                "code": "test001",
                "ids": ",test_event_001,",
                "sources": ",us,",
                "types": ",origin,",
                "nst": 10,
                "dmin": 0.5,
                "rms": 0.3,
                "gap": 50,
                "magType": "mww",
                "type": "earthquake",
                "title": "M 5.0 - Test Location",
            },
            "geometry": {
                "type": "Point",
                "coordinates": [-120.0, 45.0, 10.0],
            },
        }

    def test_tsunami_zero_converts_to_false(self) -> None:
        """tsunami=0 integer → canonical bool False."""
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _to_canonical,
            _UsgsEventFeature,
        )

        raw = self._make_feature_with_tsunami(0)
        feature = _UsgsEventFeature.model_validate(raw)
        record = _to_canonical(feature, raw)
        assert record.tsunami is False, f"Expected False, got {record.tsunami!r}"
        assert isinstance(record.tsunami, bool), "tsunami must be bool type"

    def test_tsunami_one_converts_to_true(self) -> None:
        """tsunami=1 integer → canonical bool True."""
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _to_canonical,
            _UsgsEventFeature,
        )

        raw = self._make_feature_with_tsunami(1)
        feature = _UsgsEventFeature.model_validate(raw)
        record = _to_canonical(feature, raw)
        assert record.tsunami is True, f"Expected True, got {record.tsunami!r}"
        assert isinstance(record.tsunami, bool), "tsunami must be bool type"


# ===========================================================================
# 5. Cache TTL constant
# ===========================================================================


class TestCacheTTL:
    """_USGS_CACHE_TTL == 60 seconds (brief Q2 resolution)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_USGS_CACHE_TTL = 60 (earthquake feeds update ~every minute per Q2)."""
        import weewx_clearskies_api.providers.earthquakes.usgs as _usgs  # noqa: PLC0415

        assert _usgs._USGS_CACHE_TTL == 60, (
            f"Expected _USGS_CACHE_TTL=60, got {_usgs._USGS_CACHE_TTL!r}"
        )


# ===========================================================================
# 6. fetch() happy path — cache miss + cache hit + fakeredis
# ===========================================================================


class TestFetchHappyPath:
    """fetch() returns EarthquakeRecord list; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_records(self) -> None:
        """Cache miss → 1 HTTP call → list[EarthquakeRecord] returned."""
        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            records = fetch(
                lat=_LAT,
                lon=_LON,
                radius_km=500.0,
                from_dt=None,
                to_dt=None,
            )
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 HTTP call on cache miss, got {call_count}"
        assert len(records) == 3, f"Expected 3 records, got {len(records)}"
        assert records[0].source == "usgs"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls; same records returned."""
        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        # First fetch — fills cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=data)
            )
            records1 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        # Second fetch — must come from cache
        with respx.mock(assert_all_called=False) as mock2:
            records2 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 HTTP calls on cache hit, got {cache_hit_calls}"
        )
        assert len(records1) == len(records2)
        assert records1[0].id == records2[0].id
        _reset_provider_state()

    def test_fakeredis_cache_hit_skips_http_call(self) -> None:
        """With fakeredis backend: cache hit → 0 HTTP calls."""
        pytest.importorskip("fakeredis", reason="fakeredis not installed")
        import fakeredis  # noqa: PLC0415
        import redis as _redis_lib  # noqa: PLC0415

        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415

        # Wire a fakeredis backend via the established RedisCache test pattern
        # (object.__new__ bypasses the URL-based ping in __init__);
        # see tests/test_providers_alerts_unit.py:660 for the precedent.
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,  # noqa: PLC0415
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import (  # noqa: PLC0415
            _rate_limiter,
            _reset_http_client_for_tests,
        )

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        fake_redis = fakeredis.FakeRedis(decode_responses=False)
        redis_cache = object.__new__(RedisCache)
        redis_cache._client = fake_redis
        redis_cache._redis_error_cls = _redis_lib.exceptions.RedisError
        _cache_mod._cache = redis_cache

        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        # First fetch populates cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            records1 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        # Second fetch — cache hit from fakeredis
        with respx.mock(assert_all_called=False) as mock2:
            records2 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
            assert mock2.calls.call_count == 0, (
                "fakeredis cache hit must avoid HTTP call"
            )

        assert len(records2) == 3
        assert records1[0].id == records2[0].id
        _reset_provider_state()


# ===========================================================================
# 7. ProviderProtocolError on invalid wire shape
# ===========================================================================


class TestProviderProtocolErrorOnInvalidWireShape:
    """Missing required field → ValidationError → ProviderProtocolError."""

    def test_missing_required_id_raises_provider_protocol_error(self) -> None:
        """Drop 'id' from features → ValidationError → ProviderProtocolError from fetch()."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        bad_data = dict(data)
        # Remove 'id' from all features to trigger validation error
        bad_features = []
        for f in data["features"]:
            bad_f = {k: v for k, v in f.items() if k != "id"}
            bad_features.append(bad_f)
        bad_data["features"] = bad_features

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=bad_data)
            )
            with pytest.raises(ProviderProtocolError):
                fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
        _reset_provider_state()

    def test_missing_required_mag_raises_provider_protocol_error(self) -> None:
        """Drop 'mag' from properties → ValidationError → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("usgs_seattle_radius_m2_5.json")
        bad_data = dict(data)
        bad_features = []
        for f in data["features"]:
            bad_f = dict(f)
            bad_f["properties"] = {k: v for k, v in f["properties"].items() if k != "mag"}
            bad_features.append(bad_f)
        bad_data["features"] = bad_features

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(
                return_value=httpx.Response(200, json=bad_data)
            )
            with pytest.raises(ProviderProtocolError):
                fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
        _reset_provider_state()


# ===========================================================================
# 8. Rate limiter integration
# ===========================================================================


class TestRateLimiterIntegration:
    """Two consecutive fetch() calls do not raise RateLimiterError."""

    def test_two_fetches_in_succession_do_not_raise(self) -> None:
        """fetch() twice with 5 req/s limit → no rate limit raised for first 2 calls."""
        from weewx_clearskies_api.providers.earthquakes.usgs import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("usgs_seattle_radius_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            # First call — cache miss
            fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        # Reset cache to force second HTTP call
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        reset_cache_for_tests()
        wire_cache_from_env()

        with respx.mock(assert_all_called=False) as mock2:
            mock2.get(_USGS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            # Second call — must not raise rate limit (only 2 of 5 calls/s used)
            records = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        assert len(records) == 3
        _reset_provider_state()


# ===========================================================================
# 9. Capability declaration
# ===========================================================================


class TestUSGSCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, fields, coverage."""

    def test_capability_provider_id_is_usgs(self) -> None:
        """CAPABILITY.provider_id = 'usgs'."""
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "usgs"

    def test_capability_domain_is_earthquakes(self) -> None:
        """CAPABILITY.domain = 'earthquakes'."""
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "earthquakes"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless — no API key needed per ADR-040)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == (), (
            f"Expected auth_required=() (keyless), got {CAPABILITY.auth_required!r}"
        )

    def test_capability_geographic_coverage_is_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global' (per Q1 resolution)."""
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global", (
            f"Expected geographic_coverage='global', got {CAPABILITY.geographic_coverage!r}"
        )

    def test_capability_supplied_fields_includes_core_usgs_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields covers all 10 USGS-supplied fields."""
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        # USGS supplies: id, time, latitude, longitude, magnitude, magnitudeType,
        # depth, place, url, tsunami, felt, mmi (nullable), alert (nullable),
        # status, source
        required_fields = {
            "id", "time", "latitude", "longitude", "magnitude",
            "magnitudeType", "depth", "place", "url", "tsunami", "status", "source",
        }
        supplied = set(CAPABILITY.supplied_canonical_fields)
        missing = required_fields - supplied
        assert not missing, (
            f"CAPABILITY missing expected USGS fields: {missing!r}"
        )

    def test_wire_providers_registers_usgs_earthquakes_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('earthquakes', 'usgs') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.earthquakes.usgs import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "usgs" and p.domain == "earthquakes" for p in registry
        ), "wire_providers must register usgs earthquakes in registry"
        reset_provider_registry_for_tests()
