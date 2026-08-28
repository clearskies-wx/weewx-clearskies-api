"""``ww3_chain_enabled`` is a config-file-owned per-location key carried by
the marine config push (2026-08-28; operator: "this is for internal testing
purposes, but it still needs set").

Before this change the API never carried the key at all (no occurrence of
``chain_enabled`` anywhere in this repo) although API-MANUAL §19.7a claimed
it transited the push -- it existed only as a hand-placed value in the
marine service's own marine.conf, and every push erased it (the buoy
scorecard ledger went silent after 2026-08-27 12Z).

Guards (each FAILS on the pre-change API, HEAD 33d4f53):
  KAT-1: ``_serialize_marine_locations_section`` carries the key as a bool
         when api.conf has it, and omits it when api.conf does not.
  KAT-2: an apply that OMITS the key preserves the existing api.conf value
         (config-file-owned, same treatment as the station IDs).
  KAT-3: an apply that SUPPLIES the key writes it (payload wins); the
         wizard model accepts it (extra="forbid" would 422 pre-change).

Harness mirrors tests/test_surf_preserve_keys.py (repo convention: no
cross-test-file imports).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import configobj
import pytest
from fastapi.testclient import TestClient


def _make_setup_app(tmp_path: Path) -> tuple[Any, Path]:
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
    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
    trust_manager = TrustManager(secrets_path=config_dir / "secrets.env")
    app = create_app(settings)
    app.state.trust_manager = trust_manager
    app.state.settings = settings
    app.state.config_dir = config_dir
    return app, config_dir


def _get_session_token(client: TestClient, trust_token: str) -> str:
    resp = client.post("/setup/handshake", json={"token": trust_token})
    assert resp.status_code == 200, f"Handshake failed: {resp.text}"
    return resp.json()["session_id"]


def _location(**extra: Any) -> dict[str, Any]:
    loc: dict[str, Any] = {
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
    loc.update(extra)
    return loc


def _body(locations: list[dict[str, Any]], proxy_secret: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "database": {"host": "localhost", "port": 3306, "user": "weewx",
                     "password": "secret", "name": "weewx"},
        "marine": {"locations": locations},
    }
    if proxy_secret:
        body["proxy_secret"] = proxy_secret
    return body


def _apply(client: TestClient, tmp_path: Path, body: dict[str, Any], *,
           session_id: str | None = None, proxy_secret: str | None = None):
    fake_weewx_conf = {"StdReport": {"SKIN_ROOT": str(tmp_path / "skins")}}
    headers = ({"X-Clearskies-Proxy-Auth": proxy_secret} if proxy_secret
               else {"Authorization": f"Bearer {session_id}"})
    with patch("weewx_clearskies_api.endpoints.setup.get_weewx_conf",
               return_value=fake_weewx_conf):
        return client.post("/setup/apply", json=body, headers=headers)


def _read_loc(config_dir: Path) -> dict[str, Any]:
    cfg = configobj.ConfigObj(str(config_dir / "api.conf"), interpolation=False)
    return cfg["marine"]["locations"]["test_spot"]


# KAT-1 ---------------------------------------------------------------------

def test_serializer_carries_ww3_chain_enabled_only_when_present(tmp_path: Path) -> None:
    from weewx_clearskies_api.endpoints.setup import _serialize_marine_locations_section

    with_flag = {"locations": {"spot_a": {"name": "A", "lat": "33.6", "lon": "-118.0",
                                          "activities": ["surf"],
                                          "ww3_chain_enabled": "true"},
                               "spot_b": {"name": "B", "lat": "33.7", "lon": "-118.1",
                                          "activities": ["marine"]}}}
    out = _serialize_marine_locations_section(with_flag)
    assert out["locations"]["spot_a"]["ww3_chain_enabled"] is True
    assert "ww3_chain_enabled" not in out["locations"]["spot_b"]

    off = {"locations": {"spot_a": {"name": "A", "lat": "33.6", "lon": "-118.0",
                                    "activities": ["surf"], "ww3_chain_enabled": "false"}}}
    assert _serialize_marine_locations_section(off)["locations"]["spot_a"]["ww3_chain_enabled"] is False


# KAT-2 / KAT-3 -------------------------------------------------------------

def test_apply_preserves_flag_when_omitted_and_writes_it_when_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_secret = "test-proxy-secret-chain-flag"
    monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)
    app, config_dir = _make_setup_app(tmp_path / "app")
    trust_token = app.state.trust_manager.token
    assert trust_token is not None

    with TestClient(app, raise_server_exceptions=False) as client:
        session_id = _get_session_token(client, trust_token)

        # KAT-3 (write): the wizard model accepts the key and writes it.
        resp1 = _apply(client, tmp_path,
                       _body([_location(ww3_chain_enabled=True)], proxy_secret=proxy_secret),
                       session_id=session_id)
        assert resp1.status_code == 200, resp1.text
        assert _read_loc(config_dir)["ww3_chain_enabled"] == "true"

        # KAT-2 (preserve): a second apply that omits the key keeps it.
        resp2 = _apply(client, tmp_path, _body([_location()]), proxy_secret=proxy_secret)
        assert resp2.status_code == 200, resp2.text
        assert _read_loc(config_dir)["ww3_chain_enabled"] == "true"

        # KAT-3 (payload wins): an explicit false overrides the stored true.
        resp3 = _apply(client, tmp_path, _body([_location(ww3_chain_enabled=False)]),
                       proxy_secret=proxy_secret)
        assert resp3.status_code == 200, resp3.text
        assert _read_loc(config_dir)["ww3_chain_enabled"] == "false"

        # And the ONE serializer both push and pull use carries it.
        from weewx_clearskies_api.endpoints.setup import _build_marine_service_config_payload
        payload = _build_marine_service_config_payload(config_dir)
        assert payload["marine"]["locations"]["test_spot"]["ww3_chain_enabled"] is False
