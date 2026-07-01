"""Tests for forecast correction admin endpoints in endpoints/setup.py (ADR-079 Phase 5).

Tests the three admin endpoints:
  GET  /setup/forecast-correction/status  (CorrectionStatusResponse)
  POST /setup/forecast-correction/toggle  (CorrectionToggleRequest/Response)
  POST /setup/forecast-correction/retrain (RetrainResponse)

Auth pattern: setup must be complete so proxy auth (X-Clearskies-Proxy-Auth) is
required.  Tests follow the pattern from test_branding_logo_alt.py.

Test cases:
1. Status returns correct response shape (all CorrectionStatusResponse fields present).
2. Status without proxy auth → 401.
3. Status with no DB wired → zeroed stats (not a 500 error).
4. Toggle changes enabled state.
5. Toggle changes collection_enabled.
6. Retrain with sufficient data → success=True and metrics returned.
7. Retrain with insufficient data → success=False, message mentions insufficient data.
8. Toggle with extra field → 422 (extra="forbid").
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from weewx_clearskies_api.correction import db as correction_db
from weewx_clearskies_api.config.settings import ForecastCorrectionSettings


# ---------------------------------------------------------------------------
# Shared proxy secret for all admin tests
# ---------------------------------------------------------------------------

_PROXY_SECRET = "test-correction-admin-proxy-secret-abc123"


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def _make_setup_app_with_correction(tmp_path: Path) -> tuple[Any, Path]:
    """Create a fully-wired setup-complete app for correction admin endpoint tests.

    Sets up:
    - A FastAPI app with all routes.
    - TrustManager in setup_complete=True state (proxy auth required for all setup endpoints).
    - WEEWX_CLEARSKIES_PROXY_SECRET env var set to _PROXY_SECRET.
    - ForecastCorrectionSettings wired into the setup router.
    - An in-memory SQLite correction DB wired into correction_db.
    """
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.trust import TrustManager, _write_secrets_env
    from weewx_clearskies_api.endpoints.setup import wire_forecast_correction_settings

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    secrets_path = config_dir / "secrets.env"

    # Write secrets.env with setup_complete + proxy secret so TrustManager reads them.
    _write_secrets_env(secrets_path, {
        "WEEWX_CLEARSKIES_SETUP_COMPLETE": "1",
        "WEEWX_CLEARSKIES_PROXY_SECRET": _PROXY_SECRET,
    })

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )

    trust_manager = TrustManager(secrets_path=secrets_path)
    assert trust_manager.setup_complete, "TrustManager must read SETUP_COMPLETE=1 from secrets.env"

    app = create_app(settings)
    app.state.trust_manager = trust_manager
    app.state.settings = settings
    app.state.config_dir = config_dir

    # Wire ForecastCorrectionSettings into the setup router.
    fc_settings = ForecastCorrectionSettings({
        "enabled": "true",
        "collection_enabled": "true",
        "min_samples": "100",
        "retention_years": "3",
    })
    fc_settings.model_path = str(tmp_path / "model.pkl")
    fc_settings.db_path = str(tmp_path / "correction.db")

    wire_forecast_correction_settings(fc_settings)

    return app, config_dir


def _make_correction_engine() -> Any:
    """Return an in-memory SQLite correction engine with tables created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    correction_db.wire_engine(engine)
    correction_db._create_tables(engine)
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SIXTY_DAYS_AGO = int(time.time()) - 60 * 86400


@pytest.fixture()
def correction_engine():
    """In-memory correction engine; resets module state on teardown."""
    engine = _make_correction_engine()
    yield engine
    correction_db._engine = None


@pytest.fixture(autouse=True)
def _reset_corrector_state():
    """Reset corrector and collector module state after each test."""
    yield
    import weewx_clearskies_api.correction.corrector as c
    c._model = None
    c._enabled = False
    c._model_path = None
    c._feature_medians = None

    from weewx_clearskies_api.correction.collector import set_collection_enabled
    set_collection_enabled(True)

    # Reset setup router module-level state.
    import weewx_clearskies_api.endpoints.setup as setup_mod
    setup_mod._forecast_correction_settings = None
    setup_mod._collection_enabled_override = None


@pytest.fixture(autouse=True)
def _set_proxy_env(monkeypatch):
    """Ensure WEEWX_CLEARSKIES_PROXY_SECRET is set for all tests in this module."""
    monkeypatch.setenv("WEEWX_CLEARSKIES_PROXY_SECRET", _PROXY_SECRET)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    return {"X-Clearskies-Proxy-Auth": _PROXY_SECRET}


def _insert_pairs(count: int, base_ts: int = _SIXTY_DAYS_AGO, bias: float = 2.0) -> None:
    for i in range(count):
        ts = base_ts + i * 1200
        correction_db.insert_pair(
            timestamp=ts,
            provider_id="openmeteo",
            month=6,
            hour=i % 24,
            day_of_year=180,
            fcst_temp=20.0,
            fcst_wind_dir=270.0,
            fcst_humidity=65.0,
            fcst_cloud_cover=40.0,
            fcst_wind_speed=12.0,
            actual_temp=20.0 + bias,
        )


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


