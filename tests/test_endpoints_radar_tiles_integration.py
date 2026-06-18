"""Integration tests for GET /api/v1/radar/providers/{provider_id}/tiles/{z}/{x}/{y} (3b-15).

Tests the full request → endpoint → provider → response path using respx mocks
for outbound HTTP. FastAPI TestClient exercises the full ASGI stack.

Decision tree per brief §Per-endpoint spec:

  Branch 1 — Unknown provider_id (not in _KEYED_RADAR_PROVIDERS):
    GET /radar/providers/iem_nexrad/tiles/4/4/6 → 404 Problem.
    GET /radar/providers/unknown_xyz/tiles/4/4/6 → 404 Problem.
    (Tile endpoint is keyed-only; keyless providers return 404 here.)

  Branch 2 — Known keyed provider_id but not in registry:
    GET /radar/providers/aeris/tiles/4/4/6 (aeris CAPABILITY not registered) → 404.

  Branch 3 — Missing credentials:
    GET /radar/providers/openweathermap/tiles/4/4/6 (appid not wired) → 502 Problem.
    GET /radar/providers/aeris/tiles/4/4/6 (client_id not wired) → 502 Problem.

  Branch 4 — Cache hit → 200 with Content-Type: image/png and cached bytes.

  Branch 5 — Cache miss + upstream success → 200 + cache populated.

  Branch 6 — Upstream errors:
    Upstream 429 → 503 Problem + Retry-After.
    Upstream 5xx → 502 Problem.
    Upstream 401/403 → 502 Problem (KeyInvalid).
    Upstream 404 → 404 Problem (ProviderProtocolError.status_code == 404 per LC-H).

  Branch 7 — Path parameter validation:
    z out of range (< 0 or > 22) → 422.
    x < 0 or y < 0 → 422.

  Branch 8 — ?t query parameter accepted but logged and ignored (LC-F).

Response shape (200):
  Content-Type: image/png (binary tile bytes, NOT JSON).
  No Pydantic response model — this is one of two non-JSON endpoints (noted in docstring).

ADR references: ADR-015, ADR-017, ADR-018, ADR-037, ADR-038.
OpenAPI contract: docs/contracts/openapi-v1.yaml lines 586-637.
Brief: phase-2-task-3b-15-radar-keyed-A2-brief.md §Per-endpoint spec.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES_BASE = Path(__file__).parent / "fixtures" / "providers" / "radar"
_OWM_TILE_FIXTURE = _FIXTURES_BASE / "openweathermap" / "tile_4_4_6.png"
_AERIS_TILE_FIXTURE = _FIXTURES_BASE / "aeris" / "tile_4_4_6.png"

# Upstream URLs
_OWM_TILE_URL_4_4_6 = "https://tile.openweathermap.org/map/precipitation_new/4/4/6.png"
_AERIS_TILE_URL_4_4_6 = (
    "https://maps.api.xweather.com/test_client_id_test_secret/radar/4/4/6/current.png"
)

_TEST_OWM_APPID = "test_owm_appid_for_tile_proxy"
_TEST_AERIS_CLIENT_ID = "test_client_id"
_TEST_AERIS_CLIENT_SECRET = "test_secret"


def _load_tile_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _reset_all_provider_state() -> None:
    """Reset registry, cache, rate limiters, HTTP clients, and wired credentials."""
    import os  # noqa: PLC0415

    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )

    if cache_url := os.environ.get("CLEARSKIES_CACHE_URL"):
        try:
            import redis as redis_lib  # noqa: PLC0415
            r = redis_lib.from_url(cache_url)
            r.flushdb()
        except Exception:  # noqa: BLE001
            pass

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()

    # Reset provider HTTP clients and rate limiters
    for mod_name in ("openweathermap", "aeris",
                     "rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet", "dwd_radolan"):
        try:
            mod = __import__(
                f"weewx_clearskies_api.providers.radar.{mod_name}",
                fromlist=["_reset_http_client_for_tests", "_rate_limiter"],
            )
            if hasattr(mod, "_reset_http_client_for_tests"):
                mod._reset_http_client_for_tests()
            if hasattr(mod, "_rate_limiter"):
                mod._rate_limiter._calls.clear()
        except ImportError:
            pass

    # Reset radar endpoint credential vars
    try:
        from weewx_clearskies_api.endpoints import radar as _radar_endpoint  # noqa: PLC0415
        _radar_endpoint._RADAR_AERIS_CLIENT_ID = None
        _radar_endpoint._RADAR_AERIS_CLIENT_SECRET = None
        _radar_endpoint._RADAR_OWM_APPID = None
    except (ImportError, AttributeError):
        pass


def _make_tile_app(
    provider: str | None = None,
    wire_credentials: bool = False,
) -> FastAPI:
    """Build a test FastAPI app for the tile proxy endpoint.

    provider: 'aeris' | 'openweathermap' | None.
      When None, no keyed radar CAPABILITY is registered (simulates branch 2).
    wire_credentials: if True, wires test credentials into the endpoint module.
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RadarSettings,
        Settings,
    )
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415

    _reset_all_provider_state()

    capabilities = []
    if provider:
        mod = __import__(
            f"weewx_clearskies_api.providers.radar.{provider}",
            fromlist=["CAPABILITY"],
        )
        capabilities = [mod.CAPABILITY]

    wire_providers(capabilities)

    # Wire credentials into the endpoint module if requested
    if wire_credentials and provider:
        from weewx_clearskies_api.endpoints import radar as _radar_endpoint  # noqa: PLC0415
        if provider == "openweathermap":
            _radar_endpoint._RADAR_OWM_APPID = _TEST_OWM_APPID
        elif provider == "aeris":
            _radar_endpoint._RADAR_AERIS_CLIENT_ID = _TEST_AERIS_CLIENT_ID
            _radar_endpoint._RADAR_AERIS_CLIENT_SECRET = _TEST_AERIS_CLIENT_SECRET

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
        radar=RadarSettings({"provider": provider} if provider else {}),
    )
    return create_app(settings)


