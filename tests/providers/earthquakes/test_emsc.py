"""Unit tests for the EMSC SeismicPortal earthquake provider module (3b-13).

Covers per the task-3b-13 brief §Test surface (test_emsc.py):

  Wire-shape Pydantic validation:
  - Global M2.5+ fixture loads cleanly via _EMSCResponse (3-feature FeatureCollection).
  - Extra wire fields ignored (extra="ignore").
  - Required fields enforced: missing mag → ValidationError → ProviderProtocolError.

  _to_canonical() mapping (per canonical-data-model §4.4 EMSC column):
  - id from top-level Feature.id (same as properties.unid).
  - time from properties.time (ISO 8601 Z → to_utc_iso8601_from_offset).
  - latitude from properties.lat (NOT geometry.coordinates[1]).
  - longitude from properties.lon (NOT geometry.coordinates[0]).
  - depth from properties.depth (POSITIVE — NOT geometry.coordinates[2] which is NEGATIVE).
  - magnitude from properties.mag.
  - magnitudeType from properties.magtype (LOWERCASE — differs from USGS/ReNaSS camelCase).
  - place from properties.flynn_region.
  - url constructed as f"https://www.seismicportal.eu/eventdetails.html?unid={unid}".
  - status = NOT in JSON flavor → None (goes to extras if needed; out of v0.1 scope).
  - tsunami = None (not provided by EMSC).
  - felt = None (not provided by EMSC).
  - mmi = None (not provided by EMSC).
  - alert = None (not provided by EMSC).
  - source = "emsc".
  - extras carries evtype, auth, source_id, source_catalog, lastupdate.

  EMSC-specific:
  - depth sign: geometry.coordinates[2] is NEGATIVE (GeoJSON Z-up); properties.depth is POSITIVE.
    Tests MUST verify properties.depth is used, not coordinates[2].
  - magtype is LOWERCASE (differs from USGS magType and ReNaSS magType camelCase).
  - status absent in JSON flavor — canonical status = None; NOT routed to extras.
  - url constructed from unid.

  Cache TTL:
  - _EMSC_CACHE_TTL == 60 (per brief Q2 resolution).

  fetch() happy path:
  - Cache miss → HTTP call → list[EarthquakeRecord] returned and cached.
  - Cache hit → 0 HTTP calls, same records returned.
  - Redis fakeredis cache hit → 0 HTTP calls.

  ProviderProtocolError on invalid wire shape:
  - Drop 'mag' from properties → ValidationError → ProviderProtocolError.

  Rate limiter:
  - Two fetch() calls in quick succession do not raise.

  Capability declaration:
  - CAPABILITY.provider_id = "emsc".
  - CAPABILITY.domain = "earthquakes".
  - CAPABILITY.auth_required = () (keyless).
  - CAPABILITY.geographic_coverage = "global, primary in eu+mediterranean".
  - wire_providers([CAPABILITY]) → registry has emsc earthquakes entry.

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/earthquakes/emsc_global_m2_5.json
ADR references: ADR-017, ADR-020, ADR-038, ADR-040.
"""

from __future__ import annotations

import json
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

# Station coordinates for Seattle (matching USGS fixture; EMSC query is global in fixture)
_LAT = 47.6
_LON = -122.3

_EMSC_BASE_URL = "https://www.seismicportal.eu"
_EMSC_QUERY_PATH = "/fdsnws/event/1/query"
_EMSC_QUERY_URL = _EMSC_BASE_URL + _EMSC_QUERY_PATH


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
    from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
        _reset_http_client_for_tests,
        _rate_limiter,
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


