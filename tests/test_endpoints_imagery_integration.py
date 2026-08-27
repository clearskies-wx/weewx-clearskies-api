"""Integration tests for GET /api/v1/imagery/config and
GET /api/v1/imagery/tiles/{z}/{x}/{y} (Phase LM, §LM-1).

Tests the full request -> endpoint -> provider -> response path using respx
mocks for outbound HTTP (NAIP only — ESRI never makes an outbound call).
FastAPI TestClient exercises the full ASGI stack. Mirrors
tests/test_endpoints_radar_tiles_integration.py's scaffolding — the closest
existing precedent for an API-proxied+cached tile endpoint.

KAT coverage (plan §LM-1):
  (a) NAIP proxy returns a valid PNG/JPEG tile for a known CONUS tile coordinate.
  (b) NAIP cache hit returns identical bytes on second request without upstream fetch.
  (c) ESRI config returns the correct XYZ URL template + attribution text.
  (d) non-CONUS coordinates with NAIP-preferred config -> falls back to ESRI.
  (e) imagery endpoint with no imagery config -> 404, no crash.

Plus the lead's 2026-08-03 addition:
  (f) tile z/x/y out of range -> 4xx (amplification-surface guard), not
      forwarded upstream unchecked.

Plus the IMAGERY-MAP round's addition (2026-08-26, operator ruling: replace
orthophoto background with a map-style tile base):
  (g) provider "map" config carries the World_Topo_Map template + pinned
      attribution.
  (h) "auto" never selects "map" (naip-vs-esri only, unchanged).
  (i) "map" tiles never flow through the NAIP tile proxy (browser-direct,
      same posture as "esri").
"""

from __future__ import annotations

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

_HB_LAT = 33.6595
_HB_LON = -118.0055
_LONDON_LAT = 51.5074
_LONDON_LON = -0.1278

_EXPORT_URL = (
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
)

_TILE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


def _reset_all_provider_state() -> None:
    from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )
    from weewx_clearskies_api.providers.imagery import naip  # noqa: PLC0415

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()
    imagery_endpoint.reset_imagery_settings_for_tests()
    naip._reset_http_client_for_tests()
    naip._rate_limiter._calls.clear()


def _make_imagery_app(provider: str | None) -> FastAPI:
    """Build a test FastAPI app with [imagery] provider configured.

    provider: "auto" | "naip" | "esri" | "map" | None (not configured).
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        ImagerySettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.endpoints.imagery import wire_imagery_settings  # noqa: PLC0415
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415
    from weewx_clearskies_api.providers.imagery import esri, esri_topo, naip  # noqa: PLC0415

    _reset_all_provider_state()

    capabilities = []
    if provider in ("auto", "naip"):
        capabilities.append(naip.CAPABILITY)
    if provider in ("auto", "esri"):
        capabilities.append(esri.CAPABILITY)
    if provider == "map":
        capabilities.append(esri_topo.CAPABILITY)
    wire_providers(capabilities)

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
        imagery=ImagerySettings({"provider": provider} if provider else {}),
    )
    wire_imagery_settings(settings)
    return create_app(settings)


# ===========================================================================
# KAT (e) — no imagery config -> 404, no crash
# ===========================================================================


class TestNoImageryConfig:
    def test_config_endpoint_returns_404_when_not_configured(self) -> None:
        app = _make_imagery_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"

    def test_tiles_endpoint_returns_404_when_not_configured(self) -> None:
        app = _make_imagery_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# KAT (c) — ESRI config: correct XYZ URL template + attribution text
# ===========================================================================


class TestEsriConfig:
    def test_esri_config_shape(self) -> None:
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "esri"
        assert body["proxyMode"] == "direct"
        assert "{z}" in body["tileUrl"] and "{x}" in body["tileUrl"] and "{y}" in body["tileUrl"]
        assert body["attribution"] == (
            "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community"
        )
        assert body["bounds"] is None

    def test_esri_pinned_regardless_of_location(self) -> None:
        """Explicit override pins esri even for CONUS coordinates."""
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "esri"

    def test_esri_tiles_never_flow_through_the_tile_proxy(self) -> None:
        """provider='esri' -> NAIP tile proxy is not available -> 404 (§LM-1c)."""
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# KAT (g)/(h)/(i) — "map" provider (IMAGERY-MAP round, 2026-08-26)
# ===========================================================================


class TestMapConfig:
    def test_map_config_shape(self) -> None:
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "map"
        assert body["proxyMode"] == "direct"
        assert "{z}" in body["tileUrl"] and "{x}" in body["tileUrl"] and "{y}" in body["tileUrl"]
        assert "World_Topo_Map" in body["tileUrl"]
        assert body["attribution"] == (
            "Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, "
            "NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China "
            "(Hong Kong), (c) OpenStreetMap contributors, and the GIS User Community"
        )
        assert body["bounds"] is None

    def test_map_pinned_regardless_of_location(self) -> None:
        """Explicit override pins map even for CONUS coordinates."""
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "map"

    def test_map_config_differs_from_esri_only_in_tile_url_and_attribution(self) -> None:
        """Mutation-style check (brief §Verification)."""
        map_app = _make_imagery_app(provider="map")
        map_client = TestClient(map_app, raise_server_exceptions=False)
        map_response = map_client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()

        esri_app = _make_imagery_app(provider="esri")
        esri_client = TestClient(esri_app, raise_server_exceptions=False)
        esri_response = esri_client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()

        map_body = map_response.json()
        esri_body = esri_response.json()

        assert map_body["proxyMode"] == esri_body["proxyMode"] == "direct"
        assert map_body["bounds"] == esri_body["bounds"] is None
        assert map_body["provider"] != esri_body["provider"]
        assert map_body["tileUrl"] != esri_body["tileUrl"]
        assert map_body["attribution"] != esri_body["attribution"]

    def test_map_tiles_never_flow_through_the_tile_proxy(self) -> None:
        """provider='map' -> NAIP tile proxy is not available -> 404 (§LM-1c,
        same browser-direct posture as esri)."""
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404


# ===========================================================================
# KAT (d) — non-CONUS coordinates with NAIP-preferred (auto) config -> ESRI
# ===========================================================================


class TestAutoProviderSelection:
    def test_conus_coordinates_select_naip(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "naip"
        assert body["proxyMode"] == "api"
        assert body["tileUrl"] == "/api/v1/imagery/tiles/{z}/{x}/{y}"

    def test_non_conus_coordinates_fall_back_to_esri(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "esri"
        assert body["proxyMode"] == "direct"

    def test_naip_override_pins_naip_even_for_non_conus(self) -> None:
        app = _make_imagery_app(provider="naip")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json()["provider"] == "naip"

    def test_auto_never_selects_map_for_conus(self) -> None:
        """KAT (h): "auto" resolves to naip-vs-esri only, never "map"."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] != "map"

    def test_auto_never_selects_map_for_non_conus(self) -> None:
        """KAT (h): "auto" resolves to naip-vs-esri only, never "map"."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] != "map"

    def test_missing_lat_lon_returns_422(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/config")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_unknown_query_param_rejected(self) -> None:
        """extra='forbid' security control (coding.md)."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}&nuke_the_db=1"
        )
        _reset_all_provider_state()
        assert response.status_code == 422


