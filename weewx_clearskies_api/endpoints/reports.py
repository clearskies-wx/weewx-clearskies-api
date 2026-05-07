"""Reports endpoints: GET /reports, /reports/{year}/{month}, /reports/{year}.

FastAPI route ordering matters: /reports/{year}/{month} MUST be registered
BEFORE /reports/{year} so the more-specific route wins.  Both are declared in
this router; the include_router call in app.py uses the same prefix for both,
preserving this order.

Per ADR-018: URL-path versioned under /api/v1/.
Per security-baseline §3.5: path params validated via Pydantic (int with min).
Path-traversal defence: os.path.realpath containment in services/reports.py.

ruff: noqa: N815
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from weewx_clearskies_api.errors import build_problem_response
from weewx_clearskies_api.models.responses import (
    ReportIndexResponse,
    ReportResponse,
    YearlyReportResponse,
)
from weewx_clearskies_api.services.reports import (
    get_monthly_report,
    get_report_index,
    get_yearly_report,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# GET /reports — index
# ---------------------------------------------------------------------------


@router.get(
    "/reports",
    summary="Available NOAA report files",
    tags=["Reports"],
    response_model=ReportIndexResponse,
)
def get_report_index_handler() -> ReportIndexResponse:
    """List all available NOAA monthly and yearly report files."""
    index = get_report_index()
    return ReportIndexResponse(data=index, generatedAt=_now_utc_z())


# ---------------------------------------------------------------------------
# GET /reports/{year}/{month} — MUST be declared BEFORE /reports/{year}
# ---------------------------------------------------------------------------


@router.get(
    "/reports/{year}/{month}",
    summary="One NOAA monthly report (raw text)",
    tags=["Reports"],
)
def get_monthly_report_endpoint(
    year: int,
    month: int,
    request: Request,
) -> JSONResponse:
    """Return the raw text of a monthly NOAA report.

    Path params validated here: year >= 1900, month 1..12.
    Returns 404 problem+json when the file doesn't exist.
    Returns 500 problem+json when the file exists but is not valid UTF-8.
    """
    # Validate path params.
    if year < 1900:
        return build_problem_response(
            status=400,
            title="Bad Request",
            detail="year must be >= 1900",
            request=request,
        )
    if not (1 <= month <= 12):
        return build_problem_response(
            status=400,
            title="Bad Request",
            detail="month must be between 1 and 12",
            request=request,
        )

    try:
        report = get_monthly_report(year, month)
    except UnicodeDecodeError:
        logger.critical(
            "NOAA report NOAA-%04d-%02d.txt exists but is not valid UTF-8. "
            "This is an operator-environment problem.",
            year,
            month,
        )
        return build_problem_response(
            status=500,
            title="Internal Server Error",
            detail="Report file is not valid UTF-8. Check the server logs.",
            request=request,
        )

    if report is None:
        return build_problem_response(
            status=404,
            title="Report not found",
            detail=f"No report exists for {year}-{month:02d}",
            request=request,
        )

    response_body = ReportResponse(data=report, generatedAt=_now_utc_z())
    return JSONResponse(
        status_code=200,
        content=response_body.model_dump(),
    )


# ---------------------------------------------------------------------------
# GET /reports/{year} — yearly; MUST be declared AFTER monthly
# ---------------------------------------------------------------------------


@router.get(
    "/reports/{year}",
    summary="One NOAA yearly report (raw text)",
    tags=["Reports"],
)
def get_yearly_report_endpoint(
    year: int,
    request: Request,
) -> JSONResponse:
    """Return the raw text of a yearly NOAA report.

    Path param validated: year >= 1900.
    Returns 404 problem+json when the file doesn't exist.
    Returns 500 problem+json when the file is not valid UTF-8.
    """
    if year < 1900:
        return build_problem_response(
            status=400,
            title="Bad Request",
            detail="year must be >= 1900",
            request=request,
        )

    try:
        report = get_yearly_report(year)
    except UnicodeDecodeError:
        logger.critical(
            "NOAA report NOAA-%04d.txt exists but is not valid UTF-8. "
            "This is an operator-environment problem.",
            year,
        )
        return build_problem_response(
            status=500,
            title="Internal Server Error",
            detail="Report file is not valid UTF-8. Check the server logs.",
            request=request,
        )

    if report is None:
        return build_problem_response(
            status=404,
            title="Report not found",
            detail=f"No yearly report exists for {year}",
            request=request,
        )

    response_body = YearlyReportResponse(data=report, generatedAt=_now_utc_z())
    return JSONResponse(
        status_code=200,
        content=response_body.model_dump(),
    )
