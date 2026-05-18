"""Unit tests for providers/radar/iem_nexrad.py (3b-14).

Covers per the task-3b-14 brief §Test coverage (iem_nexrad unit test file):

  Fixture loading + parse:
  - get_capabilities.xml loads via parse_wms_time_dimension for layer nexrad-n0q-wmst.
  - TIME dimension: 2011-02-16/2026-12-31/PT5M (period notation; capped at 300 frames).
  - All timestamps end with 'Z'.

  Canonical translation (_to_canonical_frames):
  - Latest timestamp → kind="current".
  - Earlier timestamps → kind="past".
  - No nowcast frames (WMS-T provider; brief lead call 4).

  Cache integration:
  - fakeredis fixture, set + get round-trip.
  - Cache miss → HTTP call; cache hit → 0 HTTP calls.

  Error mapping:
  - 429 → QuotaExhausted.
  - 5xx → TransientNetworkError.
  - Malformed XML response → ProviderProtocolError.
  - Layer not found in capabilities → ProviderProtocolError.

  CAPABILITY shape:
  - provider_id = "iem_nexrad", domain = "radar".
  - supplied_canonical_fields = () (radar has no canonical-entity mapping).
  - auth_required = () (keyless).
  - wms_endpoint_url is non-None, wms_layer_name = "nexrad-n0q-wmst".
  - tile_content_type = "image/png".
  - tile_url_template = None (WMS provider; not XYZ slippy).
  - geographic_coverage = "us-conus".

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/radar/iem_nexrad/get_capabilities.xml
ADR references: ADR-015, ADR-017, ADR-020, ADR-038.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar" / "iem_nexrad"

_IEM_BASE_URL = "https://mesonet.agron.iastate.edu"
_IEM_PATH = "/cgi-bin/wms/nexrad/n0q-t.cgi"


def _load_fixture(name: str) -> bytes:
    """Load a fixture from the iem_nexrad fixture directory as raw bytes."""
    return (_FIXTURES_DIR / name).read_bytes()


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
    from weewx_clearskies_api.providers.radar.iem_nexrad import (  # noqa: PLC0415
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
# 1. Fixture loading + parse
# ===========================================================================


class TestIEMNEXRADFixtureParsing:
    """parse_wms_time_dimension parses IEM NEXRAD fixture correctly."""

    def test_fixture_parses_to_nonempty_timestamp_list(self) -> None:
        """get_capabilities.xml parses to non-empty list for nexrad-n0q-wmst."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (
            parse_wms_time_dimension,  # noqa: PLC0415
        )

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="nexrad-n0q-wmst",
            provider_id="iem_nexrad",
            domain="radar",
        )
        assert len(result) > 0

    def test_all_timestamps_end_with_z(self) -> None:
        """All IEM NEXRAD parsed timestamps end with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (
            parse_wms_time_dimension,  # noqa: PLC0415
        )

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="nexrad-n0q-wmst",
            provider_id="iem_nexrad",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"


# ===========================================================================
# 2. Canonical translation _to_canonical_frames
# ===========================================================================


class TestIEMNEXRADToCanonicalFrames:
    """_to_canonical_frames() maps timestamps to correct RadarFrame kinds."""

    def _make_frames_from_timestamps(self, timestamps: list[str]) -> list:  # type: ignore[type-arg]
        """Build canonical RadarFrame list from a list of ISO timestamps."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import (
            _to_canonical_frames,  # noqa: PLC0415
        )

        return _to_canonical_frames(timestamps)

    def test_latest_timestamp_has_kind_current(self) -> None:
        """Latest timestamp in list → kind='current'."""
        frames = self._make_frames_from_timestamps([
            "2026-05-11T20:00:00Z",
            "2026-05-11T20:05:00Z",
            "2026-05-11T20:10:00Z",
        ])
        current_frames = [f for f in frames if f.kind == "current"]
        assert len(current_frames) == 1
        assert current_frames[0].time == "2026-05-11T20:10:00Z"

    def test_non_latest_timestamps_have_kind_past(self) -> None:
        """All non-latest timestamps → kind='past'."""
        frames = self._make_frames_from_timestamps([
            "2026-05-11T20:00:00Z",
            "2026-05-11T20:05:00Z",
            "2026-05-11T20:10:00Z",
        ])
        past_frames = [f for f in frames if f.kind == "past"]
        assert len(past_frames) == 2

    def test_no_nowcast_frames(self) -> None:
        """WMS-T frames never have kind='nowcast' (brief lead call 4)."""
        frames = self._make_frames_from_timestamps([
            "2026-05-11T20:00:00Z",
            "2026-05-11T20:05:00Z",
        ])
        nowcast_frames = [f for f in frames if f.kind == "nowcast"]
        assert len(nowcast_frames) == 0

    def test_single_timestamp_is_current(self) -> None:
        """Single timestamp → kind='current' (it is both first and latest)."""
        frames = self._make_frames_from_timestamps(["2026-05-11T20:00:00Z"])
        assert len(frames) == 1
        assert frames[0].kind == "current"

    def test_empty_timestamp_list_returns_empty_frames(self) -> None:
        """Empty input → empty RadarFrame list."""
        frames = self._make_frames_from_timestamps([])
        assert frames == []


