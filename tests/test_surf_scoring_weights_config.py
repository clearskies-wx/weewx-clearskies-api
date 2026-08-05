"""Round S deltas a+b+c (2026-08-05): system-wide surf score weights flow
through the existing admin -> API -> marine ``/config`` path (ADR-101
Consequences; EYEBALL-FIX-PLAN-2026-08-04.md §S3, operator-approved
2026-08-04 "adr 101 approved").

(a) ``ApplyRequest.surf_scoring`` (``SurfScoringWeightsApplyConfig``,
    extra="forbid", whole-section-or-absent, all five weights > 0) writes a
    TOP-LEVEL ``[surf_scoring]`` section in api.conf -- never nested under
    ``[marine]``, so the existing replace-whole-``[marine]``-section save
    can never clobber it.
(b) ``_build_marine_service_config_payload()`` attaches
    ``payload["surf_scoring"]`` when the section exists -- the ONE
    serializer used by both the push path (POST to the marine service) and
    the pull path (``GET /setup/marine/config``, marine startup recovery).
(c) ``CurrentConfigResponse.surf_scoring`` is read from the same top-level
    section so the admin form can pre-fill via the authoritative read path.

Self-contained harness mirroring tests/test_branding_logo_alt.py and
tests/test_surf_preserve_keys.py (repo convention: no cross-test-file
imports). Dev-owned per the repo's established convention for this file
family (test_surf_preserve_keys.py, test_current_config_imagery_prefill.py
-- both implementer-authored, landed with their code change).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import configobj
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Harness (mirrors test_surf_preserve_keys.py / test_current_config_imagery_prefill.py)
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


_VALID_WEIGHTS: dict[str, Any] = {
    "weight_size": 0.25,
    "weight_shape": 0.25,
    "weight_conditions": 0.2,
    "weight_power": 0.2,
    "weight_consistency": 0.1,
}


def _apply(
    client: TestClient,
    tmp_path: Path,
    body: dict[str, Any],
    *,
    session_id: str | None = None,
    proxy_secret: str | None = None,
):
    fake_weewx_conf = {"StdReport": {"SKIN_ROOT": str(tmp_path / "skins")}}
    headers = (
        {"X-Clearskies-Proxy-Auth": proxy_secret}
        if proxy_secret
        else {"Authorization": f"Bearer {session_id}"}
    )
    with patch(
        "weewx_clearskies_api.endpoints.setup.get_weewx_conf",
        return_value=fake_weewx_conf,
    ):
        return client.post("/setup/apply", json=body, headers=headers)


# ---------------------------------------------------------------------------
# (a) apply accepts and persists the section, top-level, not under [marine]
# ---------------------------------------------------------------------------


class TestSurfScoringApplyPersists:
    def test_section_written_top_level_not_under_marine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-surf-scoring"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({
            "proxy_secret": proxy_secret,
            "surf_scoring": dict(_VALID_WEIGHTS),
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            resp = _apply(client, tmp_path, body, session_id=session_id)
        assert resp.status_code == 200, resp.text

        written = configobj.ConfigObj(str(config_dir / "api.conf"), interpolation=False)
        assert "surf_scoring" in written, "must be a TOP-LEVEL section"
        assert written["surf_scoring"]["weight_size"] == "0.25"
        assert written["surf_scoring"]["weight_shape"] == "0.25"
        assert written["surf_scoring"]["weight_conditions"] == "0.2"
        assert written["surf_scoring"]["weight_power"] == "0.2"
        assert written["surf_scoring"]["weight_consistency"] == "0.1"
        # Never nested under [marine].
        marine_section = written.get("marine", {})
        assert "surf_scoring" not in marine_section


# ---------------------------------------------------------------------------
# (a) reject <=0 / malformed at the form (422)
# ---------------------------------------------------------------------------


class TestSurfScoringRejectsInvalid:
    @pytest.mark.parametrize(
        "bad_weights",
        [
            {**_VALID_WEIGHTS, "weight_size": 0},
            {**_VALID_WEIGHTS, "weight_shape": -0.1},
            {**_VALID_WEIGHTS, "weight_conditions": "not-a-number"},
        ],
        ids=["zero", "negative", "malformed-string"],
    )
    def test_invalid_weight_rejected_with_422(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_weights: dict[str, Any]
    ) -> None:
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)
        app, _config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({"surf_scoring": bad_weights})

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            resp = _apply(client, tmp_path, body, session_id=session_id)
        assert resp.status_code == 422, resp.text

    def test_partial_section_rejected_extra_forbid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whole-section-or-absent: a payload missing a required weight key
        is rejected (Pydantic ``required field missing``), not silently
        partially applied."""
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)
        app, _config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        partial = dict(_VALID_WEIGHTS)
        del partial["weight_consistency"]
        body = _minimal_apply_body({"surf_scoring": partial})

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            resp = _apply(client, tmp_path, body, session_id=session_id)
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# marine-locations-only save preserves the section
# ---------------------------------------------------------------------------