# ===========================================================================
# Branch 1 — Unknown provider_id → 404 (not a keyed provider)
# ===========================================================================


class TestBranch1UnknownOrKeylessProvider:
    """provider_id not in _KEYED_RADAR_PROVIDERS → 404 (tile endpoint is keyed-only)."""

    def test_keyless_provider_iem_nexrad_returns_404_at_tiles_endpoint(self) -> None:
        """iem_nexrad is keyless — /tiles endpoint does NOT serve it → 404."""
        app = _make_tile_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/iem_nexrad/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"Keyless provider iem_nexrad at /tiles should return 404; "
            f"got {response.status_code}: {response.text[:200]}"
        )

    def test_keyless_provider_rainviewer_returns_404_at_tiles_endpoint(self) -> None:
        """rainviewer is keyless — /tiles endpoint does NOT serve it → 404."""
        app = _make_tile_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/rainviewer/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404

    def test_completely_unknown_provider_returns_404(self) -> None:
        """Completely unknown provider_id → 404."""
        app = _make_tile_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/nonexistent_xyz/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# Branch 2 — Known keyed provider_id but not in registry → 404
# ===========================================================================


class TestBranch2KeyedProviderNotInRegistry:
    """Keyed provider_id in _KEYED_RADAR_PROVIDERS but CAPABILITY not registered → 404."""

    def test_openweathermap_not_registered_returns_404(self) -> None:
        """openweathermap in keyed frozenset but no CAPABILITY registered → 404."""
        # Make app with no provider registered
        app = _make_tile_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"Expected 404 when openweathermap not in registry; "
            f"got {response.status_code}: {response.text[:200]}"
        )

    def test_aeris_not_registered_returns_404(self) -> None:
        """aeris in keyed frozenset but no CAPABILITY registered → 404."""
        app = _make_tile_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404

    def test_aeris_registered_but_owm_requested_returns_404(self) -> None:
        """aeris registered but openweathermap requested → 404 (provider mismatch)."""
        # Register aeris only; request openweathermap
        app = _make_tile_app(provider="aeris", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"openweathermap not in registry (aeris is); expected 404; "
            f"got {response.status_code}: {response.text[:200]}"
        )


