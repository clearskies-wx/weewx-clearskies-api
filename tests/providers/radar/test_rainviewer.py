"""Unit tests for providers/radar/rainviewer.py (3b-14).

Covers per the task-3b-14 brief §Test coverage (rainviewer unit test file):

  Fixture loading + parse:
  - weather-maps.json loads via _RainViewerWeatherMaps Pydantic model.
  - 13 past frames, 0 nowcast frames in live capture.
  - Extra wire fields silently ignored (extra="ignore").

  Canonical translation (_to_canonical_frames):
  - Frame where past[i].time >= generated → kind="current".
  - Frame where past[i].time < generated → kind="past".
  - nowcast[i] → kind="nowcast".
  - time field = UTC ISO-8601 Z string (ADR-020).
  - Empty nowcast array tolerated (was empty in live capture).

  Cache integration:
  - fakeredis fixture, set + get round-trip.
  - Cache miss → HTTP call; cache hit → 0 HTTP calls.

  Error mapping:
  - 429 → QuotaExhausted.
  - 5xx → TransientNetworkError.
  - Malformed JSON → ProviderProtocolError.

  CAPABILITY shape:
  - provider_id = "rainviewer", domain = "radar".
  - supplied_canonical_fields = () (radar has no canonical-entity mapping).
  - auth_required = () (keyless).
  - tile_url_template is non-None (XYZ slippy URL template).
  - tile_content_type = "image/png".
  - wms_endpoint_url = None, wms_layer_name = None (not a WMS provider).

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/radar/rainviewer/weather-maps.json
ADR references: ADR-015, ADR-017, ADR-020, ADR-038.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar" / "rainviewer"
_RAINVIEWER_URL = "https://api.rainviewer.com/public/weather-maps.json"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture from the rainviewer fixture directory."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


def _reset_provider_state() -> None:
    """Reset provider registry, cache, rate limiter, and HTTP client."""
    import os  # noqa: PLC0415

    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.radar.rainviewer import (  # noqa: PLC0415
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


class TestRainViewerWireShapeValidation:
    """Wire-shape models validate against the live-captured weather-maps.json fixture."""

    def test_fixture_loads_via_rainviewer_weather_maps_model(self) -> None:
        """weather-maps.json loads via _RainViewerWeatherMaps without error."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert result is not None

    def test_fixture_version_is_2_0(self) -> None:
        """Fixture version field = '2.0'."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert result.version == "2.0", f"Expected version='2.0', got {result.version!r}"

    def test_fixture_has_13_past_frames(self) -> None:
        """Fixture has exactly 13 past frames (from live capture 2026-05-11)."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert len(result.radar.past) == 13, (
            f"Expected 13 past frames, got {len(result.radar.past)}"
        )

    def test_fixture_nowcast_is_empty_list(self) -> None:
        """Fixture nowcast = [] (was empty at capture time; module must tolerate this)."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert result.radar.nowcast == [], (
            f"Expected empty nowcast list, got {result.radar.nowcast!r}"
        )

    def test_extra_wire_fields_are_ignored(self) -> None:
        """Extra top-level wire fields (satellite, etc.) silently ignored."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        raw["unexpected_new_field"] = "should_be_ignored"
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert result is not None

    def test_host_field_is_tilecache_url(self) -> None:
        """Fixture host = 'https://tilecache.rainviewer.com'."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        assert result.host == "https://tilecache.rainviewer.com", (
            f"Expected host='https://tilecache.rainviewer.com', got {result.host!r}"
        )

    def test_first_past_entry_has_time_and_path(self) -> None:
        """First past entry has 'time' (int) and 'path' (str) fields."""
        from weewx_clearskies_api.providers.radar.rainviewer import _RainViewerWeatherMaps  # noqa: PLC0415

        raw = _load_fixture("weather-maps.json")
        result = _RainViewerWeatherMaps.model_validate(raw)
        entry = result.radar.past[0]
        assert isinstance(entry.time, int), f"time must be int, got {type(entry.time).__name__}"
        assert isinstance(entry.path, str), f"path must be str, got {type(entry.path).__name__}"
        assert entry.path.startswith("/"), f"path must start with /, got {entry.path!r}"


# ===========================================================================
# 2. Canonical translation _to_canonical_frames
# ===========================================================================


class TestToCanonicalFrames:
    """_to_canonical_frames() maps wire model to correct RadarFrame kinds."""

    def _make_weather_maps(
        self,
        generated: int = 1778548200,
        past_times: list[int] | None = None,
        nowcast_times: list[int] | None = None,
    ) -> Any:
        """Build a minimal _RainViewerWeatherMaps for testing."""
        from weewx_clearskies_api.providers.radar.rainviewer import (  # noqa: PLC0415
            _RainViewerWeatherMaps,
        )

        if past_times is None:
            past_times = [1778541000, 1778544600, 1778548200]
        if nowcast_times is None:
            nowcast_times = []

        raw = {
            "version": "2.0",
            "generated": generated,
            "host": "https://tilecache.rainviewer.com",
            "radar": {
                "past": [{"time": t, "path": f"/v2/radar/{t}"} for t in past_times],
                "nowcast": [{"time": t, "path": f"/v2/radar/nowcast/{t}"} for t in nowcast_times],
            },
        }
        return _RainViewerWeatherMaps.model_validate(raw)

    def test_latest_past_frame_has_kind_current(self) -> None:
        """past[-1].time == generated → kind='current'."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        # generated = last past time
        parsed = self._make_weather_maps(generated=1778548200, past_times=[1778541000, 1778544600, 1778548200])
        frames = _to_canonical_frames(parsed)
        current_frames = [f for f in frames if f.kind == "current"]
        assert len(current_frames) == 1, f"Expected exactly 1 current frame, got {len(current_frames)}"

    def test_latest_past_frame_kind_matches_current_expected_time(self) -> None:
        """The frame with kind='current' corresponds to the largest past time."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        parsed = self._make_weather_maps(generated=1778548200, past_times=[1778541000, 1778544600, 1778548200])
        frames = _to_canonical_frames(parsed)
        current_frames = [f for f in frames if f.kind == "current"]
        # The current frame should correspond to time=1778548200
        from weewx_clearskies_api.providers._common.datetime_utils import epoch_to_utc_iso8601  # noqa: PLC0415
        expected_time = epoch_to_utc_iso8601(1778548200, provider_id="rainviewer", domain="radar")
        assert current_frames[0].time == expected_time

    def test_earlier_past_frames_have_kind_past(self) -> None:
        """past[i].time < generated → kind='past' (all non-latest)."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        parsed = self._make_weather_maps(generated=1778548200, past_times=[1778541000, 1778544600, 1778548200])
        frames = _to_canonical_frames(parsed)
        past_frames = [f for f in frames if f.kind == "past"]
        assert len(past_frames) == 2, f"Expected 2 past frames, got {len(past_frames)}"

    def test_nowcast_frames_have_kind_nowcast(self) -> None:
        """nowcast entries → kind='nowcast'."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        parsed = self._make_weather_maps(
            generated=1778548200,
            past_times=[1778541000, 1778548200],
            nowcast_times=[1778551800, 1778555400],
        )
        frames = _to_canonical_frames(parsed)
        nowcast_frames = [f for f in frames if f.kind == "nowcast"]
        assert len(nowcast_frames) == 2, f"Expected 2 nowcast frames, got {len(nowcast_frames)}"

    def test_empty_nowcast_array_produces_no_nowcast_frames(self) -> None:
        """Empty nowcast array → no nowcast kind frames (matches live capture state)."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        parsed = self._make_weather_maps(past_times=[1778541000, 1778548200], nowcast_times=[])
        frames = _to_canonical_frames(parsed)
        nowcast_frames = [f for f in frames if f.kind == "nowcast"]
        assert len(nowcast_frames) == 0

    def test_all_frame_times_end_with_z(self) -> None:
        """All RadarFrame.time values end with 'Z' suffix (ADR-020)."""
        from weewx_clearskies_api.providers.radar.rainviewer import _to_canonical_frames  # noqa: PLC0415

        parsed = self._make_weather_maps()
        frames = _to_canonical_frames(parsed)
        for frame in frames:
            assert frame.time.endswith("Z"), f"Frame time {frame.time!r} must end with Z"

    def test_canonical_translation_from_real_fixture(self) -> None:
        """Live fixture → canonical frames: 13 past frames (12 past + 1 current), 0 nowcast."""
        from weewx_clearskies_api.providers.radar.rainviewer import (  # noqa: PLC0415
            _RainViewerWeatherMaps,
            _to_canonical_frames,
        )

        raw = _load_fixture("weather-maps.json")
        parsed = _RainViewerWeatherMaps.model_validate(raw)
        frames = _to_canonical_frames(parsed)

        assert len(frames) == 13, f"Expected 13 total frames, got {len(frames)}"
        current_count = sum(1 for f in frames if f.kind == "current")
        past_count = sum(1 for f in frames if f.kind == "past")
        nowcast_count = sum(1 for f in frames if f.kind == "nowcast")
        assert current_count == 1, f"Expected 1 current frame, got {current_count}"
        assert past_count == 12, f"Expected 12 past frames, got {past_count}"
        assert nowcast_count == 0, f"Expected 0 nowcast frames, got {nowcast_count}"


