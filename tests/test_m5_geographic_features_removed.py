"""Guard tests: the ADR-078 single-file geographic-features feature is removed
(M5, ADR-078 Amendment 2).

Contract: docs/planning/briefs/M5-ADR078-REMOVAL-BRIEF-2026-08-27.md "Lead
mechanics" item 3 (test-author's assertion list) and item 1 (legacy config
migration, not a crash — mirrors [imagery]'s M4-B treatment). Module under
test (pre-change): weewx_clearskies_api/endpoints/geographic_features.py,
weewx_clearskies_api/services/geographic_features.py,
weewx_clearskies_api/config/settings.py `GeographicFeaturesSettings`,
weewx_clearskies_api/app.py router wiring, weewx_clearskies_api/__main__.py
wiring call.

Pins the five assertions named in the brief item 3 (API side):
  1. A legacy api.conf carrying a `[geographic_features]` section still
     loads without error, and `Settings` no longer carries a
     `geographic_features` attribute at all (the section becomes inert —
     same treatment as `[imagery]` after M4-B).
  2. The three routes (`GET /api/v1/geographic-features/tiles`,
     `GET /api/v1/geographic-features/status`,
     `POST /setup/geographic-features/update`) are absent from
     `app.routes` (enumerated, mirroring tests/test_openapi.py's approach
     of reading the live route table rather than guessing paths).
  3. `GET /api/v1/geographic-features/status` -> 404, and it is the
     GENERIC route-not-found 404 (`detail == "Not Found"`) — proving the
     ROUTE itself is gone, not that the endpoint's own logic declined
     (pre-change, this endpoint returns 200 with `available: false` when
     the PMTiles file is absent from disk — a same-status trap would not
     even apply here since pre-change status is 200, not 404 at all; the
     `detail` check is still asserted for symmetry with the setup-route
     case below and to guard against a future reintroduction that returns
     404 for "not configured").
  4. `POST /setup/geographic-features/update` -> 404 generic
     (`detail == "Not Found"`), same reasoning.
  5. `import weewx_clearskies_api.endpoints.geographic_features` ->
     `importlib.util.find_spec` is None (or the parent-package variant
     raises ModuleNotFoundError, per the M4-B "AMENDED" precedent for
     leftover empty directories under git-pull deploys — both outcomes
     treated as "gone"). Same check extended to
     `weewx_clearskies_api.services.geographic_features` (also named in
     the brief's "what goes" inventory, brief line 18).

PRE-CHANGE EVIDENCE: this file was authored and run BEFORE m5-api/m5-stack
were told to start (per brief item 3, "test-author writes and runs its
guards at the pinned HEADs BEFORE telling the devs to start editing shared
files"). Live pytest transcript against clean pre-change API HEAD
(811fe88) — a real run that happened, captured verbatim below (not a
reconstruction; full raw stdout also pasted into the m5-test closeout
report per rules/verification.md "A transcript is pasted only from a run
that happened"):

    $ .venv/Scripts/python.exe -m pytest tests/test_m5_geographic_features_removed.py \
          -q -p no:cacheprovider
    FFFFFFFF                                                                 [100%]
    FAILED ...::TestLegacyGeographicFeaturesSectionIsInert::\
        test_conf_with_geographic_features_section_loads_and_is_ignored
        AssertionError: assert not True
         +  where True = hasattr(<Settings ...>, 'geographic_features')
    FAILED ...::TestRoutesAreGone::test_tiles_route_gone
        AssertionError: assert '/api/v1/geographic-features/tiles' not in [...]
    FAILED ...::TestRoutesAreGone::test_status_route_gone
        AssertionError: assert '/api/v1/geographic-features/status' not in [...]
    FAILED ...::TestRoutesAreGone::test_setup_update_route_gone
        AssertionError: assert '/setup/geographic-features/update' not in [...]
    FAILED ...::TestStatusEndpointGone::test_status_endpoint_gone_returns_generic_404
        assert 200 == 404   (route present pre-change; returns available:false)
    FAILED ...::TestSetupEndpointGone::test_setup_update_gone_returns_generic_404
        assert 503 == 404   (route present pre-change; declines with 503 no-secret,
        not 404 -- proves this guard discriminates "route gone" from "route
        present but auth-declined", same reasoning as the M4-B imagery guard)
    FAILED ...::TestModuleImportsAreGone::test_endpoints_module_has_no_spec
        AssertionError: assert False  (find_spec returns a real spec pre-change)
    FAILED ...::TestModuleImportsAreGone::test_services_module_has_no_spec
        AssertionError: assert False  (find_spec returns a real spec pre-change)
    8 failed, 1 warning in 0.80s

All 8 assertions FAIL pre-change as required. Exit code 1.

POST-CHANGE RUN (this file, against HEAD after m5-api lands) is pasted into
the closeout report, not here (this docstring's guard-run evidence is
authored once, before the post-change run, per "guard must fail pre-change"
discipline).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app() -> FastAPI:
    """Full configured app via the standard conftest wiring path, but built
    directly here (not the `client` fixture) so this file has no fixture
    coupling to conftest.py and stays readable as a standalone guard."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
    return create_app(settings)


