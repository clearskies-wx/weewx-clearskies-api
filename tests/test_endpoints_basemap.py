"""Integration tests for the basemap endpoint family (Phase M, task M1 —
CS-BASEMAP): GET /api/v1/basemap/{tier}/tiles, GET /api/v1/basemap/status,
POST /setup/basemap/update.

Contract: docs/planning/MARINE-AND-MAPS-PLAN-2026-08-27.md §M1, "Lead
mechanics — API side"; module under test is weewx_clearskies_api/endpoints/
basemap.py. Written by clearskies-test-author, M1-API round, mirroring
tests/test_endpoints_imagery_integration.py's FastAPI TestClient scaffolding
(closest existing precedent for a tile-serving + admin-action endpoint
family) and the auth/status-shape pattern documented in the removed
endpoints/geographic_features.py (ADR-078, the feature this generalises --
deleted M5, ADR-078 Amendment 2).

KAT coverage (brief §Scope — test-author, "Endpoints"):
  (a) GET /api/v1/basemap/local/tiles with a Range header -> 206 +
      Accept-Ranges: bytes (tmp_path + monkeypatch basemap_extract.tier_path
      — never /etc, rules/coding.md security constraint 1).
  (b) unknown tier -> 404 problem+json (RFC 9457, via the global handler).
  (c) known tier, file absent -> 404 plain JSON (ADR-078's shape, not
      problem+json).
  (d) GET /api/v1/basemap/status shape: all three tiers present under
      "tiers", updating: false, last_error: null on a fresh/reset state.
  (e) POST /setup/basemap/update: 503 no secret, 401 wrong secret, 202
      {"status": "started"} with the extractor patched to a no-op (never
      spawns a real pmtiles subprocess in tests), 409 while an extraction
      is already running.

PRE-CHANGE EVIDENCE: by the time this file was written, m1api-dev's commits
(45d1b63..6fda155) had already landed on API `main`, so there was no window
left to run this suite against a genuinely absent endpoints/basemap.py
without touching another agent's working tree (which this test-author was
explicitly told not to do, after an earlier incident on the sibling test
file — see tests/test_basemap_extract.py's docstring). Read-only proof that
the pre-round baseline (cf0318d, the brief's stated HEAD) had none of this
--
    $ git show cf0318d:weewx_clearskies_api/app.py | grep -c basemap
    0
    $ git ls-tree -r cf0318d --name-only -- weewx_clearskies_api/endpoints/ | grep basemap
    (no output -- endpoints/basemap.py did not exist in the tree at cf0318d)
--
so every request this suite makes (GET /api/v1/basemap/*, POST /setup/
basemap/update) would have 404'd against an unmounted route or failed
import at that commit. This file's actual first run (against the endpoint
module as committed) is below, and it was clean on the first try -- see the
closeout report for the raw output.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PROXY_SECRET_ENV_VAR = "WEEWX_CLEARSKIES_PROXY_SECRET"
_PROXY_AUTH_HEADER = "X-Clearskies-Proxy-Auth"
_TEST_SECRET = "test-basemap-secret"

_TILE_BYTES = b"PMTiles-fake-content-0123456789ABCDEF"


def _reset_basemap_state() -> None:
    from weewx_clearskies_api.endpoints import basemap as basemap_endpoint
    from weewx_clearskies_api.services import basemap_extract

    basemap_endpoint.reset_basemap_settings_for_tests()
    basemap_extract.reset_state_for_tests()


def _make_basemap_app(*, basemap_enabled: bool = True) -> FastAPI:
    """Build a full FastAPI app (configured mode) with [basemap] wired."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        BasemapSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.endpoints.basemap import wire_basemap_settings

    _reset_basemap_state()

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
        basemap=BasemapSettings({"enabled": "true" if basemap_enabled else "false"}),
    )
    wire_basemap_settings(settings)
    return create_app(settings)


def _make_client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# (a) Range request -> 206 + Accept-Ranges: bytes
# ===========================================================================


