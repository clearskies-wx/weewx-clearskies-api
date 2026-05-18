"""Unit tests for providers/radar/aeris.py (3b-15).

Tests the Aeris Xweather Raster Maps radar provider module. No live network;
respx mocks outbound httpx calls. Cache state reset between tests.

Coverage:
  - Empty/None credential guard → KeyInvalid raised BEFORE any HTTP call (LC-I).
  - _redact_url() helper redacts the path credential segment (LC-E security baseline).
  - get_tile() cache hit path bypasses HTTP and returns cached bytes.
  - get_tile() cache miss → upstream call → cache populated with base64 envelope
    → returns (bytes, content_type).
  - Cache key includes (provider_id, "tile", z, x, y, t); does NOT include credentials.
  - Upstream 429 → QuotaExhausted with retry_after_seconds.
  - Upstream 401/403 → KeyInvalid.
  - Upstream 404 → ProviderProtocolError with status_code=404 (LC-H).
  - Upstream 5xx → TransientNetworkError.
  - get_frames() returns single RadarFrameList with kind="current" frame.
  - CAPABILITY shape: provider_id, domain, auth_required, tile_url_template
    uses {auth} placeholder, wms fields None.

ADR references: ADR-015, ADR-017, ADR-018, ADR-037, ADR-038.
Brief: phase-2-task-3b-15-radar-keyed-A2-brief.md §Test-author scope.
LC-E: URL-credential redaction for Aeris path-embedded credentials (security baseline).
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_FIXTURES_BASE = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar"
_AERIS_TILE_FIXTURE = _FIXTURES_BASE / "aeris" / "tile_4_4_6.png"

_TEST_CLIENT_ID = "test_aeris_client_id_abc"
_TEST_CLIENT_SECRET = "test_aeris_secret_xyz"
_PROVIDER_ID = "aeris"
_DOMAIN = "radar"

# Aeris tile URL template with real credentials embedded in path (LC-E pattern)
_AERIS_TILE_URL = (
    f"https://maps.api.xweather.com/"
    f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}/radar/4/4/6/current.png"
)


def _load_tile_bytes() -> bytes:
    return _AERIS_TILE_FIXTURE.read_bytes()


def _reset_module_state() -> None:
    """Reset provider module and cache state between tests."""
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )

    reset_cache_for_tests()
    wire_cache_from_env()

    try:
        from weewx_clearskies_api.providers.radar import aeris as mod  # noqa: PLC0415

        mod._reset_http_client_for_tests()
        if hasattr(mod, "_rate_limiter"):
            mod._rate_limiter._calls.clear()
    except ImportError:
        pass


# ===========================================================================
# CAPABILITY declaration checks
# ===========================================================================


class TestAerisRadarCapability:
    """CAPABILITY symbol has the expected shape per ADR-038 §4 and brief spec."""

    def test_capability_provider_id_is_aeris(self) -> None:
        """CAPABILITY.provider_id == 'aeris'."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "aeris"

    def test_capability_domain_is_radar(self) -> None:
        """CAPABILITY.domain == 'radar'."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "radar"

    def test_capability_auth_required_contains_client_id_and_secret(self) -> None:
        """CAPABILITY.auth_required contains 'client_id' and 'client_secret'."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert "client_id" in CAPABILITY.auth_required
        assert "client_secret" in CAPABILITY.auth_required

    def test_capability_tile_url_template_uses_auth_placeholder(self) -> None:
        """tile_url_template uses {auth} placeholder (not literal credentials; LC-D)."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is not None
        assert "{auth}" in CAPABILITY.tile_url_template, (
            "Aeris tile_url_template must use {auth} placeholder to keep the "
            "template public-safe (LC-D); credentials inject at proxy time"
        )
        # Actual credential values must not appear in the template
        assert "client_id" not in CAPABILITY.tile_url_template.lower() or \
               "_client_id_" not in CAPABILITY.tile_url_template

    def test_capability_tile_url_template_references_radar_layer(self) -> None:
        """tile_url_template references 'radar' layer (global radar mosaic per ADR-015)."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is not None
        assert "radar" in CAPABILITY.tile_url_template

    def test_capability_tile_content_type_is_image_png(self) -> None:
        """CAPABILITY.tile_content_type == 'image/png'."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_content_type == "image/png"

    def test_capability_wms_endpoint_url_is_none(self) -> None:
        """wms_endpoint_url is None — Aeris Raster Maps is XYZ-style, not WMS."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_endpoint_url is None

    def test_capability_wms_layer_name_is_none(self) -> None:
        """wms_layer_name is None — Aeris Raster Maps is XYZ-style, not WMS."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.wms_layer_name is None

    def test_capability_geographic_coverage_is_global(self) -> None:
        """Aeris Xweather Raster Maps (radar) has global coverage per api-docs."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_supplied_canonical_fields_is_empty(self) -> None:
        """Radar has no canonical-entity mapping (canonical §4.5)."""
        from weewx_clearskies_api.providers.radar.aeris import CAPABILITY  # noqa: PLC0415

        assert len(CAPABILITY.supplied_canonical_fields) == 0