# ===========================================================================
# 3. Cache TTL constant
# ===========================================================================


class TestCacheTTL:
    """_CACHE_TTL = 60 seconds (brief lead call 5)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_CACHE_TTL = 60 (brief lead call 5 — frame index cache TTL)."""
        import weewx_clearskies_api.providers.radar.rainviewer as rv  # noqa: PLC0415

        assert rv._CACHE_TTL == 60, f"Expected _CACHE_TTL=60, got {rv._CACHE_TTL!r}"


# ===========================================================================
# 4. get_frames() happy path — cache miss + cache hit + fakeredis
# ===========================================================================


class TestGetFramesHappyPath:
    """get_frames() returns RadarFrameList; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_frames(self) -> None:
        """Cache miss → 1 HTTP call → RadarFrameList returned and cached."""
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("weather-maps.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(return_value=httpx.Response(200, json=data))
            result = get_frames()
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 HTTP call on cache miss, got {call_count}"
        assert result.providerId == "rainviewer"
        assert len(result.frames) == 13
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls; same frame list returned."""
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("weather-maps.json")

        # First call — fills cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(return_value=httpx.Response(200, json=data))
            result1 = get_frames()

        # Second call — must come from cache
        with respx.mock(assert_all_called=False) as mock2:
            result2 = get_frames()
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 HTTP calls on cache hit, got {cache_hit_calls}"
        )
        assert len(result1.frames) == len(result2.frames)
        assert result2.providerId == "rainviewer"
        _reset_provider_state()

    def test_fakeredis_cache_hit_skips_http_call(self) -> None:
        """With fakeredis backend: cache hit → 0 HTTP calls."""
        pytest.importorskip("fakeredis", reason="fakeredis not installed")
        import fakeredis  # noqa: PLC0415
        import redis as _redis_lib  # noqa: PLC0415

        import weewx_clearskies_api.providers._common.cache as _cache_mod  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
            RedisCache,
            reset_cache_for_tests,
            wire_cache_from_env,
        )
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            reset_provider_registry_for_tests,
        )
        from weewx_clearskies_api.providers.radar.rainviewer import (  # noqa: PLC0415
            _reset_http_client_for_tests,
            _rate_limiter,
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

        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        data = _load_fixture("weather-maps.json")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(return_value=httpx.Response(200, json=data))
            result1 = get_frames()

        with respx.mock(assert_all_called=False) as mock2:
            result2 = get_frames()
            assert mock2.calls.call_count == 0, "fakeredis cache hit must avoid HTTP call"

        assert len(result2.frames) == 13
        assert result1.frames[0].time == result2.frames[0].time
        _reset_provider_state()

    def test_result_attribution_is_non_empty(self) -> None:
        """get_frames() result has non-empty attribution string."""
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        data = _load_fixture("weather-maps.json")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(return_value=httpx.Response(200, json=data))
            result = get_frames()
        assert result.attribution is not None
        assert len(result.attribution) > 0
        _reset_provider_state()


# ===========================================================================
# 5. Error mapping
# ===========================================================================


class TestErrorMapping:
    """get_frames() maps HTTP errors to canonical taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            with pytest.raises(QuotaExhausted):
                get_frames()
        _reset_provider_state()

    def test_5xx_raises_transient_network_error(self) -> None:
        """HTTP 5xx → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(
                return_value=httpx.Response(503, text="service unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_frames()
        _reset_provider_state()

    def test_malformed_json_raises_provider_protocol_error(self) -> None:
        """Non-JSON response body → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(
                return_value=httpx.Response(200, text="not-json-at-all")
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()

    def test_missing_required_field_raises_provider_protocol_error(self) -> None:
        """JSON missing required 'radar' field → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.rainviewer import get_frames  # noqa: PLC0415

        _reset_provider_state()
        bad_data = {"version": "2.0", "generated": 12345, "host": "https://example.com"}
        # Missing "radar" key — ValidationError → ProviderProtocolError
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_RAINVIEWER_URL).mock(return_value=httpx.Response(200, json=bad_data))
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()


# ===========================================================================
# 6. Capability declaration
# ===========================================================================


class TestRainViewerCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, fields, tile template."""

    def test_capability_provider_id_is_rainviewer(self) -> None:
        """CAPABILITY.provider_id = 'rainviewer'."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "rainviewer"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain = 'radar'."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless — no API key needed per ADR-015)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == (), (
            f"Expected auth_required=() (keyless), got {CAPABILITY.auth_required!r}"
        )

    def test_capability_supplied_canonical_fields_is_empty_tuple(self) -> None:
        """CAPABILITY.supplied_canonical_fields = () (radar has no canonical-entity mapping per §4.5)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.supplied_canonical_fields == (), (
            f"Radar has no canonical-entity mapping; expected (), got "
            f"{CAPABILITY.supplied_canonical_fields!r}"
        )

    def test_capability_tile_url_template_is_non_none(self) -> None:
        """CAPABILITY.tile_url_template is non-None (XYZ slippy URL template for rainviewer)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is not None, (
            "rainviewer CAPABILITY must have tile_url_template (XYZ slippy provider)"
        )

    def test_capability_tile_url_template_contains_xyz_placeholders(self) -> None:
        """CAPABILITY.tile_url_template contains {z}/{x}/{y} placeholders."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        template = CAPABILITY.tile_url_template or ""
        assert "{z}" in template, f"tile_url_template must contain {{z}}; got {template!r}"
        assert "{x}" in template, f"tile_url_template must contain {{x}}; got {template!r}"
        assert "{y}" in template, f"tile_url_template must contain {{y}}; got {template!r}"

    def test_capability_tile_content_type_is_png(self) -> None:
        """CAPABILITY.tile_content_type = 'image/png'."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png", (
            f"Expected tile_content_type='image/png', got {CAPABILITY.tile_content_type!r}"
        )

    def test_capability_wms_endpoint_url_is_none(self) -> None:
        """CAPABILITY.wms_endpoint_url = None (rainviewer is XYZ, not WMS)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_endpoint_url is None, (
            f"rainviewer is XYZ provider, not WMS; wms_endpoint_url must be None, "
            f"got {CAPABILITY.wms_endpoint_url!r}"
        )

    def test_capability_wms_layer_name_is_none(self) -> None:
        """CAPABILITY.wms_layer_name = None (rainviewer is XYZ, not WMS)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name is None, (
            f"rainviewer is XYZ provider; wms_layer_name must be None, "
            f"got {CAPABILITY.wms_layer_name!r}"
        )

    def test_capability_geographic_coverage_is_global(self) -> None:
        """CAPABILITY.geographic_coverage = 'global' (default fallback per ADR-015)."""
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global", (
            f"Expected geographic_coverage='global', got {CAPABILITY.geographic_coverage!r}"
        )

    def test_wire_providers_registers_rainviewer_radar_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('radar', 'rainviewer') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.radar.rainviewer import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "rainviewer" and p.domain == "radar" for p in registry
        ), "wire_providers must register rainviewer radar in registry"
        reset_provider_registry_for_tests()