# ===========================================================================
# Branch 3 — Missing credentials → 502
# ===========================================================================


class TestBranch3MissingCredentials:
    """Keyed provider registered but credentials not wired → 502."""

    def test_owm_missing_appid_returns_502(self) -> None:
        """openweathermap CAPABILITY registered but appid not wired → 502."""
        # Register openweathermap capability but do NOT wire credentials
        app = _make_tile_app(provider="openweathermap", wire_credentials=False)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 502, (
            f"Missing OWM appid should return 502; got {response.status_code}: {response.text[:200]}"
        )

    def test_aeris_missing_client_id_returns_502(self) -> None:
        """aeris CAPABILITY registered but client_id not wired → 502."""
        app = _make_tile_app(provider="aeris", wire_credentials=False)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 502, (
            f"Missing Aeris credentials should return 502; "
            f"got {response.status_code}: {response.text[:200]}"
        )

    def test_missing_credentials_response_is_problem_json(self) -> None:
        """Missing credentials → RFC 9457 problem+json body (ADR-018)."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=False)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 502
        # RFC 9457: body should have "type", "status", "detail" fields
        body = response.json()
        assert "status" in body or "type" in body or "detail" in body


# ===========================================================================
# Branch 4 — Cache hit → 200 with cached bytes
# ===========================================================================


class TestBranch4CacheHit:
    """Pre-populated cache hit → 200 with Content-Type: image/png and cached bytes."""

    def test_owm_cache_hit_returns_200_with_image_png(self) -> None:
        """OWM cache pre-populated → /tiles returns 200 image/png without HTTP call."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.openweathermap import (  # noqa: PLC0415
            _build_tile_cache_key,
        )

        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        cache_key = _build_tile_cache_key(z=4, x=4, y=6, t=None)

        app = _make_tile_app(provider="openweathermap", wire_credentials=True)

        # Pre-populate cache with base64 envelope (LC-A format)
        envelope = {
            "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
            "content_type": "image/png",
        }
        get_cache().set(cache_key, envelope, ttl_seconds=300)

        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get("https://tile.openweathermap.org/").mock(
                return_value=httpx.Response(200, content=tile_bytes)
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")
            assert not route.called, "HTTP should not be called on cache hit"

        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("image/png")
        assert response.content == tile_bytes

    def test_aeris_cache_hit_returns_200_with_image_png(self) -> None:
        """Aeris cache pre-populated → /tiles returns 200 image/png without HTTP call."""
        from weewx_clearskies_api.providers._common.cache import get_cache  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.aeris import (  # noqa: PLC0415
            _build_tile_cache_key,
        )

        tile_bytes = _load_tile_bytes(_AERIS_TILE_FIXTURE)
        cache_key = _build_tile_cache_key(z=4, x=4, y=6, t=None)

        app = _make_tile_app(provider="aeris", wire_credentials=True)

        envelope = {
            "_tile_b64": base64.b64encode(tile_bytes).decode("ascii"),
            "content_type": "image/png",
        }
        get_cache().set(cache_key, envelope, ttl_seconds=300)

        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get("https://maps.api.xweather.com/").mock(
                return_value=httpx.Response(200, content=tile_bytes)
            )
            response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")
            assert not route.called

        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.content == tile_bytes


# ===========================================================================
# Branch 5 — Cache miss + upstream success → 200
# ===========================================================================


class TestBranch5CacheMissUpstreamSuccess:
    """Cache miss + upstream success → 200 with tile bytes + cache populated."""

    def test_owm_cache_miss_returns_200(self) -> None:
        """OWM: cache miss → upstream call → 200 image/png response."""
        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=True) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:200]}"
        )
        assert response.headers.get("content-type", "").startswith("image/png"), (
            f"Expected image/png; got {response.headers.get('content-type')!r}"
        )

    def test_owm_cache_miss_response_body_is_tile_bytes(self) -> None:
        """OWM cache miss → response body is the raw tile bytes (not JSON)."""
        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.content == tile_bytes, "Response body must be raw tile bytes, not JSON"

    def test_aeris_cache_miss_returns_200(self) -> None:
        """Aeris: cache miss → upstream call → 200 image/png response."""
        tile_bytes = _load_tile_bytes(_AERIS_TILE_FIXTURE)
        app = _make_tile_app(provider="aeris", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        aeris_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_AERIS_CLIENT_ID}_{_TEST_AERIS_CLIENT_SECRET}/radar/4/4/6/current.png"
        )

        with respx.mock(assert_all_called=True) as mock:
            mock.get(aeris_url).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("image/png")

    def test_owm_t_query_param_accepted_and_returns_200(self) -> None:
        """?t query param accepted (not rejected with 422) and returns 200 (LC-F ignored at v0.1)."""
        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            # ?t is an ISO datetime — accepted but ignored per LC-F
            response = client.get(
                "/api/v1/radar/providers/openweathermap/tiles/4/4/6"
                "?t=2026-05-11T12:00:00Z"
            )

        _reset_all_provider_state()
        # Must not return 422 (param should be accepted)
        assert response.status_code != 422, (
            "?t parameter must be accepted (not rejected with 422); per LC-F"
        )
        assert response.status_code == 200


