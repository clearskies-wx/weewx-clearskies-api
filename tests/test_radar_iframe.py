"""Unit + endpoint tests for providers/radar/iframe.py (3b-16).

Covers per the task-3b-16 brief:

  make_capability() shape:
  - Returns a ProviderCapability with provider_id='iframe', domain='radar'.
  - iframe_url matches the input URL.
  - Tile/WMS fields (tile_url_template, wms_endpoint_url, wms_layer_name,
    tile_content_type) are all None — iframe has no tile API.
  - auth_required is an empty tuple (no credentials needed).
  - default_poll_interval_seconds == 0 (static URL; no polling).

  RadarSettings validation:
  - provider='iframe' with iframe_url set passes validate().
  - provider='iframe' with no iframe_url raises ValueError.

  Endpoint behaviour:
  - GET /radar/providers/iframe/frames with iframe capability registered
    → 501 (iframe has no frame index; dashboard reads iframe_url from
    /capabilities directly).
  - GET /radar/providers/iframe/tiles/5/10/10 → 404 (iframe is not a
    keyed provider; tile proxy is keyed-only).

  Dispatch table:
  - ('radar', 'iframe') key exists in PROVIDER_MODULES.

No DB, no live network, no respx mocking needed — iframe makes no outbound
calls at all.

ADR references: ADR-015, ADR-037, ADR-038.
Brief: phase-2-task-3b-16-radar-iframe.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_all_provider_state() -> None:
    """Reset registry and cache between endpoint tests."""
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


def _make_iframe_app(register_capability: bool = True) -> "FastAPI":
    """Build a test FastAPI app with the radar router.

    register_capability: if True, registers an iframe ProviderCapability in
    the registry (simulates operator having configured provider='iframe').
    """
    from fastapi import FastAPI  # noqa: PLC0415

    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RadarSettings,
        RateLimitSettings,
        Settings,
    )
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415
    from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

    _reset_all_provider_state()

    capabilities = []
    if register_capability:
        capabilities = [make_capability(iframe_url="https://example.com/radar")]

    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        radar=RadarSettings(
            {"provider": "iframe", "iframe_url": "https://example.com/radar"}
            if register_capability
            else {}
        ),
    )
    return create_app(settings)


# ===========================================================================
# 1. make_capability() — ProviderCapability shape
# ===========================================================================


class TestMakeCapabilityShape:
    """make_capability() returns a correctly populated ProviderCapability."""

    def test_make_capability_returns_provider_capability(self) -> None:
        """make_capability() returns a ProviderCapability instance."""
        from weewx_clearskies_api.providers._common.capability import ProviderCapability  # noqa: PLC0415
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert isinstance(cap, ProviderCapability), (
            f"make_capability() must return ProviderCapability, got {type(cap).__name__}"
        )

    def test_make_capability_provider_id_is_iframe(self) -> None:
        """make_capability() returns capability with provider_id='iframe'."""
        from weewx_clearskies_api.providers.radar.iframe import PROVIDER_ID, make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.provider_id == "iframe", (
            f"Expected provider_id='iframe', got {cap.provider_id!r}"
        )
        assert cap.provider_id == PROVIDER_ID, (
            f"provider_id must match module-level PROVIDER_ID constant; "
            f"got {cap.provider_id!r} vs {PROVIDER_ID!r}"
        )

    def test_make_capability_domain_is_radar(self) -> None:
        """make_capability() returns capability with domain='radar'."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.domain == "radar", (
            f"Expected domain='radar', got {cap.domain!r}"
        )

    def test_make_capability_iframe_url_populated(self) -> None:
        """make_capability() returns capability with iframe_url matching input URL."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        url = "https://www.bom.gov.au/products/national_radar_sat.loop.shtml"
        cap = make_capability(iframe_url=url)
        assert cap.iframe_url == url, (
            f"Expected iframe_url={url!r}, got {cap.iframe_url!r}"
        )

    def test_make_capability_no_tile_url_template(self) -> None:
        """make_capability() returns capability with tile_url_template=None (no tile API)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.tile_url_template is None, (
            f"iframe has no tile API; tile_url_template must be None, "
            f"got {cap.tile_url_template!r}"
        )

    def test_make_capability_no_wms_endpoint_url(self) -> None:
        """make_capability() returns capability with wms_endpoint_url=None (no WMS)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.wms_endpoint_url is None, (
            f"iframe has no WMS; wms_endpoint_url must be None, "
            f"got {cap.wms_endpoint_url!r}"
        )

    def test_make_capability_no_wms_layer_name(self) -> None:
        """make_capability() returns capability with wms_layer_name=None (no WMS layer)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.wms_layer_name is None, (
            f"iframe has no WMS layer; wms_layer_name must be None, "
            f"got {cap.wms_layer_name!r}"
        )

    def test_make_capability_no_tile_content_type(self) -> None:
        """make_capability() returns capability with tile_content_type=None (no tile bytes)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.tile_content_type is None, (
            f"iframe delivers no tile bytes; tile_content_type must be None, "
            f"got {cap.tile_content_type!r}"
        )

    def test_make_capability_no_auth_required(self) -> None:
        """make_capability() returns capability with auth_required=() (no credentials)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.auth_required == (), (
            f"iframe requires no credentials; auth_required must be (), "
            f"got {cap.auth_required!r}"
        )

    def test_make_capability_no_polling(self) -> None:
        """make_capability() returns capability with default_poll_interval_seconds=0 (static URL)."""
        from weewx_clearskies_api.providers.radar.iframe import make_capability  # noqa: PLC0415

        cap = make_capability(iframe_url="https://example.com/radar")
        assert cap.default_poll_interval_seconds == 0, (
            f"iframe is a static URL config slot; poll interval must be 0, "
            f"got {cap.default_poll_interval_seconds!r}"
        )