# ===========================================================================
# KAT (a) — NAIP proxy returns a valid tile for a known CONUS tile coordinate
# ===========================================================================


class TestNaipTileProxyCacheMiss:
    def test_naip_tile_returns_200_png(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=True) as mock:
            mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(
                    200, content=_TILE_BYTES, headers={"Content-Type": "image/png"}
                )
            )
            response = client.get("/api/v1/imagery/tiles/12/700/1600")

        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == _TILE_BYTES


# ===========================================================================
# KAT (b) — NAIP cache hit returns identical bytes without a second upstream fetch
# ===========================================================================


class TestNaipTileProxyCacheHit:
    def test_second_request_does_not_call_upstream_again(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(
                    200, content=_TILE_BYTES, headers={"Content-Type": "image/png"}
                )
            )
            first = client.get("/api/v1/imagery/tiles/12/700/1600")
            second = client.get("/api/v1/imagery/tiles/12/700/1600")

        _reset_all_provider_state()
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.content == second.content == _TILE_BYTES
        assert route.call_count == 1, "Second request should be served from cache, not upstream"


# ===========================================================================
# (f) — z/x/y out-of-range guard (lead ruling 2026-08-03)
# ===========================================================================


class TestTileCoordinateRangeGuard:
    def test_x_out_of_range_for_zoom_returns_400(self) -> None:
        """z=1 -> valid x/y in [0, 2); x=5 is out of range."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/1/5/0")
        _reset_all_provider_state()
        assert response.status_code == 400

    def test_y_out_of_range_for_zoom_returns_400(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/1/0/5")
        _reset_all_provider_state()
        assert response.status_code == 400

    def test_zoom_above_max_returns_422(self) -> None:
        """z > _MAX_ZOOM rejected by the FastAPI Path constraint."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/21/0/0")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_x_at_exact_boundary_2_pow_z_returns_400(self) -> None:
        """Boundary pin (audit aud-c4lm1 F1, 2026-08-03): valid x is [0, 2**z)
        EXCLUSIVE — x == 2**z is the first invalid value and must 400 before
        any upstream call is constructed. An off-by-one (`<=` for `<`) in
        `_validate_tile_coords` escaped every prior KAT because they tested
        far outside the boundary (x=5 at z=1); this test fails against that
        exact mutation."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/3/8/0")  # x == 2**3
        _reset_all_provider_state()
        assert response.status_code == 400

    def test_y_at_exact_boundary_2_pow_z_returns_400(self) -> None:
        """Same boundary pin as above, y axis (y == 2**z)."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/3/0/8")  # y == 2**3
        _reset_all_provider_state()
        assert response.status_code == 400

    def test_negative_zoom_returns_422(self) -> None:
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/-1/0/0")
        _reset_all_provider_state()
        assert response.status_code == 422

    def test_valid_boundary_coordinates_are_accepted(self) -> None:
        """z=1, x=1, y=1 is the max valid index for zoom 1 -> should reach upstream, not 400."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)

        with respx.mock(assert_all_called=True) as mock:
            mock.get(_EXPORT_URL).mock(
                return_value=httpx.Response(
                    200, content=_TILE_BYTES, headers={"Content-Type": "image/png"}
                )
            )
            response = client.get("/api/v1/imagery/tiles/1/1/1")

        _reset_all_provider_state()
        assert response.status_code == 200