class TestSurfScoringSurvivesMarineOnlySave:
    def test_marine_locations_only_apply_preserves_surf_scoring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-marine-preserve"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)

            first_body = _minimal_apply_body({
                "proxy_secret": proxy_secret,
                "surf_scoring": dict(_VALID_WEIGHTS),
            })
            resp1 = _apply(client, tmp_path, first_body, session_id=session_id)
            assert resp1.status_code == 200, resp1.text

            # Second apply: [marine] locations only, surf_scoring omitted
            # entirely -- the [marine]-section-replace must NOT touch the
            # sibling top-level [surf_scoring] section.
            second_body = _minimal_apply_body({
                "marine": {
                    "locations": [
                        {
                            "id": "test_spot",
                            "name": "Test Spot",
                            "lat": 33.6565,
                            "lon": -118.0025,
                            "activities": ["surf"],
                            "surf": {
                                "segment_start_lat": 33.6560,
                                "segment_start_lon": -118.0030,
                                "segment_end_lat": 33.6570,
                                "segment_end_lon": -118.0020,
                                "bottom_type": "sand",
                                "topographic_feature": "point_break",
                            },
                        }
                    ]
                },
            })
            resp2 = _apply(client, tmp_path, second_body, proxy_secret=proxy_secret)
            assert resp2.status_code == 200, resp2.text

        written = configobj.ConfigObj(str(config_dir / "api.conf"), interpolation=False)
        assert "test_spot" in written["marine"]["locations"]
        assert written["surf_scoring"]["weight_size"] == "0.25", (
            "surf_scoring must survive a marine-locations-only apply"
        )


# ---------------------------------------------------------------------------
# (b) serializer attaches surf_scoring; (c) current-config round-trips it
# ---------------------------------------------------------------------------


class TestSurfScoringSerializerAndCurrentConfig:
    def test_build_marine_service_config_payload_attaches_surf_scoring(
        self, tmp_path: Path
    ) -> None:
        """Direct unit test of the ONE serializer both the push path (POST
        to the marine service) and the pull path (GET /setup/marine/config)
        call -- covers both by construction (module docstring "one
        serializer, both paths")."""
        from weewx_clearskies_api.endpoints.setup import (
            _build_marine_service_config_payload,
        )

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        conf_path = config_dir / "api.conf"
        cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
        cfg["surf_scoring"] = dict(_VALID_WEIGHTS)
        cfg.write()

        payload = _build_marine_service_config_payload(config_dir)
        assert payload["surf_scoring"] == {
            "weight_size": 0.25,
            "weight_shape": 0.25,
            "weight_conditions": 0.2,
            "weight_power": 0.2,
            "weight_consistency": 0.1,
        }

    def test_absent_section_omitted_from_payload(self, tmp_path: Path) -> None:
        from weewx_clearskies_api.endpoints.setup import (
            _build_marine_service_config_payload,
        )

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        conf_path = config_dir / "api.conf"
        cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
        cfg["marine"] = {"locations": {}}
        cfg.write()

        payload = _build_marine_service_config_payload(config_dir)
        assert "surf_scoring" not in payload

    def test_current_config_round_trips_saved_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-current-config"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, _config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({
            "proxy_secret": proxy_secret,
            "surf_scoring": dict(_VALID_WEIGHTS),
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            apply_resp = _apply(client, tmp_path, body, session_id=session_id)
            assert apply_resp.status_code == 200, apply_resp.text

            cfg_resp = client.get(
                "/setup/current-config",
                headers={"X-Clearskies-Proxy-Auth": proxy_secret},
            )
        assert cfg_resp.status_code == 200, cfg_resp.text
        surf_scoring = cfg_resp.json()["surf_scoring"]
        assert surf_scoring == {
            "weight_size": 0.25,
            "weight_shape": 0.25,
            "weight_conditions": 0.2,
            "weight_power": 0.2,
            "weight_consistency": 0.1,
        }

    def test_current_config_absent_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-current-config-absent"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, _config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        body = _minimal_apply_body({"proxy_secret": proxy_secret})

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)
            apply_resp = _apply(client, tmp_path, body, session_id=session_id)
            assert apply_resp.status_code == 200, apply_resp.text

            cfg_resp = client.get(
                "/setup/current-config",
                headers={"X-Clearskies-Proxy-Auth": proxy_secret},
            )
        assert cfg_resp.status_code == 200, cfg_resp.text
        assert cfg_resp.json()["surf_scoring"] is None