# ===========================================================================
# 2. RadarSettings validation
# ===========================================================================


class TestRadarSettingsIframeValidation:
    """RadarSettings.validate() correctly handles provider='iframe'."""

    def test_radar_settings_accepts_iframe_provider_with_url(self) -> None:
        """provider='iframe' with iframe_url set passes validate() without error."""
        from weewx_clearskies_api.config.settings import RadarSettings  # noqa: PLC0415

        s = RadarSettings({"provider": "iframe", "iframe_url": "https://example.com/radar"})
        s.validate()  # must not raise

    def test_radar_settings_iframe_requires_url(self) -> None:
        """provider='iframe' without iframe_url raises ValueError at validate()."""
        from weewx_clearskies_api.config.settings import RadarSettings  # noqa: PLC0415

        s = RadarSettings({"provider": "iframe"})
        with pytest.raises(ValueError, match="iframe_url"):
            s.validate()

    def test_radar_settings_iframe_requires_url_whitespace_treated_as_missing(self) -> None:
        """provider='iframe' with whitespace-only iframe_url raises ValueError."""
        from weewx_clearskies_api.config.settings import RadarSettings  # noqa: PLC0415

        s = RadarSettings({"provider": "iframe", "iframe_url": "   "})
        with pytest.raises(ValueError, match="iframe_url"):
            s.validate()


# ===========================================================================
# 3. Endpoint behaviour
# ===========================================================================


class TestFramesEndpointReturns501ForIframe:
    """GET /radar/providers/iframe/frames → 501 (iframe has no frame index)."""

    def test_frames_endpoint_returns_501_for_iframe(self) -> None:
        """iframe capability registered → GET /frames → 501 Not Implemented."""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        app = _make_iframe_app(register_capability=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/iframe/frames")
        _reset_all_provider_state()
        assert response.status_code == 501, (
            f"iframe /frames must return 501 (no frame index); "
            f"got {response.status_code}: {response.text[:300]}"
        )

    def test_frames_endpoint_501_is_problem_json(self) -> None:
        """GET /radar/providers/iframe/frames → 501 response is RFC 9457 problem+json.

        Note: The global error handler masks 5xx detail messages for security
        (errors.py line 169-177). The 501 status code itself is the contract;
        the detail content is sanitised by the handler. Asserting status+type
        field rather than the literal detail from the endpoint.
        """
        from fastapi.testclient import TestClient  # noqa: PLC0415

        app = _make_iframe_app(register_capability=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/iframe/frames")
        _reset_all_provider_state()
        assert response.status_code == 501
        body = response.json()
        # RFC 9457 problem+json shape: must have "status" field matching HTTP status
        assert body.get("status") == 501, (
            f"Problem+json body must have status=501; got {body!r}"
        )
        # Must have a "detail" or "title" field (not empty response)
        assert "detail" in body or "title" in body, (
            f"Problem+json body must have detail or title; got {body!r}"
        )


class TestTilesEndpointReturns404ForIframe:
    """GET /radar/providers/iframe/tiles/... → 404 (iframe is not a keyed provider)."""

    def test_tiles_endpoint_returns_404_for_iframe(self) -> None:
        """iframe is not a keyed provider → tile proxy returns 404."""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        app = _make_iframe_app(register_capability=True)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/iframe/tiles/5/10/10")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"iframe /tiles must return 404 (keyed-only endpoint); "
            f"got {response.status_code}: {response.text[:300]}"
        )


# ===========================================================================
# 4. Dispatch table
# ===========================================================================


class TestDispatchTableIncludesIframe:
    """PROVIDER_MODULES dispatch table contains the ('radar', 'iframe') key."""

    def test_dispatch_table_includes_iframe(self) -> None:
        """('radar', 'iframe') is a key in PROVIDER_MODULES dispatch table."""
        from weewx_clearskies_api.providers._common.dispatch import PROVIDER_MODULES  # noqa: PLC0415

        assert ("radar", "iframe") in PROVIDER_MODULES, (
            "PROVIDER_MODULES must contain ('radar', 'iframe') for the iframe config-slot provider"
        )

    def test_dispatch_table_iframe_module_has_make_capability(self) -> None:
        """The module at ('radar', 'iframe') in PROVIDER_MODULES exposes make_capability."""
        from weewx_clearskies_api.providers._common.dispatch import PROVIDER_MODULES  # noqa: PLC0415

        mod = PROVIDER_MODULES[("radar", "iframe")]
        assert hasattr(mod, "make_capability"), (
            "iframe module in dispatch table must export make_capability(); "
            f"got attributes: {[a for a in dir(mod) if not a.startswith('_')]}"
        )

    def test_dispatch_table_iframe_module_has_no_get_frames(self) -> None:
        """The iframe module correctly has NO get_frames() (it is a config slot, not a frame provider)."""
        from weewx_clearskies_api.providers._common.dispatch import PROVIDER_MODULES  # noqa: PLC0415

        mod = PROVIDER_MODULES[("radar", "iframe")]
        assert not hasattr(mod, "get_frames"), (
            "iframe module must NOT export get_frames(); "
            "iframe is a config-slot, not a frame-index provider. "
            "Exposing get_frames() would mislead callers and break the 501 guard."
        )
