"""Optional proxy-auth middleware per ADR-008.

Behaviour matrix (load-bearing comment — this is non-obvious behaviour):

  WEEWX_CLEARSKIES_PROXY_SECRET env var | X-Clearskies-Proxy-Auth header | Outcome
  --------------------------------------|--------------------------------|--------
  Unset (empty)                         | Any / absent                   | Ignored; request continues as untrusted; rate-limit applies.
  Set                                   | Absent                         | Request continues as untrusted; rate-limit applies.
  Set                                   | Correct value                  | request.state.proxy_trusted = True; rate-limit bypassed.
  Set                                   | Wrong value                    | 401 application/problem+json returned immediately.

The constant-time comparison (hmac.compare_digest) prevents timing attacks
that would leak the secret length or content. This is required even though
the secret is on the wire in plaintext — defence-in-depth.

The environment variable name is WEEWX_CLEARSKIES_PROXY_SECRET per ADR-008
lines 55-56 and ADR-027 §3 naming convention (WEEWX_CLEARSKIES_<DOMAIN>_<FIELD>).
"""

from __future__ import annotations

import hmac
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from weewx_clearskies_api.errors import PROBLEM_BASE_URI, build_problem_response

logger = logging.getLogger(__name__)

_HEADER_NAME = "X-Clearskies-Proxy-Auth"
_ENV_VAR = "WEEWX_CLEARSKIES_PROXY_SECRET"


class ProxyAuthMiddleware(BaseHTTPMiddleware):
    """Optional shared-secret check per ADR-008.

    See module docstring for the full behaviour matrix.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # Secret is read once at construction time. If the env var changes
        # at runtime (unusual), a process restart is required.
        self._secret: str = os.environ.get(_ENV_VAR, "").strip()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Initialise proxy_trusted state for this request.
        request.state.proxy_trusted = False

        if not self._secret:
            # Secret not configured — header silently ignored; request untrusted.
            return await call_next(request)

        provided = request.headers.get(_HEADER_NAME, "")

        # hmac.compare_digest requires both args to be the same type.
        # Both are already str here; encode to bytes for the comparison to
        # guarantee constant-time regardless of unicode folding.
        expected_bytes = self._secret.encode("utf-8")
        provided_bytes = provided.encode("utf-8")

        if not provided:
            # Secret configured, header absent — untrusted, continue normally.
            # Rate-limit middleware will apply its normal quota.
            return await call_next(request)

        if not hmac.compare_digest(provided_bytes, expected_bytes):
            # Secret configured, header present but wrong — 401.
            logger.warning(
                "Proxy-auth failed: incorrect X-Clearskies-Proxy-Auth",
                extra={"path": str(request.url.path)},
            )
            return self._unauthorized(request)

        # Secret configured, header present and correct — mark as trusted.
        request.state.proxy_trusted = True
        return await call_next(request)

    def _unauthorized(self, request: Request) -> Response:
        return build_problem_response(
            status=401,
            title="Unauthorized",
            detail="Invalid or missing proxy authentication.",
            request=request,
            problem_type=f"{PROBLEM_BASE_URI}/unauthorized",
        )
