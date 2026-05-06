"""Request-ID middleware.

Generates a UUID4 per request and stores it in:
  1. The request_id_var ContextVar (read by RequestIdFilter for log records).
  2. The X-Request-ID response header (useful for correlating logs with
     client-side errors).

This middleware is registered OUTERMOST so every subsequent middleware and
handler runs within its context — request_id is available in all log records.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from weewx_clearskies_api.logging.redaction_filter import request_id_var


class RequestIdMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that assigns a UUID4 request-id to every request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Honour an incoming X-Request-ID if the proxy injected one, otherwise
        # generate a new UUID. Inbound value is not trusted for security decisions —
        # it's only used for log correlation.
        incoming = request.headers.get("X-Request-ID", "").strip()
        request_id = incoming if incoming else str(uuid.uuid4())

        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        return response
