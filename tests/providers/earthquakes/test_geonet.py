"""Unit tests for the GeoNet (NZ) earthquake provider module (3b-13).

Covers per the task-3b-13 brief §Test surface (test_geonet.py):

  Wire-shape Pydantic validation:
  - NZ MMI=3 fixture loads cleanly via _GeoNetResponse (3-feature FeatureCollection).
  - Extra wire fields ignored (extra="ignore").
  - Required fields enforced: missing publicID → ValidationError → ProviderProtocolError.

  _to_canonical() mapping (per canonical-data-model §4.4 GeoNet column):
  - id from properties.publicID (NO top-level Feature.id in GeoNet response).
  - time from properties.time (ISO 8601 Z string → to_utc_iso8601_from_offset).
  - latitude from geometry.coordinates[1].
  - longitude from geometry.coordinates[0].
  - depth from properties.depth (NOT geometry.coordinates — only 2 coords in GeoNet).
  - magnitude from properties.magnitude.
  - magnitudeType = None (GeoNet does not expose magnitudeType field per §4.4).
  - place from properties.locality.
  - url constructed as f"https://www.geonet.org.nz/earthquake/{publicID}".
  - mmi from properties.mmi (LOWERCASE — not MMI like query param).
  - tsunami = None (not provided by GeoNet).
  - felt = None (not provided by GeoNet).
  - alert = None (not provided by GeoNet).
  - status from properties.quality.
  - source = "geonet".
  - extras carries quality and any non-canonical properties.

  GeoNet-specific:
  - mmi field is LOWERCASE in response (confirmed live 2026-05-11; §4.4 amended).
  - url is constructed (not in response) from publicID.
  - magnitudeType is always None (not exposed).
  - geometry.coordinates has 2 elements only (no depth Z component).
  - quality maps to status.

  Cache TTL:
  - _GEONET_CACHE_TTL == 60 (per brief Q2 resolution).

  fetch() happy path:
  - Cache miss → HTTP call → list[EarthquakeRecord] returned and cached.
  - Cache hit → 0 HTTP calls, same records returned.
  - Redis fakeredis cache hit → 0 HTTP calls.

  ProviderProtocolError on invalid wire shape:
  - Drop 'publicID' from properties → ValidationError → ProviderProtocolError.

  Rate limiter:
  - Two fetch() calls in quick succession do not raise.

  Capability declaration:
  - CAPABILITY.provider_id = "geonet".
  - CAPABILITY.domain = "earthquakes".
  - CAPABILITY.auth_required = () (keyless).
  - CAPABILITY.geographic_coverage = "nz".
  - wire_providers([CAPABILITY]) → registry has geonet earthquakes entry.

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/earthquakes/geonet_nz_mmi3.json
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

_LAT = -41.3  # Wellington, NZ
_LON = 174.8

_GEONET_BASE_URL = "https://api.geonet.org.nz"
_GEONET_QUAKE_PATH = "/quake"
_GEONET_QUAKE_URL = _GEONET_BASE_URL + _GEONET_QUAKE_PATH


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
    from weewx_clearskies_api.providers.earthquakes.geonet import (  # noqa: PLC0415
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


class TestGeoNetWireShapeValidation:
    """Wire-shape models validate correctly against the fixture and edge-case shapes."""

    def test_fixture_loads_cleanly_via_response_model(self) -> None:
        """geonet_nz_mmi3.json loads via _GeoNetResponse without error."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        assert response.type == "FeatureCollection"
        assert len(response.features) == 3

    def test_fixture_has_three_features(self) -> None:
        """Fixture has exactly 3 features (sliced from live capture)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        assert len(response.features) == 3

    def test_extra_wire_fields_are_ignored(self) -> None:
        """Extra wire fields ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        raw["unexpected_future_field"] = "dropped"
        raw["features"][0]["properties"]["newField"] = "also_dropped"
        response = _GeoNetResponse.model_validate(raw)
        assert response is not None

    def test_public_id_parsed_correctly(self) -> None:
        """properties.publicID = '2026p353000' (canonical id source for GeoNet)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        assert response.features[0].properties.publicID == "2026p353000", (
            f"Expected publicID='2026p353000', got {response.features[0].properties.publicID!r}"
        )

    def test_mmi_is_lowercase_integer(self) -> None:
        """properties.mmi = 3 (LOWERCASE field name; confirmed live 2026-05-11)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        assert response.features[0].properties.mmi == 3, (
            f"Expected mmi=3 (lowercase field), got {response.features[0].properties.mmi!r}"
        )

    def test_geometry_has_two_coordinates_only(self) -> None:
        """GeoNet geometry.coordinates has 2 elements [lon, lat] — NO depth Z component."""
        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetResponse  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        coords = response.features[0].geometry.coordinates
        assert len(coords) == 2, (
            f"GeoNet coordinates must have 2 elements [lon,lat], got {len(coords)}: {coords!r}"
        )

    def test_missing_public_id_raises_validation_error(self) -> None:
        """Dropping 'publicID' from properties → ValidationError (required field)."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.earthquakes.geonet import _GeoNetEventFeature  # noqa: PLC0415

        raw = _load_fixture("geonet_nz_mmi3.json")
        feature_raw = dict(raw["features"][0])
        feature_raw["properties"] = {
            k: v for k, v in feature_raw["properties"].items() if k != "publicID"
        }
        with pytest.raises(ValidationError):
            _GeoNetEventFeature.model_validate(feature_raw)


# ===========================================================================
# 2. _to_canonical() — field mapping
# ===========================================================================


class TestGeoNetToCanonical:
    """_to_canonical() maps every §4.4 GeoNet field correctly."""

    def _get_first_canonical(self) -> Any:
        """Load fixture, parse, and get first canonical EarthquakeRecord."""
        from weewx_clearskies_api.providers.earthquakes.geonet import (  # noqa: PLC0415
            _GeoNetResponse,
            _to_canonical,
        )

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        feature = response.features[0]
        return _to_canonical(feature, raw["features"][0])

    def test_id_from_public_id(self) -> None:
        """id = properties.publicID = '2026p353000' (no top-level Feature.id)."""
        record = self._get_first_canonical()
        assert record.id == "2026p353000", f"Expected id='2026p353000', got {record.id!r}"

    def test_time_is_utc_iso_z_format(self) -> None:
        """time is UTC ISO-8601 Z format from properties.time ISO string."""
        record = self._get_first_canonical()
        assert record.time.endswith("Z"), f"time must end with Z, got {record.time!r}"
        assert record.time == "2026-05-11T14:38:39Z", (
            f"Expected '2026-05-11T14:38:39Z', got {record.time!r}"
        )

    def test_latitude_from_coordinates_1(self) -> None:
        """latitude = geometry.coordinates[1] = -38.977..."""
        record = self._get_first_canonical()
        assert abs(record.latitude - (-38.977085114)) < 1e-6, (
            f"Expected latitude≈-38.977, got {record.latitude!r}"
        )

    def test_longitude_from_coordinates_0(self) -> None:
        """longitude = geometry.coordinates[0] = 175.256..."""
        record = self._get_first_canonical()
        assert abs(record.longitude - 175.256698608) < 1e-6, (
            f"Expected longitude≈175.257, got {record.longitude!r}"
        )

    def test_depth_from_properties_depth(self) -> None:
        """depth = properties.depth = 20.33... (positive km; NOT from geometry)."""
        record = self._get_first_canonical()
        assert record.depth is not None
        assert abs(record.depth - 20.33428955078125) < 1e-4, (
            f"Expected depth≈20.33, got {record.depth!r}"
        )

    def test_magnitude_from_properties_magnitude(self) -> None:
        """magnitude = properties.magnitude = 2.4993..."""
        record = self._get_first_canonical()
        assert abs(record.magnitude - 2.4993398757078116) < 1e-6, (
            f"Expected magnitude≈2.499, got {record.magnitude!r}"
        )

    def test_magnitude_type_is_none(self) -> None:
        """magnitudeType = None (GeoNet does not expose magnitudeType per §4.4)."""
        record = self._get_first_canonical()
        assert record.magnitudeType is None, (
            f"Expected magnitudeType=None (not provided by GeoNet), got {record.magnitudeType!r}"
        )

    def test_place_from_properties_locality(self) -> None:
        """place = properties.locality = '10 km south of Taumarunui'."""
        record = self._get_first_canonical()
        assert record.place == "10 km south of Taumarunui", (
            f"Expected place='10 km south of Taumarunui', got {record.place!r}"
        )

    def test_url_constructed_from_public_id(self) -> None:
        """url = 'https://www.geonet.org.nz/earthquake/{publicID}' (constructed, not in response)."""
        record = self._get_first_canonical()
        expected_url = "https://www.geonet.org.nz/earthquake/2026p353000"
        assert record.url == expected_url, (
            f"Expected url={expected_url!r}, got {record.url!r}"
        )

    def test_mmi_from_lowercase_mmi_field(self) -> None:
        """mmi = properties.mmi = 3 (lowercase field name; verified live 2026-05-11)."""
        record = self._get_first_canonical()
        assert record.mmi == 3.0, f"Expected mmi=3, got {record.mmi!r}"

    def test_tsunami_is_none(self) -> None:
        """tsunami = None (GeoNet does not provide tsunami field per §4.4)."""
        record = self._get_first_canonical()
        assert record.tsunami is None, f"Expected tsunami=None, got {record.tsunami!r}"

    def test_felt_is_none(self) -> None:
        """felt = None (GeoNet does not provide felt reports per §4.4)."""
        record = self._get_first_canonical()
        assert record.felt is None, f"Expected felt=None, got {record.felt!r}"

    def test_alert_is_none(self) -> None:
        """alert = None (GeoNet does not provide PAGER alert per §4.4)."""
        record = self._get_first_canonical()
        assert record.alert is None, f"Expected alert=None, got {record.alert!r}"

    def test_status_from_properties_quality(self) -> None:
        """status = properties.quality = 'best' (quality maps to canonical status)."""
        record = self._get_first_canonical()
        assert record.status == "best", f"Expected status='best', got {record.status!r}"

    def test_source_is_geonet(self) -> None:
        """source = 'geonet' (provider_id literal per §4.4)."""
        record = self._get_first_canonical()
        assert record.source == "geonet", f"Expected source='geonet', got {record.source!r}"

    def test_deleted_quality_maps_to_deleted_status(self) -> None:
        """Feature[2] quality='deleted' → status='deleted'."""
        from weewx_clearskies_api.providers.earthquakes.geonet import (  # noqa: PLC0415
            _GeoNetResponse,
            _to_canonical,
        )

        raw = _load_fixture("geonet_nz_mmi3.json")
        response = _GeoNetResponse.model_validate(raw)
        feature = response.features[2]  # quality="deleted"
        record = _to_canonical(feature, raw["features"][2])
        assert record.status == "deleted", (
            f"Expected status='deleted' for quality='deleted', got {record.status!r}"
        )


# ===========================================================================
# 3. Cache TTL constant
# ===========================================================================


class TestCacheTTL:
    """_GEONET_CACHE_TTL == 60 seconds (brief Q2 resolution)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_GEONET_CACHE_TTL = 60."""
        import weewx_clearskies_api.providers.earthquakes.geonet as _geonet  # noqa: PLC0415

        assert _geonet._GEONET_CACHE_TTL == 60, (
            f"Expected _GEONET_CACHE_TTL=60, got {_geonet._GEONET_CACHE_TTL!r}"
        )


# ===========================================================================
# 4. fetch() happy path — cache miss + cache hit + fakeredis
# ===========================================================================


class TestFetchHappyPath:
    """fetch() returns EarthquakeRecord list; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_records(self) -> None:
        """Cache miss → 1 HTTP call → list[EarthquakeRecord] returned."""
        from weewx_clearskies_api.providers.earthquakes.geonet import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("geonet_nz_mmi3.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_GEONET_QUAKE_URL).mock(
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
        assert records[0].source == "geonet"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls; same records returned."""
        from weewx_clearskies_api.providers.earthquakes.geonet import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("geonet_nz_mmi3.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_GEONET_QUAKE_URL).mock(return_value=httpx.Response(200, json=data))
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
            RedisCache,
            reset_cache_for_tests,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.earthquakes.geonet import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
        )
        import redis as _redis_lib  # noqa: PLC0415
        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        # Inject fakeredis via the established RedisCache test pattern
        # (object.__new__ bypasses the URL-based ping in __init__);
        # see tests/test_providers_alerts_unit.py:660 for the precedent.
        fake_redis = fakeredis.FakeRedis(decode_responses=False)
        redis_cache = object.__new__(RedisCache)
        redis_cache._client = fake_redis
        redis_cache._redis_error_cls = _redis_lib.exceptions.RedisError
        _cache_mod._cache = redis_cache

        from weewx_clearskies_api.providers.earthquakes.geonet import fetch  # noqa: PLC0415

        data = _load_fixture("geonet_nz_mmi3.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_GEONET_QUAKE_URL).mock(return_value=httpx.Response(200, json=data))
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
    """Missing required publicID → ValidationError → ProviderProtocolError."""

    def test_missing_public_id_raises_provider_protocol_error(self) -> None:
        """Drop 'publicID' from properties → ValidationError → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.earthquakes.geonet import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("geonet_nz_mmi3.json")
        bad_data = dict(data)
        bad_features = []
        for f in data["features"]:
            bad_f = dict(f)
            bad_f["properties"] = {
                k: v for k, v in f["properties"].items() if k != "publicID"
            }
            bad_features.append(bad_f)
        bad_data["features"] = bad_features

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_GEONET_QUAKE_URL).mock(
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
        """fetch() twice with 5 req/s limit → no rate limit raised for first 2 calls."""
        from weewx_clearskies_api.providers.earthquakes.geonet import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("geonet_nz_mmi3.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_GEONET_QUAKE_URL).mock(return_value=httpx.Response(200, json=data))
            fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        reset_cache_for_tests()
        wire_cache_from_env()

        with respx.mock(assert_all_called=False) as mock2:
            mock2.get(_GEONET_QUAKE_URL).mock(return_value=httpx.Response(200, json=data))
            records = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        assert len(records) == 3
        _reset_provider_state()


# ===========================================================================
# 7. Capability declaration
# ===========================================================================


class TestGeoNetCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, fields, coverage."""

    def test_capability_provider_id_is_geonet(self) -> None:
        """CAPABILITY.provider_id = 'geonet'."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "geonet"

    def test_capability_domain_is_earthquakes(self) -> None:
        """CAPABILITY.domain = 'earthquakes'."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "earthquakes"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless per ADR-040)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_geographic_coverage_is_nz(self) -> None:
        """CAPABILITY.geographic_coverage = 'nz' (per Q1 resolution)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "nz", (
            f"Expected geographic_coverage='nz', got {CAPABILITY.geographic_coverage!r}"
        )

    def test_capability_supplied_fields_includes_core_geonet_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields covers GeoNet-supplied fields."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        # GeoNet supplies: id, time, latitude, longitude, magnitude, depth,
        # place, url, mmi, status, source
        required_fields = {
            "id", "time", "latitude", "longitude", "magnitude",
            "depth", "place", "url", "mmi", "status", "source",
        }
        supplied = set(CAPABILITY.supplied_canonical_fields)
        missing = required_fields - supplied
        assert not missing, (
            f"CAPABILITY missing expected GeoNet fields: {missing!r}"
        )

    def test_capability_does_not_include_tsunami(self) -> None:
        """CAPABILITY does NOT include 'tsunami' (GeoNet doesn't provide it)."""
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        assert "tsunami" not in CAPABILITY.supplied_canonical_fields, (
            "GeoNet CAPABILITY must not include 'tsunami' (not provided)"
        )

    def test_wire_providers_registers_geonet_earthquakes_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('earthquakes', 'geonet') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.earthquakes.geonet import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "geonet" and p.domain == "earthquakes" for p in registry
        ), "wire_providers must register geonet earthquakes in registry"
        reset_provider_registry_for_tests()
