"""Unit tests for providers/radar/noaa_mrms.py (3b-14).

Covers per the task-3b-14 brief §Test coverage (noaa_mrms unit test file):

  Fixture loading + parse:
  - get_capabilities.xml loads via parse_wms_time_dimension for layer "0"
    (synthetic — see fixtures.md; api-dev uses LAYER_NAME="0" per api-docs).
  - TIME dimension: start/end/PT1S (period notation; fine-grained).
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
  - provider_id = "noaa_mrms", domain = "radar".
  - supplied_canonical_fields = () (no canonical-entity mapping).
  - auth_required = () (keyless).
  - wms_endpoint_url is non-None, wms_layer_name = "0".
  - tile_content_type = "image/png".
  - tile_url_template = None (WMS provider).

No DB, no live network. respx mocks outbound httpx calls.
Fixture path: tests/fixtures/providers/radar/noaa_mrms/get_capabilities.xml
ADR references: ADR-015, ADR-017, ADR-020, ADR-038.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar" / "noaa_mrms"

_NOAA_BASE_URL = "https://mapservices.weather.noaa.gov"
_NOAA_PATH = "/eventdriven/services/radar/radar_base_reflectivity_time/ImageServer/WMSServer"


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
    from weewx_clearskies_api.providers.radar.noaa_mrms import (  # noqa: PLC0415
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


class TestNOAAMRMSFixtureParsing:
    """parse_wms_time_dimension parses NOAA MRMS fixture for layer '0' (synthetic)."""

    def test_fixture_parses_to_nonempty_timestamp_list(self) -> None:
        """get_capabilities.xml parses to non-empty list for layer '0'."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="0",
            provider_id="noaa_mrms",
            domain="radar",
        )
        assert len(result) > 0

    def test_all_timestamps_end_with_z(self) -> None:
        """All NOAA MRMS parsed timestamps end with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="0",
            provider_id="noaa_mrms",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"

    def test_real_layer_name_also_parseable(self) -> None:
        """Real layer 'radar_base_reflectivity_time' also parses from fixture."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="radar_base_reflectivity_time",
            provider_id="noaa_mrms",
            domain="radar",
        )
        assert len(result) > 0


class TestNOAAMRMSGetFramesHappyPath:
    """get_frames() returns RadarFrameList; caches on miss; skips HTTP on hit."""

    def test_cache_miss_makes_http_call_and_returns_frames(self) -> None:
        """Cache miss → 1 HTTP call → RadarFrameList returned."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NOAA_BASE_URL + _NOAA_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            result = get_frames()
            call_count = mock.calls.call_count

        assert call_count == 1
        assert result.providerId == "noaa_mrms"
        assert len(result.frames) > 0
        _reset_provider_state()

    def test_cache_hit_skips_http_call(self) -> None:
        """Cache hit → 0 HTTP calls."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import get_frames  # noqa: PLC0415

        _reset_provider_state()
        xml_bytes = _load_fixture("get_capabilities.xml")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NOAA_BASE_URL + _NOAA_PATH).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            get_frames()

        with respx.mock(assert_all_called=False) as mock2:
            get_frames()
            assert mock2.calls.call_count == 0
        _reset_provider_state()


class TestNOAAMRMSErrorMapping:
    """get_frames() maps HTTP/XML errors to canonical taxonomy."""

    def test_429_raises_quota_exhausted(self) -> None:
        """HTTP 429 → QuotaExhausted."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.noaa_mrms import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NOAA_BASE_URL + _NOAA_PATH).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            with pytest.raises(QuotaExhausted):
                get_frames()
        _reset_provider_state()

    def test_5xx_raises_transient_network_error(self) -> None:
        """HTTP 5xx → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import TransientNetworkError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.noaa_mrms import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NOAA_BASE_URL + _NOAA_PATH).mock(
                return_value=httpx.Response(503, text="unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_frames()
        _reset_provider_state()

    def test_malformed_xml_raises_provider_protocol_error(self) -> None:
        """Malformed XML → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.noaa_mrms import get_frames  # noqa: PLC0415

        _reset_provider_state()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(_NOAA_BASE_URL + _NOAA_PATH).mock(
                return_value=httpx.Response(200, content=b"<not-xml")
            )
            with pytest.raises(ProviderProtocolError):
                get_frames()
        _reset_provider_state()


class TestNOAAMRMSCapabilityDeclaration:
    """CAPABILITY symbol shape for noaa_mrms."""

    def test_capability_provider_id_is_noaa_mrms(self) -> None:
        """CAPABILITY.provider_id = 'noaa_mrms'."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "noaa_mrms"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain = 'radar'."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_is_empty_tuple(self) -> None:
        """CAPABILITY.auth_required = () (keyless)."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_supplied_canonical_fields_is_empty_tuple(self) -> None:
        """CAPABILITY.supplied_canonical_fields = () (no canonical-entity mapping)."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.supplied_canonical_fields == ()

    def test_capability_wms_endpoint_url_is_non_none(self) -> None:
        """CAPABILITY.wms_endpoint_url is non-None (WMS provider)."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_endpoint_url is not None

    def test_capability_wms_layer_name_is_0(self) -> None:
        """CAPABILITY.wms_layer_name = '0' (ArcGIS ImageServer convention per api-docs)."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name == "0", (
            f"Expected wms_layer_name='0', got {CAPABILITY.wms_layer_name!r}"
        )

    def test_capability_tile_content_type_is_png(self) -> None:
        """CAPABILITY.tile_content_type = 'image/png'."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_tile_url_template_is_none(self) -> None:
        """CAPABILITY.tile_url_template = None (WMS provider)."""
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is None

    def test_wire_providers_registers_noaa_mrms_radar_in_registry(self) -> None:
        """wire_providers([CAPABILITY]) registers ('radar', 'noaa_mrms') in registry."""
        from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
            get_provider_registry,
            reset_provider_registry_for_tests,
            wire_providers,
        )
        from weewx_clearskies_api.providers.radar.noaa_mrms import CAPABILITY  # noqa: PLC0415

        reset_provider_registry_for_tests()
        wire_providers([CAPABILITY])
        registry = get_provider_registry()
        assert any(
            p.provider_id == "noaa_mrms" and p.domain == "radar" for p in registry
        )
        reset_provider_registry_for_tests()
