"""Unit tests for providers/imagery/naip.py (Phase LM, §LM-1).

No live network; respx mocks outbound httpx calls. Cache state reset between
tests. Mirrors tests/providers/radar/test_openweathermap_unit.py — the
closest existing precedent (API-proxied+cached keyless tile provider).

Coverage:
  - CAPABILITY shape: provider_id, domain, geographic_coverage="CONUS",
    tile_content_type, no auth required.
  - is_conus(): CONUS coordinates → True; non-CONUS (London) → False.
    (KAT d support — this is also the mutation target named in the brief's
    falsifiability instruction: break is_conus() → KAT d fails.)
  - get_tile() cache miss → upstream exportImage call → cache populated with
    base64 envelope → returns (bytes, content_type). (KAT a)
  - get_tile() cache hit → returns cached bytes without any HTTP call. (KAT b)
  - Cache key includes (provider_id, "tile", z, x, y); does NOT include
    ttl_seconds (same envelope regardless of TTL used to store it).
  - Upstream 429 → QuotaExhausted with retry_after_seconds.
  - Upstream 5xx (after retries exhausted) → TransientNetworkError.
  - Upstream unexpected 4xx → ProviderProtocolError with status_code set
    (coding.md "dispatch on exception state via attributes", not message).
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

_TEST_TTL = 604800

# A known CONUS coordinate: Huntington Beach, CA (per the plan's accept
# criteria — "NAIP proxy serves a recognizable tile of Huntington Beach").
_HB_LAT = 33.6595
_HB_LON = -118.0055

# A known non-CONUS coordinate: London, UK.
_LONDON_LAT = 51.5074
_LONDON_LON = -0.1278

_EXPORT_URL = (
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
)


def _reset_module_state() -> None:
    """Reset provider module and cache state between tests."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )

    reset_cache_for_tests()
    wire_cache_from_env()

    from weewx_clearskies_api.providers.imagery import naip as mod  # noqa: PLC0415

    mod._reset_http_client_for_tests()
    mod._rate_limiter._calls.clear()


# ===========================================================================
# CAPABILITY declaration checks
# ===========================================================================


