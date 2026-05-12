"""Unit tests for providers/radar/dwd_radolan.py (3b-14).

Covers per the task-3b-14 brief §Test coverage (dwd_radolan unit test file):

  Fixture loading + parse:
  - get_capabilities.xml loads via parse_wms_time_dimension for layer "dwd:RX-Produkt"
    (synthetic — see fixtures.md; api-dev uses LAYER_NAME="dwd:RX-Produkt" per api-docs).
  - TIME dimension: 2026-05-08T00:00:00Z/2026-05-12T03:15:00Z/PT5M (4+ day range; capped at 300).
  - All timestamps end with 'Z'.

  Canonical translation (_to_canonical_frames):
  - Latest timestamp → kind="current".
  - Earlier timestamps → kind="past".
  - No nowcast frames (WMS-T provider; brief lead call 4).

  Cache integration:
  - Cache miss → HTTP call; cache hit → 0 HTTP calls.

  Error mapping:
  - 429 → QuotaExhausted.
  - 5xx → TransientNetworkError.
  - Malformed XML → ProviderProtocolError.

  CAPABILITY shape:
  - provider_id = "dwd_radolan", domain = "radar".
  - supplied_canonical_fields = () (no canonical-entity mapping).
  - auth_required = () (keyless).
  - wms_endpoint_url is non-None, wms_layer_name = "dwd:RX-Produkt".
  - tile_content_type = "image/png".
  - tile_url_template = None (WMS provider).
  - geographic_coverage = "germany".

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/radar/dwd_radolan/get_capabilities.xml
ADR references: ADR-015, ADR-017, ADR-020, ADR-038.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar" / "dwd_radolan"

_DWD_BASE_URL = "https://maps.dwd.de"
_DWD_PATH = "/geoserver/dwd/wms"


def _load_fixture(name: str) -> bytes:
    return (_FIXTURES_DIR / name).read_bytes()


def _reset_provider_state() -> None:
    import os  # noqa: PLC0415

    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.radar.dwd_radolan import (  # noqa: PLC0415
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


class TestDWDRADOLANFixtureParsing:
    """parse_wms_time_dimension parses DWD RADOLAN fixture for dwd:RX-Produkt (synthetic)."""

    def test_fixture_parses_to_nonempty_list(self) -> None:
        """get_capabilities.xml parses to non-empty list for dwd:RX-Produkt."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="dwd:RX-Produkt",
            provider_id="dwd_radolan",
            domain="radar",
        )
        assert len(result) > 0

    def test_fixture_is_capped_at_max_period_frames(self) -> None:
        """4+ day range at PT5M capped at _MAX_PERIOD_FRAMES (300)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (  # noqa: PLC0415
            _MAX_PERIOD_FRAMES,
            parse_wms_time_dimension,
        )

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="dwd:RX-Produkt",
            provider_id="dwd_radolan",
            domain="radar",
        )
        assert len(result) == _MAX_PERIOD_FRAMES, (
            f"Expected cap at {_MAX_PERIOD_FRAMES}; got {len(result)}"
        )

    def test_all_timestamps_end_with_z(self) -> None:
        """All DWD parsed timestamps end with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="dwd:RX-Produkt",
            provider_id="dwd_radolan",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"

    def test_real_niederschlagsradar_layer_also_parseable(self) -> None:
        """Niederschlagsradar (real layer from live capture) also parses correctly."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="Niederschlagsradar",
            provider_id="dwd_radolan",
            domain="radar",
        )
        assert len(result) > 0


class TestDWDRADOLANGetFramesHappyPath:
    """get_frames() returns RadarFrameList; caches correctly."""

    def test_cache_miss_makes_http_call_and_returns_frames(self) -> None:
        """Cache miss → 1 HTTP call → RadarFrameList returned."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()
            call_count = mock.calls.call_count

        assert call_count == 1
        assert result.providerId == "dwd_radolan"
        assert len(result.frames) > 0
        _reset_provider_state()

    def test_frames_have_exactly_one_current_frame(self) -> None:
        """DWD frames: exactly 1 'current', 0 'nowcast', rest are 'past'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()

        current_count = sum(1 for f in result.frames if f.kind == "current")
        nowcast_count = sum(1 for f in result.frames if f.kind == "nowcast")
        assert current_count == 1, f"Expected 1 current frame, got {current_count}"
        assert nowcast_count == 0, f"Expected 0 nowcast frames (WMS-T), got {nowcast_count}"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            get_frames()

        with respx.mock(assert_all_called=False) as mock2:
            get_frames()
            assert mock2.calls.call_count == 0
        _reset_provider_state()


class TestDWDRADOLANErrorMapping:
    """get_frames() maps HTTP/XML errors to canonical taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            with pytest.raises(QuotaExhausted):
                get_frames()
        _reset_provider_state()

    def test_5xx_raises_transient_network_error(self) -> None:
        """HTTP 5xx → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(503, text="unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_frames()
        _reset_provider_state()

    def test_malformed_xml_raises_provider_protocol_error(self) -> None:
        """Malformed XML → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.dwd_radolan import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_DWD_BASE_URL + _DWD_PATH).mock(
                return_value=httpx.Response(200, content=b"<broken")
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()


class TestDWDRADOLANCapabilityDeclaration:
    """CAPABILITY symbol shape for dwd_radolan."""

    def test_capability_provider_id_is_dwd_radolan(self) -> None:
        """CAPABILITY.provider_id = 'dwd_radolan'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "dwd_radolan"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain = 'radar'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless)."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_supplied_canonical_fields_is_empty_tuple(self) -> None:
        """CAPABILITY.supplied_canonical_fields = () (no canonical-entity mapping)."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.supplied_canonical_fields == ()

    def test_capability_geographic_coverage_is_germany(self) -> None:
        """CAPABILITY.geographic_coverage = 'germany'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "germany"

    def test_capability_wms_layer_name_is_dwd_rx_produkt(self) -> None:
        """CAPABILITY.wms_layer_name = 'dwd:RX-Produkt'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name == "dwd:RX-Produkt"

    def test_capability_tile_content_type_is_png(self) -> None:
        """CAPABILITY.tile_content_type = 'image/png'."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_tile_url_template_is_none(self) -> None:
        """CAPABILITY.tile_url_template = None (WMS provider)."""
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is None

    def test_wire_providers_registers_dwd_radolan_radar_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('radar', 'dwd_radolan') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.radar.dwd_radolan import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "dwd_radolan" and p.domain == "radar" for p in registry
        )
        reset_provider_registry_for_tests()