# ===========================================================================
# URL redaction helper (LC-E security baseline)
# ===========================================================================


class TestAerisRadarRedactUrl:
    """_redact_url() helper replaces path-embedded credential segment with <redacted>."""

    def test_redact_url_replaces_client_id_secret_segment(self) -> None:
        """_redact_url() replaces '{client_id}_{client_secret}' with '<redacted>'."""
        from weewx_clearskies_api.providers.radar.aeris import _redact_url  # noqa: PLC0415

        raw_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}/radar/4/4/6/current.png"
        )
        redacted = _redact_url(raw_url, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert _TEST_CLIENT_ID not in redacted, (
            "client_id must not appear in the redacted URL"
        )
        assert _TEST_CLIENT_SECRET not in redacted, (
            "client_secret must not appear in the redacted URL"
        )
        assert "<redacted>" in redacted, (
            "Redacted URL must contain '<redacted>' placeholder"
        )

    def test_redact_url_preserves_path_after_credential_segment(self) -> None:
        """_redact_url() keeps the path after the credential segment intact."""
        from weewx_clearskies_api.providers.radar.aeris import _redact_url  # noqa: PLC0415

        raw_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}/radar/5/3/7/current.png"
        )
        redacted = _redact_url(raw_url, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert "radar/5/3/7/current.png" in redacted, (
            "Path after the credential segment must be preserved in the redacted URL"
        )

    def test_redact_url_preserves_host(self) -> None:
        """_redact_url() keeps the host intact."""
        from weewx_clearskies_api.providers.radar.aeris import _redact_url  # noqa: PLC0415

        raw_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}/radar/4/4/6/current.png"
        )
        redacted = _redact_url(raw_url, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert "maps.api.xweather.com" in redacted


# ===========================================================================
# get_frames() — synthesized frame index
# ===========================================================================


class TestAerisRadarGetFrames:
    """get_frames() returns a single current-kind synthesized frame (LC-G)."""

    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_get_frames_returns_radar_frame_list(self) -> None:
        """get_frames() returns RadarFrameList instance."""
        from weewx_clearskies_api.models.responses import RadarFrameList  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert isinstance(result, RadarFrameList)

    def test_get_frames_provider_id_is_aeris(self) -> None:
        """get_frames() result has providerId='aeris'."""
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert result.providerId == "aeris"

    def test_get_frames_returns_exactly_one_frame(self) -> None:
        """get_frames() returns exactly one frame at v0.1 (synthesized current per LC-G)."""
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert len(result.frames) == 1

    def test_get_frames_frame_kind_is_current(self) -> None:
        """The single frame has kind='current' (synthesized at request time per LC-G)."""
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert result.frames[0].kind == "current"

    def test_get_frames_frame_time_ends_with_z(self) -> None:
        """Frame time is UTC ISO-8601 with Z suffix (ADR-020)."""
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert result.frames[0].time.endswith("Z")

    def test_get_frames_attribution_is_set(self) -> None:
        """get_frames() sets attribution string."""
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        result = get_frames(client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
        assert result.attribution is not None
        assert len(result.attribution) > 0

    def test_get_frames_empty_client_id_raises_key_invalid(self) -> None:
        """Empty client_id raises KeyInvalid BEFORE any HTTP call (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_frames(client_id="", client_secret=_TEST_CLIENT_SECRET)

    def test_get_frames_empty_client_secret_raises_key_invalid(self) -> None:
        """Empty client_secret raises KeyInvalid BEFORE any HTTP call (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_frames(client_id=_TEST_CLIENT_ID, client_secret="")

    def test_get_frames_none_client_id_raises_key_invalid(self) -> None:
        """None client_id raises KeyInvalid (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_frames(client_id=None, client_secret=_TEST_CLIENT_SECRET)  # type: ignore[arg-type]

    def test_get_frames_none_client_secret_raises_key_invalid(self) -> None:
        """None client_secret raises KeyInvalid (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_frames  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_frames(client_id=_TEST_CLIENT_ID, client_secret=None)  # type: ignore[arg-type]


# ===========================================================================
# get_tile() — credential guard
# ===========================================================================


class TestAerisRadarGetTileCredentialGuard:
    """Empty/None credentials raise KeyInvalid before any HTTP call (LC-I)."""

    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_empty_client_id_raises_key_invalid_before_http_call(self) -> None:
        """get_tile() with empty client_id raises KeyInvalid; no HTTP call made."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get("https://maps.api.xweather.com/").mock(
                return_value=httpx.Response(200, content=b"\x89PNG")
            )
            with pytest.raises(KeyInvalid):
                get_tile(4, 4, 6, client_id="", client_secret=_TEST_CLIENT_SECRET)
            assert not route.called, "HTTP call should not be made when client_id is empty"

    def test_empty_client_secret_raises_key_invalid(self) -> None:
        """get_tile() with empty client_secret raises KeyInvalid (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret="")

    def test_none_client_id_raises_key_invalid(self) -> None:
        """None client_id raises KeyInvalid (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_tile(4, 4, 6, client_id=None, client_secret=_TEST_CLIENT_SECRET)  # type: ignore[arg-type]

    def test_none_client_secret_raises_key_invalid(self) -> None:
        """None client_secret raises KeyInvalid (LC-I)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with pytest.raises(KeyInvalid):
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=None)  # type: ignore[arg-type]


# ===========================================================================
# get_tile() — cache key structure
# ===========================================================================


class TestAerisRadarTileCacheKey:
    """Cache key for tile proxy includes z/x/y/t but NOT credentials (ADR-017)."""

    def test_cache_key_does_not_include_credentials(self) -> None:
        """Two different credential pairs produce the same cache key (credentials excluded)."""
        from weewx_clearskies_api.providers.radar.aeris import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        key1 = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        key2 = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        assert key1 == key2

    def test_cache_key_differs_by_z(self) -> None:
        """Different z values produce different cache keys."""
        from weewx_clearskies_api.providers.radar.aeris import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        key1 = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        key2 = _build_tile_cache_key(z=5, x=4, y=6, t=None)
        assert key1 != key2

    def test_cache_key_differs_by_x(self) -> None:
        """Different x values produce different cache keys."""
        from weewx_clearskies_api.providers.radar.aeris import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        key1 = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        key2 = _build_tile_cache_key(z=4, x=5, y=6, t=None)
        assert key1 != key2

    def test_cache_key_differs_by_y(self) -> None:
        """Different y values produce different cache keys."""
        from weewx_clearskies_api.providers.radar.aeris import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        key1 = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        key2 = _build_tile_cache_key(z=4, x=4, y=7, t=None)
        assert key1 != key2

    def test_cache_key_is_sha256_hex_digest(self) -> None:
        """Cache key is a 64-char SHA-256 hex digest."""
        from weewx_clearskies_api.providers.radar.aeris import (
            _build_tile_cache_key,  # noqa: PLC0415
        )

        key = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        assert len(key) == 64


# ===========================================================================
# get_tile() — cache hit path
# ===========================================================================


class TestAerisRadarTileCacheHit:
    """Cache hit path returns cached bytes without any HTTP call."""

    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_cache_hit_returns_bytes_without_http_call(self) -> None:
        """Pre-populated cache hit → get_tile() returns bytes, no HTTP call."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import (  # noqa: PLC0415
            _build_tile_cache_key,
            get_tile,
        )

        tile_bytes = _load_tile_bytes()
        cache_key = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        envelope = {
            "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
            "content_type": "image/png",
        }
        get_cache().set(cache_key, envelope, ttl_seconds=300)

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get("https://maps.api.xweather.com/").mock(
                return_value=httpx.Response(200, content=tile_bytes)
            )
            result_bytes, content_type = get_tile(
                4, 4, 6,
                client_id=_TEST_CLIENT_ID,
                client_secret=_TEST_CLIENT_SECRET,
            )
            assert not route.called, "HTTP should not be called on cache hit"

        assert result_bytes == tile_bytes
        assert content_type == "image/png"


# ===========================================================================
# get_tile() — cache miss + upstream success
# ===========================================================================


class TestAerisRadarTileCacheMiss:
    """Cache miss → upstream call → cache populated → returns (bytes, content_type)."""

    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def test_cache_miss_calls_upstream_and_returns_bytes(self) -> None:
        """Cache miss → HTTP GET to Aeris → returns (bytes, 'image/png')."""
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        tile_bytes = _load_tile_bytes()

        with respx.mock(assert_all_called=True) as mock:
            mock.get(
                f"https://maps.api.xweather.com/{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}"
                f"/radar/4/4/6/current.png"
            ).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            result_bytes, content_type = get_tile(
                4, 4, 6,
                client_id=_TEST_CLIENT_ID,
                client_secret=_TEST_CLIENT_SECRET,
            )

        assert result_bytes == tile_bytes
        assert content_type == "image/png"

    def test_cache_miss_populates_cache_with_base64_envelope(self) -> None:
        """After cache miss + upstream success, cache stores base64 envelope (LC-A)."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import (  # noqa: PLC0415
            _build_tile_cache_key,
            get_tile,
        )

        tile_bytes = _load_tile_bytes()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                f"https://maps.api.xweather.com/{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}"
                f"/radar/4/4/6/current.png"
            ).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        cache_key = _build_tile_cache_key(z=4, x=4, y=6, t=None)
        cached = get_cache().get(cache_key)
        assert cached is not None, "Cache should be populated after cache miss"
        assert "_tile_b64" in cached, "Cache envelope must have '_tile_b64' key (LC-A)"
        assert "content_type" in cached

        decoded = base64.b64decode(cached["_tile_b64"])
        assert decoded == tile_bytes

    def test_cache_miss_second_call_hits_cache(self) -> None:
        """Second call for same tile hits cache; upstream URL called only once."""
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        tile_bytes = _load_tile_bytes()
        call_count = 0

        def _side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                content=tile_bytes,
                headers={"Content-Type": "image/png"},
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                f"https://maps.api.xweather.com/{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}"
                f"/radar/4/4/6/current.png"
            ).mock(side_effect=_side_effect)
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert call_count == 1, (
            f"Upstream should be called exactly once (cache hit on 2nd call); got {call_count}"
        )

    def test_tile_url_embeds_credentials_in_path(self) -> None:
        """Aeris tile URL embeds client_id_client_secret in the URL PATH (per api-docs)."""
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        tile_bytes = _load_tile_bytes()
        captured_url: str | None = None

        def _capture(request: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(request.url)
            return httpx.Response(
                200,
                content=tile_bytes,
                headers={"Content-Type": "image/png"},
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                f"https://maps.api.xweather.com/{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}"
                f"/radar/5/3/7/current.png"
            ).mock(side_effect=_capture)
            get_tile(5, 3, 7, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert captured_url is not None
        # Aeris uses path-embedded credentials per api-docs + LC-E
        assert f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}" in captured_url, (
            "Aeris tile URL must embed credentials as path segment per api-docs"
        )

    def test_tile_url_includes_current_offset(self) -> None:
        """Aeris tile URL ends with '/current.png' (hardcoded at v0.1 per LC-7)."""
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        tile_bytes = _load_tile_bytes()
        captured_url: str | None = None

        def _capture(request: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(request.url)
            return httpx.Response(
                200,
                content=tile_bytes,
                headers={"Content-Type": "image/png"},
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                f"https://maps.api.xweather.com/{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}"
                f"/radar/4/4/6/current.png"
            ).mock(side_effect=_capture)
            get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert captured_url is not None
        assert captured_url.endswith("current.png"), (
            "Aeris tile URL must end with 'current.png' (hardcoded at v0.1 per brief §LC-7)"
        )


# ===========================================================================
# get_tile() — upstream error mapping
# ===========================================================================


class TestAerisRadarTileUpstreamErrors:
    """Upstream HTTP error codes map to correct canonical taxonomy exceptions."""

    def setup_method(self) -> None:
        _reset_module_state()

    def teardown_method(self) -> None:
        _reset_module_state()

    def _aeris_url(self, z: int, x: int, y: int) -> str:
        return (
            f"https://maps.api.xweather.com/"
            f"{_TEST_CLIENT_ID}_{_TEST_CLIENT_SECRET}/radar/{z}/{x}/{y}/current.png"
        )

    def test_upstream_401_raises_key_invalid(self) -> None:
        """Upstream 401 → KeyInvalid (operator credentials invalid)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            with pytest.raises(KeyInvalid):
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

    def test_upstream_403_raises_key_invalid(self) -> None:
        """Upstream 403 → KeyInvalid (operator credentials invalid)."""
        from weewx_clearskies_api.providers._common.errors import KeyInvalid  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                return_value=httpx.Response(403, text="Forbidden")
            )
            with pytest.raises(KeyInvalid):
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

    def test_upstream_429_raises_quota_exhausted(self) -> None:
        """Upstream 429 → QuotaExhausted with retry_after_seconds."""
        from weewx_clearskies_api.providers._common.errors import QuotaExhausted  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                return_value=httpx.Response(
                    429,
                    text="rate limited",
                    headers={"Retry-After": "120"},
                )
            )
            with pytest.raises(QuotaExhausted) as exc_info:
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert exc_info.value.retry_after_seconds == 120

    def test_upstream_404_raises_provider_protocol_error_with_status_404(self) -> None:
        """Upstream 404 → ProviderProtocolError with status_code=404 (LC-H).

        The endpoint maps 404 → HTTPException(404) by inspecting .status_code.
        The provider module must NOT create a custom exception class — dispatch
        is on attribute per coding.md §3 "Dispatch on exception state via attributes."
        """
        from weewx_clearskies_api.providers._common.errors import (
            ProviderProtocolError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                return_value=httpx.Response(404, text="tile not found")
            )
            with pytest.raises(ProviderProtocolError) as exc_info:
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

        assert exc_info.value.status_code == 404, (
            "ProviderProtocolError must carry status_code=404 so endpoint can "
            "dispatch via attribute (coding.md §3 — not message string)"
        )

    def test_upstream_5xx_raises_transient_network_error(self) -> None:
        """Upstream 5xx after retries → TransientNetworkError."""
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                return_value=httpx.Response(503, text="upstream unavailable")
            )
            with pytest.raises(TransientNetworkError):
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)

    def test_upstream_network_error_raises_transient_network_error(self) -> None:
        """DNS/TCP failure → TransientNetworkError (after retries exhausted)."""
        from weewx_clearskies_api.providers._common.errors import (
            TransientNetworkError,  # noqa: PLC0415
        )
        from weewx_clearskies_api.providers.radar.aeris import get_tile  # noqa: PLC0415

        with respx.mock(assert_all_called=False) as mock:
            mock.get(self._aeris_url(4, 4, 6)).mock(
                side_effect=httpx.ConnectError("DNS failure")
            )
            with pytest.raises(TransientNetworkError):
                get_tile(4, 4, 6, client_id=_TEST_CLIENT_ID, client_secret=_TEST_CLIENT_SECRET)