class TestEMSCWireShapeValidation:
    """Wire-shape models validate correctly against the fixture and edge-case shapes."""

    def test_fixture_loads_cleanly_via_response_model(self) -> None:
        """emsc_global_m2_5.json loads via _EMSCResponse without error."""
        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCResponse  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        assert response.type == "FeatureCollection"
        assert len(response.features) == 3

    def test_extra_wire_fields_are_ignored(self) -> None:
        """Extra wire fields ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCResponse  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        raw["unexpected_future_field"] = "dropped"
        raw["features"][0]["properties"]["futureField"] = "dropped"
        response = _EMSCResponse.model_validate(raw)
        assert response is not None

    def test_first_feature_id_from_top_level(self) -> None:
        """Feature[0].id = '20260511_0000281' (top-level Feature.id)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCResponse  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        assert response.features[0].id == "20260511_0000281", (
            f"Expected id='20260511_0000281', got {response.features[0].id!r}"
        )

    def test_depth_from_properties_is_positive(self) -> None:
        """properties.depth = 5.0 (POSITIVE km; geometry.coordinates[2] is -5.0 NEGATIVE)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCResponse  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        assert response.features[0].properties.depth == 5.0, (
            f"properties.depth must be 5.0 (positive), got {response.features[0].properties.depth!r}"
        )

    def test_magtype_is_lowercase(self) -> None:
        """properties.magtype = 'm' (LOWERCASE — EMSC uses lowercase, differs from USGS)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCResponse  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        assert response.features[0].properties.magtype == "m", (
            f"Expected magtype='m' (lowercase), got {response.features[0].properties.magtype!r}"
        )

    def test_missing_mag_raises_validation_error(self) -> None:
        """Dropping 'mag' from properties → ValidationError (required field)."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.earthquakes.emsc import _EMSCFeature  # noqa: PLC0415

        raw = _load_fixture("emsc_global_m2_5.json")
        feature_raw = dict(raw["features"][0])
        feature_raw["properties"] = {
            k: v for k, v in feature_raw["properties"].items() if k != "mag"
        }
        with pytest.raises(ValidationError):
            _EMSCFeature.model_validate(feature_raw)


# ===========================================================================
# 2. _to_canonical() — field mapping (including depth sign test)
# ===========================================================================


class TestEMSCToCanonical:
    """_to_canonical() maps every §4.4 EMSC field correctly."""

    def _get_first_canonical(self) -> Any:
        """Load fixture, parse, and get first canonical EarthquakeRecord."""
        from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
            _EMSCResponse,
            _to_canonical,
        )

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        feature = response.features[0]
        return _to_canonical(feature, raw["features"][0])

    def test_id_from_top_level_feature_id(self) -> None:
        """id = Feature.id = '20260511_0000281'."""
        record = self._get_first_canonical()
        assert record.id == "20260511_0000281", (
            f"Expected id='20260511_0000281', got {record.id!r}"
        )

    def test_time_is_utc_iso_z_format(self) -> None:
        """time is UTC ISO-8601 Z format from properties.time."""
        record = self._get_first_canonical()
        assert record.time.endswith("Z"), f"time must end with Z, got {record.time!r}"
        assert record.time == "2026-05-11T17:00:26Z", (
            f"Expected '2026-05-11T17:00:26Z', got {record.time!r}"
        )

    def test_latitude_from_properties_lat(self) -> None:
        """latitude = properties.lat = 1.39 (not geometry.coordinates[1])."""
        record = self._get_first_canonical()
        assert abs(record.latitude - 1.39) < 1e-4, (
            f"Expected latitude≈1.39, got {record.latitude!r}"
        )

    def test_longitude_from_properties_lon(self) -> None:
        """longitude = properties.lon = 126.66 (not geometry.coordinates[0])."""
        record = self._get_first_canonical()
        assert abs(record.longitude - 126.66) < 1e-4, (
            f"Expected longitude≈126.66, got {record.longitude!r}"
        )

    def test_depth_from_properties_depth_not_coordinates(self) -> None:
        """depth = properties.depth = 5.0 (POSITIVE; NOT geometry.coordinates[2]=-5.0)."""
        record = self._get_first_canonical()
        assert record.depth is not None
        assert record.depth == 5.0, (
            f"Expected depth=5.0 (from properties.depth, positive), got {record.depth!r}"
        )
        # Critically: must be POSITIVE, not the GeoJSON negative sign
        assert record.depth > 0, (
            f"EMSC depth must be POSITIVE (from properties.depth), got {record.depth!r}"
        )

    def test_depth_does_not_use_geometry_negative_sign(self) -> None:
        """depth != -5.0 (geometry.coordinates[2] is negative; must not use it)."""
        record = self._get_first_canonical()
        assert record.depth != -5.0, (
            "EMSC implementation must use properties.depth (+5.0), not coordinates[2] (-5.0)"
        )

    def test_magnitude_from_properties_mag(self) -> None:
        """magnitude = properties.mag = 2.5."""
        record = self._get_first_canonical()
        assert record.magnitude == 2.5, (
            f"Expected magnitude=2.5, got {record.magnitude!r}"
        )

    def test_magnitude_type_from_lowercase_magtype(self) -> None:
        """magnitudeType = properties.magtype = 'm' (EMSC lowercase)."""
        record = self._get_first_canonical()
        assert record.magnitudeType == "m", (
            f"Expected magnitudeType='m' (lowercase from EMSC), got {record.magnitudeType!r}"
        )

    def test_place_from_properties_flynn_region(self) -> None:
        """place = properties.flynn_region = 'MOLUCCA SEA'."""
        record = self._get_first_canonical()
        assert record.place == "MOLUCCA SEA", (
            f"Expected place='MOLUCCA SEA', got {record.place!r}"
        )

    def test_url_constructed_from_unid(self) -> None:
        """url constructed as seismicportal eventdetails URL from unid."""
        record = self._get_first_canonical()
        expected_url = "https://www.seismicportal.eu/eventdetails.html?unid=20260511_0000281"
        assert record.url == expected_url, (
            f"Expected url={expected_url!r}, got {record.url!r}"
        )

    def test_tsunami_is_none(self) -> None:
        """tsunami = None (EMSC JSON flavor does not provide tsunami field)."""
        record = self._get_first_canonical()
        assert record.tsunami is None, f"Expected tsunami=None, got {record.tsunami!r}"

    def test_felt_is_none(self) -> None:
        """felt = None (EMSC does not provide felt reports)."""
        record = self._get_first_canonical()
        assert record.felt is None, f"Expected felt=None, got {record.felt!r}"

    def test_mmi_is_none(self) -> None:
        """mmi = None (EMSC does not provide MMI)."""
        record = self._get_first_canonical()
        assert record.mmi is None, f"Expected mmi=None, got {record.mmi!r}"

    def test_alert_is_none(self) -> None:
        """alert = None (EMSC does not provide PAGER alert)."""
        record = self._get_first_canonical()
        assert record.alert is None, f"Expected alert=None, got {record.alert!r}"

    def test_source_is_emsc(self) -> None:
        """source = 'emsc' (provider_id literal per §4.4)."""
        record = self._get_first_canonical()
        assert record.source == "emsc", f"Expected source='emsc', got {record.source!r}"

    def test_extras_contains_evtype(self) -> None:
        """extras['evtype'] = 'ke' (EMSC event type; routes through extras per §4.4)."""
        record = self._get_first_canonical()
        assert "evtype" in record.extras, "extras must contain 'evtype' key"
        assert record.extras["evtype"] == "ke", (
            f"Expected extras['evtype']='ke', got {record.extras['evtype']!r}"
        )

    def test_extras_contains_auth(self) -> None:
        """extras['auth'] = 'BMKG' (publishing agency; routes through extras)."""
        record = self._get_first_canonical()
        assert "auth" in record.extras, "extras must contain 'auth' key"
        assert record.extras["auth"] == "BMKG", (
            f"Expected extras['auth']='BMKG', got {record.extras['auth']!r}"
        )

    def test_extras_contains_source_id(self) -> None:
        """extras['source_id'] routes through extras per §4.4."""
        record = self._get_first_canonical()
        assert "source_id" in record.extras, "extras must contain 'source_id' key"

    def test_extras_contains_source_catalog(self) -> None:
        """extras['source_catalog'] = 'EMSC-RTS' routes through extras."""
        record = self._get_first_canonical()
        assert "source_catalog" in record.extras, "extras must contain 'source_catalog' key"
        assert record.extras["source_catalog"] == "EMSC-RTS", (
            f"Expected extras['source_catalog']='EMSC-RTS', got {record.extras['source_catalog']!r}"
        )

    def test_extras_contains_lastupdate(self) -> None:
        """extras['lastupdate'] routes through extras per §4.4."""
        record = self._get_first_canonical()
        assert "lastupdate" in record.extras, "extras must contain 'lastupdate' key"

    def test_second_feature_depth_is_positive(self) -> None:
        """Feature[1] depth = 37.8 (positive); geometry.coordinates[2] = -37.8 (negative)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
            _EMSCResponse,
            _to_canonical,
        )

        raw = _load_fixture("emsc_global_m2_5.json")
        response = _EMSCResponse.model_validate(raw)
        feature = response.features[1]
        record = _to_canonical(feature, raw["features"][1])
        assert record.depth == 37.8, (
            f"Expected depth=37.8 (from properties.depth, positive), got {record.depth!r}"
        )
        assert record.depth > 0, "depth must be positive (properties.depth used, not coordinates)"


