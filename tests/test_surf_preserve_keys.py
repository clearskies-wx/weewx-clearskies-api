"""Known-answer tests for surf-level config-file-owned field preservation.

Operator ruling 2026-08-03 (AUDIT-OPUS-WINDOW-2026-08-03.md decision item 8):
the wizard/admin apply flow deliberately replaces the whole ``[marine]``
section on every apply so deleted locations don't persist (setup.py:1946's
own comment). Six surf-level fields have no wizard/admin UI at all
(``max_hs_m``, ``beach_slope``, ``transect_spacing_m``, ``l3_enabled``,
``breaker_formula``, ``surf_height_display``) and were previously either
reset to a hardcoded model default (``max_hs_m``) or silently deleted
(``beach_slope``, not on the Pydantic model at all) on every apply from
either surface. The ruling: these six become CONFIG-FILE-OWNED —
preserved from the existing api.conf when absent from the payload;
payload-present still wins; a location removed from the payload stays
removed (no resurrection of any of its keys).

Covers:
  - KAT-1: apply WITHOUT the six fields, against an existing config
    carrying custom values -> all six survive verbatim in the written
    api.conf.
  - KAT-2: apply WITH explicit values for the five modeled fields ->
    payload wins.
  - KAT-3: fresh location, fields absent -> today's documented defaults
    written for the three fields that carried a hard model default before
    this change (max_hs_m 4.0, transect_spacing_m 10.0, l3_enabled auto);
    beach_slope stays absent (breaker_formula/surf_height_display get their
    pre-change explicit defaults per aud-r1 F1; only beach_slope had no
    config-file default at write time for those three).
  - KAT-4: a location removed from the payload is fully gone (no key
    resurrection) on the next apply.

httpx/weewx conf are mocked/faked throughout — no real network or weewx
process is touched. Pattern mirrors test_branding_logo_alt.py's
integration harness (``_make_setup_app`` / two-sequential-apply /
proxy-auth-after-setup-complete).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import configobj
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Harness (mirrors test_branding_logo_alt.py)
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


def _minimal_marine_surf(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Required-only surf sub-block; overrides layer the optional six on top."""
    surf: dict[str, Any] = {
        "segment_start_lat": 33.6560,
        "segment_start_lon": -118.0030,
        "segment_end_lat": 33.6570,
        "segment_end_lon": -118.0020,
        "bottom_type": "sand",
        "topographic_feature": "point_break",
    }
    if overrides:
        surf.update(overrides)
    return surf


def _marine_apply_body(
    *,
    locations: list[dict[str, Any]],
    proxy_secret: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "database": {
            "host": "localhost",
            "port": 3306,
            "user": "weewx",
            "password": "secret",
            "name": "weewx",
        },
        "marine": {"locations": locations},
    }
    if proxy_secret:
        body["proxy_secret"] = proxy_secret
    return body


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


def _read_surf_section(config_dir: Path, location_id: str) -> dict[str, Any]:
    conf_path = config_dir / "api.conf"
    written = configobj.ConfigObj(str(conf_path), interpolation=False)
    return written["marine"]["locations"][location_id]["surf"]


# ---------------------------------------------------------------------------
# KAT-1: absence preserves existing config-file value (all six fields).
# ---------------------------------------------------------------------------


class TestSurfPreserveOnOmission:
    def test_six_fields_survive_omission_from_second_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-surf-preserve"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)

            # First apply: set custom values for the five modeled fields,
            # and mark setup complete via proxy_secret.
            first_surf = _minimal_marine_surf({
                "max_hs_m": 6.5,
                "transect_spacing_m": 15.0,
                "l3_enabled": "on",
                "breaker_formula": "caldwell",
                "surf_height_display": "hawaiian",
            })
            first_body = _marine_apply_body(
                locations=[{
                    "id": "test_spot",
                    "name": "Test Spot",
                    "lat": 33.6565,
                    "lon": -118.0025,
                    "activities": ["surf"],
                    "surf": first_surf,
                }],
                proxy_secret=proxy_secret,
            )
            resp1 = _apply(client, tmp_path, first_body, session_id=session_id)
            assert resp1.status_code == 200, resp1.text

            # beach_slope has no apply-model field at all (never accepted
            # from any client) -- seed it directly into api.conf the way a
            # config-file-owned value gets there in practice.
            conf_path = config_dir / "api.conf"
            cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
            cfg["marine"]["locations"]["test_spot"]["surf"]["beach_slope"] = "0.05"
            cfg.write()

            before = _read_surf_section(config_dir, "test_spot")
            assert before["max_hs_m"] == "6.5"
            assert before["beach_slope"] == "0.05"

            # Second apply: omit all six fields entirely.
            second_surf = _minimal_marine_surf()
            second_body = _marine_apply_body(
                locations=[{
                    "id": "test_spot",
                    "name": "Test Spot",
                    "lat": 33.6565,
                    "lon": -118.0025,
                    "activities": ["surf"],
                    "surf": second_surf,
                }],
            )
            resp2 = _apply(client, tmp_path, second_body, proxy_secret=proxy_secret)
            assert resp2.status_code == 200, resp2.text

        after = _read_surf_section(config_dir, "test_spot")
        assert after["max_hs_m"] == "6.5", "max_hs_m must survive omission, not reset to 4.0"
        assert after["transect_spacing_m"] == "15.0"
        assert after["l3_enabled"] == "on"
        assert after["breaker_formula"] == "caldwell"
        assert after["surf_height_display"] == "hawaiian"
        assert after["beach_slope"] == "0.05", "beach_slope must survive, not be deleted"


# ---------------------------------------------------------------------------
# KAT-2: payload-present wins over existing config-file value.
# ---------------------------------------------------------------------------


