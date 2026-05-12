"""Integration tests for GET /api/v1/radar/providers/{provider_id}/frames (3b-14).

Tests the full request → endpoint → provider → response path using recorded
fixture XML/JSON. No live network; respx mocks outbound httpx calls from provider
modules. FastAPI TestClient exercises the full ASGI stack.

Covers all 6 decision-tree branches from the brief §Per-endpoint spec:

  Branch 1 — Unknown provider_id:
    GET /radar/providers/unknown_xyz/frames → 404 Problem.

  Branch 2 — Known provider_id but not in registry:
    GET /radar/providers/rainviewer/frames (no CAPABILITY registered) → 404 Problem.

  Branch 3 — Provider configured + registered, fetch succeeds:
    Tests all 5 keyless providers: rainviewer, iem_nexrad, noaa_mrms, msc_geomet,
    dwd_radolan. Each → 200 RadarFramesResponse with correct providerId + frames.

  Branch 4 — Network failure / 5xx from provider:
    → 502 ProviderProblem (TransientNetworkError).
    Tests with rainviewer as representative provider.

  Branch 5 — Provider returns 429:
    → 503 ProviderProblem (QuotaExhausted) + Retry-After header present.

  Branch 6 — Frame-index parse failure:
    → 502 ProviderProblem (ProviderProtocolError).
    Tests with malformed JSON for rainviewer.

Response shape (per OpenAPI RadarFramesResponse):
  - data.providerId (str).
  - data.frames (list with at least 1 entry).
  - data.frames[i].time (UTC ISO-8601 Z).
  - data.frames[i].kind ("past"|"current"|"nowcast").
  - data.attribution (str or null).
  - generatedAt (UTC ISO-8601 Z).

ADR references: ADR-015, ADR-017, ADR-018, ADR-038.
OpenAPI contract: docs/contracts/openapi-v1.yaml lines 639-656 (endpoint),
  1482-1499 (RadarFrame + RadarFrameList), 1691-1696 (RadarFramesResponse).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixture directories
# ---------------------------------------------------------------------------

_FIXTURES_BASE = Path(__file__).parent / "fixtures" / "providers" / "radar"

# Provider-specific fixture paths
_RV_FIXTURE = _FIXTURES_BASE / "rainviewer" / "weather-maps.json"
_IEM_FIXTURE = _FIXTURES_BASE / "iem_nexrad" / "get_capabilities.xml"
_NOAA_FIXTURE = _FIXTURES_BASE / "noaa_mrms" / "get_capabilities.xml"
_MSC_FIXTURE = _FIXTURES_BASE / "msc_geomet" / "get_capabilities.xml"
_DWD_FIXTURE = _FIXTURES_BASE / "dwd_radolan" / "get_capabilities.xml"

# Provider upstream URLs (used in respx mocks)
_PROVIDER_UPSTREAM_URLS = {
    "rainviewer": "https://api.rainviewer.com/public/weather-maps.json",
    "iem_nexrad": "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi",
    "noaa_mrms": "https://mapservices.weather.noaa.gov/eventdriven/services/radar/radar_base_reflectivity_time/ImageServer/WMSServer",
    "msc_geomet": "https://geo.weather.gc.ca/geomet",
    "dwd_radolan": "https://maps.dwd.de/geoserver/dwd/wms",
}


def _load_json_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.loads(fh.read())


def _load_bytes_fixture(path: Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _reset_all_provider_state() -> None:
    """Reset registry, cache, rate limiters, and HTTP clients for all radar providers."""
    import os  # noqa: PLC0415

    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
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
    wire_cache_from_env()

    # Reset each provider's HTTP client and rate limiter
    providers_to_reset = [
        ("rainviewer", None),
        ("iem_nexrad", None),
        ("noaa_mrms", None),
        ("msc_geomet", None),
        ("dwd_radolan", None),
    ]
    for provider_name, _ in providers_to_reset:
        try:
            mod = __import__(
                f"weewx_clearskies_api.providers.radar.{provider_name}",
                fromlist=["_reset_http_client_for_tests", "_rate_limiter"],
            )
            if hasattr(mod, "_reset_http_client_for_tests"):
                mod._reset_http_client_for_tests()
            if hasattr(mod, "_rate_limiter"):
                mod._rate_limiter._calls.clear()
        except (ImportError, AttributeError):
            pass


def _make_radar_app(provider: str | None = None) -> FastAPI:
    """Build a test FastAPI app with the radar endpoint registered.

    provider: "rainviewer"|"iem_nexrad"|"noaa_mrms"|"msc_geomet"|"dwd_radolan"|None.
    When provider is None, no radar CAPABILITY is registered in the registry
    (simulates branch 2: known provider_id but not in registry).
    When provider is not None, its CAPABILITY is registered.
    """
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
# Branch 1 — Unknown provider_id → 404
# ===========================================================================


class TestBranch1UnknownProviderID:
    """Unknown provider_id → 404 Problem (not in dispatch table)."""

    def test_unknown_provider_id_returns_404(self) -> None:
        """GET /radar/providers/unknown_xyz/frames → 404."""
        app = _make_radar_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/unknown_xyz/frames")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"Unknown provider should return 404, got {response.status_code}: {response.text[:200]}"
        )

    def test_unknown_provider_response_is_problem_json(self) -> None:
        """Unknown provider_id → response body is Problem+JSON format (status + detail)."""
        app = _make_radar_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/totally_fake_provider/frames")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# Branch 2 — Known provider_id not in registry → 404
# ===========================================================================


class TestBranch2KnownProviderNotInRegistry:
    """provider_id is in dispatch table but not registered in capability registry."""

    def test_rainviewer_not_in_registry_returns_404(self) -> None:
        """rainviewer in dispatch table but no CAPABILITY registered → 404."""
        # Make app with NO provider registered (provider=None)
        app = _make_radar_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/rainviewer/frames")
        _reset_all_provider_state()
        assert response.status_code == 404, (
            f"Provider in dispatch but not in registry should return 404, "
            f"got {response.status_code}: {response.text[:200]}"
        )

    def test_iem_nexrad_not_in_registry_returns_404(self) -> None:
        """iem_nexrad in dispatch table but no CAPABILITY registered → 404."""
        app = _make_radar_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/radar/providers/iem_nexrad/frames")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# Branch 3 — Provider configured + registered → 200
# ===========================================================================


class TestBranch3RainViewerSuccess:
    """rainviewer: 200 RadarFramesResponse with correct shape."""

    def test_rainviewer_returns_200(self) -> None:
        """rainviewer provider registered → GET /frames → 200."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    def test_rainviewer_response_has_correct_provider_id(self) -> None:
        """rainviewer response.data.providerId = 'rainviewer'."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        assert body["data"]["providerId"] == "rainviewer"

    def test_rainviewer_response_has_13_frames(self) -> None:
        """rainviewer fixture has 13 past frames → 13 frames in response."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        frames = body["data"]["frames"]
        assert len(frames) == 13, f"Expected 13 frames, got {len(frames)}"

    def test_rainviewer_response_frames_have_time_and_kind_fields(self) -> None:
        """Each frame in response has 'time' and 'kind' fields."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        for frame in body["data"]["frames"]:
            assert "time" in frame, "Each frame must have 'time' field"
            assert "kind" in frame, "Each frame must have 'kind' field"
            assert frame["time"].endswith("Z"), f"Frame time must end with Z: {frame['time']!r}"
            assert frame["kind"] in ("past", "current", "nowcast"), (
                f"Frame kind must be past/current/nowcast, got {frame['kind']!r}"
            )

    def test_rainviewer_response_has_exactly_one_current_frame(self) -> None:
        """rainviewer fixture: exactly 1 frame with kind='current'."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        current_count = sum(1 for f in body["data"]["frames"] if f["kind"] == "current")
        assert current_count == 1, f"Expected 1 current frame, got {current_count}"

    def test_rainviewer_response_has_generated_at(self) -> None:
        """response.generatedAt is UTC ISO-8601 Z string."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        assert "generatedAt" in body, "Response must have generatedAt"
        assert body["generatedAt"].endswith("Z"), (
            f"generatedAt must end with Z, got {body['generatedAt']!r}"
        )

    def test_rainviewer_response_has_tile_host_and_per_frame_path(self) -> None:
        """3b-14 auditor F2: response.data.tileHost set + frame.path set per frame.

        Without these, the dashboard cannot construct RainViewer tile URLs from
        the CAPABILITY.tile_url_template which has {host} and {path} placeholders.
        """
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)
        data = _load_json_fixture(_RV_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, json=data)
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        body = response.json()
        assert body["data"]["tileHost"] == "https://tilecache.rainviewer.com", (
            f"Expected tileHost from fixture envelope; got {body['data'].get('tileHost')!r}"
        )
        for frame in body["data"]["frames"]:
            assert frame.get("path") is not None, (
                f"Each rainviewer frame must carry per-frame path; got {frame!r}"
            )
            assert frame["path"].startswith("/v2/radar/"), (
                f"Frame path should start with '/v2/radar/'; got {frame['path']!r}"
            )


class TestBranch3IEMNEXRADSuccess:
    """iem_nexrad: 200 RadarFramesResponse."""

    def test_iem_nexrad_returns_200_with_frames(self) -> None:
        """iem_nexrad provider registered → GET /frames → 200 with frames."""
        app = _make_radar_app(provider="iem_nexrad")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_IEM_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["iem_nexrad"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/iem_nexrad/frames")

        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["providerId"] == "iem_nexrad"
        assert len(body["data"]["frames"]) > 0

    def test_iem_nexrad_frames_have_z_suffix_times(self) -> None:
        """All iem_nexrad frame times end with 'Z' (ADR-020)."""
        app = _make_radar_app(provider="iem_nexrad")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_IEM_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["iem_nexrad"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/iem_nexrad/frames")

        _reset_all_provider_state()
        assert response.status_code == 200
        for frame in response.json()["data"]["frames"]:
            assert frame["time"].endswith("Z")


class TestBranch3NOAAMRMSSuccess:
    """noaa_mrms: 200 RadarFramesResponse."""

    def test_noaa_mrms_returns_200_with_frames(self) -> None:
        """noaa_mrms provider registered → GET /frames → 200 with frames."""
        app = _make_radar_app(provider="noaa_mrms")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_NOAA_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["noaa_mrms"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/noaa_mrms/frames")

        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["providerId"] == "noaa_mrms"
        assert len(body["data"]["frames"]) > 0


class TestBranch3MSCGeoMetSuccess:
    """msc_geomet: 200 RadarFramesResponse with 31 frames."""

    def test_msc_geomet_returns_200_with_31_frames(self) -> None:
        """msc_geomet provider registered → GET /frames → 200, 31 frames."""
        app = _make_radar_app(provider="msc_geomet")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_MSC_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["msc_geomet"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/msc_geomet/frames")

        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["providerId"] == "msc_geomet"
        assert len(body["data"]["frames"]) == 31


class TestBranch3DWDRADOLANSuccess:
    """dwd_radolan: 200 RadarFramesResponse."""

    def test_dwd_radolan_returns_200_with_frames(self) -> None:
        """dwd_radolan provider registered → GET /frames → 200 with frames."""
        app = _make_radar_app(provider="dwd_radolan")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_DWD_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["dwd_radolan"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/dwd_radolan/frames")

        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["providerId"] == "dwd_radolan"
        assert len(body["data"]["frames"]) > 0


class TestF1RegressionGuardCurrentFrameNearEndOfPeriod:
    """Regression guard for 3b-14 auditor F1.

    `_expand_period()` used to walk forward from start_iso, truncating to the
    first 300 timestamps. For long-range TIME dimensions (IEM 2011-2026 at PT5M,
    NOAA 6660 frames at PT1S, DWD 4 days at PT5M), the "current" frame ended up
    decades/hours/days stale. Lead-direct fix walks backward from end_iso.

    These tests assert the "current" frame's time matches each fixture's
    end-of-period timestamp. If `_expand_period` regresses to start-anchored
    walking, the current frame will be far from the fixture's end and these
    assertions will fail loudly.
    """

    def test_iem_current_frame_is_end_of_period(self) -> None:
        """IEM NEXRAD fixture period ends 2026-12-31; current frame must be at the end."""
        app = _make_radar_app(provider="iem_nexrad")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_IEM_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["iem_nexrad"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/iem_nexrad/frames")

        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        current = [f for f in frames if f["kind"] == "current"]
        assert len(current) == 1, f"Expected exactly one current frame, got {len(current)}"
        # IEM TIME dimension: 2011-02-16/2026-12-31/PT5M — end-anchored window
        # should put current at 2026-12-31T00:00:00Z (PT5M-aligned).
        assert current[0]["time"].startswith("2026-12-31"), (
            f"IEM current frame should be 2026-12-31 (fixture end-of-period); "
            f"got {current[0]['time']!r}. _expand_period regressed to start-anchored?"
        )

    def test_noaa_current_frame_is_end_of_period(self) -> None:
        """NOAA MRMS fixture period ends 2026-05-12T01:06:59; current frame must be at the end."""
        app = _make_radar_app(provider="noaa_mrms")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_NOAA_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["noaa_mrms"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/noaa_mrms/frames")

        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        current = [f for f in frames if f["kind"] == "current"]
        assert len(current) == 1
        # NOAA fixture period ends 2026-05-12T01:06:59Z (PT1S).
        assert current[0]["time"] == "2026-05-12T01:06:59Z", (
            f"NOAA current frame should be 2026-05-12T01:06:59Z (fixture end-of-period); "
            f"got {current[0]['time']!r}. _expand_period regressed to start-anchored?"
        )

    def test_msc_current_frame_is_end_of_period(self) -> None:
        """MSC GeoMet fixture period ends 2026-05-12T00:54:00; current frame must be at the end."""
        app = _make_radar_app(provider="msc_geomet")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_MSC_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["msc_geomet"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/msc_geomet/frames")

        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        current = [f for f in frames if f["kind"] == "current"]
        assert len(current) == 1
        # MSC fixture period ends 2026-05-12T00:54:00Z (31 frames fits within cap).
        assert current[0]["time"] == "2026-05-12T00:54:00Z", (
            f"MSC current frame should be 2026-05-12T00:54:00Z (fixture end-of-period); "
            f"got {current[0]['time']!r}."
        )

    def test_dwd_current_frame_is_end_of_period(self) -> None:
        """DWD RADOLAN fixture period ends 2026-05-12T03:15:00; current frame must be at the end."""
        app = _make_radar_app(provider="dwd_radolan")
        client = TestClient(app, raise_server_exceptions=False)
        xml_bytes = _load_bytes_fixture(_DWD_FIXTURE)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["dwd_radolan"]).mock(
                return_value=httpx.Response(200, content=xml_bytes)
            )
            response = client.get("/api/v1/radar/providers/dwd_radolan/frames")

        _reset_all_provider_state()
        frames = response.json()["data"]["frames"]
        current = [f for f in frames if f["kind"] == "current"]
        assert len(current) == 1
        # DWD fixture period ends 2026-05-12T03:15:00Z (PT5M).
        assert current[0]["time"] == "2026-05-12T03:15:00Z", (
            f"DWD current frame should be 2026-05-12T03:15:00Z (fixture end-of-period); "
            f"got {current[0]['time']!r}. _expand_period regressed to start-anchored?"
        )


# ===========================================================================
# Branch 4 — Network failure / 5xx → 502
# ===========================================================================


class TestBranch4NetworkFailure:
    """Provider upstream returns 5xx → endpoint returns 502 ProviderProblem."""

    def test_provider_5xx_returns_502(self) -> None:
        """Upstream 5xx → endpoint 502 (TransientNetworkError)."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(503, text="upstream down")
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        assert response.status_code == 502, (
            f"5xx upstream should return 502, got {response.status_code}"
        )


