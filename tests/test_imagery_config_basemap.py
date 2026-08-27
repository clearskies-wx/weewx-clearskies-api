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
  zoomMax=19.

AMENDED 2026-08-27 (M4-B, Q10-6, coordinator ruling): the imagery provider
machinery this module's fixtures depended on (`ImagerySettings`,
`wire_imagery_settings()`, `reset_imagery_settings_for_tests()`, the
providers/imagery/{esri,esri_topo,naip} modules, and the
`/imagery/tiles/{z}/{x}/{y}` route) was deleted in a733c24. Two dispositions
in the same commit as this docstring edit:
  - `TestConfigShapeAcrossImageryProviderValues` and
    `TestLegacyTopLevelFieldsCarryLightValues`: SETUP-ONLY amendment.
    `_make_imagery_app()`/`_reset_all_provider_state()` no longer construct
    `ImagerySettings` or call `wire_imagery_settings()`/
    `reset_imagery_settings_for_tests()` (those symbols no longer exist);
    the `provider` parameter is now a no-op (kept only so existing call
    sites need no edits) since /imagery/config no longer varies by
    [imagery] config at all. Every assertion in both classes is
    byte-identical to before.
  - `TestIgnoredProviderWarningLoggedOnceAtWiring` (asserted
    `wire_imagery_settings()`'s WARNING-logging behavior) and
    `TestTileProxyUnchangedForNaip` (asserted `/imagery/tiles` still
    proxied NAIP tiles) DELETED outright — both classes' subject under
    test no longer exists (the function and the route were removed, not
    just reconfigured), so there is no setup-only fix. The
    `/imagery/tiles/... -> 404 (route gone)` guard now lives in
    `tests/test_m4b_imagery_removed.py` instead.

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

from fastapi import FastAPI
from fastapi.testclient import TestClient

_HB_LAT = 33.6595
_HB_LON = -118.0055
_LONDON_LAT = 51.5074
_LONDON_LON = -0.1278

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
    from weewx_clearskies_api.providers._common.cache import (  # noqa: PLC0415
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (  # noqa: PLC0415
        reset_provider_registry_for_tests,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()


def _make_imagery_app(provider: str | None) -> FastAPI:
    """Build a test FastAPI app.

    provider: kept for call-site compatibility only — a no-op since M4-B
    (Q10-6): [imagery] config and the naip/esri/esri_topo provider modules
    are deleted, and /imagery/config no longer varies by any config value
    at all (see module docstring "AMENDED 2026-08-27").
    """
    from weewx_clearskies_api.app import create_app  # noqa: PLC0415
    from weewx_clearskies_api.config.settings import (  # noqa: PLC0415
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.providers._common.capability import wire_providers  # noqa: PLC0415

    del provider  # no-op — see docstring
    _reset_all_provider_state()
    wire_providers([])

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
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
# TestIgnoredProviderWarningLoggedOnceAtWiring and TestTileProxyUnchangedForNaip
# DELETED 2026-08-27 (M4-B, Q10-6, coordinator ruling — see module docstring
# "AMENDED 2026-08-27"): wire_imagery_settings() (the WARNING-logging
# function under test) and the /imagery/tiles/{z}/{x}/{y} route (the proxy
# under test) were both removed outright in a733c24 — their subject no
# longer exists, so there is no setup-only fix available. The
# /imagery/tiles -> 404 (route gone) guard now lives in
# tests/test_m4b_imagery_removed.py.
# ===========================================================================