# ===========================================================================
# Branch 6 — Upstream errors → 502/503
# ===========================================================================


class TestBranch6UpstreamErrors:
    """Upstream errors propagate as RFC 9457 problem+json per ADR-018."""

    def test_owm_upstream_429_returns_503(self) -> None:
        """OWM upstream 429 → endpoint 503 (QuotaExhausted) per ADR-018."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 503, (
            f"OWM 429 should produce 503; got {response.status_code}"
        )

    def test_owm_upstream_5xx_returns_502(self) -> None:
        """OWM upstream 5xx → endpoint 502 (TransientNetworkError) per ADR-018."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(503, text="upstream down")
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 502

    def test_owm_upstream_401_returns_502(self) -> None:
        """OWM upstream 401 → endpoint 502 (KeyInvalid) per ADR-018."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 502

    def test_owm_upstream_404_returns_404(self) -> None:
        """OWM upstream 404 (tile out of domain) → endpoint 404 (LC-H mapping).

        ProviderProtocolError.status_code == 404 → endpoint maps to 404 HTTPException.
        """
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_OWM_TILE_URL_4_4_6).mock(
                return_value=httpx.Response(404, text="tile not found")
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"Upstream 404 (tile out of domain) should produce 404; "
            f"got {response.status_code} — LC-H: endpoint maps ProviderProtocolError.status_code==404"
        )

    def test_aeris_upstream_429_returns_503(self) -> None:
        """Aeris upstream 429 → endpoint 503 (QuotaExhausted)."""
        app = _make_tile_app(provider="aeris", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        aeris_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_AERIS_CLIENT_ID}_{_TEST_AERIS_CLIENT_SECRET}/radar/4/4/6/current.png"
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.get(aeris_url).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 503

    def test_aeris_upstream_404_returns_404(self) -> None:
        """Aeris upstream 404 → endpoint 404 (LC-H: ProviderProtocolError.status_code==404)."""
        app = _make_tile_app(provider="aeris", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        aeris_url = (
            f"https://maps.api.xweather.com/"
            f"{_TEST_AERIS_CLIENT_ID}_{_TEST_AERIS_CLIENT_SECRET}/radar/4/4/6/current.png"
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.get(aeris_url).mock(
                return_value=httpx.Response(404, text="tile not found")
            )
            response = client.get("/api/v1/radar/providers/aeris/tiles/4/4/6")

        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# Branch 7 — Path parameter validation (FastAPI auto-422)
# ===========================================================================


class TestBranch7PathParameterValidation:
    """z/x/y path parameters validated by FastAPI → 422 on bad input."""

    def test_z_above_22_returns_422(self) -> None:
        """z=23 is out of range (max 22 per OpenAPI) → 422."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/23/4/6")
        _reset_all_provider_state()
        assert response.status_code == 422, (
            f"z=23 should return 422; got {response.status_code}"
        )

    def test_z_negative_returns_422(self) -> None:
        """z=-1 is negative → 422."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/-1/4/6")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_x_negative_returns_422(self) -> None:
        """x=-1 → 422."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/-1/6")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_y_negative_returns_422(self) -> None:
        """y=-1 → 422."""
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/tiles/4/4/-1")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_valid_z_0_is_accepted(self) -> None:
        """z=0 (minimum valid) is accepted (not 422)."""
        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://tile.openweathermap.org/map/precipitation_new/0/0/0.png").mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/0/0/0")

        _reset_all_provider_state()
        assert response.status_code != 422, f"z=0 should be valid; got {response.status_code}"

    def test_valid_z_22_is_accepted(self) -> None:
        """z=22 (maximum valid per OpenAPI) is accepted (not 422)."""
        tile_bytes = _load_tile_bytes(_OWM_TILE_FIXTURE)
        app = _make_tile_app(provider="openweathermap", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                "https://tile.openweathermap.org/map/precipitation_new/22/0/0.png"
            ).mock(
                return_value=httpx.Response(
                    200,
                    content=tile_bytes,
                    headers={"Content-Type": "image/png"},
                )
            )
            response = client.get("/api/v1/radar/providers/openweathermap/tiles/22/0/0")

        _reset_all_provider_state()
        assert response.status_code != 422


