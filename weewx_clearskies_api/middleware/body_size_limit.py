"""Body-size-limit middleware.

Rejects requests whose Content-Length exceeds max_bytes before the body
is consumed. This protects against memory exhaustion from large uploads.

Default: 1 MiB (security baseline §3.1). Configurable via
[api] max_request_bytes in api.conf.

Returns RFC 9457 application/problem+json with status 413.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from weewx_clearskies_api.errors import PROBLEM_BASE_URI, build_problem_response

_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with Content-Length > max_bytes (413 problem+json)."""

    def __init__(self, app: ASGIApp, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                size = int(content_length)
            except ValueError:
                # Malformed Content-Length — let FastAPI's own validation handle it.
                pass
            else:
                if size > self._max_bytes:
                    return self._too_large(request, size)

        return await call_next(request)

    def _too_large(self, request: Request, size: int) -> Response:
        return build_problem_response(
            status=413,
            title="Request Entity Too Large",
            detail=(
                f"Request body ({size} bytes) exceeds the configured limit "
                f"({self._max_bytes} bytes)."
            ),
            request=request,
            problem_type=f"{PROBLEM_BASE_URI}/request-too-large",
        )
