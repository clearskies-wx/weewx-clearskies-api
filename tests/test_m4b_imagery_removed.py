"""Guard tests: the imagery provider machinery is removed (M4-B, Q10-6).

Plan MARINE-AND-MAPS-PLAN-2026-08-27.md §M4 round B; operator ruling "if we
dont need it then get rid of it" (Q10-6, 2026-08-27, recorded as PA9
extended). Pins the five assertions named in the M4-B brief §4:

  1. A legacy `api.conf` carrying an `[imagery]` section (from before this
     round) still loads without error, and `Settings` no longer carries an
     `imagery` attribute at all (the section is inert — lead mechanics
     item 1: "the loader must IGNORE an unknown [imagery] section
     silently").
  2. `GET /imagery/tiles/1/1/1` -> 404, and it is the GENERIC
     route-not-found 404 (`detail == "Not Found"`), not the endpoint's old
     business-logic 404 — proving the ROUTE itself is gone, not just
     returning 404 for an unconfigured provider (which it already did
     pre-change with no [imagery] section — a same-status-different-cause
     trap; see the docstring of `TestTileRouteIsGone` below).
  3. `GET /imagery/config`'s response body is unchanged (still the fixed
     product-basemap answer, PA9/M4-API's contract; brief lead-mechanics
     item 2: "stays byte-identical in its response").
  4. `providers/_common/dispatch.PROVIDER_MODULES` has no `("imagery", *)`
     key.
  5. The provider submodules are gone: `importlib.util.find_spec(...)` is
     None for `providers.imagery.esri`/`.esri_topo`/`.naip` (see "AMENDED"
     note below for why this replaces a package-level ImportError check).

PRE-CHANGE EVIDENCE: m4b-api's WIP landed in this repo faster than my
capture window (concurrent session timing — see scope-ack thread). No live
pytest run of THIS file exists against clean pre-change HEAD (39cd51a) —
per coordinator ruling 2026-08-27 ("A. Proceed — `git show 39cd51a:<file>`
excerpts in the docstring are the sanctioned pre-change record when a live
run is impossible. Never write a transcript you did not run."), pre-change
behaviour is established here by direct citation of `git show 39cd51a:<path>`
content (verified, not fabricated) rather than a pytest transcript:

  (1) `git show 39cd51a:weewx_clearskies_api/config/settings.py` — class
      `ImagerySettings` (lines 897-955) exists; `Settings.__init__` takes
      an `imagery: "ImagerySettings | None"` kwarg (line 1638) and always
      sets `self.imagery` (line 1676, defaulting to `ImagerySettings({})`);
      `load_settings()` constructs `imagery_cfg = ImagerySettings(dict(cfg
      .get("imagery", {})))` (line 1819) and passes it through. So
      pre-change, `hasattr(settings, "imagery")` is always True — assertion
      2 of `TestLegacyImagerySectionIsInert` below would FAIL pre-change.
      An out-of-schema legacy value (e.g. a provider id from before the
      2026-08-26 IMAGERY-MAP round's "map" addition never existed, so any
      value outside {"auto","naip","esri","map"}) would additionally raise
      ValueError from `ImagerySettings.validate()` (lines 946-952) inside
      `Settings.validate()` — not exercised directly here since the brief's
      KAT uses a realistic legacy value ("naip"), which was already valid
      pre-change and loads fine both ways; the `hasattr` assertion is the
      actual discriminator.
  (2) `git show 39cd51a:weewx_clearskies_api/endpoints/imagery.py` — the
      route `@router.get("/imagery/tiles/{z}/{x}/{y}", ...)` (line 257) and
      `get_imagery_tile()` (line 263) exist; with no `[imagery]` configured
      (`_imagery_provider is None`, the module default, line 96), hitting
      that route raises `HTTPException(404, detail="NAIP tile proxy is not
      available for this deployment. Check the [imagery] provider setting
      in api.conf.")` (lines 286-292) — status 404 but `detail` != "Not
      Found", so `TestTileRouteIsGone.test_tile_route_gone_returns_generic_404`
      below would FAIL pre-change on the `detail` assertion even though the
      status code alone would coincidentally match.
  (3) `git show 39cd51a:weewx_clearskies_api/providers/_common/dispatch.py`
      — `PROVIDER_MODULES` (lines 76-103) contains `("imagery","esri")`,
      `("imagery","map")`, `("imagery","naip")` — assertion in
      `TestDispatchHasNoImageryDomain` below would FAIL pre-change (the
      keys exist).
  (4) `git show 39cd51a:weewx_clearskies_api/providers/imagery/esri.py`,
      `.../esri_topo.py`, `.../naip.py` all exist and are real, importable
      submodules pre-change — `importlib.util.find_spec()` for each
      returns a real spec, not None. `TestProviderSubmodulesAreGone` below
      would FAIL pre-change (specs found, not None).

POST-CHANGE RUN (this file, against HEAD after a733c24 landed) — pasted
into the closeout report, not here (this docstring is authored once,
before the post-change run, per "guard must fail pre-change" discipline).

AMENDED 2026-08-27 (coordinator ruling, after a finding during authoring):
the brief's literal wording — "`import
weewx_clearskies_api.providers.imagery` raises ImportError" — is
environment-dependent, not deterministic: in a working tree where `git rm`
has deleted the tracked `.py` files but the now-empty
`providers/imagery/` directory (plus its untracked `__pycache__/`) still
exists on disk, Python 3's implicit PEP 420 namespace-package mechanism
makes the PACKAGE import succeed anyway (as an empty namespace module),
even though every submodule underneath it is gone. A fresh `git clone`
of the same commit would raise ModuleNotFoundError correctly (git never
materializes empty directories) — but a guard that only passes on a fresh
clone and silently passes-for-the-wrong-reason on an in-place `git pull`
deploy is not deterministic. Ruling: assert on the SUBMODULES instead —
`importlib.util.find_spec("weewx_clearskies_api.providers.imagery.esri")`
(and `.esri_topo`, `.naip`) must be None. This is true regardless of
whether the parent directory happens to still exist, and is exactly what
actually matters (the naip/esri/esri_topo provider code itself is gone).

Note: `find_spec()` on a dotted submodule path raises ModuleNotFoundError
(rather than returning None) when even the PARENT package can't be found —
which is what happens once the leftover `providers/imagery/` directory is
itself removed (m4b-api cleaned it up after this finding was reported;
confirmed absent in this tree at guard-authoring time). `_submodule_is_gone()`
below treats both outcomes (None, or ModuleNotFoundError) as "gone" — only
an actual importable spec means "still here".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_HB_LAT = 33.6595
_HB_LON = -118.0055

_EXPECTED_BASEMAP_BODY = {
    "provider": "basemap",
    "tileUrl": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": "© OpenStreetMap contributors",
    "proxyMode": "direct",
    "bounds": None,
    "light": {
        "tileUrl": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
    },
    "dark": {
        "pmtilesUrl": "/api/v1/basemap/local/tiles",
        "maxDataZoom": 15,
        "attribution": "© OpenStreetMap contributors © Protomaps",
    },
    "zoomMin": 0,
    "zoomMax": 19,
}


def _make_app() -> FastAPI:
    """Minimal configured app with the imagery router mounted, no [imagery]
    settings dependency (that settings section no longer exists)."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.providers._common.cache import (
        reset_cache_for_tests,
        wire_cache_from_env,
    )
    from weewx_clearskies_api.providers._common.capability import (
        reset_provider_registry_for_tests,
        wire_providers,
    )

    reset_cache_for_tests()
    reset_provider_registry_for_tests()
    wire_cache_from_env()
    wire_providers([])

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
    return create_app(settings)