# ===========================================================================
# 1. Legacy [geographic_features] conf section is inert.
# ===========================================================================


class TestLegacyGeographicFeaturesSectionIsInert:
    def test_conf_with_geographic_features_section_loads_and_is_ignored(
        self, tmp_path: Path
    ) -> None:
        from weewx_clearskies_api.config.settings import load_settings

        content = (
            "[api]\nbind_host = 127.0.0.1\nbind_port = 8765\n"
            "[geographic_features]\nenabled = true\n"
            "bounds = -119.0,33.0,-117.0,34.5\nmaxzoom = 12\n"
        )
        conf = tmp_path / "api.conf"
        conf.write_text(content)

        settings = load_settings(config_path=conf)  # must not raise

        assert settings.api.bind_host == "127.0.0.1"
        assert not hasattr(settings, "geographic_features")


# ===========================================================================
# 2. All three geographic-features routes are absent from app.routes.
# ===========================================================================


class TestRoutesAreGone:
    def test_tiles_route_gone(self) -> None:
        app = _make_app()
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/api/v1/geographic-features/tiles" not in paths

    def test_status_route_gone(self) -> None:
        app = _make_app()
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/api/v1/geographic-features/status" not in paths

    def test_setup_update_route_gone(self) -> None:
        app = _make_app()
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/setup/geographic-features/update" not in paths


# ===========================================================================
# 3. GET /api/v1/geographic-features/status -> generic 404 (route gone).
# ===========================================================================


class TestStatusEndpointGone:
    def test_status_endpoint_gone_returns_generic_404(self) -> None:
        """Pre-change this endpoint returns 200 {"available": false, ...}
        when the PMTiles file is absent from disk (the common case on a dev
        machine) — so this guard fails pre-change on the status code alone,
        not merely on `detail`. Post-change it must be Starlette's own
        route-not-found 404 with detail=="Not Found"."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/v1/geographic-features/status")

        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"


# ===========================================================================
# 4. POST /setup/geographic-features/update -> generic 404 (route gone).
# ===========================================================================


class TestSetupEndpointGone:
    def test_setup_update_gone_returns_generic_404(self) -> None:
        """Pre-change this endpoint returns 503 (no proxy-auth secret
        configured) rather than 404 — so this guard fails pre-change on the
        status code, distinguishing "route removed" from "route present but
        auth-declined"."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/setup/geographic-features/update")

        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"


# ===========================================================================
# 5. Module imports are gone (endpoints + services, brief's inventory).
# ===========================================================================


def _module_is_gone(dotted_name: str) -> bool:
    """True if `dotted_name` cannot be found — either find_spec() returns
    None (parent package exists, module doesn't), or it raises
    ModuleNotFoundError (parent package itself doesn't exist). Both outcomes
    mean "gone"; only a real ImportSpec means "still here" (mirrors
    tests/test_m4b_imagery_removed.py's `_submodule_is_gone` precedent)."""
    try:
        return importlib.util.find_spec(dotted_name) is None
    except ModuleNotFoundError:
        return True


class TestModuleImportsAreGone:
    def test_endpoints_module_has_no_spec(self) -> None:
        assert _module_is_gone("weewx_clearskies_api.endpoints.geographic_features")

    def test_services_module_has_no_spec(self) -> None:
        assert _module_is_gone("weewx_clearskies_api.services.geographic_features")
