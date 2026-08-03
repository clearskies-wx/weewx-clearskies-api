"""LM-3 finding 3 (2026-08-03): /setup/current-config must include the imagery
provider so wizard re-run pre-fills it.

`current_config()`'s provider-merge loop iterates a hardcoded
``_PROVIDER_DOMAINS`` tuple; before this round it excluded ``"imagery"``, so a
previously-saved ``[imagery] provider`` silently never pre-filled on wizard
re-run (defaulting back to Auto) — the same silent-reset defect class as the
study-area wizard bug. This file pins the fix with a full apply→current-config
round trip.

Self-contained harness mirroring tests/test_branding_logo_alt.py (repo
convention: no cross-test-file imports).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_setup_app(tmp_path: Path) -> tuple[Any, Path]:
    """Create a setup-mode app wired for integration tests."""
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


def _minimal_apply_body(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "database": {
            "host": "localhost",
            "port": 3306,
            "user": "weewx",
            "password": "secret",
            "name": "weewx",
        }
    }
    if extra:
        body.update(extra)
    return body


class TestImageryProviderPrefill:
    """[imagery] provider written by /apply is returned by /current-config."""

    def test_imagery_provider_round_trips_apply_to_current_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-imagery"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, _config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({
            "proxy_secret": proxy_secret,
            # apply.providers is dict[str, ProviderConfig] keyed by domain
            "providers": {"imagery": {"provider": "naip"}},
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            fake_weewx_conf = {"StdReport": {"SKIN_ROOT": str(tmp_path / "skins")}}
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=fake_weewx_conf,
            ):
                apply_resp = client.post(
                    "/setup/apply",
                    json=body,
                    headers={"Authorization": f"Bearer {session_id}"},
                )
            assert apply_resp.status_code == 200, apply_resp.text

            cfg_resp = client.get(
                "/setup/current-config",
                headers={"X-Clearskies-Proxy-Auth": proxy_secret},
            )
        assert cfg_resp.status_code == 200, cfg_resp.text
        providers = cfg_resp.json()["providers"]
        assert "imagery" in providers, (
            "imagery must be in current-config's providers merge — its absence "
            "is exactly the wizard-re-run silent-reset defect this test pins"
        )
        assert providers["imagery"]["provider"] == "naip"