# ===========================================================================
# 1. Legacy [imagery] conf section is inert.
# ===========================================================================


class TestLegacyImagerySectionIsInert:
    def test_conf_with_imagery_section_loads_and_is_ignored(self, tmp_path: Path) -> None:
        from weewx_clearskies_api.config.settings import load_settings

        content = (
            "[api]\nbind_host = 127.0.0.1\nbind_port = 8765\n"
            "[imagery]\nprovider = naip\ntile_cache_ttl_seconds = 604800\n"
        )
        conf = tmp_path / "api.conf"
        conf.write_text(content)

        settings = load_settings(config_path=conf)  # must not raise

        assert settings.api.bind_host == "127.0.0.1"
        assert not hasattr(settings, "imagery")


# ===========================================================================
# 2. /imagery/tiles/{z}/{x}/{y} route is gone entirely.
# ===========================================================================


class TestTileRouteIsGone:
    def test_tile_route_gone_returns_generic_404(self) -> None:
        """Same-status trap: pre-change, an unconfigured [imagery] section
        ALSO produced a 404 for this URL (business logic: "no provider
        configured") — status code alone can't tell "route removed" from
        "route present but declined". The `detail` field disambiguates:
        Starlette's own route-not-found 404 always carries
        detail=="Not Found"; the old endpoint's business 404 carried a
        provider-specific message instead. See module docstring evidence
        item (2) for the pre-change detail string."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/v1/imagery/tiles/1/1/1")

        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"


# ===========================================================================
# 3. /imagery/config unchanged.
# ===========================================================================


class TestImageryConfigUnchanged:
    def test_config_response_body_unchanged(self) -> None:
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get(f"/api/v1/imagery/config?lat={_HB_LAT}&lon={_HB_LON}")

        assert response.status_code == 200
        assert response.json() == _EXPECTED_BASEMAP_BODY


# ===========================================================================
# 4. Dispatch table has no imagery domain.
# ===========================================================================


class TestDispatchHasNoImageryDomain:
    def test_provider_modules_has_no_imagery_key(self) -> None:
        from weewx_clearskies_api.providers._common.dispatch import PROVIDER_MODULES

        imagery_keys = [key for key in PROVIDER_MODULES if key[0] == "imagery"]
        assert imagery_keys == []


# ===========================================================================
# 5. Provider submodules are gone (deterministic replacement for a
#    package-level ImportError check — see module docstring "AMENDED").
# ===========================================================================


def _submodule_is_gone(dotted_name: str) -> bool:
    """True if `dotted_name` cannot be found — either find_spec() returns
    None (parent package exists, submodule doesn't), or it raises
    ModuleNotFoundError (parent package itself doesn't exist either, e.g.
    once the leftover empty `providers/imagery/` directory is cleaned up).
    Both outcomes mean "gone"; only a real ImportSpec means "still here"."""
    try:
        return importlib.util.find_spec(dotted_name) is None
    except ModuleNotFoundError:
        return True


class TestProviderSubmodulesAreGone:
    def test_esri_submodule_has_no_spec(self) -> None:
        assert _submodule_is_gone("weewx_clearskies_api.providers.imagery.esri")

    def test_esri_topo_submodule_has_no_spec(self) -> None:
        assert _submodule_is_gone("weewx_clearskies_api.providers.imagery.esri_topo")

    def test_naip_submodule_has_no_spec(self) -> None:
        assert _submodule_is_gone("weewx_clearskies_api.providers.imagery.naip")
