"""Observation endpoints: GET /current and GET /archive.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-012: read-only, per-request sessions via get_db_session().
Per ADR-019: units block embedded in every response, no server-side conversion.
Per ADR-020: UTC ISO-8601 with Z on the wire.
Per security-baseline §3.5: query params validated via Pydantic with
  ConfigDict(extra="forbid"), enforced through Depends() + model_validate()
  on the raw query-param dict so FastAPI's routing layer does not silently
  discard unknown keys before the model sees them.

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.reflection import ColumnRegistry
from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.models.params import ArchiveQueryParams

# Alias kept for backwards-compatibility with tests that import ArchiveParams
# from this module (test_archive_params.py).  The class is defined in
# models/params.py and re-exported here under the original name.
ArchiveParams = ArchiveQueryParams
from weewx_clearskies_api.models.responses import (
    ArchiveResponse,
    ObservationResponse,
)
from weewx_clearskies_api.services.archive import (
    decode_cursor,
    get_archive,
    get_current,
)
from weewx_clearskies_api.services.units import get_units_block

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Dependency: parse + validate /archive query params
#
# FastAPI's routing layer extracts only the declared param names before
# populating a Depends() model, so extra="forbid" in a plain Depends(Model)
# call never fires for unknown HTTP keys.  The fix is to validate the full
# raw query-param dict via model_validate() inside a dependency function —
# that way Pydantic sees every key the client sent and raises ValidationError
# on any unknown one.  The ValidationError is caught here and surfaced as 400
# problem+json via FastAPI's RequestValidationError handler.
# ---------------------------------------------------------------------------


def _get_archive_params(request: Request) -> ArchiveQueryParams:
    """Dependency: parse and validate /archive query params from the raw dict.

    Calling model_validate(dict(request.query_params)) passes every HTTP key
    the client sent to Pydantic.  ConfigDict(extra="forbid") on ArchiveQueryParams
    then rejects any key not in the model's field set (security-baseline §3.5).
    """
    try:
        return ArchiveQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        # Re-raise as RequestValidationError so the existing RFC 9457 error
        # handler shapes it as 400/422 problem+json.
        from fastapi.exceptions import RequestValidationError
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# GET /current
# ---------------------------------------------------------------------------


@router.get(
    "/current",
    summary="Most recent observation",
    tags=["Observations"],
    response_model=ObservationResponse,
)
def get_current_endpoint(
    request: Request,
    db: Annotated[Session, Depends(get_db_session)],
) -> ObservationResponse:
    """Return the most-recent archive row.

    Returns data: null when the archive is empty — not 404 (brief §1).
    """
    registry = get_registry()
    units = get_units_block()

    observation = get_current(db, registry)

    return ObservationResponse(
        data=observation,
        units=units,
        source="weewx",
        generatedAt=_now_utc_z(),
    )


# ---------------------------------------------------------------------------
# GET /archive
# ---------------------------------------------------------------------------


@router.get(
    "/archive",
    summary="Historical archive records",
    tags=["Observations"],
    response_model=ArchiveResponse,
)
def get_archive_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    params: Annotated[ArchiveQueryParams, Depends(_get_archive_params)],
) -> ArchiveResponse:
    """Return archive records within a time window.

    Supports raw / hour / day interval aggregation and cursor + page pagination.
    Unknown query parameters are rejected with 400 per security-baseline §3.5.
    """
    registry = get_registry()

    # Validate cursor if provided.
    if params.cursor is not None:
        try:
            decode_cursor(params.cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cursor: {exc}") from exc

    # Validate fields if provided.
    parsed_field_names: list[str] | None = None
    if params.fields is not None:
        names = [f.strip() for f in params.fields.split(",") if f.strip()]
        mapped_names = {
            info.canonical_name
            for info in registry.stock.values()
            if info.canonical_name is not None
        }
        unknown = [n for n in names if n not in mapped_names]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown field(s): {', '.join(unknown)}",
            )
        parsed_field_names = names

    units = get_units_block()

    try:
        records, page_info = get_archive(
            db=db,
            registry=registry,
            from_dt=params.from_,
            to_dt=params.to,
            interval=params.interval,
            fields=parsed_field_names,
            limit=params.limit,
            cursor=params.cursor,
            page=params.page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ArchiveResponse(
        data=records,
        units=units,
        source="weewx",
        generatedAt=_now_utc_z(),
        page=page_info,
    )