class TestNaipCapability:
    def test_capability_provider_id_is_naip(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "naip"

    def test_capability_domain_is_imagery(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "imagery"

    def test_capability_geographic_coverage_is_conus(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "CONUS"

    def test_capability_no_auth_required(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_tile_content_type_is_image_png(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_supplied_canonical_fields_is_empty(self) -> None:
        """Imagery has no canonical-entity mapping — display-only (phase hard rule)."""
        from weewx_clearskies_api.providers.imagery.naip import CAPABILITY  # noqa: PLC0415

        assert len(CAPABILITY.supplied_canonical_fields) == 0


# ===========================================================================
# is_conus() — CONUS bbox test (KAT d support; mutation target)
# ===========================================================================


class TestIsConus:
    def test_huntington_beach_is_conus(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import is_conus  # noqa: PLC0415

        assert is_conus(_HB_LAT, _HB_LON) is True

    def test_london_is_not_conus(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import is_conus  # noqa: PLC0415

        assert is_conus(_LONDON_LAT, _LONDON_LON) is False

    def test_bounds_boundaries_are_inclusive(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            CONUS_BOUNDS,
            is_conus,
        )

        assert is_conus(CONUS_BOUNDS["south"], CONUS_BOUNDS["west"]) is True
        assert is_conus(CONUS_BOUNDS["north"], CONUS_BOUNDS["east"]) is True

    def test_just_outside_bounds_is_false(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            CONUS_BOUNDS,
            is_conus,
        )

        assert is_conus(CONUS_BOUNDS["south"] - 1.0, -100.0) is False
        assert is_conus(50.0, CONUS_BOUNDS["west"] - 1.0) is False


# ===========================================================================
# get_tile() — cache hit path (KAT b)
# ===========================================================================


class TestNaipGetTileCacheHit:
    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_cache_hit_bypasses_http_and_returns_cached_bytes(self) -> None:
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            _build_tile_cache_key,
            get_tile,
        )

        tile_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
        cache_key = _build_tile_cache_key(4, 4, 6)
        envelope = {
            "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
            "content_type": "image/png",
        }
        get_cache().set(cache_key, envelope, ttl_seconds=_TEST_TTL)

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(200, content=tile_bytes)
            )
            result_bytes, content_type = get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)
            assert not route.called, "HTTP should not be called on cache hit"

        assert result_bytes == tile_bytes
        assert content_type == "image/png"


# ===========================================================================
# get_tile() — cache miss + upstream success (KAT a)
# ===========================================================================


class TestNaipGetTileCacheMiss:
    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_cache_miss_calls_exportimage_and_returns_bytes(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import get_tile  # noqa: PLC0415

        tile_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60

        with respx.mock(assert_all_called=True) as mock:
            mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(
                    200, content=tile_bytes, headers={"Content-Type": "image/png"}
                )
            )
            result_bytes, content_type = get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

        assert result_bytes == tile_bytes
        assert content_type == "image/png"

    def test_cache_miss_populates_cache_with_base64_envelope(self) -> None:
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            _build_tile_cache_key,
            get_tile,
        )

        tile_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(
                    200, content=tile_bytes, headers={"Content-Type": "image/png"}
                )
            )
            get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

        cached = get_cache().get(_build_tile_cache_key(4, 4, 6))
        assert cached is not None
        assert cached["content_type"] == "image/png"
        assert base64.b64decode(cached["_tile_b64"]) == tile_bytes

    def test_export_request_includes_bbox_and_size_params(self) -> None:
        """exportImage is called with the expected fixed params (format/size/f)."""
        from weewx_clearskies_api.providers.imagery.naip import get_tile  # noqa: PLC0415

        tile_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, content=tile_bytes, headers={"Content-Type": "image/png"})

        with respx.mock(assert_all_called=True) as mock:
            mock.get(_EXPORT_URL).mock(side_effect=_capture)
            get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

        assert captured["format"] == "png"
        assert captured["f"] == "image"
        assert captured["size"] == "256,256"
        assert captured["bboxSR"] == "102100"
        assert "bbox" in captured

    def test_cache_key_independent_of_ttl(self) -> None:
        """Cache key depends only on (z, x, y), not on ttl_seconds."""
        from weewx_clearskies_api.providers.imagery.naip import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        assert _build_tile_cache_key(4, 4, 6) == _build_tile_cache_key(4, 4, 6)


# ===========================================================================
# get_tile() — upstream error mapping
# ===========================================================================


class TestNaipGetTileErrorMapping:
    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_upstream_429_raises_quota_exhausted(self) -> None:
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.imagery.naip import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "30"})
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

        assert exc_info.value.retry_after_seconds == 30

    def test_upstream_5xx_raises_transient_network_error(self) -> None:
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.imagery.naip import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EXPORT_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(TransientNetworkError):
                get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

    def test_upstream_404_raises_provider_protocol_error_with_status_code(self) -> None:
        """Unexpected 4xx (e.g. out-of-domain bbox) → ProviderProtocolError.status_code."""
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.imagery.naip import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_EXPORT_URL).mock(return_value=httpx.Response(404))
            with pytest.raises(ProviderProtocolError) as exc_info:
                get_tile(4, 4, 6, ttl_seconds=_TEST_TTL)

        assert exc_info.value.status_code == 404


# ===========================================================================
# Slippy-tile -> Web Mercator bbox math
# ===========================================================================


class TestTileToWebMercatorBbox:
    def test_zoom_0_covers_the_whole_world(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            _ORIGIN_SHIFT,
            _tile_to_web_mercator_bbox,
        )

        xmin, ymin, xmax, ymax = _tile_to_web_mercator_bbox(0, 0, 0)
        assert xmin == pytest.approx(-_ORIGIN_SHIFT)
        assert xmax == pytest.approx(_ORIGIN_SHIFT)
        assert ymin == pytest.approx(-_ORIGIN_SHIFT)
        assert ymax == pytest.approx(_ORIGIN_SHIFT)

    def test_bbox_shrinks_as_zoom_increases(self) -> None:
        from weewx_clearskies_api.providers.imagery.naip import (  # noqa: PLC0415
            _tile_to_web_mercator_bbox,
        )

        xmin0, ymin0, xmax0, ymax0 = _tile_to_web_mercator_bbox(0, 0, 0)
        xmin1, ymin1, xmax1, ymax1 = _tile_to_web_mercator_bbox(1, 0, 0)
        width0 = xmax0 - xmin0
        width1 = xmax1 - xmin1
        assert width1 == pytest.approx(width0 / 2)
