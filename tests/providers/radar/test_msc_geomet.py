"""Unit tests for providers/radar/msc_geomet.py (3b-14).

Covers per the task-3b-14 brief §Test coverage (msc_geomet unit test file):

  Fixture loading + parse:
  - get_capabilities.xml loads via parse_wms_time_dimension for layer "RADAR_1KM_RDPR"
    (synthetic — see fixtures.md; api-dev uses LAYER_NAME="RADAR_1KM_RDPR" per api-docs).
  - TIME dimension: 2026-05-11T21:54:00Z/2026-05-12T00:54:00Z/PT6M → 31 frames.
  - All timestamps end with 'Z'.

  Canonical translation (_to_canonical_frames):
  - Latest timestamp → kind="current".
  - Earlier timestamps → kind="past".
  - No nowcast frames (WMS-T provider).

  Cache integration:
  - Cache miss → HTTP call; cache hit → 0 HTTP calls.

  Error mapping:
  - 429 → QuotaExhausted.
  - 5xx → TransientNetworkError.
  - Malformed XML → ProviderProtocolError.

  CAPABILITY shape:
  - provider_id = "msc_geomet", domain = "radar".
  - supplied_canonical_fields = () (no canonical-entity mapping).
  - auth_required = () (keyless).
  - wms_endpoint_url is non-None, wms_layer_name = "RADAR_1KM_RDPR".
  - tile_content_type = "image/png".
  - tile_url_template = None (WMS provider).
  - geographic_coverage = "canada".

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/radar/msc_geomet/get_capabilities.xml
ADR references: ADR-015, ADR-017, ADR-020, ADR-038.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar" / "msc_geomet"

_MSC_BASE_URL = "https://geo.weather.gc.ca"
_MSC_PATH = "/geomet"


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
    from weewx_clearskies_api.providers.radar.msc_geomet import (  # noqa: PLC0415
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


class TestMSCGeoMetFixtureParsing:
    """parse_wms_time_dimension parses MSC GeoMet fixture for RADAR_1KM_RDPR (synthetic)."""

    def test_fixture_parses_to_31_frames(self) -> None:
        """get_capabilities.xml: RADAR_1KM_RDPR, 3h/PT6M → 31 frames."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RDPR",
            provider_id="msc_geomet",
            domain="radar",
        )
        assert len(result) == 31, f"Expected 31 frames for 3h/PT6M range; got {len(result)}"

    def test_first_timestamp_is_start_of_range(self) -> None:
        """First timestamp = 2026-05-11T21:54:00Z (start of period range)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RDPR",
            provider_id="msc_geomet",
            domain="radar",
        )
        assert result[0] == "2026-05-11T21:54:00Z"

    def test_last_timestamp_is_end_of_range(self) -> None:
        """Last timestamp = 2026-05-12T00:54:00Z (end of period range)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RDPR",
            provider_id="msc_geomet",
            domain="radar",
        )
        assert result[-1] == "2026-05-12T00:54:00Z"

    def test_all_timestamps_end_with_z(self) -> None:
        """All MSC GeoMet parsed timestamps end with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RDPR",
            provider_id="msc_geomet",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"


class TestMSCGeoMetGetFramesHappyPath:
    """get_frames() returns RadarFrameList with 31 frames; caches correctly."""

    def test_cache_miss_returns_31_frames(self) -> None:
        """Cache miss → HTTP call → 31 RadarFrames."""
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()
            call_count = mock.calls.call_count

        assert call_count == 1
        assert result.providerId == "msc_geomet"
        assert len(result.frames) == 31
        _reset_provider_state()

    def test_frames_have_correct_current_and_past_split(self) -> None:
        """31 frames: last = 'current', others = 'past'; no 'nowcast'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()

        current_count = sum(1 for f in result.frames if f.kind == "current")
        past_count = sum(1 for f in result.frames if f.kind == "past")
        nowcast_count = sum(1 for f in result.frames if f.kind == "nowcast")
        assert current_count == 1, f"Expected 1 current frame, got {current_count}"
        assert past_count == 30, f"Expected 30 past frames, got {past_count}"
        assert nowcast_count == 0, f"Expected 0 nowcast frames, got {nowcast_count}"
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls."""
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            get_frames()

        with respx.mock(assert_all_called=False) as mock2:
            get_frames()
            assert mock2.calls.call_count == 0
        _reset_provider_state()


class TestMSCGeoMetErrorMapping:
    """get_frames() maps HTTP/XML errors to canonical taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            with pytest.raises(QuotaExhausted):
                get_frames()
        _reset_provider_state()

    def test_5xx_raises_transient_network_error(self) -> None:
        """HTTP 5xx → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(503, text="unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_frames()
        _reset_provider_state()

    def test_malformed_xml_raises_provider_protocol_error(self) -> None:
        """Malformed XML → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.msc_geomet import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_MSC_BASE_URL + _MSC_PATH).mock(
                return_value=httpx.Response(200, content=b"<not-xml")
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()


class TestMSCGeoMetCapabilityDeclaration:
    """CAPABILITY symbol shape for msc_geomet."""

    def test_capability_provider_id_is_msc_geomet(self) -> None:
        """CAPABILITY.provider_id = 'msc_geomet'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "msc_geomet"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain = 'radar'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless)."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_supplied_canonical_fields_is_empty_tuple(self) -> None:
        """CAPABILITY.supplied_canonical_fields = () (no canonical-entity mapping)."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.supplied_canonical_fields == ()

    def test_capability_geographic_coverage_is_canada(self) -> None:
        """CAPABILITY.geographic_coverage = 'canada'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "canada"

    def test_capability_wms_layer_name_is_radar_1km_rdpr(self) -> None:
        """CAPABILITY.wms_layer_name = 'RADAR_1KM_RDPR'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name == "RADAR_1KM_RDPR"

    def test_capability_tile_content_type_is_png(self) -> None:
        """CAPABILITY.tile_content_type = 'image/png'."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_tile_url_template_is_none(self) -> None:
        """CAPABILITY.tile_url_template = None (WMS provider)."""
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is None

    def test_wire_providers_registers_msc_geomet_radar_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('radar', 'msc_geomet') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.radar.msc_geomet import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "msc_geomet" and p.domain == "radar" for p in registry
        )
        reset_provider_registry_for_tests()
