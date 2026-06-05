"""Custom query endpoint: GET /charts/custom-query/{series_id}.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-012: read-only, per-request sessions via get_db_session().
Per ADR-020: UTC ISO-8601 with Z on the wire.
Per security-baseline §3.5: query params validated via Pydantic with
  ConfigDict(extra="forbid"), enforced through model_validate() on the raw
  query-param dict so FastAPI's routing layer does not silently discard unknown
  keys before the model sees them.
Per ADR-018: all errors use application/problem+json (RFC 9457).

Security: the SQL query never crosses the HTTP boundary.  The endpoint only
accepts a series_id path parameter that maps to a pre-loaded, pre-validated
query.  The SQL itself was loaded from charts.conf on disk at startup.

Response:
{
  "data": [{"x": <value>, "y": <value>}, ...],
  "seriesId": "growing_degree_days",
  "source": "weewx",
  "generatedAt": "2026-06-05T12:00:00Z"
}
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.errors import PROBLEM_BASE_URI
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.custom_query import (
    execute_custom_query,
    get_validated_series_ids,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CustomQueryPoint(BaseModel):
    """One (x, y) data point returned by a custom SQL query."""

    x: float | int | str | None
    y: float | int | str | None


class CustomQueryResponse(BaseModel):
    """Full response envelope for GET /charts/custom-query/{series_id}."""

    data: list[CustomQueryPoint]
    seriesId: str  # noqa: N815
    source: str = "weewx"
    generatedAt: str  # noqa: N815


# ---------------------------------------------------------------------------
# Query param model and Depends-wrapper
# ---------------------------------------------------------------------------


class CustomQueryParams(BaseModel):
    """Validated query parameters for GET /charts/custom-query/{series_id}.

    extra="forbid" per security-baseline §3.5 — unknown query keys are
    rejected with 422 (reshaped to problem+json by the error handler).

    from_ / to: ISO 8601 datetime strings bounding the query window.
    When omitted the query runs without epoch filters (the SQL may ignore
    them or use its own defaults).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str | None = Field(None, alias="from")
    to: str | None = None


def _get_custom_query_params(request: Request) -> CustomQueryParams:
    """Depends-wrapper: parse raw query params through CustomQueryParams.

    Using model_validate(dict(request.query_params)) ensures extra="forbid"
    fires — FastAPI's Depends() with a Pydantic model would not pass unknown
    keys through to validation.
    """
    try:
        return CustomQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Date parsing helpers (same logic as endpoints/wind_rose.py)
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW_HOURS = 24


def _parse_datetime(value: str, param_name: str) -> datetime:
    """Parse an ISO 8601 datetime string to a UTC-aware datetime.

    Accepts:
      - Full ISO 8601 with offset: "2026-06-05T12:00:00Z", "...+00:00", "...-07:00"
      - Naive ISO 8601 (assumed UTC per ADR-020): "2026-06-05T12:00:00"

    Args:
        value: Raw string from the query parameter.
        param_name: Parameter name for error messages.

    Returns:
        UTC-aware datetime.

    Raises:
        RequestValidationError: Date string cannot be parsed.
    """
    # Normalise "Z" suffix so Python 3.10 fromisoformat() also works.
    normalised = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "datetime_parsing",
                    "loc": ("query", param_name),
                    "msg": (
                        f"Invalid ISO 8601 datetime for {param_name!r}: {value!r}. "
                        "Expected format: 2026-06-05T12:00:00Z or 2026-06-05T12:00:00+00:00"
                    ),
                    "input": value,
                    "ctx": {"error": "cannot parse datetime"},
                }
            ]
        ) from exc

    # Attach UTC if naive; normalise tz-aware to UTC.
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt


def _resolve_window(
    params: CustomQueryParams,
) -> tuple[float | None, float | None]:
    """Resolve from/to strings to Unix epoch floats, or None if omitted.

    Unlike the wind rose endpoint, both parameters are fully optional — a
    custom SQL query may not use time filters at all.

    Returns:
        (from_epoch, to_epoch) — each is a float or None.

    Raises:
        RequestValidationError: Date string cannot be parsed.
    """
    from_epoch = (
        _parse_datetime(params.from_, "from").timestamp()
        if params.from_ is not None
        else None
    )
    to_epoch = (
        _parse_datetime(params.to, "to").timestamp()
        if params.to is not None
        else None
    )
    return from_epoch, to_epoch


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/charts/custom-query/{series_id}",
    summary="Custom SQL query chart data",
    tags=["Charts"],
    response_model=CustomQueryResponse,
)
def get_custom_query(
    series_id: str,
    request: Request,
    params: Annotated[CustomQueryParams, Depends(_get_custom_query_params)],
    db: Annotated[Session, Depends(get_db_session)],
) -> CustomQueryResponse | JSONResponse:
    """Return chart data from a pre-validated custom SQL query.

    The SQL query is loaded from charts.conf at startup and validated before
    the API accepts any requests.  The series_id path parameter identifies
    which pre-loaded query to run; no SQL ever crosses the HTTP boundary.

    Query parameters:
      from: ISO 8601 start datetime (optional — passed as :from_ts if the
            query uses that named parameter)
      to:   ISO 8601 end datetime   (optional — passed as :to_ts if the
            query uses that named parameter)

    Errors:
      404 — series_id not found in validated queries
      422 — invalid or extra query parameters
      500 — query execution error
    """
    from_epoch, to_epoch = _resolve_window(params)

    try:
        rows = execute_custom_query(
            db=db,
            series_id=series_id,
            from_epoch=from_epoch,
            to_epoch=to_epoch,
        )
    except KeyError:
        valid_ids = get_validated_series_ids()
        logger.warning(
            "Custom query request for unknown series_id %r (validated: %s)",
            series_id,
            valid_ids,
        )
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": f"{PROBLEM_BASE_URI}/404",
                "title": "Not Found",
                "status": 404,
                "detail": (
                    f"No validated custom SQL query found for series_id {series_id!r}. "
                    "Check that use_custom_sql=True and the query passed startup "
                    "validation (review API logs for warnings)."
                ),
                "instance": str(request.url),
            },
        )
    except Exception as exc:  # noqa: BLE001 — broad catch; log full detail, return safe message
        logger.exception(
            "Custom query execution failed for series_id %r: %s",
            series_id,
            exc,
        )
        return JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": f"{PROBLEM_BASE_URI}/500",
                "title": "Internal Server Error",
                "status": 500,
                "detail": (
                    "Custom SQL query execution failed. "
                    "Check the API logs for details."
                ),
                "instance": str(request.url),
            },
        )

    data = [CustomQueryPoint(x=row["x"], y=row["y"]) for row in rows]

    return CustomQueryResponse(
        data=data,
        seriesId=series_id,
        source="weewx",
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
