"""Station metadata endpoint (3a-2 — replaces task-1 placeholder).

GET /station — singleton per ADR-011.  No query params.

Data sources (per the brief):
  - stationId / name / lat / lon / alt / timezone / unitSystem / hardware:
    cached StationInfo from services.station (loaded at startup from weewx.conf).
  - firstRecord / lastRecord: MIN(dateTime) / MAX(dateTime) from archive via
    get_db_session() FastAPI Depends injection (ADR-012: never bypass the
    session factory, never call the generator directly outside Depends).
  - units block: get_units_block() from services.units.

Altitude is passed through in whatever unit weewx uses — ADR-019 is authoritative.
The OpenAPI description "Meters above mean sea level" is a contract typo that the
lead will fix in a separate post-3a-2 commit.  See brief §6 (resolved call #6).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.models.responses import (
    StationMetadata,
    StationResponse,
    utc_isoformat,
)
from weewx_clearskies_api.services.station import get_station_info
from weewx_clearskies_api.services.units import get_units_block

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/station", summary="Station identity and metadata", tags=["Station"])
def get_station(
    db: Annotated[Session, Depends(get_db_session)],
) -> StationResponse:
    """Return station metadata.

    Singleton per ADR-011 (no query params, no ?station= filtering).
    firstRecord / lastRecord come from a DB query; all other fields from
    the startup-cached StationInfo.
    """
    info = get_station_info()

    # --- MIN / MAX dateTime from archive (ADR-012: session via Depends) ---
    first_record: str | None = None
    last_record: str | None = None
    try:
        row = db.execute(
            text("SELECT MIN(dateTime), MAX(dateTime) FROM archive")
        ).fetchone()
        if row is not None:
            min_ts, max_ts = row[0], row[1]
            if min_ts is not None:
                first_record = datetime.fromtimestamp(
                    int(min_ts), tz=UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if max_ts is not None:
                last_record = datetime.fromtimestamp(
                    int(max_ts), tz=UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except SQLAlchemyError as exc:
        # "no such table: archive" happens when the archive table hasn't been
        # created yet (weewx has never run to create its schema, or the operator
        # pointed clearskies-api at a fresh DB).  Treat the same as an empty
        # archive — return null firstRecord / lastRecord rather than 500.
        # A connection error or other SQLAlchemy failure is still a 500.
        exc_str = str(exc).lower()
        if "no such table" in exc_str or "table" in exc_str and "not exist" in exc_str:
            logger.warning(
                "archive table not found when querying MIN/MAX dateTime; "
                "returning null firstRecord/lastRecord (weewx may not have run yet): %s",
                exc,
            )
        else:
            logger.error(
                "DB error querying archive MIN/MAX dateTime: %s",
                exc,
                extra={"exc_type": type(exc).__name__},
            )
            raise HTTPException(
                status_code=500,
                detail="Database error querying archive timestamps.",
            ) from exc

    units = get_units_block()

    metadata = StationMetadata(
        stationId=info.station_id,
        name=info.name,
        latitude=info.latitude,
        longitude=info.longitude,
        altitude=info.altitude,
        timezone=info.timezone,
        timezoneOffsetMinutes=info.timezone_offset_minutes,
        unitSystem=info.unit_system,
        firstRecord=first_record,
        lastRecord=last_record,
        hardware=info.hardware,
    )

    return StationResponse(
        data=metadata,
        units=units,
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
