"""Climatology endpoint: GET /climatology/monthly.

Returns 12-month average values (high temp, low temp, dewpoint, rainfall)
computed from the full weewx archive.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-019: units block is NOT included — climatology values are raw weewx
  archive units; the dashboard applies unit conversion for display.
Per ADR-020: generatedAt is UTC ISO-8601 with Z suffix.

Fields self-hide when their backing archive column is absent from the
ColumnRegistry (consistent with the records service self-hide rule).

Optional query params (both must be supplied together or both omitted):
  fields — comma-separated list of archive column names to aggregate
  agg    — aggregation type: avg_max, avg_min, avg, avg_monthly_total, sum

When both params are absent, the legacy fixed response (avgHighTemp,
avgLowTemp, avgDewpoint, avgRainfall) is returned unchanged.

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.services.climatology import (
    _VALID_AGG_TYPES,
    get_climatology_by_fields,
    get_monthly_climatology,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get(
    "/climatology/monthly",
    summary="12-month climatology averages",
    tags=["Climatology"],
)
def get_monthly_climatology_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    fields: str | None = Query(
        None,
        description=(
            "Comma-separated archive column names to aggregate "
            "(e.g. outTemp,dewpoint,rain). Must be supplied together with 'agg'."
        ),
    ),
    agg: str | None = Query(
        None,
        description=(
            "Aggregation type for the requested fields. "
            "One of: avg_max, avg_min, avg, avg_monthly_total, sum. "
            "Must be supplied together with 'fields'."
        ),
    ),
) -> dict[str, Any]:
    """Return 12-element arrays of monthly climatology averages.

    No params (legacy path):
      - avgHighTemp: average of daily maximum outTemp per month
      - avgLowTemp:  average of daily minimum outTemp per month
      - avgDewpoint: straight average dewpoint per month
      - avgRainfall: average of monthly total rainfall per month

    With fields + agg params (generalized path):
      Returns a 'results' dict keyed by field name, each value a 12-element list.

    Fields whose archive column is absent from the ColumnRegistry are
    omitted from the response (self-hide rule).

    Both 'fields' and 'agg' must be supplied together; providing only one
    returns 422. An unknown 'agg' value returns 422.
    """
    # Validate param co-presence: both or neither.
    # Use direct is-None checks so mypy can narrow types in the generalized branch.
    if (fields is None) != (agg is None):
        raise HTTPException(
            status_code=422,
            detail="both 'fields' and 'agg' are required when either is specified",
        )

    # Generalized path: fields + agg both supplied.
    # After the co-presence guard above, both are either None (legacy) or str (generalized).
    if fields is not None and agg is not None:
        if agg not in _VALID_AGG_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"invalid 'agg' value {agg!r}; "
                    f"must be one of: {', '.join(sorted(_VALID_AGG_TYPES))}"
                ),
            )
        parsed_fields = [f.strip() for f in fields.split(",") if f.strip()]
        if not parsed_fields:
            raise HTTPException(
                status_code=422,
                detail="'fields' must contain at least one non-empty field name",
            )

        registry = get_registry()
        clim_data = get_climatology_by_fields(
            db=db, registry=registry, fields=parsed_fields, agg=agg
        )
        return {
            "data": clim_data,
            "source": "weewx",
            "generatedAt": _now_utc_z(),
        }

    # Legacy path: no params — use the cached/pre-computed fixed response.
    # Cache-check-first guard (ADR-045).  The warmer pre-computes the monthly
    # climatology on a 6-hour interval; use the cached result when available.
    try:
        cached = get_cache().get("warmer:climatology:monthly")
        if cached is not None:
            logger.debug("climatology cache hit")
            return {
                "data": cached,
                "source": "weewx",
                "generatedAt": _now_utc_z(),
            }
    except Exception:
        logger.debug("climatology cache miss or error", exc_info=True)

    registry = get_registry()

    clim_data = get_monthly_climatology(db=db, registry=registry)

    return {
        "data": clim_data,
        "source": "weewx",
        "generatedAt": _now_utc_z(),
    }
