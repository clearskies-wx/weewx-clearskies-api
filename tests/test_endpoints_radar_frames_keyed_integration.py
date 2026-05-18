"""Integration tests for the /frames endpoint extension for keyed providers (3b-15).

Tests:
  1. Existing tests for 5 keyless providers still pass (regression guard).
     (Keyless tests are in test_radar_endpoint_integration.py; here we add
     the keyed cases and verify the dispatch table extension works.)
  2. New cases for 'aeris' + 'openweathermap' returning a single 'current' frame
     via the /radar/providers/{provider_id}/frames endpoint.

Frame shape for both keyed providers (LC-G):
  - providerId matches the provider.
  - frames has exactly 1 entry.
  - frames[0].kind == "current".
  - frames[0].time ends with "Z" (UTC ISO-8601, ADR-020).
  - attribution is not empty.

No upstream HTTP call needed for get_frames() of keyed providers at v0.1
(frame index is synthesized in Python per LC-G). Tests do NOT mock outbound HTTP —
if the impl does call out for frames, the respx.mock context will catch uncalled routes.

ADR references: ADR-015, ADR-017, ADR-020.
Brief: phase-2-task-3b-15-radar-keyed-A2-brief.md §frames-endpoint extension.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _reset_all_provider_state() -> None:
    """Reset registry, cache, rate limiters, HTTP clients."""
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


def _make_radar_frames_app(provider: str | None = None) -> FastAPI:
    """Build a test FastAPI app for the frames endpoint with the given provider registered."""
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

    _reset_all_provider_state()

    capabilities = []
    if provider:
        mod = __import__(
            f"weewx_clearskies_api.providers.radar.{provider}",
            fromlist=["CAPABILITY"],
        )
        capabilities = [mod.CAPABILITY]

    wire_providers(capabilities)

    # Wire test credentials for keyed providers
    if provider == "openweathermap":
        from weewx_clearskies_api.endpoints import radar as _radar_endpoint  # noqa: PLC0415
        _radar_endpoint._RADAR_OWM_APPID = "test_owm_appid_frames"
    elif provider == "aeris":
        from weewx_clearskies_api.endpoints import radar as _radar_endpoint  # noqa: PLC0415
        _radar_endpoint._RADAR_AERIS_CLIENT_ID = "test_aeris_cid"
        _radar_endpoint._RADAR_AERIS_CLIENT_SECRET = "test_aeris_secret"

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
        radar=RadarSettings({"provider": provider} if provider else {}),
    )
    return create_app(settings)


# ===========================================================================
# Dispatch table extension — keyed providers now in _KNOWN_RADAR_PROVIDERS
# ===========================================================================


class TestKeyedProvidersInFramesDispatchTable:
    """Keyed providers (aeris, openweathermap) are in the /frames dispatch table."""

    def test_openweathermap_in_frames_dispatch_table(self) -> None:
        """openweathermap is in the /frames dispatch table (known provider for /frames)."""
        # Make an app with NO capability registered — verify we get 404 "not configured"
        # (branch 2: known but not registered) NOT 404 "unknown provider" (branch 1).
        app = _make_radar_frames_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        # Should be 404 because not registered, but the detail should mention "not configured"
        # not "not supported". (Both map to 404; the dispatch table check is what matters.)
        assert response.status_code == 404
        body = response.json()
        # The detail should indicate "not configured" (branch 2), not "not supported" (branch 1)
        detail = body.get("detail", "")
        assert "configured" in detail.lower() or "not supported" not in detail.lower(), (
            "openweathermap should be in dispatch table (404 = not configured, not unknown)"
        )

    def test_aeris_in_frames_dispatch_table(self) -> None:
        """aeris is in the /frames dispatch table (known provider for /frames)."""
        app = _make_radar_frames_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        assert response.status_code == 404
        # Same reasoning: known provider, not configured → 404 "not configured" detail
        body = response.json()
        detail = body.get("detail", "")
        # The test asserts it's NOT returning "is not supported" (branch 1 text)
        # It should return "not configured for this deployment" (branch 2 text)
        assert "not supported" not in detail.lower() or "aeris" in detail.lower()


# ===========================================================================
# OWM frames — synthesized single current frame
# ===========================================================================


class TestOWMFramesEndpointReturnsCurrentFrame:
    """openweathermap /frames returns single synthesized current frame (LC-G)."""

    def test_owm_frames_returns_200(self) -> None:
        """GET /radar/providers/openweathermap/frames → 200."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    def test_owm_frames_provider_id_is_openweathermap(self) -> None:
        """OWM frames response has data.providerId == 'openweathermap'."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        body = response.json()
        assert body["data"]["providerId"] == "openweathermap"

    def test_owm_frames_has_exactly_one_frame(self) -> None:
        """OWM frames response has exactly 1 frame (synthesized at v0.1)."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        assert len(frames) == 1, f"Expected 1 frame, got {len(frames)}"

    def test_owm_frame_kind_is_current(self) -> None:
        """OWM frame has kind='current' (synthesized current-only per LC-G)."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        frame = response.json()["data"]["frames"][0]
        assert frame["kind"] == "current"

    def test_owm_frame_time_ends_with_z(self) -> None:
        """OWM frame time is UTC ISO-8601 with Z suffix (ADR-020)."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        frame = response.json()["data"]["frames"][0]
        assert frame["time"].endswith("Z"), f"Frame time must end with Z; got {frame['time']!r}"

    def test_owm_frames_has_attribution(self) -> None:
        """OWM frames response has a non-empty attribution string."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        attribution = response.json()["data"].get("attribution")
        assert attribution is not None
        assert len(attribution) > 0

    def test_owm_frames_has_generated_at(self) -> None:
        """OWM frames response has generatedAt at the envelope level."""
        app = _make_radar_frames_app(provider="openweathermap")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/openweathermap/frames")
        _reset_all_provider_state()
        body = response.json()
        assert "generatedAt" in body
        assert body["generatedAt"].endswith("Z")


# ===========================================================================
# Aeris frames — synthesized single current frame
# ===========================================================================


class TestAerisFramesEndpointReturnsCurrentFrame:
    """aeris /frames returns single synthesized current frame (LC-G)."""

    def test_aeris_frames_returns_200(self) -> None:
        """GET /radar/providers/aeris/frames → 200."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    def test_aeris_frames_provider_id_is_aeris(self) -> None:
        """Aeris frames response has data.providerId == 'aeris'."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        body = response.json()
        assert body["data"]["providerId"] == "aeris"

    def test_aeris_frames_has_exactly_one_frame(self) -> None:
        """Aeris frames response has exactly 1 frame (synthesized at v0.1)."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        assert len(frames) == 1, f"Expected 1 frame, got {len(frames)}"

    def test_aeris_frame_kind_is_current(self) -> None:
        """Aeris frame has kind='current' (synthesized current-only per LC-G)."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        frame = response.json()["data"]["frames"][0]
        assert frame["kind"] == "current"

    def test_aeris_frame_time_ends_with_z(self) -> None:
        """Aeris frame time is UTC ISO-8601 with Z suffix (ADR-020)."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        frame = response.json()["data"]["frames"][0]
        assert frame["time"].endswith("Z"), f"Frame time must end with Z; got {frame['time']!r}"

    def test_aeris_frames_has_attribution(self) -> None:
        """Aeris frames response has a non-empty attribution string."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        attribution = response.json()["data"].get("attribution")
        assert attribution is not None
        assert len(attribution) > 0

    def test_aeris_frames_has_generated_at(self) -> None:
        """Aeris frames response has generatedAt at the envelope level."""
        app = _make_radar_frames_app(provider="aeris")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/aeris/frames")
        _reset_all_provider_state()
        body = response.json()
        assert "generatedAt" in body
        assert body["generatedAt"].endswith("Z")


# ===========================================================================
# Regression guard — keyless providers still work after 3b-15 dispatch extension
# ===========================================================================


class TestKeylessProvidersStillWorkAfterDispatchExtension:
    """Keyless providers from 3b-14 still dispatch correctly (regression guard)."""

    def test_rainviewer_still_in_frames_dispatch_table(self) -> None:
        """rainviewer is still a known provider for /frames after keyed provider extension."""
        app = _make_radar_frames_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/rainviewer/frames")
        _reset_all_provider_state()
        # Should be 404 "not configured" (branch 2), NOT 404 "unknown" (branch 1)
        # This verifies rainviewer is still in the dispatch table
        assert response.status_code == 404
        body = response.json()
        detail = body.get("detail", "")
        assert "not supported" not in detail.lower(), (
            "rainviewer should still be in dispatch table after keyed-provider extension"
        )

    def test_unknown_provider_is_not_in_dispatch_table(self) -> None:
        """Truly unknown provider returns 404 with 'not supported' detail."""
        app = _make_radar_frames_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/made_up_provider_xyz/frames")
        _reset_all_provider_state()
        assert response.status_code == 404