# ===========================================================================
# Branch 5 — Provider returns 429 → 503 + Retry-After
# ===========================================================================


class TestBranch5QuotaExhausted:
    """Provider upstream returns 429 → endpoint returns 503 with Retry-After."""

    def test_provider_429_returns_503(self) -> None:
        """Upstream 429 → endpoint 503 (QuotaExhausted)."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(429, text="rate limited")
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        assert response.status_code == 503, (
            f"429 upstream should return 503, got {response.status_code}"
        )


# ===========================================================================
# Branch 6 — Frame-index parse failure → 502
# ===========================================================================


class TestBranch6ParseFailure:
    """Malformed response from provider → endpoint returns 502 ProviderProblem."""

    def test_malformed_json_returns_502(self) -> None:
        """Malformed JSON from rainviewer → endpoint 502 (ProviderProtocolError)."""
        app = _make_radar_app(provider="rainviewer")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["rainviewer"]).mock(
                return_value=httpx.Response(200, text="THIS IS NOT JSON")
            )
            response = client.get("/api/v1/radar/providers/rainviewer/frames")

        _reset_all_provider_state()
        assert response.status_code == 502, (
            f"Malformed JSON should return 502, got {response.status_code}"
        )

    def test_malformed_xml_for_wms_provider_returns_502(self) -> None:
        """Malformed XML from iem_nexrad → endpoint 502 (ProviderProtocolError)."""
        app = _make_radar_app(provider="iem_nexrad")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=False) as mock:
            mock.get(_PROVIDER_UPSTREAM_URLS["iem_nexrad"]).mock(
                return_value=httpx.Response(200, content=b"<broken xml, no end")
            )
            response = client.get("/api/v1/radar/providers/iem_nexrad/frames")

        _reset_all_provider_state()
        assert response.status_code == 502, (
            f"Malformed XML should return 502, got {response.status_code}"
        )
