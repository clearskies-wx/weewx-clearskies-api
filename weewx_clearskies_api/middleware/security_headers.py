"""Security-headers middleware.

Adds the security response headers that the API service is responsible for.
Per ADR-037 and security baseline §3.1, HSTS / CSP / X-Frame-Options are
set by the reverse proxy (Caddy / Apache / nginx), NOT here. This avoids
double-setting headers and keeps the inner-service headers minimal.

Headers set here:
  X-Content-Type-Options: nosniff   — prevents MIME-type sniffing attacks
  Referrer-Policy: no-referrer      — no referrer data leaked from API responses
  Server: (removed)                 — suppressed to avoid fingerprinting

Headers NOT set here (proxy's responsibility per ADR-037):
  Strict-Transport-Security (HSTS)
  Content-Security-Policy (CSP)
  X-Frame-Options
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers and suppress Server: on every response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"

        # Suppress Server: header to avoid fingerprinting the framework/version.
        # Starlette/uvicorn set "uvicorn" by default; we remove it entirely.
        if "server" in response.headers:
            del response.headers["server"]

        return response
