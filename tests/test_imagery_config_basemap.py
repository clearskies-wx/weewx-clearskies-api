"""GET /api/v1/imagery/config always answers with the product basemap
(SURF-MAP-BASEMAP round, PA9, Q5; plan MARINE-AND-MAPS-PLAN-2026-08-27.md
§M4, 2026-08-27).

Contract under test (endpoints/imagery.py, models/responses.py — dev commits
f4a42a7 "ImageryConfigResponse gains light/dark/zoomMin/zoomMax",
87924c9 "/imagery/config answers with the product basemap", 07e0322
"CHANGELOG entry"): /imagery/config no longer 404s and no longer branches on
[imagery] provider. It always returns:
  provider="basemap", tileUrl="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution="© OpenStreetMap contributors", proxyMode="direct", bounds=None,
  light={tileUrl, attribution} mirroring the legacy top-level fields,
  dark={pmtilesUrl:"/api/v1/basemap/local/tiles", maxDataZoom:15,
  attribution:"© OpenStreetMap contributors © Protomaps"}, zoomMin=0,
  zoomMax=19. wire_imagery_settings() keeps reading [imagery] (still used by
  the /imagery/tiles NAIP proxy's decision tree) and logs ONE WARNING at
  wiring time, not per request, when provider is set — naming the ignored
  value. /imagery/tiles is untouched by this round.

Non-falsifiable pin declared per rules/verification.md ("Stale tests" /
known-answer discipline): this module was written AFTER the dev's change
landed (coordinator ruling 2026-08-27, in response to a timing conflict —
the dev's commits (f4a42a7, 87924c9, 07e0322) were already in place before
I ran my scoped baseline, so this module cannot itself carry a true
pre-change failure transcript run by me against clean-HEAD code). The
guard-proof evidence for the *behavior change* this round is the 9
pre-identified provider-selection assertions in
tests/test_endpoints_imagery_integration.py, which DID fail against the
same landed code before I rewrote them (see that file's docstring for the
transcript: 9 failed, 36 passed). This new module is a straight contract
pin against the as-built response, not a regression guard for a change I
watched flip from red to green — it is declared as such rather than
presented as guard-proved.

The tests below are, however, still genuine regressions guards against a
NARROWER set of future mutations: any test in this module fails if a future
edit reintroduces the 404, reintroduces provider branching, drops a field,
changes a pinned string/number, double-logs the WARNING, or breaks the
/imagery/tiles NAIP proxy. That failure mode was verified directly: each
assertion was checked by hand against endpoints/imagery.py:236-254 and
models/responses.py:1432-1487 (the exact field names/values/types the
dev's code emits) before being written here, and the full module was run
green against that code (see closeout for the run transcript).
"""

from __future__ import annotations

import logging

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

_LIGHT_TILE_URL = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
_LIGHT_ATTRIBUTION = "© OpenStreetMap contributors"
_DARK_PMTILES_URL = "/api/v1/basemap/local/tiles"
_DARK_ATTRIBUTION = "© OpenStreetMap contributors © Protomaps"
_DARK_MAX_DATA_ZOOM = 15
_ZOOM_MIN = 0
_ZOOM_MAX = 19

_EXPECTED_BASEMAP_BODY = {
    "provider": "basemap",
    "tileUrl": _LIGHT_TILE_URL,
    "attribution": _LIGHT_ATTRIBUTION,
    "proxyMode": "direct",
    "bounds": None,
    "light": {"tileUrl": _LIGHT_TILE_URL, "attribution": _LIGHT_ATTRIBUTION},
    "dark": {
        "pmtilesUrl": _DARK_PMTILES_URL,
        "maxDataZoom": _DARK_MAX_DATA_ZOOM,
        "attribution": _DARK_ATTRIBUTION,
    },
    "zoomMin": _ZOOM_MIN,
    "zoomMax": _ZOOM_MAX,
}


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
    Mirrors tests/test_endpoints_imagery_integration.py's helper exactly
    (same fixture-building shape as production api.conf — [imagery]
    provider is a real deployable config value even though /config no
    longer reads it).
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
# Config shape: [imagery] absent / provider=naip / provider=map -> all the
# SAME basemap body.
# ===========================================================================