# ===========================================================================
# 3. Cache TTL constant
# ===========================================================================


class TestCacheTTL:
    """_EMSC_CACHE_TTL == 60 seconds (brief Q2 resolution)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_EMSC_CACHE_TTL = 60."""
        import weewx_clearskies_api.providers.earthquakes.emsc as _emsc  # noqa: PLC0415

        assert _emsc._EMSC_CACHE_TTL == 60, (
            f"Expected _EMSC_CACHE_TTL=60, got {_emsc._EMSC_CACHE_TTL!r}"
        )


# ===========================================================================
# 4. fetch() happy path — cache miss + cache hit + fakeredis
# ===========================================================================


class TestFetchHappyPath:
    """fetch() returns EarthquakeRecord list; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_records(self) -> None:
        """Cache miss → 1 HTTP call → list[EarthquakeRecord] returned."""
        from weewx_clearskies_api.providers.earthquakes.emsc import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(
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
        assert records[0].source == "emsc"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls; same records returned."""
        from weewx_clearskies_api.providers.earthquakes.emsc import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            records1 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        with respx.mock(assert_all_called=False) as mock2:
            records2 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
            assert mock2.calls.call_count == 0, "Cache hit must skip HTTP call"

        assert len(records1) == len(records2)
        assert records1[0].id == records2[0].id
        _reset_provider_state()

    def test_fakeredis_cache_hit_skips_http_call(self) -> None:
        """With fakeredis backend: cache hit → 0 HTTP calls."""
        pytest.importorskip("fakeredis", reason="fakeredis not installed")
        import fakeredis  # noqa: PLC0415

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            _RedisCache,
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.emsc import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
        )
        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        fake_redis = fakeredis.FakeRedis()
        _cache_mod._cache_instance = _RedisCache(fake_redis)

        from weewx_clearskies_api.providers.earthquakes.emsc import fetch  # noqa: PLC0415

        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            records1 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        with respx.mock(assert_all_called=False) as mock2:
            records2 = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
            assert mock2.calls.call_count == 0, "fakeredis cache hit must skip HTTP"

        assert len(records2) == 3
        assert records1[0].id == records2[0].id
        _reset_provider_state()


