"""Tests for GET /setup/skin-file endpoint (ADR-043 image import).

Security-critical: directory traversal prevention is the primary concern.
All tests use TestClient against a real FastAPI app with a wired TrustManager
so the auth path is exercised (not mocked away).

Test structure mirrors test_skin_conf.py:
  - A helper that creates a setup-mode app with a real TrustManager.
  - A helper that exchanges the trust token for a session_id via /setup/handshake.
  - Test classes grouped by concern: traversal, valid paths, auth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_setup_app(tmp_path: Path) -> tuple[FastAPI, Path]:
    """Create a setup-mode app wired for test calls.

    Returns (app, config_dir) where config_dir is a writable tmp directory.
    """
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        RateLimitSettings,
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
        ratelimit=RateLimitSettings({}),
        database=DatabaseSettings({}),
    )

    trust_manager = TrustManager(secrets_path=secrets_path)

    app = create_app(settings)
    app.state.trust_manager = trust_manager
    app.state.settings = settings
    app.state.config_dir = config_dir

    return app, config_dir


def _get_session_token(client: TestClient, trust_token: str) -> str:
    """Exchange the trust token for a session_id via /setup/handshake."""
    resp = client.post("/setup/handshake", json={"token": trust_token})
    assert resp.status_code == 200, f"Handshake failed: {resp.text}"
    return resp.json()["session_id"]


def _fake_weewx_conf(skin_root: Path) -> dict[str, Any]:
    """Return a fake weewx.conf dict pointing SKIN_ROOT at a tmp directory."""
    return {"StdReport": {"SKIN_ROOT": str(skin_root)}}


# ---------------------------------------------------------------------------
# Path traversal security tests (most critical)
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """Directory traversal attacks must be blocked with HTTP 400."""

    def test_path_traversal_dotdot_in_path(self, tmp_path: Path) -> None:
        """path=../../etc/passwd → 400: resolved path escapes skin_dir."""
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown").mkdir(parents=True)

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "../../etc/passwd"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 400, (
            f"Expected 400 for dotdot path, got {resp.status_code}: {resp.text}"
        )

    def test_path_traversal_dotdot_in_skin(self, tmp_path: Path) -> None:
        """skin=../../../etc → 400: skin name contains '..'."""
        skin_root = tmp_path / "skins"
        skin_root.mkdir(parents=True)

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "../../../etc", "path": "passwd"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 400, (
            f"Expected 400 for dotdot skin, got {resp.status_code}: {resp.text}"
        )

    def test_path_traversal_backslash_in_skin(self, tmp_path: Path) -> None:
        """skin=foo\\bar → 400: skin name contains backslash path separator."""
        skin_root = tmp_path / "skins"
        skin_root.mkdir(parents=True)

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "foo\\bar", "path": "logo.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 400, (
            f"Expected 400 for backslash in skin, got {resp.status_code}: {resp.text}"
        )

    def test_path_traversal_forward_slash_in_skin(self, tmp_path: Path) -> None:
        """skin=foo/bar → 400: skin name contains forward-slash path separator."""
        skin_root = tmp_path / "skins"
        skin_root.mkdir(parents=True)

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "foo/bar", "path": "logo.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 400, (
            f"Expected 400 for slash in skin, got {resp.status_code}: {resp.text}"
        )

    def test_path_traversal_encoded_dotdot(self, tmp_path: Path) -> None:
        """path=images/..%2F..%2Fetc/passwd — FastAPI URL-decodes before we see it.

        After URL decoding, this becomes images/../../etc/passwd which our
        resolve()-based check catches.  Result must be 400 or 404 (never 200).
        """
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown").mkdir(parents=True)

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                # TestClient percent-encodes query parameters automatically;
                # pass the raw string and let TestClient encode it.
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/../../etc/passwd"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        # Must NOT be 200 — traversal must be blocked (400) or file absent (404).
        assert resp.status_code in (400, 404), (
            f"Encoded dotdot traversal must be blocked (400) or absent (404), "
            f"got {resp.status_code}: {resp.text}"
        )
        # Specifically it should be 400 because the resolved path escapes skin_dir.
        assert resp.status_code == 400, (
            f"Expected 400 for encoded dotdot traversal, got {resp.status_code}"
        )

    def test_prefix_attack_blocked(self, tmp_path: Path) -> None:
        """skin_dir=/skins/Foo must NOT match /skins/FooBar/secret.

        The os.sep suffix in the startswith check prevents this prefix attack.
        We create a sibling skin FooBar to ensure the guard works.
        """
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown").mkdir(parents=True)
        sibling = skin_root / "BelchertownExtra"
        sibling.mkdir(parents=True)
        secret = sibling / "secret.txt"
        secret.write_text("SENSITIVE", encoding="utf-8")

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                # Attempt to reach /skins/BelchertownExtra/secret.txt by
                # specifying skin=Belchertown and a crafted path.
                # After resolve: file_path = /skins/BelchertownExtra/secret.txt
                # which does NOT start with /skins/Belchertown/ (with sep).
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "../BelchertownExtra/secret.txt"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 400, (
            f"Prefix attack must be blocked with 400, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Valid path tests
# ---------------------------------------------------------------------------


class TestValidPaths:
    """Happy-path requests return the correct file with 200."""

    def test_valid_path_returns_file(self, tmp_path: Path) -> None:
        """Valid path to an existing file → 200 with the file content."""
        skin_root = tmp_path / "skins"
        skin_dir = skin_root / "Belchertown"
        images_dir = skin_dir / "images"
        images_dir.mkdir(parents=True)
        logo = images_dir / "logo.png"
        logo.write_bytes(b"\x89PNG\r\n\x1a\n")  # Minimal PNG header bytes

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/logo.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 200, (
            f"Expected 200 for valid path, got {resp.status_code}: {resp.text}"
        )
        assert resp.content == b"\x89PNG\r\n\x1a\n"

    def test_nested_path_returns_file(self, tmp_path: Path) -> None:
        """path=images/subfolder/logo.png works when the file exists."""
        skin_root = tmp_path / "skins"
        nested = skin_root / "Belchertown" / "images" / "subfolder"
        nested.mkdir(parents=True)
        logo = nested / "logo.png"
        logo.write_bytes(b"PNG_CONTENT")

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/subfolder/logo.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 200, (
            f"Expected 200 for nested path, got {resp.status_code}: {resp.text}"
        )
        assert resp.content == b"PNG_CONTENT"


# ---------------------------------------------------------------------------
# 404 / not-found tests
# ---------------------------------------------------------------------------


class TestNotFound:
    """Missing files and skins return 404."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        """Valid skin dir exists but requested file is absent → 404."""
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown").mkdir(parents=True)  # Skin exists, file does not

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/nonexistent.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 404, (
            f"Expected 404 for missing file, got {resp.status_code}: {resp.text}"
        )

    def test_skin_not_found(self, tmp_path: Path) -> None:
        """Non-existent skin directory → 404 (file_path.is_file() is False)."""
        skin_root = tmp_path / "skins"
        skin_root.mkdir(parents=True)  # SKIN_ROOT exists but skin does not

        app, _ = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "NoSuchSkin", "path": "images/logo.png"},
                    headers={"Authorization": f"Bearer {session_id}"},
                )

        assert resp.status_code == 404, (
            f"Expected 404 for non-existent skin, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Unauthenticated requests are rejected before file I/O is attempted."""

    def test_unauthenticated_request_rejected(self, tmp_path: Path) -> None:
        """No Authorization header → 401 (setup session required)."""
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown" / "images").mkdir(parents=True)
        (skin_root / "Belchertown" / "images" / "logo.png").write_bytes(b"PNG")

        app, _ = _make_setup_app(tmp_path / "app")

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/logo.png"},
                    # No Authorization header
                )

        assert resp.status_code in (401, 403), (
            f"Expected 401 or 403 for unauthenticated request, got {resp.status_code}: {resp.text}"
        )

    def test_invalid_session_token_rejected(self, tmp_path: Path) -> None:
        """Invalid Bearer token → 401."""
        skin_root = tmp_path / "skins"
        (skin_root / "Belchertown" / "images").mkdir(parents=True)
        (skin_root / "Belchertown" / "images" / "logo.png").write_bytes(b"PNG")

        app, _ = _make_setup_app(tmp_path / "app")

        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=_fake_weewx_conf(skin_root),
            ):
                resp = client.get(
                    "/setup/skin-file",
                    params={"skin": "Belchertown", "path": "images/logo.png"},
                    headers={"Authorization": "Bearer totally-fake-token"},
                )

        assert resp.status_code == 401, (
            f"Expected 401 for invalid session token, got {resp.status_code}: {resp.text}"
        )
