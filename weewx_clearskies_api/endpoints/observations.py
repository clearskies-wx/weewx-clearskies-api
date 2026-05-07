"""Observation endpoints: GET /current and GET /archive.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-012: read-only, per-request sessions via get_db_session().
Per ADR-019: units block embedded in every response, no server-side conversion.
Per ADR-020: UTC ISO-8601 with Z on the wire.
Per security-baseline §3.5: all query params validated via Pydantic with
  ConfigDict(extra="forbid").

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.reflection import ColumnRegistry
from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.errors import build_problem_response
from weewx_clearskies_api.models.responses import (
    ArchiveResponse,
    ObservationResponse,
    PageInfo,
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
# Query-param Pydantic models
# ---------------------------------------------------------------------------


class ArchiveParams(BaseModel):
    """Validated query parameters for GET /archive.

    extra="forbid" per security-baseline §3.5 — unknown keys → 400.
    """

    model_config = ConfigDict(extra="forbid")

    from_: datetime | None = None
    to: datetime | None = None
    interval: str = "raw"
    fields: str | None = None
    limit: int = 1000
    cursor: str | None = None
    page: int | None = None

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        allowed = {"raw", "hour", "day"}
        if v not in allowed:
            raise ValueError(f"interval must be one of {sorted(allowed)}")
        return v

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v: int) -> int:
        if not (1 <= v <= 10000):
            raise ValueError("limit must be between 1 and 10000")
        return v

    @field_validator("page")
    @classmethod
    def validate_page(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("page must be >= 1")
        return v

    @model_validator(mode="after")
    def check_cursor_page_exclusive(self) -> "ArchiveParams":
        if self.cursor is not None and self.page is not None:
            raise ValueError("cursor and page are mutually exclusive")
        return self

    def parsed_fields(self, registry: ColumnRegistry) -> list[str] | None:
        """Parse and validate the `fields` param against the registry.

        Returns a list of validated canonical field names, or None if absent.
        Raises ValueError for any unknown field name.
        """
        if self.fields is None:
            return None
        names = [f.strip() for f in self.fields.split(",") if f.strip()]
        mapped_names = {
            info.canonical_name
            for info in registry.stock.values()
            if info.canonical_name is not None
        }
        unknown = [n for n in names if n not in mapped_names]
        if unknown:
            raise ValueError(f"Unknown field(s): {', '.join(unknown)}")
        return names


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
    request: Request,
    db: Annotated[Session, Depends(get_db_session)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    interval: str = "raw",
    fields: str | None = None,
    limit: int = 1000,
    cursor: str | None = None,
    page: int | None = None,
) -> ArchiveResponse:
    """Return archive records within a time window.

    Supports raw / hour / day interval aggregation and cursor + page pagination.
    """
    # Validate all params via Pydantic.
    try:
        params = ArchiveParams(
            from_=from_,
            to=to,
            interval=interval,
            fields=fields,
            limit=limit,
            cursor=cursor,
            page=page,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    registry = get_registry()

    # Validate cursor if provided.
    if params.cursor is not None:
        try:
            decode_cursor(params.cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cursor: {exc}") from exc

    # Validate fields if provided.
    try:
        parsed_field_names = params.parsed_fields(registry)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