# ===========================================================================
# 5. ProviderProtocolError on invalid wire shape
# ===========================================================================


class TestProviderProtocolErrorOnInvalidWireShape:
    """Missing required mag → ValidationError → ProviderProtocolError."""

    def test_missing_mag_raises_provider_protocol_error(self) -> None:
        """Drop 'mag' from properties → ValidationError → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.earthquakes.emsc import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("emsc_global_m2_5.json")
        bad_data = dict(data)
        bad_features = []
        for f in data["features"]:
            bad_f = dict(f)
            bad_f["properties"] = {k: v for k, v in f["properties"].items() if k != "mag"}
            bad_features.append(bad_f)
        bad_data["features"] = bad_features

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(
                return_value=httpx.Response(200, json=bad_data)
            )
            with pytest.raises(ProviderProtocolError):
                fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)
        _reset_provider_state()


# ===========================================================================
# 6. Rate limiter integration
# ===========================================================================


class TestRateLimiterIntegration:
    """Two consecutive fetch() calls do not raise RateLimiterError."""

    def test_two_fetches_in_succession_do_not_raise(self) -> None:
        """fetch() twice → no rate limit raised for first 2 calls."""
        from weewx_clearskies_api.providers.earthquakes.emsc import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("emsc_global_m2_5.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        reset_cache_for_tests()
        wire_cache_from_env()

        with respx.mock(assert_all_called=False) as mock2:
            mock2.get(_EMSC_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            records = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        assert len(records) == 3
        _reset_provider_state()


# ===========================================================================
# 7. Capability declaration
# ===========================================================================


class TestEMSCCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, fields, coverage."""

    def test_capability_provider_id_is_emsc(self) -> None:
        """CAPABILITY.provider_id = 'emsc'."""
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "emsc"

    def test_capability_domain_is_earthquakes(self) -> None:
        """CAPABILITY.domain = 'earthquakes'."""
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "earthquakes"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless per ADR-040)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_geographic_coverage_is_eu_plus_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global, primary in eu+mediterranean' (Q1)."""
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global, primary in eu+mediterranean", (
            f"Expected 'global, primary in eu+mediterranean', got {CAPABILITY.geographic_coverage!r}"
        )

    def test_capability_supplied_fields_includes_core_emsc_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields covers EMSC-supplied fields."""
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        # EMSC supplies: id, time, latitude, longitude, magnitude, magnitudeType,
        # depth, place, url, source
        required_fields = {
            "id", "time", "latitude", "longitude", "magnitude",
            "magnitudeType", "depth", "place", "url", "source",
        }
        supplied = set(CAPABILITY.supplied_canonical_fields)
        missing = required_fields - supplied
        assert not missing, (
            f"CAPABILITY missing expected EMSC fields: {missing!r}"
        )

    def test_wire_providers_registers_emsc_earthquakes_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('earthquakes', 'emsc') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.earthquakes.emsc import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "emsc" and p.domain == "earthquakes" for p in registry
        ), "wire_providers must register emsc earthquakes in registry"
        reset_provider_registry_for_tests()
