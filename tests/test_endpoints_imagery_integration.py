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
      attribution. [STALE — see below]
  (h) "auto" never selects "map" (naip-vs-esri only, unchanged). [STALE — see below]
  (i) "map" tiles never flow through the NAIP tile proxy (browser-direct,
      same posture as "esri"). [/imagery/tiles — unchanged, kept]

SURF-MAP-BASEMAP round (PA9, Q5; plan MARINE-AND-MAPS-PLAN-2026-08-27.md
§M4, 2026-08-27): /imagery/config now ALWAYS answers with the product
basemap (provider="basemap") regardless of [imagery] provider — the KAT
(c)/(d)/(g)/(h) assertions above that pinned the naip/esri/map/auto
*provider-selection* decision tree for /config are STALE BY DESIGN
(lead-identified; test-author updates them, M4-API brief 2026-08-27) and
rewritten below to assert the new invariant: /config output does not vary
with [imagery] provider or with coordinates. The 404-when-not-configured
KAT (e) for /config is likewise stale — the endpoint never 404s now (the
surf map must always get a basemap). /imagery/tiles KATs (a)/(b)/(e-tiles)/
(f)/(i) are UNCHANGED — the tile proxy's own decision tree is untouched by
this round.

Guard proof (ran against the M4-API dev's landed change, commit 87924c9,
BEFORE this file's own edits — this transcript IS the pre-change-behavior
evidence for the assertions rewritten below, per coordinator ruling
2026-08-27: "the 9 pre-identified provider-selection assertions failing
against the new code... ARE the proof that behaviour changed"):

  Command: .venv\\Scripts\\python.exe -m pytest tests/test_endpoints_imagery_integration.py
    tests/test_wire_imagery_settings.py tests/test_config_settings_imagery_validation.py
    tests/test_current_config_imagery_prefill.py -q -p no:cacheprovider

  9 failed, 36 passed, 1 warning in 1.94s
  FAILED TestNoImageryConfig::test_config_endpoint_returns_404_when_not_configured
  FAILED TestEsriConfig::test_esri_config_shape
  FAILED TestEsriConfig::test_esri_pinned_regardless_of_location
  FAILED TestMapConfig::test_map_config_shape
  FAILED TestMapConfig::test_map_pinned_regardless_of_location
  FAILED TestMapConfig::test_map_config_differs_from_esri_only_in_tile_url_and_attribution
    (AssertionError: assert 'basemap' != 'basemap')
  FAILED TestAutoProviderSelection::test_conus_coordinates_select_naip
    (AssertionError: assert 'basemap' == 'naip')
  FAILED TestAutoProviderSelection::test_non_conus_coordinates_fall_back_to_esri
    (AssertionError: assert 'basemap' == 'esri')
  FAILED TestAutoProviderSelection::test_naip_override_pins_naip_even_for_non_conus
    (AssertionError: assert 'basemap' == 'naip')

  (test_auto_never_selects_map_for_conus / _for_non_conus already passed —
  vacuously true once /config always answers "basemap"; both rewritten below
  to positively assert "basemap" so they pin real behavior, not a leftover.)
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
    def test_config_endpoint_returns_basemap_when_not_configured(self) -> None:
        """STALE→rewritten (§M4, PA9): /config never 404s — the surf map must
        always get a basemap answer, [imagery] configured or not."""
        app = _make_imagery_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json()["provider"] == "basemap"

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
        """STALE→rewritten (§M4, PA9): [imagery] provider="esri" no longer
        selects an esri-branded /config answer — /config always answers
        "basemap", carrying the OSM light tile template + attribution."""
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "basemap"
        assert body["proxyMode"] == "direct"
        assert "{z}" in body["tileUrl"] and "{x}" in body["tileUrl"] and "{y}" in body["tileUrl"]
        assert body["attribution"] == "© OpenStreetMap contributors"
        assert body["bounds"] is None

    def test_esri_pinned_regardless_of_location(self) -> None:
        """STALE→rewritten (§M4, PA9): [imagery] provider="esri" no longer
        pins anything at /config — the basemap answer is now the same
        regardless of the spot's location (positive pin of the new
        invariant, same test name/intent, new mechanism)."""
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "basemap"

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
        """STALE→rewritten (§M4, PA9): [imagery] provider="map" no longer
        selects a World_Topo_Map /config answer — always "basemap" now."""
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "basemap"
        assert body["proxyMode"] == "direct"
        assert "{z}" in body["tileUrl"] and "{x}" in body["tileUrl"] and "{y}" in body["tileUrl"]
        assert "tile.openstreetmap.org" in body["tileUrl"]
        assert body["attribution"] == "© OpenStreetMap contributors"
        assert body["bounds"] is None

    def test_map_pinned_regardless_of_location(self) -> None:
        """STALE→rewritten (§M4, PA9): same invariant as the esri case —
        /config answer is location-independent under provider="map" too."""
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "basemap"

    def test_map_config_is_identical_to_esri_config(self) -> None:
        """STALE→rewritten (§M4, PA9): the premise of the old test (map and
        esri /config answers differ only in tileUrl/attribution) is gone —
        under [imagery] provider="map" vs "esri", /config now returns the
        SAME basemap body either way. This is the positive pin of the new
        invariant: /config output does not depend on [imagery] provider."""
        map_app = _make_imagery_app(provider="map")
        map_client = TestClient(map_app, raise_server_exceptions=False)
        map_response = map_client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()

        esri_app = _make_imagery_app(provider="esri")
        esri_client = TestClient(esri_app, raise_server_exceptions=False)
        esri_response = esri_client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()

        assert map_response.json() == esri_response.json()

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
        """STALE→rewritten (§M4, PA9): CONUS coordinates no longer select
        naip at /config — always "basemap" regardless of location."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "basemap"
        assert body["proxyMode"] == "direct"

    def test_non_conus_coordinates_fall_back_to_esri(self) -> None:
        """STALE→rewritten (§M4, PA9): non-CONUS coordinates no longer fall
        back to esri at /config — always "basemap", same as CONUS."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "basemap"
        assert body["proxyMode"] == "direct"

    def test_naip_override_pins_naip_even_for_non_conus(self) -> None:
        """STALE→rewritten (§M4, PA9): explicit provider="naip" no longer
        pins naip at /config — always "basemap"."""
        app = _make_imagery_app(provider="naip")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json()["provider"] == "basemap"

    def test_auto_never_selects_map_for_conus(self) -> None:
        """STALE reasoning→rewritten (§M4, PA9): the old assertion
        (provider != "map") passed vacuously once /config always answers
        "basemap" — it no longer pins the KAT (h) decision-tree reasoning it
        was written for. Rewritten to positively assert the real current
        answer."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "basemap"

    def test_auto_never_selects_map_for_non_conus(self) -> None:
        """STALE reasoning→rewritten (§M4, PA9): see above, non-CONUS case."""
        app = _make_imagery_app(provider="auto")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.json()["provider"] == "basemap"

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