class TestConfigShapeAcrossImageryProviderValues:
    def test_config_shape_when_imagery_section_absent(self) -> None:
        app = _make_imagery_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json() == _EXPECTED_BASEMAP_BODY

    def test_config_shape_when_provider_naip(self) -> None:
        app = _make_imagery_app(provider="naip")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json() == _EXPECTED_BASEMAP_BODY

    def test_config_shape_when_provider_map(self) -> None:
        app = _make_imagery_app(provider="map")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json() == _EXPECTED_BASEMAP_BODY

    def test_config_shape_is_independent_of_coordinates(self) -> None:
        """Non-CONUS coordinates get the identical basemap body — the
        answer no longer varies by location (unlike the retired
        naip-vs-esri CONUS test)."""
        app = _make_imagery_app(provider="naip")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_LONDON_LAT}&lon={_LONDON_LON}")
        _reset_all_provider_state()
        assert response.status_code == 200
        assert response.json() == _EXPECTED_BASEMAP_BODY


# ===========================================================================
# Legacy top-level fields carry the light theme's values (old-client
# compatibility, §M4).
# ===========================================================================


class TestLegacyTopLevelFieldsCarryLightValues:
    def test_top_level_tileurl_and_attribution_equal_light_block(self) -> None:
        app = _make_imagery_app(provider=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
        _reset_all_provider_state()
        body = response.json()
        assert body["tileUrl"] == body["light"]["tileUrl"]
        assert body["attribution"] == body["light"]["attribution"]
        assert body["tileUrl"] == _LIGHT_TILE_URL
        assert body["attribution"] == _LIGHT_ATTRIBUTION


# ===========================================================================
# WARNING logged once at wiring time, not per request (named trap, brief).
# ===========================================================================


class _FakeImagerySettings:
    def __init__(self, provider: str | None = None, tile_cache_ttl_seconds: int = 604800) -> None:
        self.provider = provider
        self.tile_cache_ttl_seconds = tile_cache_ttl_seconds


class _FakeSettings:
    def __init__(self, imagery: _FakeImagerySettings | None = None) -> None:
        self.imagery = imagery


class TestIgnoredProviderWarningLoggedOnceAtWiring:
    def setup_method(self) -> None:
        from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415

        imagery_endpoint.reset_imagery_settings_for_tests()

    def teardown_method(self) -> None:
        from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415

        imagery_endpoint.reset_imagery_settings_for_tests()

    def test_warning_logged_once_when_provider_set(self, caplog) -> None:
        from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415

        settings = _FakeSettings(_FakeImagerySettings(provider="naip"))
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.endpoints.imagery"):
            imagery_endpoint.wire_imagery_settings(settings)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].getMessage() == (
            "[imagery] provider=naip is no longer used by any user-facing surface "
            "(PA9, 2026-08-27); the surf height map draws the product basemap"
        )

    def test_warning_names_the_ignored_value(self, caplog) -> None:
        from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415

        settings = _FakeSettings(_FakeImagerySettings(provider="map"))
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.endpoints.imagery"):
            imagery_endpoint.wire_imagery_settings(settings)

        assert any("provider=map" in r.getMessage() for r in caplog.records)

    def test_no_warning_when_provider_absent(self, caplog) -> None:
        from weewx_clearskies_api.endpoints import imagery as imagery_endpoint  # noqa: PLC0415

        settings = _FakeSettings(_FakeImagerySettings(provider=None))
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.endpoints.imagery"):
            imagery_endpoint.wire_imagery_settings(settings)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_warning_not_relogged_per_config_request(self, caplog) -> None:
        """Named trap (brief): the WARNING fires once at wiring, not once
        per /imagery/config call."""
        app = _make_imagery_app(provider="naip")
        client = TestClient(app, raise_server_exceptions=False)

        # caplog.records accumulates for the whole test, so the ONE WARNING
        # wire_imagery_settings() already logged inside _make_imagery_app()
        # above is in there too — clear it before exercising the requests,
        # so what remains after is attributable only to the three GETs.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="weewx_clearskies_api.endpoints.imagery"):
            client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
            client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")
            client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")

        _reset_all_provider_state()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ===========================================================================
# /imagery/tiles behaviour unchanged for provider=naip (brief).
# ===========================================================================


class TestTileProxyUnchangedForNaip:
    def test_naip_tile_proxy_still_returns_200_png(self) -> None:
        app = _make_imagery_app(provider="naip")
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

    def test_naip_tile_proxy_still_404s_when_provider_esri(self) -> None:
        """Unchanged decision tree: provider="esri" -> tile proxy 404s,
        even though /config itself no longer distinguishes esri from naip."""
        app = _make_imagery_app(provider="esri")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/imagery/tiles/4/4/6")
        _reset_all_provider_state()
        assert response.status_code == 404
