"""HTTP request metrics middleware per ADR-031.

Records http_requests_total and http_request_duration_seconds for every
request that reaches the main FastAPI app. Registered as the outermost
middleware (step 0) in app.py so the timing wraps all inner middleware.

Route template extraction:
  After call_next() completes, Starlette has matched the route and stored it
  in request.scope["route"]. We use route.path (e.g. "/api/v1/archive") as
  the endpoint label — low cardinality, no per-user or per-IP data.
  For unmatched paths (404 routes), we fall back to request.url.path, which
  carries the raw URL. This is acceptable since 404s are rare and their paths
  do not blow up cardinality in practice; operators can filter them by status.
"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from weewx_clearskies_api.metrics import (
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_TOTAL,
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record HTTP request count and duration per ADR-031."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start

        # Route template (low cardinality) — available after routing completes.
        route = request.scope.get("route")
        endpoint: str = (
            route.path  # type: ignore[union-attr]
            if route and hasattr(route, "path")
            else request.url.path
        )
        method = request.method
        status = str(response.status_code)

        HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=status).inc()
        HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)

        return response
