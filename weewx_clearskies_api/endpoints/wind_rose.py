"""Wind rose endpoint: GET /charts/wind-rose.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-012: read-only, per-request sessions via get_db_session().
Per ADR-020: UTC ISO-8601 with Z on the wire.
Per security-baseline §3.5: query params validated via Pydantic with
  ConfigDict(extra="forbid"), enforced through model_validate() on the raw
  query-param dict so FastAPI's routing layer does not silently discard unknown
  keys before the model sees them.
Per ADR-018: all errors use application/problem+json (RFC 9457).

Response: 16-direction × 7-Beaufort-category wind rose matrix, expressed as
percentage of total valid records per (direction, speed) bin.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.errors import PROBLEM_BASE_URI
from weewx_clearskies_api.models.responses import utc_isoformat
from weewx_clearskies_api.services.wind_rose import (
    BEAUFORT_LABELS,
    DIRECTIONS,
    compute_wind_rose,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BeaufortCategory(BaseModel):
    """One Beaufort speed category."""

    beaufort: int
    label: str


class WindRoseData(BaseModel):
    """The data payload of the wind rose response."""

    directions: list[str]
    categories: list[BeaufortCategory]
    bins: list[list[float]]
    totalRecords: int  # noqa: N815
    calmPercentage: float  # noqa: N815


class WindRoseResponse(BaseModel):
    """Full wind rose response envelope."""

    data: WindRoseData
    source: str = "weewx"
    generatedAt: str  # noqa: N815


# ---------------------------------------------------------------------------
# Beaufort category metadata (order must match services/wind_rose.py bins)
# ---------------------------------------------------------------------------

_BEAUFORT_CATEGORIES: list[BeaufortCategory] = [
    BeaufortCategory(beaufort=i, label=label)
    for i, label in enumerate(BEAUFORT_LABELS)
]

# ---------------------------------------------------------------------------
# Query param model and Depends-wrapper
# ---------------------------------------------------------------------------


class WindRoseQueryParams(BaseModel):
    """Validated query parameters for GET /charts/wind-rose.

    extra="forbid" per security-baseline §3.5 — unknown query keys are
    rejected with 422 (reshaped to problem+json by the error handler).

    from_ / to: ISO 8601 datetime strings bounding the query window.
    Defaults: from = now - 24h, to = now.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str | None = Field(None, alias="from")
    to: str | None = None


def _get_wind_rose_params(request: Request) -> WindRoseQueryParams:
    """Depends-wrapper: parse raw query params through WindRoseQueryParams.

    Using model_validate(dict(request.query_params)) ensures extra="forbid"
    fires — FastAPI's Depends() with a Pydantic model would not pass unknown
    keys through to validation.
    """
    try:
        return WindRoseQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW_HOURS = 24


def _parse_datetime(value: str, param_name: str) -> datetime:
    """Parse an ISO 8601 datetime string to a UTC-aware datetime.

    Accepts:
      - Full ISO 8601 with offset: "2026-06-05T12:00:00Z", "…+00:00", "…-07:00"
      - Naive ISO 8601 (assumed UTC per ADR-020): "2026-06-05T12:00:00"

    Args:
        value: Raw string from the query parameter.
        param_name: Parameter name for error messages.

    Returns:
        UTC-aware datetime.

    Raises:
        RequestValidationError: Date string cannot be parsed.
    """
    # Try with fromisoformat (Python 3.11+ handles Z; earlier versions do not).
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


def _resolve_window(params: WindRoseQueryParams) -> tuple[float, float]:
    """Resolve from/to strings to Unix epoch floats.

    Defaults: to = now, from = now - 24h.

    Returns:
        (from_epoch, to_epoch) as floats.

    Raises:
        RequestValidationError: Date string cannot be parsed.
    """
    now = datetime.now(tz=UTC)

    to_dt = _parse_datetime(params.to, "to") if params.to is not None else now
    from_dt = (
        _parse_datetime(params.from_, "from")
        if params.from_ is not None
        else to_dt - timedelta(hours=_DEFAULT_WINDOW_HOURS)
    )

    return from_dt.timestamp(), to_dt.timestamp()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/charts/wind-rose",
    summary="Wind rose data",
    tags=["Charts"],
    response_model=WindRoseResponse,
)
def get_wind_rose(
    request: Request,
    params: Annotated[WindRoseQueryParams, Depends(_get_wind_rose_params)],
    db: Annotated[Session, Depends(get_db_session)],
) -> WindRoseResponse | JSONResponse:
    """Return a 16-direction × 7-Beaufort wind rose matrix.

    Queries the archive for windSpeed and windDir records in the requested
    window and bins them into a 16-direction × 7-Beaufort-speed matrix.
    Each bin value is the percentage of total valid records that fall in that
    direction-speed combination.

    Query parameters:
      from: ISO 8601 start datetime (default: now - 24h)
      to:   ISO 8601 end datetime   (default: now)

    Errors:
      422 — invalid or extra query parameters
      404 — windSpeed or windDir columns not present in the archive
    """
    from_epoch, to_epoch = _resolve_window(params)

    try:
        result = compute_wind_rose(db=db, from_epoch=from_epoch, to_epoch=to_epoch)
    except KeyError as exc:
        logger.warning(
            "Wind rose: archive missing wind columns: %s",
            exc,
        )
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": f"{PROBLEM_BASE_URI}/404",
                "title": "Not Found",
                "status": 404,
                "detail": "Wind data columns not available in archive",
                "instance": str(request.url),
            },
        )

    data = WindRoseData(
        directions=DIRECTIONS,
        categories=_BEAUFORT_CATEGORIES,
        bins=result.bins,
        totalRecords=result.total_records,
        calmPercentage=result.calm_percentage,
    )

    return WindRoseResponse(
        data=data,
        source="weewx",
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
