"""RFC 9457 application/problem+json error handling per ADR-018.

Every non-2xx response from this API is application/problem+json. This
module registers exception handlers on the FastAPI application so that:

  - FastAPI HTTPException → problem+json (replaces FastAPI's default JSON format)
  - Pydantic RequestValidationError → 422 problem+json
  - ProviderError → 502 or 503 ProviderProblem per ADR-038 taxonomy
  - Unhandled exceptions → 500 problem+json (full context logged, safe detail only)

The `detail` field in the problem response is always operator-safe:
  - No stack traces
  - No DB schema names or column names
  - No internal file paths
  - No raw exception messages from untrusted sources

Full diagnostic context (stack trace, request path, request body) goes to
the logger at ERROR level. The request_id in the log record enables
correlation with the response the client received.

Per ADR-018: error format is non-versioned — consistent across /api/v1, /api/v2.

ProviderProblem extension fields (ADR-018, OpenAPI ProviderProblem schema):
  - providerId: stable provider id (e.g. "nws")
  - domain: one of forecast/alerts/aqi/earthquakes/radar
  - errorCode: class name from ADR-038 canonical taxonomy
  - retryAfterSeconds: set only for QuotaExhausted

HTTP status mapping (brief §behavior decision tree):
  - QuotaExhausted         → 503 + Retry-After header
  - GeographicallyUnsupported → 503
  - KeyInvalid             → 502
  - FieldUnsupported       → 502
  - TransientNetworkError  → 502
  - ProviderProtocolError  → 502
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Base URI for problem type URIs. In production this would be a real URL.
# Single source of truth — imported by middleware modules and error handlers.
PROBLEM_BASE_URI = "https://clearskies.example/problems"


def build_problem_response(
    status: int,
    title: str,
    detail: str,
    request: Request,
    problem_type: str | None = None,
) -> JSONResponse:
    """Build a RFC 9457 problem+json JSONResponse.

    Public helper — imported by middleware modules so the problem+json shape
    has a single definition. Centralises both the base URI and the field set.
    """
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": problem_type or f"{PROBLEM_BASE_URI}/{status}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": str(request.url),
        },
    )


def _problem_response(
    status: int,
    title: str,
    detail: str,
    request: Request,
    problem_type: str | None = None,
) -> JSONResponse:
    """Build a RFC 9457 problem+json JSONResponse. Delegates to build_problem_response."""
    return build_problem_response(
        status=status,
        title=title,
        detail=detail,
        request=request,
        problem_type=problem_type,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register RFC 9457 exception handlers on the FastAPI app.

    Call this in create_app() after the app is constructed.
    Includes the ProviderError handler for ADR-038 canonical taxonomy.
    """
    # Import here to avoid circular import (providers._common.errors imports
    # nothing from this module; this module imports from providers._common.errors).
    from weewx_clearskies_api.providers._common.errors import (  # noqa: PLC0415
        GeographicallyUnsupported,
        ProviderError,
        QuotaExhausted,
    )

    @app.exception_handler(ProviderError)
    async def provider_error_handler(
        request: Request, exc: ProviderError
    ) -> JSONResponse:
        """Handle canonical provider errors per ADR-038 §5 + ADR-018.

        Status mapping:
          503 → QuotaExhausted (with Retry-After), GeographicallyUnsupported
          502 → KeyInvalid, FieldUnsupported, TransientNetworkError, ProviderProtocolError
        """
        status = 503 if isinstance(exc, (QuotaExhausted, GeographicallyUnsupported)) else 502
        error_code = type(exc).__name__

        body: dict[str, object] = {
            "type": f"https://clearskies.weewx.org/errors/provider/{error_code.lower()}",
            "title": error_code,
            "status": status,
            "detail": str(exc),
            "providerId": exc.provider_id,
            "domain": exc.domain,
            "errorCode": error_code,
        }
        headers: dict[str, str] = {}
        if exc.retry_after_seconds is not None:
            body["retryAfterSeconds"] = exc.retry_after_seconds
            headers["Retry-After"] = str(exc.retry_after_seconds)

        log_level = logging.ERROR if status == 502 else logging.WARNING
        logger.log(
            log_level,
            "Provider error %s [%s/%s]: %s",
            error_code,
            exc.domain,
            exc.provider_id,
            exc,
            extra={
                "provider_id": exc.provider_id,
                "domain": exc.domain,
                "error_code": error_code,
                "http_status": status,
            },
        )

        return JSONResponse(
            status_code=status,
            content=body,
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # HTTPException.detail is set by FastAPI/application code — it may or
        # may not be operator-safe. We pass it through only for well-known
        # status codes where the detail is expected to be safe (e.g. 404 "Not Found").
        # For 500+ we replace with a generic message.
        if exc.status_code >= 500:
            logger.error(
                "HTTP %d: %s %s",
                exc.status_code,
                request.method,
                request.url,
                extra={"http_status": exc.status_code, "detail": exc.detail},
            )
            detail = "An unexpected error occurred. Check the server logs for details."
        else:
            detail = str(exc.detail) if exc.detail else _http_title(exc.status_code)

        return _problem_response(
            status=exc.status_code,
            title=_http_title(exc.status_code),
            detail=detail,
            request=request,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic validation errors include field names but NOT DB internals —
        # safe to include in the client-facing detail.
        errors = exc.errors()
        first_error = errors[0] if errors else {}
        loc = " -> ".join(str(x) for x in first_error.get("loc", []))
        msg = first_error.get("msg", "Validation error")
        detail = f"Validation error at {loc!r}: {msg}" if loc else str(msg)
        logger.info(
            "Request validation error: %s",
            detail,
            extra={"validation_errors": errors},
        )
        return _problem_response(
            status=422,
            title="Unprocessable Entity",
            detail=detail,
            request=request,
            problem_type=f"{PROBLEM_BASE_URI}/validation-error",
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # Log the full stack trace for operators. Never expose it to clients.
        logger.error(
            "Unhandled exception: %s",
            type(exc).__name__,
            extra={
                "exc_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
                "path": str(request.url),
                "method": request.method,
            },
        )
        return _problem_response(
            status=500,
            title="Internal Server Error",
            detail="An unexpected error occurred. Check the server logs for details.",
            request=request,
        )


def _http_title(status_code: int) -> str:
    """Return a standard HTTP reason phrase for common status codes."""
    _titles: dict[int, str] = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        413: "Request Entity Too Large",
        422: "Unprocessable Entity",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    return _titles.get(status_code, "HTTP Error")
