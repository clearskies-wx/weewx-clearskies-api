"""Tests for logo_alt round-trip and non-empty alt guarantee (ADR-022, WCAG 2.1 AA §5.5).

Covers:
  - BrandingSettings reads logo_alt from config section.
  - services/branding.get_branding() plumbs logo_alt → LogoBranding.alt.
  - Non-empty alt fallback: when logo configured but alt empty → uses site_title or default.
  - BrandingApplyConfig accepts logo_alt; it is written to api.conf via POST /setup/apply.
  - CurrentConfigBrandingSection reads logo_alt back from api.conf (round-trip).
  - POST /setup/current-config returns logo_alt previously written by /setup/apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import configobj
import pytest
from fastapi.testclient import TestClient

from weewx_clearskies_api.config.settings import BrandingSettings, SocialSettings
from weewx_clearskies_api.endpoints.setup import BrandingApplyConfig, CurrentConfigBrandingSection
from weewx_clearskies_api.models.responses import LogoBranding
from weewx_clearskies_api.services.branding import get_branding


# ---------------------------------------------------------------------------
# BrandingSettings unit tests
# ---------------------------------------------------------------------------


class TestBrandingSettingsLogoAlt:
    """BrandingSettings reads logo_alt from config section."""

    def test_reads_logo_alt_from_section(self) -> None:
        section = {
            "logo_light_url": "https://example.com/logo.png",
            "logo_alt": "My Weather Station logo",
        }
        s = BrandingSettings(section)
        assert s.logo_alt == "My Weather Station logo"

    def test_logo_alt_defaults_to_empty_string(self) -> None:
        s = BrandingSettings({})
        assert s.logo_alt == ""

    def test_logo_alt_is_stripped(self) -> None:
        s = BrandingSettings({"logo_alt": "  My Logo  "})
        assert s.logo_alt == "My Logo"


# ---------------------------------------------------------------------------
# get_branding() unit tests — non-empty alt guarantee
# ---------------------------------------------------------------------------


class TestGetBrandingLogoAlt:
    """get_branding() plumbs logo_alt and enforces non-empty alt guarantee."""

    def test_explicit_alt_is_used(self) -> None:
        settings = BrandingSettings({
            "logo_light_url": "https://example.com/logo.png",
            "logo_alt": "Clear Skies Weather logo",
            "site_title": "Clear Skies",
        })
        cfg = get_branding(settings)
        assert cfg.logo is not None
        assert cfg.logo.alt == "Clear Skies Weather logo"

    def test_fallback_to_site_title_when_alt_empty(self) -> None:
        """When logo is set but alt is blank → fall back to '<site_title> logo'."""
        settings = BrandingSettings({
            "logo_light_url": "https://example.com/logo.png",
            "logo_alt": "",
            "site_title": "My Weather Site",
        })
        cfg = get_branding(settings)
        assert cfg.logo is not None
        assert cfg.logo.alt == "My Weather Site logo"

    def test_fallback_to_default_when_alt_and_title_both_empty(self) -> None:
        """When logo is set but both alt and site_title are blank → 'Weather station logo'."""
        settings = BrandingSettings({
            "logo_light_url": "https://example.com/logo.png",
            "logo_alt": "",
            "site_title": "",
        })
        cfg = get_branding(settings)
        assert cfg.logo is not None
        assert cfg.logo.alt == "Weather station logo"

    def test_alt_is_always_nonempty_when_logo_configured(self) -> None:
        """Invariant: logo.alt must never be empty string when logo is present."""
        for alt, title in [("", ""), ("", "My Site"), ("  ", "")]:
            settings = BrandingSettings({
                "logo_light_url": "https://example.com/logo.png",
                "logo_alt": alt.strip(),
                "site_title": title,
            })
            cfg = get_branding(settings)
            assert cfg.logo is not None
            assert cfg.logo.alt, (
                f"logo.alt must be non-empty (alt={alt!r}, title={title!r})"
            )

    def test_no_logo_when_no_light_url(self) -> None:
        """No logo block emitted when logo_light_url is absent, even if alt is set."""
        settings = BrandingSettings({
            "logo_light_url": "",
            "logo_alt": "Orphan alt text",
        })
        cfg = get_branding(settings)
        assert cfg.logo is None


# ---------------------------------------------------------------------------
# BrandingApplyConfig unit tests
# ---------------------------------------------------------------------------


class TestBrandingApplyConfigLogoAlt:
    """BrandingApplyConfig accepts and validates logo_alt."""

    def test_logo_alt_accepted(self) -> None:
        obj = BrandingApplyConfig(logo_alt="My logo alt text")
        assert obj.logo_alt == "My logo alt text"

    def test_logo_alt_defaults_to_none(self) -> None:
        obj = BrandingApplyConfig()
        assert obj.logo_alt is None

    def test_extra_field_rejected(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BrandingApplyConfig(logo_alt="fine", unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# CurrentConfigBrandingSection unit tests
# ---------------------------------------------------------------------------


class TestCurrentConfigBrandingSectionLogoAlt:
    """CurrentConfigBrandingSection carries logo_alt."""

    def test_logo_alt_default_empty(self) -> None:
        sec = CurrentConfigBrandingSection()
        assert sec.logo_alt == ""

    def test_logo_alt_set(self) -> None:
        sec = CurrentConfigBrandingSection(logo_alt="Station logo")
        assert sec.logo_alt == "Station logo"


# ---------------------------------------------------------------------------
# Integration: POST /setup/apply persists logo_alt; /setup/current-config returns it.
# ---------------------------------------------------------------------------


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


class TestLogoAltRoundTrip:
    """POST /setup/apply writes logo_alt; GET /setup/current-config returns it."""

    def test_logo_alt_round_trips_apply_to_current_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """logo_alt written by /apply is returned by /current-config.

        Uses a proxy_secret so current-config can be called after setup
        completes (setup_complete=True requires X-Clearskies-Proxy-Auth).
        """
        proxy_secret = "test-proxy-secret-for-round-trip"
        # Ensure clean env before the test sets its value via apply.
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({
            "proxy_secret": proxy_secret,
            "branding": {
                "logo_light_url": "https://example.com/logo.png",
                "logo_alt": "My Station logo",
                "site_title": "My Station",
            }
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

            # After apply, setup_complete=True; current-config requires proxy auth.
            cfg_resp = client.get(
                "/setup/current-config",
                headers={"X-Clearskies-Proxy-Auth": proxy_secret},
            )
        assert cfg_resp.status_code == 200, cfg_resp.text
        branding = cfg_resp.json()["branding"]
        assert branding["logo_alt"] == "My Station logo"

    def test_logo_alt_persisted_in_api_conf_file(
        self, tmp_path: Path
    ) -> None:
        """After /apply, api.conf [branding] contains logo_alt key."""
        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({
            "branding": {
                "logo_light_url": "https://example.com/logo.png",
                "logo_alt": "Persisted alt text",
            }
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            fake_weewx_conf = {"StdReport": {"SKIN_ROOT": str(tmp_path / "skins")}}
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=fake_weewx_conf,
            ):
                resp = client.post(
                    "/setup/apply",
                    json=body,
                    headers={"Authorization": f"Bearer {session_id}"},
                )
        assert resp.status_code == 200, resp.text

        # Read the written api.conf and confirm logo_alt is there.
        conf_path = config_dir / "api.conf"
        assert conf_path.exists(), "api.conf must be written"
        written = configobj.ConfigObj(str(conf_path), interpolation=False)
        assert written.get("branding", {}).get("logo_alt") == "Persisted alt text"

    def test_apply_without_logo_alt_does_not_overwrite_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sending branding block without logo_alt leaves existing logo_alt intact.

        Two sequential /setup/apply calls: first sets logo_alt, second omits it.
        The second call must not clear the logo_alt set by the first.
        Uses proxy_secret so the second apply (after setup_complete) can authenticate.
        """
        proxy_secret = "test-proxy-secret-preserve"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        # First apply: set logo_alt and proxy_secret (marks setup_complete).
        first_body = _minimal_apply_body({
            "proxy_secret": proxy_secret,
            "branding": {
                "logo_light_url": "https://example.com/logo.png",
                "logo_alt": "Original alt text",
            }
        })
        # Second apply: send branding without logo_alt → should not overwrite.
        second_body = _minimal_apply_body({
            "branding": {
                "site_title": "Updated Title",
            }
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            fake_weewx_conf = {"StdReport": {"SKIN_ROOT": str(tmp_path / "skins")}}
            with patch(
                "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
                return_value=fake_weewx_conf,
            ):
                resp1 = client.post(
                    "/setup/apply",
                    json=first_body,
                    headers={"Authorization": f"Bearer {session_id}"},
                )
                assert resp1.status_code == 200, resp1.text
                # Second apply uses proxy auth (setup now complete).
                resp2 = client.post(
                    "/setup/apply",
                    json=second_body,
                    headers={"X-Clearskies-Proxy-Auth": proxy_secret},
                )
                assert resp2.status_code == 200, resp2.text

        conf_path = config_dir / "api.conf"
        written = configobj.ConfigObj(str(conf_path), interpolation=False)
        # logo_alt was set in first apply and not touched in second apply.
        assert written.get("branding", {}).get("logo_alt") == "Original alt text"