class TestCorrectionStatus:
    def test_status_returns_correct_response_shape(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """GET /setup/forecast-correction/status returns all CorrectionStatusResponse fields."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/setup/forecast-correction/status",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        required_fields = [
            "model_available",
            "is_active",
            "enabled",
            "collection_enabled",
            "retrain_schedule",
            "pair_count",
            "date_range_start",
            "date_range_end",
            "last_trained",
            "sample_count",
            "mae_raw",
            "mae_corrected",
            "provider_score",
            "correction_pct",
            "training_status",
        ]
        for field in required_fields:
            assert field in body, (
                f"CorrectionStatusResponse missing field: {field!r}\n"
                f"Response body keys: {list(body.keys())}"
            )

    def test_status_without_proxy_auth_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """GET /setup/forecast-correction/status without auth header → 401."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/setup/forecast-correction/status")

        assert resp.status_code == 401, (
            f"Expected 401 without proxy auth, got {resp.status_code}: {resp.text}"
        )

    def test_status_with_no_db_wired_returns_zeroed_stats(
        self, tmp_path: Path
    ) -> None:
        """Status endpoint returns zeroed stats (not 500) when correction DB is not initialised."""
        # Do NOT call _make_correction_engine() — correction_db._engine stays None
        # (it was reset in the autouse _reset_corrector_state fixture teardown from
        # prior test, but more importantly we DON'T wire it here).
        correction_db._engine = None  # Explicit: no correction DB.

        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/setup/forecast-correction/status",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, (
            f"Status endpoint must not 500 when DB not wired; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["pair_count"] == 0, (
            f"pair_count must be 0 when DB not wired, got {body['pair_count']}"
        )
        assert body["model_available"] is False


# ---------------------------------------------------------------------------
# Toggle endpoint
# ---------------------------------------------------------------------------


class TestCorrectionToggle:
    def test_toggle_changes_enabled_state(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """POST toggle with enabled=true; follow-up status shows enabled=True."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            toggle_resp = client.post(
                "/setup/forecast-correction/toggle",
                json={"enabled": True},
                headers=_auth_headers(),
            )
            assert toggle_resp.status_code == 200, toggle_resp.text
            toggle_body = toggle_resp.json()
            assert toggle_body["enabled"] is True

            # Follow-up toggle to disable.
            disable_resp = client.post(
                "/setup/forecast-correction/toggle",
                json={"enabled": False},
                headers=_auth_headers(),
            )
            assert disable_resp.status_code == 200, disable_resp.text
            assert disable_resp.json()["enabled"] is False

    def test_toggle_changes_collection_enabled(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """POST toggle with collection_enabled=false; response shows collection_enabled=False."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/setup/forecast-correction/toggle",
                json={"collection_enabled": False},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["collection_enabled"] is False

    def test_toggle_extra_field_rejected_with_422(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """POST toggle with unknown field → 422 (CorrectionToggleRequest has extra='forbid')."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/setup/forecast-correction/toggle",
                json={"enabled": True, "bogus_field": 42},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422, (
            f"Expected 422 for extra field in toggle body, got {resp.status_code}: {resp.text}"
        )

    def test_toggle_without_proxy_auth_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """POST toggle without proxy auth header → 401."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/setup/forecast-correction/toggle",
                json={"enabled": True},
            )

        assert resp.status_code == 401, (
            f"Expected 401 without proxy auth, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Retrain endpoint
# ---------------------------------------------------------------------------


class TestCorrectionRetrain:
    def test_retrain_with_sufficient_data_returns_success(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """POST retrain with >=100 pairs → success=True and metrics in response."""
        _insert_pairs(count=200, bias=2.0)

        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/setup/forecast-correction/retrain",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, f"Retrain failed: {resp.text}"
        body = resp.json()

        assert body["success"] is True, (
            f"Expected success=True with sufficient data, got: {body}"
        )
        # Metrics must be present on success.
        assert body.get("mae_raw") is not None, "mae_raw must be present on successful retrain"
        assert body.get("mae_corrected") is not None, "mae_corrected must be present on success"
        assert body.get("provider_score") is not None, "provider_score must be present"
        assert body.get("correction_pct") is not None, "correction_pct must be present"
        assert body.get("sample_count", 0) >= 100, (
            f"sample_count must be >= 100, got {body.get('sample_count')}"
        )

    def test_retrain_with_insufficient_data_returns_success_false(
        self, tmp_path: Path, correction_engine: Any
    ) -> None:
        """POST retrain with 50 pairs (< min_samples=100) → success=False, message mentions insufficient."""
        _insert_pairs(count=50, bias=1.0)

        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/setup/forecast-correction/retrain",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}: {resp.text}"
        body = resp.json()

        assert body["success"] is False, (
            f"Expected success=False with insufficient data, got: {body}"
        )
        assert body.get("message"), "Message must be non-empty when success=False"
        msg_lower = body["message"].lower()
        # Message must mention the insufficiency: either "insufficient" or the counts.
        assert "insufficient" in msg_lower or "50" in body["message"] or "100" in body["message"], (
            f"Expected message about insufficient data, got: {body['message']!r}"
        )

    def test_retrain_without_proxy_auth_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """POST retrain without proxy auth → 401."""
        app, _ = _make_setup_app_with_correction(tmp_path)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/setup/forecast-correction/retrain")

        assert resp.status_code == 401, (
            f"Expected 401 without proxy auth, got {resp.status_code}: {resp.text}"
        )