# ===========================================================================
# Query-param hardening — extra="forbid" on /tiles and valid ?t param (3b-16)
# ===========================================================================


def _assert_problem_json_tiles(response: object) -> None:
    """Assert RFC 9457 problem+json shape (status field required)."""
    content_type = response.headers.get("content-type", "")  # type: ignore[attr-defined]
    assert "json" in content_type, (
        f"Expected JSON content-type for error response, got {content_type!r}"
    )
    body = response.json()  # type: ignore[attr-defined]
    assert "status" in body, f"Problem response must have 'status'; got {body}"


class TestTilesQueryParamHardening:
    """GET /tiles rejects unknown query params but accepts valid ?t (extra='forbid' on RadarTilesQueryParams).

    The endpoint wires a Pydantic model via Depends() so any unrecognised query
    key is rejected before handler logic runs.  The only accepted param is the
    optional timestamp hint ?t (logged and ignored at v0.1 per LC-F).
    """

    def test_radar_tiles_unknown_query_param_returns_400_or_422(self) -> None:
        """Unknown query param on /tiles → 400 or 422 problem+json (extra='forbid')."""
        app = _make_tile_app(provider="aeris", wire_credentials=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/api/v1/radar/providers/aeris/tiles/5/16/11",
            params={"totally_unknown_param": "yes"},
        )
        _reset_all_provider_state()
        assert response.status_code in (400, 422), (
            f"Expected 400/422 for unknown query param on /tiles, "
            f"got {response.status_code}: {response.text[:300]}"
        )
        _assert_problem_json_tiles(response)

    def test_radar_tiles_valid_t_param_accepted(self) -> None:
        """?t=<unix-timestamp> on /tiles is accepted (not rejected as unknown).

        The endpoint may return 404 or 502 because aeris credentials are not
        configured in the test environment — that is expected and acceptable.
        The assertion is that ?t does NOT produce 400 or 422 (param must be accepted).
        """
        app = _make_tile_app(provider="aeris", wire_credentials=False)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/api/v1/radar/providers/aeris/tiles/5/16/11",
            params={"t": "1234567890"},
        )
        _reset_all_provider_state()
        assert response.status_code not in (400, 422), (
            f"Valid ?t query param must be accepted (not 400/422); "
            f"got {response.status_code}: {response.text[:300]}"
        )
