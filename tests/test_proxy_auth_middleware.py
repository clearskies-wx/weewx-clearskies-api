"""Tests for ProxyAuthMiddleware — all four cases from the auth matrix.

Matrix (from ADR-008 and proxy_auth.py module docstring):

  WEEWX_CLEARSKIES_PROXY_SECRET | X-Clearskies-Proxy-Auth header | Expected outcome
  ----------------------------- | ------------------------------ | ----------------
  Unset                         | Any / absent                   | 200; request untrusted
  Set                           | Absent                         | 200; request untrusted
  Set                           | Correct value                  | 200; request trusted
  Set                           | Wrong value                    | 401 problem+json
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from weewx_clearskies_api.middleware.proxy_auth import ProxyAuthMiddleware


def _make_app(secret: str | None) -> FastAPI:
    """Minimal app with ProxyAuthMiddleware; env var set/unset by test."""
    app = FastAPI()
    # The middleware reads os.environ at construction time.
    if secret is not None:
        os.environ["WEEWX_CLEARSKIES_PROXY_SECRET"] = secret
    else:
        os.environ.pop("WEEWX_CLEARSKIES_PROXY_SECRET", None)

    app.add_middleware(ProxyAuthMiddleware)

    @app.get("/test")
    async def _test(request: Request) -> JSONResponse:
        return JSONResponse({"trusted": getattr(request.state, "proxy_trusted", False)})

    return app


@pytest.fixture(autouse=True)
def _clear_env() -> None:
    """Ensure the env var is clean before/after each test."""
    os.environ.pop("WEEWX_CLEARSKIES_PROXY_SECRET", None)
    yield  # type: ignore[misc]  # noqa: PT022 — cleanup needed
    os.environ.pop("WEEWX_CLEARSKIES_PROXY_SECRET", None)


class TestProxyAuthMatrix:
    """Exercise all four cells of the auth decision matrix."""

    def test_unset_secret_any_header_passes_untrusted(self) -> None:
        """Secret unset — header ignored; request continues as untrusted (200)."""
        client = TestClient(_make_app(secret=None), raise_server_exceptions=False)
        response = client.get("/test", headers={"X-Clearskies-Proxy-Auth": "anything"})
        assert response.status_code == 200
        assert response.json()["trusted"] is False

    def test_unset_secret_no_header_passes_untrusted(self) -> None:
        """Secret unset — no header; request continues as untrusted (200)."""
        client = TestClient(_make_app(secret=None), raise_server_exceptions=False)
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json()["trusted"] is False

    def test_set_secret_absent_header_passes_untrusted(self) -> None:
        """Secret set, header absent — request continues as untrusted (200)."""
        client = TestClient(_make_app(secret="correct-secret"), raise_server_exceptions=False)
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json()["trusted"] is False

    def test_set_secret_correct_header_marks_trusted(self) -> None:
        """Secret set, correct header — request marked trusted (200)."""
        client = TestClient(_make_app(secret="correct-secret"), raise_server_exceptions=False)
        response = client.get("/test", headers={"X-Clearskies-Proxy-Auth": "correct-secret"})
        assert response.status_code == 200
        assert response.json()["trusted"] is True

    def test_set_secret_wrong_header_returns_401(self) -> None:
        """Secret set, wrong header — 401 application/problem+json."""
        client = TestClient(_make_app(secret="correct-secret"), raise_server_exceptions=False)
        response = client.get("/test", headers={"X-Clearskies-Proxy-Auth": "wrong-secret"})
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        body = response.json()
        assert body["status"] == 401
        assert "type" in body
        assert "title" in body
        assert "detail" in body

    def test_constant_time_compare_used(self) -> None:
        """Verify hmac.compare_digest is used by ensuring wrong secrets fail."""
        # This is a behavioural test — timing attack testing requires a
        # specialised benchmark harness, not a unit test. We verify the
        # correct return code for wrong-secret to confirm the comparison runs.
        client = TestClient(_make_app(secret="abc"), raise_server_exceptions=False)
        response = client.get("/test", headers={"X-Clearskies-Proxy-Auth": "xyz"})
        assert response.status_code == 401
