"""Records endpoint: GET /records.

Section-grouped highs and lows.  Period: ytd (default), all-time, or YYYY.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-019: units block embedded in response.
Per security-baseline §3.5: params validated with extra="forbid".

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.models.responses import RecordsResponse
from weewx_clearskies_api.services.records import get_records
from weewx_clearskies_api.services.units import get_units_block

logger = logging.getLogger(__name__)

router = APIRouter()

_YEAR_RE = re.compile(r"^\d{4}$")

_VALID_SECTIONS = frozenset(
    ["temperature", "wind", "rain", "humidity", "barometer",
     "sun", "aqi", "inside-temp", "custom"]
)


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordsParams(BaseModel):
    """Validated query parameters for GET /records."""

    model_config = ConfigDict(extra="forbid")

    period: str = "ytd"
    section: str | None = None

    @field_validator("period")
    @classmethod
    def validate_period(cls, v: str) -> str:
        if v in ("ytd", "all-time"):
            return v
        if _YEAR_RE.match(v):
            year = int(v)
            if year < 1900:
                raise ValueError("Year must be >= 1900")
            current_year = datetime.now(tz=UTC).year
            if year > current_year:
                raise ValueError(f"Year {year} is in the future")
            return v
        raise ValueError("period must be 'ytd', 'all-time', or a 4-digit year")

    @field_validator("section")
    @classmethod
    def validate_section(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_SECTIONS:
            raise ValueError(
                f"section must be one of: {', '.join(sorted(_VALID_SECTIONS))}"
            )
        return v


@router.get(
    "/records",
    summary="Highs and lows",
    tags=["Records"],
    response_model=RecordsResponse,
)
def get_records_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    period: str = "ytd",
    section: str | None = None,
) -> RecordsResponse:
    """Return section-grouped records for the requested period.

    Each section self-hides when its backing data is unavailable.
    """
    try:
        params = RecordsParams(period=period, section=section)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