class TestSurfPayloadWins:
    def test_explicit_payload_values_overwrite_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-payload-wins"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)

            first_surf = _minimal_marine_surf({
                "max_hs_m": 6.5,
                "transect_spacing_m": 15.0,
                "l3_enabled": "on",
                "breaker_formula": "caldwell",
                "surf_height_display": "hawaiian",
            })
            first_body = _marine_apply_body(
                locations=[{
                    "id": "test_spot",
                    "name": "Test Spot",
                    "lat": 33.6565,
                    "lon": -118.0025,
                    "activities": ["surf"],
                    "surf": first_surf,
                }],
                proxy_secret=proxy_secret,
            )
            resp1 = _apply(client, tmp_path, first_body, session_id=session_id)
            assert resp1.status_code == 200, resp1.text

            # Second apply: explicit different values for all five modeled fields.
            second_surf = _minimal_marine_surf({
                "max_hs_m": 2.0,
                "transect_spacing_m": 20.0,
                "l3_enabled": "off",
                "breaker_formula": "komar_gaughan",
                "surf_height_display": "face",
            })
            second_body = _marine_apply_body(
                locations=[{
                    "id": "test_spot",
                    "name": "Test Spot",
                    "lat": 33.6565,
                    "lon": -118.0025,
                    "activities": ["surf"],
                    "surf": second_surf,
                }],
            )
            resp2 = _apply(client, tmp_path, second_body, proxy_secret=proxy_secret)
            assert resp2.status_code == 200, resp2.text

        after = _read_surf_section(config_dir, "test_spot")
        assert after["max_hs_m"] == "2.0"
        assert after["transect_spacing_m"] == "20.0"
        assert after["l3_enabled"] == "off"
        assert after["breaker_formula"] == "komar_gaughan"
        assert after["surf_height_display"] == "face"


# ---------------------------------------------------------------------------
# KAT-3: fresh location, fields absent -> today's documented defaults.
# ---------------------------------------------------------------------------


class TestSurfFreshLocationDefaults:
    def test_fresh_location_gets_documented_defaults(self, tmp_path: Path) -> None:
        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)

            surf = _minimal_marine_surf()  # omits all six fields
            body = _marine_apply_body(
                locations=[{
                    "id": "fresh_spot",
                    "name": "Fresh Spot",
                    "lat": 33.6565,
                    "lon": -118.0025,
                    "activities": ["surf"],
                    "surf": surf,
                }],
            )
            resp = _apply(client, tmp_path, body, session_id=session_id)
            assert resp.status_code == 200, resp.text

        surf_out = _read_surf_section(config_dir, "fresh_spot")
        # Byte-identical to pre-change output for EVERY key the old
        # Field(default=...) + str(surf.X) write path produced (aud-r1 F1:
        # pre-change code always wrote breaker_formula/surf_height_display
        # explicitly for a fresh location too — the first cut of this test
        # wrongly pinned their absence).
        assert surf_out["max_hs_m"] == "4.0"
        assert surf_out["transect_spacing_m"] == "10.0"
        assert surf_out["l3_enabled"] == "auto"
        assert surf_out["breaker_formula"] == "komar_gaughan"
        assert surf_out["surf_height_display"] == "face"
        # beach_slope alone was never written pre-change (never on the
        # model) — absent here, relying on the read-time default in
        # _serialize_marine_locations_section.
        assert "beach_slope" not in surf_out


# ---------------------------------------------------------------------------
# KAT-4: a location removed from the payload stays removed.
# ---------------------------------------------------------------------------


class TestSurfLocationDeletionStays:
    def test_deleted_location_and_its_surf_keys_are_fully_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_secret = "test-proxy-secret-deletion"
        monkeypatch.delenv("WEEWX_CLEARSKIES_PROXY_SECRET", raising=False)

        app, config_dir = _make_setup_app(tmp_path / "app")
        trust_token = app.state.trust_manager.token
        assert trust_token is not None

        with TestClient(app, raise_server_exceptions=False) as client:
            session_id = _get_session_token(client, trust_token)

            first_body = _marine_apply_body(
                locations=[
                    {
                        "id": "spot_a",
                        "name": "Spot A",
                        "lat": 33.6565,
                        "lon": -118.0025,
                        "activities": ["surf"],
                        "surf": _minimal_marine_surf({"max_hs_m": 5.5}),
                    },
                    {
                        "id": "spot_b",
                        "name": "Spot B",
                        "lat": 34.0,
                        "lon": -119.0,
                        "activities": ["surf"],
                        "surf": _minimal_marine_surf({"max_hs_m": 3.3}),
                    },
                ],
                proxy_secret=proxy_secret,
            )
            resp1 = _apply(client, tmp_path, first_body, session_id=session_id)
            assert resp1.status_code == 200, resp1.text

            conf_path = config_dir / "api.conf"
            written1 = configobj.ConfigObj(str(conf_path), interpolation=False)
            assert "spot_a" in written1["marine"]["locations"]

            # Second apply: spot_a omitted entirely.
            second_body = _marine_apply_body(
                locations=[
                    {
                        "id": "spot_b",
                        "name": "Spot B",
                        "lat": 34.0,
                        "lon": -119.0,
                        "activities": ["surf"],
                        "surf": _minimal_marine_surf({"max_hs_m": 3.3}),
                    },
                ],
            )
            resp2 = _apply(client, tmp_path, second_body, proxy_secret=proxy_secret)
            assert resp2.status_code == 200, resp2.text

        written2 = configobj.ConfigObj(str(conf_path), interpolation=False)
        assert "spot_a" not in written2["marine"]["locations"], (
            "a location removed from the payload must stay removed, "
            "with no resurrection of any of its keys"
        )
        assert "spot_b" in written2["marine"]["locations"]
