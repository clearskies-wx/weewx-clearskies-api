"""Records endpoint: GET /records.

Section-grouped highs and lows.  Period: ytd (default), all-time, or YYYY.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-019: units block embedded in response.
Per security-baseline §3.5: params validated via Pydantic extra="forbid",
  enforced through Depends() + model_validate() on the raw query-param dict.

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.models.params import RecordsQueryParams

# Alias kept for backwards-compatibility with tests that import RecordsParams
# from this module (test_archive_params.py).
RecordsParams = RecordsQueryParams
from weewx_clearskies_api.models.responses import RecordsResponse
from weewx_clearskies_api.services.records import get_records
from weewx_clearskies_api.services.units import get_units_block

logger = logging.getLogger(__name__)

router = APIRouter()


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_records_params(request: Request) -> RecordsQueryParams:
    """Dependency: parse and validate /records query params from the raw dict.

    model_validate(dict(request.query_params)) passes every HTTP key to Pydantic.
    ConfigDict(extra="forbid") on RecordsQueryParams rejects unknown keys.
    """
    try:
        return RecordsQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.get(
    "/records",
    summary="Highs and lows",
    tags=["Records"],
    response_model=RecordsResponse,
)
def get_records_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    params: Annotated[RecordsQueryParams, Depends(_get_records_params)],
) -> RecordsResponse:
    """Return section-grouped records for the requested period.

    Each section self-hides when its backing data is unavailable.
    Unknown query parameters are rejected with 400 per security-baseline §3.5.
    """
    registry = get_registry()
    units = get_units_block()

    bundle = get_records(
        db=db,
        registry=registry,
        period=params.period,
        section_filter=params.section,
    )

    return RecordsResponse(
        data=bundle,
        units=units,
        source="weewx",
        generatedAt=_now_utc_z(),
    )