# ===========================================================================
# 3. Cache TTL constant
# ===========================================================================


class TestIEMNEXRADCacheTTL:
    """_CACHE_TTL = 60 seconds (brief lead call 5)."""

    def test_cache_ttl_is_60_seconds(self) -> None:
        """_CACHE_TTL = 60."""
        import weewx_clearskies_api.providers.radar.iem_nexrad as _iem  # noqa: PLC0415

        assert _iem._CACHE_TTL == 60


# ===========================================================================
# 4. get_frames() happy path
# ===========================================================================


class TestIEMNEXRADGetFramesHappyPath:
    """get_frames() returns RadarFrameList; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_frames(self) -> None:
        """Cache miss → 1 HTTP call → RadarFrameList returned."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()
            call_count = mock.calls.call_count

        assert call_count == 1, f"Expected 1 HTTP call, got {call_count}"
        assert result.providerId == "iem_nexrad"
        assert len(result.frames) > 0
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        # Fill cache
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result1 = get_frames()

        # Cache hit
        with respx.mock(assert_all_called=False) as mock2:
            result2 = get_frames()
            cache_hit_calls = mock2.calls.call_count

        assert cache_hit_calls == 0, (
            f"Expected 0 HTTP calls on cache hit, got {cache_hit_calls}"
        )
        assert len(result1.frames) == len(result2.frames)
        _reset_provider_state()

    def test_result_has_correct_provider_id(self) -> None:
        """get_frames() result.providerId = 'iem_nexrad'."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()
        assert result.providerId == "iem_nexrad"
        _reset_provider_state()


# ===========================================================================
# 5. Error mapping
# ===========================================================================


class TestIEMNEXRADErrorMapping:
    """get_frames() maps HTTP and XML errors to canonical taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            with pytest.raises(QuotaExhausted):
                get_frames()
        _reset_provider_state()

    def test_5xx_raises_transient_network_error(self) -> None:
        """HTTP 5xx → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(503, text="service unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_frames()
        _reset_provider_state()

    def test_malformed_xml_raises_provider_protocol_error(self) -> None:
        """Malformed XML response → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(200, content=b"<broken xml")
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()

    def test_capabilities_missing_target_layer_raises_provider_protocol_error(self) -> None:
        """GetCapabilities XML where the nexrad-n0q-wmst layer is absent → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.iem_nexrad import get_frames  # noqa: PLC0415

        _reset_provider_state()
        # A valid WMS document but with a different layer name
        no_layer_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer>
        <Name>some-other-layer</Name>
        <Dimension name="time" units="ISO8601">2026-05-11T00:00:00Z/2026-05-11T01:00:00Z/PT5M</Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>"""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_IEM_BASE_URL + _IEM_PATH).mock(
                return_value=httpx.Response(200, content=no_layer_xml)
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()


# ===========================================================================
# 6. Capability declaration
# ===========================================================================


class TestIEMNEXRADCapabilityDeclaration:
    """CAPABILITY symbol correct domain, auth, WMS fields."""

    def test_capability_provider_id_is_iem_nexrad(self) -> None:
        """CAPABILITY.provider_id = 'iem_nexrad'."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "iem_nexrad"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain = 'radar'."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless)."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_supplied_canonical_fields_is_empty_tuple(self) -> None:
        """CAPABILITY.supplied_canonical_fields = () (radar has no canonical-entity mapping)."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.supplied_canonical_fields == ()

    def test_capability_geographic_coverage_is_us_conus(self) -> None:
        """CAPABILITY.geographic_coverage = 'us-conus' (IEM NEXRAD CONUS-only per ADR-015)."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "us-conus"

    def test_capability_wms_endpoint_url_is_non_none(self) -> None:
        """CAPABILITY.wms_endpoint_url is non-None (WMS provider)."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_endpoint_url is not None

    def test_capability_wms_layer_name_is_nexrad_n0q_wmst(self) -> None:
        """CAPABILITY.wms_layer_name = 'nexrad-n0q-wmst'."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name == "nexrad-n0q-wmst", (
            f"Expected 'nexrad-n0q-wmst', got {CAPABILITY.wms_layer_name!r}"
        )

    def test_capability_tile_content_type_is_png(self) -> None:
        """CAPABILITY.tile_content_type = 'image/png'."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_tile_url_template_is_none(self) -> None:
        """CAPABILITY.tile_url_template = None (WMS provider; not XYZ slippy)."""
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is None, (
            f"iem_nexrad is WMS provider; tile_url_template must be None, "
            f"got {CAPABILITY.tile_url_template!r}"
        )

    def test_wire_providers_registers_iem_nexrad_radar_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('radar', 'iem_nexrad') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.radar.iem_nexrad import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "iem_nexrad" and p.domain == "radar" for p in registry
        ), "wire_providers must register iem_nexrad radar in registry"
        reset_provider_registry_for_tests()