class TestTileRangeRequest:
    def test_range_header_returns_206_partial_content(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract

        tile_path = tmp_path / "basemap-local.pmtiles"
        tile_path.write_bytes(_TILE_BYTES)
        monkeypatch.setattr(basemap_extract, "tier_path", lambda tier: tile_path)

        app = _make_basemap_app()
        client = _make_client(app)
        response = client.get(
            "/api/v1/basemap/local/tiles", headers={"Range": "bytes=0-3"}
        )
        _reset_basemap_state()

        assert response.status_code == 206
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == _TILE_BYTES[0:4]

    def test_no_range_header_returns_200_full_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract

        tile_path = tmp_path / "basemap-local.pmtiles"
        tile_path.write_bytes(_TILE_BYTES)
        monkeypatch.setattr(basemap_extract, "tier_path", lambda tier: tile_path)

        app = _make_basemap_app()
        client = _make_client(app)
        response = client.get("/api/v1/basemap/local/tiles")
        _reset_basemap_state()

        assert response.status_code == 200
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == _TILE_BYTES


# ===========================================================================
# (b) unknown tier -> 404 problem+json
# ===========================================================================


class TestUnknownTier:
    def test_unknown_tier_returns_404_problem_json(self) -> None:
        app = _make_basemap_app()
        client = _make_client(app)
        response = client.get("/api/v1/basemap/regional/tiles")
        _reset_basemap_state()

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"


# ===========================================================================
# (c) known tier, file absent -> 404 plain JSON (ADR-078 shape)
# ===========================================================================


class TestMissingTileFile:
    def test_known_tier_missing_file_returns_404_plain_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract

        absent_path = tmp_path / "basemap-world.pmtiles"  # never written
        monkeypatch.setattr(basemap_extract, "tier_path", lambda tier: absent_path)

        app = _make_basemap_app()
        client = _make_client(app)
        response = client.get("/api/v1/basemap/world/tiles")
        _reset_basemap_state()

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        assert "detail" in response.json()

    def test_disabled_basemap_returns_404_even_when_file_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[basemap] enabled=false gates tile serving even if a stale file
        is still on disk (ONE key, per directive 14 — this is that key's
        only behaviour)."""
        from weewx_clearskies_api.services import basemap_extract

        tile_path = tmp_path / "basemap-world.pmtiles"
        tile_path.write_bytes(_TILE_BYTES)
        monkeypatch.setattr(basemap_extract, "tier_path", lambda tier: tile_path)

        app = _make_basemap_app(basemap_enabled=False)
        client = _make_client(app)
        response = client.get("/api/v1/basemap/world/tiles")
        _reset_basemap_state()

        assert response.status_code == 404


# ===========================================================================
# (d) GET /api/v1/basemap/status shape
# ===========================================================================


class TestBasemapStatus:
    def test_fresh_state_has_all_three_tiers_not_updating_no_error(self) -> None:
        app = _make_basemap_app()
        client = _make_client(app)
        response = client.get("/api/v1/basemap/status")
        _reset_basemap_state()

        assert response.status_code == 200
        body = response.json()
        assert set(body["tiers"].keys()) == {"world", "local", "radar"}
        for tier in ("world", "local", "radar"):
            assert body["tiers"][tier]["available"] is False
        assert body["updating"] is False
        assert body["last_error"] is None


# ===========================================================================
# (e) POST /setup/basemap/update
# ===========================================================================


class TestBasemapUpdateAuth:
    def test_no_secret_configured_returns_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_PROXY_SECRET_ENV_VAR, raising=False)
        app = _make_basemap_app()
        client = _make_client(app)
        response = client.post("/setup/basemap/update")
        _reset_basemap_state()

        assert response.status_code == 503

    def test_wrong_secret_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_PROXY_SECRET_ENV_VAR, _TEST_SECRET)
        app = _make_basemap_app()
        client = _make_client(app)
        response = client.post(
            "/setup/basemap/update", headers={_PROXY_AUTH_HEADER: "not-the-secret"}
        )
        _reset_basemap_state()

        assert response.status_code == 401

    def test_missing_auth_header_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_PROXY_SECRET_ENV_VAR, _TEST_SECRET)
        app = _make_basemap_app()
        client = _make_client(app)
        response = client.post("/setup/basemap/update")
        _reset_basemap_state()

        assert response.status_code == 401


class TestBasemapUpdateStart:
    def test_valid_secret_starts_extraction_returns_202(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract

        monkeypatch.setenv(_PROXY_SECRET_ENV_VAR, _TEST_SECRET)
        # No-op extractor -- never spawns a real pmtiles subprocess in tests.
        monkeypatch.setattr(
            basemap_extract, "start_extract_in_background", lambda settings: True
        )

        app = _make_basemap_app()
        client = _make_client(app)
        response = client.post(
            "/setup/basemap/update", headers={_PROXY_AUTH_HEADER: _TEST_SECRET}
        )
        _reset_basemap_state()

        assert response.status_code == 202
        assert response.json() == {"status": "started"}

    def test_already_running_returns_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from weewx_clearskies_api.services import basemap_extract

        monkeypatch.setenv(_PROXY_SECRET_ENV_VAR, _TEST_SECRET)
        # Simulates one extraction already in progress -- start_extract_in_
        # background() returns False rather than starting a second thread.
        monkeypatch.setattr(
            basemap_extract, "start_extract_in_background", lambda settings: False
        )

        app = _make_basemap_app()
        client = _make_client(app)
        response = client.post(
            "/setup/basemap/update", headers={_PROXY_AUTH_HEADER: _TEST_SECRET}
        )
        _reset_basemap_state()

        assert response.status_code == 409
        assert response.json() == {"status": "already_running"}


# ===========================================================================
# FIRST-RUN TRANSCRIPT (commit 6fda155, uv run --frozen --extra dev pytest
# tests/test_endpoints_basemap.py -q --tb=short):
#
# ...........                                                              [100%]
# 11 passed, 1 warning in 1.00s
#
# Clean on the first run -- no test-side bugs found in this file (unlike the
# sibling tests/test_basemap_extract.py, which had two genuine test-author
# bugs against the dev's WIP, corrected there; see that file's docstring and
# the closeout report for the full accounting).
# ===========================================================================
