"""Tests for GET /setup/marine/health (Marine Model Restoration Plan B4, part A).

Covers:
  - Reachable path returns the marine service's /health body verbatim,
    including a key the API has never heard of (the requirement this
    endpoint exists to satisfy — no cherry-picking, no reshaping).
  - marine_service_url not configured.
  - Connection refused.
  - Timeout.
  - Non-2xx upstream status.
  - Non-JSON upstream body.

httpx is mocked throughout — no real network calls are made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


def _make_setup_app(tmp_path: Path) -> tuple[Any, Path]:
    """Create a setup-mode app wired for integration tests (mirrors
    test_branding_logo_alt.py's ``_make_setup_app``)."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.trust import TrustManager

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    secrets_path = config_dir / "secrets.env"

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
    trust_manager = TrustManager(secrets_path=secrets_path)

    app = create_app(settings)
    app.state.trust_manager = trust_manager
    app.state.settings = settings
    app.state.config_dir = config_dir

    return app, config_dir


def _get_session_token(client: TestClient, trust_token: str) -> str:
    resp = client.post("/setup/handshake", json={"token": trust_token})
    assert resp.status_code == 200, f"Handshake failed: {resp.text}"
    return resp.json()["session_id"]


def _write_marine_service_url(config_dir: Path, url: str = "https://marine.example:8780") -> None:
    """Write api.conf [providers] marine_service_url so
    _read_marine_service_connection() finds it."""
    import configobj

    conf_path = config_dir / "api.conf"
    cfg = configobj.ConfigObj(str(conf_path), interpolation=False) if conf_path.exists() else configobj.ConfigObj()
    cfg.filename = str(conf_path)
    cfg.setdefault("providers", {})
    cfg["providers"]["marine_service_url"] = url
    cfg.write()


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any = None, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._json_body


class _FakeAsyncClient:
    """Minimal async context manager mimicking httpx.AsyncClient for .get()."""

    def __init__(self, response: Any = None, raise_exc: Exception | None = None, **kwargs: Any) -> None:
        self._response = response
        self._raise_exc = raise_exc

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, url: str) -> Any:
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _client_factory(response: Any = None, raise_exc: Exception | None = None):
    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(response=response, raise_exc=raise_exc)

    return _factory


def _call_marine_health(app: Any, config_dir: Path, client: TestClient) -> Any:
    trust_token = app.state.trust_manager.token
    session_id = _get_session_token(client, trust_token)
    return client.get(
        "/setup/marine/health",
        headers={"Authorization": f"Bearer {session_id}"},
    )


class TestMarineHealthReachable:
    def test_reachable_returns_body_verbatim_including_unknown_key(self, tmp_path: Path) -> None:
        """The single most important assertion in this file: an invented key
        the API has never heard of must survive the round trip unmodified."""
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        upstream_body = {
            "status": "degraded",
            "version": "0.3.1",
            "last_run": "2026-07-27T12:00:00Z",
            "spots": 2,
            "run_in_progress": False,
            "reasons": ["stale_input:wind"],
            "inputs": {"ww3_boundary": {"available": True, "age_s": 42}},
            "invariants": {"fired_total": 1, "last_fired_at": "2026-07-27T11:59:00Z", "last_fired_names": ["x"]},
            "an_invented_key_the_api_has_never_heard_of": {"nested": [1, 2, 3]},
        }

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(response=_FakeResponse(200, upstream_body)),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reachable"] is True
        assert data["error"] is None
        assert data["health"] == upstream_body
        assert data["health"]["an_invented_key_the_api_has_never_heard_of"] == {"nested": [1, 2, 3]}


class TestMarineHealthUnconfigured:
    def test_unconfigured_url(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path)
        # No marine_service_url written.

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "reachable": False,
            "error": "Marine service is not configured",
            "health": None,
        }


class TestMarineHealthConnectionRefused:
    def test_connection_refused(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(
                    raise_exc=httpx.ConnectError("refused", request=None)
                ),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {"reachable": False, "error": "Connection refused", "health": None}


class TestMarineHealthTimeout:
    def test_timeout(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(
                    raise_exc=httpx.TimeoutException("timed out", request=None)
                ),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {"reachable": False, "error": "Connection timed out", "health": None}


class TestMarineHealthNon2xx:
    def test_non_2xx_upstream(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(response=_FakeResponse(503, {"detail": "down"})),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "reachable": False,
            "error": "Marine service returned HTTP 503",
            "health": None,
        }


class TestMarineHealthNonJsonBody:
    def test_non_json_body(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(
                    response=_FakeResponse(200, json_raises=True)
                ),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "reachable": False,
            "error": "Marine service returned a non-JSON response",
            "health": None,
        }

    def test_non_dict_json_body(self, tmp_path: Path) -> None:
        """A JSON body that parses but isn't an object (e.g. a bare list)
        is also a non-JSON-object response for this contract."""
        app, config_dir = _make_setup_app(tmp_path)
        _write_marine_service_url(config_dir)

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "httpx.AsyncClient",
                side_effect=_client_factory(response=_FakeResponse(200, [1, 2, 3])),
            ):
                resp = _call_marine_health(app, config_dir, client)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "reachable": False,
            "error": "Marine service returned a non-JSON response",
            "health": None,
        }
