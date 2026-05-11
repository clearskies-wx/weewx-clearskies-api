"""Unit tests for the ReNaSS (France) earthquake provider module (3b-13).

Covers per the task-3b-13 brief §Test surface (test_renass.py):

  Wire-shape Pydantic validation:
  - France recent fixture loads cleanly via _RenassResponse (3-feature FeatureCollection).
  - Extra wire fields ignored (extra="ignore").
  - Bilingual description and url fields parse as dict[str, str].
  - Required fields enforced: missing id → ValidationError → ProviderProtocolError.

  _to_canonical() mapping (per canonical-data-model §4.4 ReNaSS column):
  - id from top-level Feature.id.
  - time from properties.time (ISO 8601 Z → to_utc_iso8601_from_offset).
  - latitude from geometry.coordinates[1].
  - longitude from geometry.coordinates[0].
  - depth from properties.depth (POSITIVE — NOT geometry.coordinates[2] which is NEGATIVE).
  - magnitude from properties.mag.
  - magnitudeType from properties.magType (camelCase — differs from EMSC lowercase magtype).
  - place = properties.description.en (bilingual .en taken).
  - url = properties.url.en (bilingual .en taken).
  - status derived from properties.automatic (true → "automatic", false → "reviewed").
  - tsunami = None (not provided).
  - felt = None (not provided).
  - mmi = None (not provided).
  - alert = None (not provided).
  - source = "renass".
  - extras: type, description.fr → extras["description_fr"], url.fr → extras["url_fr"].

  ReNaSS-specific:
  - description and url are bilingual objects {fr, en}; canonical takes .en.
  - .fr halves route to extras["description_fr"] and extras["url_fr"] (flat string keys).
  - automatic boolean → status: true → "automatic", false → "reviewed".
  - geometry.coordinates[2] is NEGATIVE (GeoJSON Z-up); properties.depth is POSITIVE.
    Tests MUST verify properties.depth is used.
  - magType is camelCase (MLv, ML) — differs from EMSC lowercase magtype.

  Cache TTL:
  - _RENASS_CACHE_TTL == 60 (per brief Q2 resolution).

  fetch() happy path:
  - Cache miss → HTTP call → list[EarthquakeRecord] returned and cached.
  - Cache hit → 0 HTTP calls, same records returned.
  - Redis fakeredis cache hit → 0 HTTP calls.

  ProviderProtocolError on invalid wire shape:
  - Drop 'id' from features → ValidationError → ProviderProtocolError.

  Rate limiter:
  - Two fetch() calls in quick succession do not raise.

  Capability declaration:
  - CAPABILITY.provider_id = "renass".
  - CAPABILITY.domain = "earthquakes".
  - CAPABILITY.auth_required = () (keyless).
  - CAPABILITY.geographic_coverage = "fr".
  - wire_providers([CAPABILITY]) → registry has renass earthquakes entry.

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/earthquakes/renass_france_recent.json
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

# Station coordinates — Strasbourg area (matching fixture query)
_LAT = 48.5
_LON = 7.7

_RENASS_BASE_URL = "https://api.franceseisme.fr"
_RENASS_QUERY_PATH = "/fdsnws/event/1/query"
_RENASS_QUERY_URL = _RENASS_BASE_URL + _RENASS_QUERY_PATH


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
    from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
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


class TestReNaSSWireShapeValidation:
    """Wire-shape models validate correctly against the fixture and edge-case shapes."""

    def test_fixture_loads_cleanly_via_response_model(self) -> None:
        """renass_france_recent.json loads via _RenassResponse without error."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        assert response.type == "FeatureCollection"
        assert len(response.features) == 3

    def test_extra_wire_fields_are_ignored(self) -> None:
        """Extra wire fields ignored (extra='ignore')."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        raw["unexpected_future_field"] = "dropped"
        response = _RenassResponse.model_validate(raw)
        assert response is not None

    def test_first_feature_id_is_fr2026trycyd(self) -> None:
        """Feature[0].id = 'fr2026trycyd' (top-level Feature.id)."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        assert response.features[0].id == "fr2026trycyd", (
            f"Expected id='fr2026trycyd', got {response.features[0].id!r}"
        )

    def test_description_is_bilingual_dict(self) -> None:
        """properties.description is dict with 'fr' and 'en' keys."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        desc = response.features[0].properties.description
        assert desc is not None
        assert isinstance(desc, dict), f"description must be dict, got {type(desc).__name__!r}"
        assert "en" in desc, f"description must have 'en' key, got keys: {list(desc.keys())!r}"
        assert "fr" in desc, f"description must have 'fr' key, got keys: {list(desc.keys())!r}"

    def test_url_is_bilingual_dict(self) -> None:
        """properties.url is dict with 'fr' and 'en' keys."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        url = response.features[0].properties.url
        assert url is not None
        assert isinstance(url, dict), f"url must be dict, got {type(url).__name__!r}"
        assert "en" in url, f"url must have 'en' key, got keys: {list(url.keys())!r}"
        assert "fr" in url, f"url must have 'fr' key, got keys: {list(url.keys())!r}"

    def test_automatic_is_boolean(self) -> None:
        """properties.automatic is Python bool (True/False, not int 0/1)."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        auto = response.features[0].properties.automatic
        assert isinstance(auto, bool), f"automatic must be bool, got {type(auto).__name__!r}"

    def test_depth_from_properties_is_positive(self) -> None:
        """properties.depth = 14.126... (POSITIVE; geometry.coordinates[2] is -14.126 NEGATIVE)."""
        from weewx_clearskies_api.providers.earthquakes.renass import _RenassResponse  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        assert response.features[0].properties.depth > 0, (
            f"properties.depth must be positive, got {response.features[0].properties.depth!r}"
        )

    def test_missing_id_raises_validation_error(self) -> None:
        """Dropping 'id' from Feature → ValidationError (required field)."""
        from pydantic import ValidationError  # noqa: PLC0415

        from weewx_clearskies_api.providers.earthquakes.renass import _RenassEventFeature  # noqa: PLC0415

        raw = _load_fixture("renass_france_recent.json")
        feature_raw = {k: v for k, v in raw["features"][0].items() if k != "id"}
        with pytest.raises(ValidationError):
            _RenassEventFeature.model_validate(feature_raw)


# ===========================================================================
# 2. _to_canonical() — field mapping (bilingual + status derivation)
# ===========================================================================


class TestReNaSSToCanonical:
    """_to_canonical() maps every §4.4 ReNaSS field correctly."""

    def _get_canonical_at(self, index: int = 0) -> Any:
        """Load fixture, parse, and get canonical EarthquakeRecord at index."""
        from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
            _RenassResponse,
            _to_canonical,
        )

        raw = _load_fixture("renass_france_recent.json")
        response = _RenassResponse.model_validate(raw)
        feature = response.features[index]
        return _to_canonical(feature, raw["features"][index])

    def test_id_from_top_level_feature_id(self) -> None:
        """id = Feature.id = 'fr2026trycyd'."""
        record = self._get_canonical_at(0)
        assert record.id == "fr2026trycyd", f"Expected id='fr2026trycyd', got {record.id!r}"

    def test_time_is_utc_iso_z_format(self) -> None:
        """time is UTC ISO-8601 Z format from properties.time."""
        record = self._get_canonical_at(0)
        assert record.time.endswith("Z"), f"time must end with Z, got {record.time!r}"
        assert record.time == "2026-05-11T16:36:59Z", (
            f"Expected '2026-05-11T16:36:59Z', got {record.time!r}"
        )

    def test_latitude_from_coordinates_1(self) -> None:
        """latitude = geometry.coordinates[1] = 43.006..."""
        record = self._get_canonical_at(0)
        assert abs(record.latitude - 43.00603485) < 1e-6, (
            f"Expected latitude≈43.006, got {record.latitude!r}"
        )

    def test_longitude_from_coordinates_0(self) -> None:
        """longitude = geometry.coordinates[0] = 0.269..."""
        record = self._get_canonical_at(0)
        assert abs(record.longitude - 0.2690240741) < 1e-6, (
            f"Expected longitude≈0.269, got {record.longitude!r}"
        )

    def test_depth_from_properties_depth_not_coordinates(self) -> None:
        """depth = properties.depth = 14.126... (POSITIVE; NOT geometry.coordinates[2]=-14.126)."""
        record = self._get_canonical_at(0)
        assert record.depth is not None
        assert abs(record.depth - 14.12690163) < 1e-4, (
            f"Expected depth≈14.127, got {record.depth!r}"
        )
        assert record.depth > 0, f"depth must be POSITIVE (from properties.depth), got {record.depth!r}"

    def test_depth_does_not_use_geometry_negative_sign(self) -> None:
        """depth != -14.126... (geometry.coordinates[2] is negative; must not use it)."""
        record = self._get_canonical_at(0)
        assert record.depth is not None
        assert record.depth > 0, (
            "ReNaSS impl must use properties.depth (positive), not coordinates[2] (negative)"
        )

    def test_magnitude_from_properties_mag(self) -> None:
        """magnitude = properties.mag = 1.7169..."""
        record = self._get_canonical_at(0)
        assert abs(record.magnitude - 1.716991822) < 1e-4, (
            f"Expected magnitude≈1.717, got {record.magnitude!r}"
        )

    def test_magnitude_type_camelcase_from_properties_mag_type(self) -> None:
        """magnitudeType = properties.magType = 'MLv' (camelCase; differs from EMSC lowercase)."""
        record = self._get_canonical_at(0)
        assert record.magnitudeType == "MLv", (
            f"Expected magnitudeType='MLv' (camelCase from ReNaSS), got {record.magnitudeType!r}"
        )

    def test_place_from_description_en(self) -> None:
        """place = properties.description.en = 'Event of magnitude 1.7, near Pau'."""
        record = self._get_canonical_at(0)
        assert record.place == "Event of magnitude 1.7, near Pau", (
            f"Expected place from description.en, got {record.place!r}"
        )

    def test_url_from_url_en(self) -> None:
        """url = properties.url.en (bilingual; .en taken per §4.4)."""
        record = self._get_canonical_at(0)
        expected_url = "https://renass.unistra.fr/en/events/fr2026trycyd"
        assert record.url == expected_url, (
            f"Expected url={expected_url!r}, got {record.url!r}"
        )

    def test_automatic_true_maps_to_status_automatic(self) -> None:
        """properties.automatic=True → status='automatic'."""
        record = self._get_canonical_at(0)  # Feature[0] has automatic=True
        assert record.status == "automatic", (
            f"Expected status='automatic' (automatic=True), got {record.status!r}"
        )

    def test_automatic_false_maps_to_status_reviewed(self) -> None:
        """properties.automatic=False → status='reviewed'."""
        record = self._get_canonical_at(2)  # Feature[2] has automatic=False
        assert record.status == "reviewed", (
            f"Expected status='reviewed' (automatic=False), got {record.status!r}"
        )

    def test_tsunami_is_none(self) -> None:
        """tsunami = None (ReNaSS does not provide tsunami flag per §4.4)."""
        record = self._get_canonical_at(0)
        assert record.tsunami is None, f"Expected tsunami=None, got {record.tsunami!r}"

    def test_felt_is_none(self) -> None:
        """felt = None (ReNaSS does not provide felt reports)."""
        record = self._get_canonical_at(0)
        assert record.felt is None, f"Expected felt=None, got {record.felt!r}"

    def test_mmi_is_none(self) -> None:
        """mmi = None (ReNaSS does not provide MMI)."""
        record = self._get_canonical_at(0)
        assert record.mmi is None, f"Expected mmi=None, got {record.mmi!r}"

    def test_alert_is_none(self) -> None:
        """alert = None (ReNaSS does not provide PAGER alert)."""
        record = self._get_canonical_at(0)
        assert record.alert is None, f"Expected alert=None, got {record.alert!r}"

    def test_source_is_renass(self) -> None:
        """source = 'renass' (provider_id literal per §4.4)."""
        record = self._get_canonical_at(0)
        assert record.source == "renass", f"Expected source='renass', got {record.source!r}"

    def test_extras_description_fr_is_french_text(self) -> None:
        """extras['description_fr'] = French description text (flat string key per §4.4)."""
        record = self._get_canonical_at(0)
        assert "description_fr" in record.extras, (
            "extras must contain 'description_fr' key (bilingual .fr half)"
        )
        desc_fr = record.extras["description_fr"]
        assert isinstance(desc_fr, str), f"description_fr must be string, got {type(desc_fr).__name__!r}"
        assert "Pau" in desc_fr, (
            f"description_fr must contain 'Pau', got {desc_fr!r}"
        )

    def test_extras_url_fr_is_french_url(self) -> None:
        """extras['url_fr'] = French URL (flat string key per §4.4)."""
        record = self._get_canonical_at(0)
        assert "url_fr" in record.extras, (
            "extras must contain 'url_fr' key (bilingual .fr half)"
        )
        url_fr = record.extras["url_fr"]
        expected_fr_url = "https://renass.unistra.fr/fr/evenements/fr2026trycyd"
        assert url_fr == expected_fr_url, (
            f"Expected url_fr={expected_fr_url!r}, got {url_fr!r}"
        )

    def test_extras_type_is_none_for_earthquake_events(self) -> None:
        """extras['type'] = None when properties.type is null (standard earthquake)."""
        record = self._get_canonical_at(0)
        assert "type" in record.extras, "extras must contain 'type' key from properties.type"
        assert record.extras["type"] is None, (
            f"Expected extras['type']=None for null type, got {record.extras['type']!r}"
        )

    def test_extras_type_is_quarry_blast_for_feature_2(self) -> None:
        """extras['type'] = 'quarry blast' for Feature[2] (passthrough per brief)."""
        record = self._get_canonical_at(2)
        assert record.extras.get("type") == "quarry blast", (
            f"Expected extras['type']='quarry blast', got {record.extras.get('type')!r}"
        )

    def test_surface_event_depth_is_zero(self) -> None:
        """Feature[2] depth = 0.0 (surface quarry blast; must not become negative)."""
        record = self._get_canonical_at(2)
        assert record.depth == 0.0, (
            f"Expected depth=0.0 for surface quarry blast, got {record.depth!r}"
        )


# ===========================================================================
# 3. Cache TTL constant
# ===========================================================================


class TestCacheTTL:
    """_RENASS_CACHE_TTL == 60 seconds (brief Q2 resolution)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_RENASS_CACHE_TTL = 60."""
        import weewx_clearskies_api.providers.earthquakes.renass as _renass  # noqa: PLC0415

        assert _renass._RENASS_CACHE_TTL == 60, (
            f"Expected _RENASS_CACHE_TTL=60, got {_renass._RENASS_CACHE_TTL!r}"
        )


# ===========================================================================
# 4. fetch() happy path — cache miss + cache hit + fakeredis
# ===========================================================================


class TestFetchHappyPath:
    """fetch() returns EarthquakeRecord list; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_records(self) -> None:
        """Cache miss → 1 HTTP call → list[EarthquakeRecord] returned."""
        from weewx_clearskies_api.providers.earthquakes.renass import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(
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
        assert records[0].source == "renass"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls; same records returned."""
        from weewx_clearskies_api.providers.earthquakes.renass import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
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
        from weewx_clearskies_api.providers.earthquakes.renass import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
        )
        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415

        reset_cache_for_tests()
        reset_provider_registry_for_tests()
        _reset_http_client_for_tests()
        _rate_limiter._calls.clear()

        # Inject fakeredis via the established RedisCache test pattern
        # (object.__new__ bypasses the URL-based ping in __init__);
        # see tests/test_providers_alerts_unit.py:660 for the precedent.
        import redis as _redis_lib  # noqa: PLC0415
        fake_redis = fakeredis.FakeRedis(decode_responses=False)
        redis_cache = object.__new__(RedisCache)
        redis_cache._client = fake_redis
        redis_cache._redis_error_cls = _redis_lib.exceptions.RedisError
        _cache_mod._cache = redis_cache

        from weewx_clearskies_api.providers.earthquakes.renass import fetch  # noqa: PLC0415

        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
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
    """Missing required id → ValidationError → ProviderProtocolError."""

    def test_missing_id_raises_provider_protocol_error(self) -> None:
        """Drop 'id' from features → ValidationError → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.earthquakes.renass import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("renass_france_recent.json")
        bad_data = dict(data)
        bad_features = []
        for f in data["features"]:
            bad_f = {k: v for k, v in f.items() if k != "id"}
            bad_features.append(bad_f)
        bad_data["features"] = bad_features

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(
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
        from weewx_clearskies_api.providers.earthquakes.renass import fetch  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("renass_france_recent.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        reset_cache_for_tests()
        wire_cache_from_env()

        with respx.mock(assert_all_called=False) as mock2:
            mock2.get(_RENASS_QUERY_URL).mock(return_value=httpx.Response(200, json=data))
            records = fetch(lat=_LAT, lon=_LON, radius_km=500.0, from_dt=None, to_dt=None)

        assert len(records) == 3
        _reset_provider_state()


# ===========================================================================
# 7. Capability declaration
# ===========================================================================


class TestReNaSSCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, fields, coverage."""

    def test_capability_provider_id_is_renass(self) -> None:
        """CAPABILITY.provider_id = 'renass'."""
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "renass"

    def test_capability_domain_is_earthquakes(self) -> None:
        """CAPABILITY.domain = 'earthquakes'."""
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "earthquakes"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless per ADR-040)."""
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_geographic_coverage_is_fr(self) -> None:
        """CAPABILITY.geographic_coverage = 'fr' (per Q1 resolution)."""
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "fr", (
            f"Expected geographic_coverage='fr', got {CAPABILITY.geographic_coverage!r}"
        )

    def test_capability_supplied_fields_includes_core_renass_fields(self) -> None:
        """CAPABILITY.supplied_canonical_fields covers ReNaSS-supplied fields."""
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        # ReNaSS supplies: id, time, latitude, longitude, magnitude, magnitudeType,
        # depth, place, url, status, source
        required_fields = {
            "id", "time", "latitude", "longitude", "magnitude",
            "magnitudeType", "depth", "place", "url", "status", "source",
        }
        supplied = set(CAPABILITY.supplied_canonical_fields)
        missing = required_fields - supplied
        assert not missing, (
            f"CAPABILITY missing expected ReNaSS fields: {missing!r}"
        )

    def test_wire_providers_registers_renass_earthquakes_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('earthquakes', 'renass') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.earthquakes.renass import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "renass" and p.domain == "earthquakes" for p in registry
        ), "wire_providers must register renass earthquakes in registry"
        reset_provider_registry_for_tests()
