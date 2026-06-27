"""Grouped archive endpoint: GET /archive/grouped.

Returns grouped aggregation data from the weewx archive for arbitrary fields,
time ranges, and group dimensions.  Replaces the hardcoded /climatology/monthly
path with a general-purpose data-access endpoint.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-019: units block is NOT included — values are raw weewx archive units;
  the dashboard applies unit conversion for display.
Per ADR-020: generatedAt is UTC ISO-8601 with Z suffix.
Per security-baseline §3.5: query params validated via Pydantic
  ConfigDict(extra="forbid") enforced through Depends() + model_validate()
  on the raw query-param dict.

ruff: noqa: N815  (canonical field names use weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.services.archive_grouped import (
    _VALID_GROUP_BY,
    get_grouped_archive,
)
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_VALID_AGG_TYPES = frozenset({"avg", "sum", "max", "min"})
_VALID_AVG_TYPES = frozenset({"max", "min", "sum"})

# ---------------------------------------------------------------------------
# Query param model
# ---------------------------------------------------------------------------


class ArchiveGroupedQueryParams(BaseModel):
    """Validated query parameters for GET /archive/grouped.

    group_by: required — one of month, day, hour, year.
    fields: required — comma-separated per-field specs:
        'field:agg_type[:avg_type]'
        Examples: 'outTemp:avg:max', 'rain:avg:sum', 'dewpoint:avg'
    from: optional epoch timestamp (int seconds) — inclusive lower bound.
    to: optional epoch timestamp (int seconds) — exclusive upper bound.
    force_full_period: optional bool, default True for month/hour; ignored for
        day/year (always observed-only for variable dimensions).

    extra="forbid" per security-baseline §3.5.
    The Depends-wrapper pattern in _get_params() ensures the full raw query
    string flows through Pydantic so extra="forbid" actually fires.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    group_by: str
    fields: str
    from_: int | None = Field(default=None, alias="from", ge=0)
    to: int | None = Field(default=None, ge=0)
    force_full_period: bool = True

    @field_validator("group_by")
    @classmethod
    def validate_group_by(cls, v: str) -> str:
        if v not in _VALID_GROUP_BY:
            raise ValueError(
                f"group_by must be one of: {', '.join(sorted(_VALID_GROUP_BY))}. "
                f"Got {v!r}."
            )
        return v

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, v: str) -> str:
        """Validate that each field spec has the correct structure.

        Accepts: 'field:agg_type' or 'field:agg_type:avg_type'.
        Rejects empty field names, unknown agg_type values, unknown avg_type values.
        Does NOT validate field names against the registry here — that happens in
        the service layer where the registry is available.
        """
        specs = [s.strip() for s in v.split(",") if s.strip()]
        if not specs:
            raise ValueError(
                "'fields' must contain at least one non-empty field spec"
            )
        for spec in specs:
            parts = spec.split(":")
            if len(parts) < 2 or len(parts) > 3:
                raise ValueError(
                    f"Invalid field spec {spec!r}. "
                    "Format: 'field:agg_type' or 'field:agg_type:avg_type'."
                )
            field_name = parts[0].strip()
            agg_type = parts[1].strip()
            if not field_name:
                raise ValueError(
                    f"Empty field name in spec {spec!r}."
                )
            if agg_type not in _VALID_AGG_TYPES:
                raise ValueError(
                    f"Invalid agg_type {agg_type!r} in spec {spec!r}. "
                    f"Must be one of: {', '.join(sorted(_VALID_AGG_TYPES))}."
                )
            if len(parts) == 3:
                avg_type = parts[2].strip()
                if avg_type not in _VALID_AVG_TYPES:
                    raise ValueError(
                        f"Invalid avg_type {avg_type!r} in spec {spec!r}. "
                        f"Must be one of: {', '.join(sorted(_VALID_AVG_TYPES))}."
                    )
        return v

    @model_validator(mode="after")
    def check_time_range(self) -> ArchiveGroupedQueryParams:
        """Reject inverted time ranges."""
        if (
            self.from_ is not None
            and self.to is not None
            and self.from_ >= self.to
        ):
            raise ValueError("'from' must be strictly less than 'to'")
        return self


def _parse_field_specs(
    fields_str: str,
) -> list[tuple[str, str, str | None]]:
    """Parse 'field:agg_type[:avg_type]' strings into structured tuples.

    Assumes the fields string has already passed validate_fields().
    Returns list of (field, agg_type, avg_type_or_None).
    """
    result: list[tuple[str, str, str | None]] = []
    for spec in fields_str.split(","):
        spec = spec.strip()
        if not spec:
            continue
        parts = spec.split(":")
        field_name = parts[0].strip()
        agg_type = parts[1].strip()
        avg_type: str | None = parts[2].strip() if len(parts) == 3 else None
        result.append((field_name, agg_type, avg_type))
    return result


# ---------------------------------------------------------------------------
# Dependency: parse + validate query params from raw request
# ---------------------------------------------------------------------------


def _get_params(request: Request) -> ArchiveGroupedQueryParams:
    """Dependency: parse and validate /archive/grouped query params.

    Passes the full raw query-param dict to Pydantic so extra="forbid" fires
    for any unknown key the client sends (security-baseline §3.5).
    RequestValidationError propagates through FastAPI's error handler as
    RFC 9457 problem+json per ADR-018.
    """
    try:
        return ArchiveGroupedQueryParams.model_validate(
            dict(request.query_params)
        )
    except Exception as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            raise RequestValidationError(exc.errors()) from exc
        raise


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# GET /archive/grouped
# ---------------------------------------------------------------------------


@router.get(
    "/archive/grouped",
    summary="Grouped aggregation from the weewx archive",
    tags=["Archive"],
)
def get_archive_grouped_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    params: Annotated[ArchiveGroupedQueryParams, Depends(_get_params)],
) -> dict[str, Any]:
    """Return grouped aggregation data for arbitrary fields and time ranges.

    Query parameters:
      group_by  — required; one of month, day, hour, year
      fields    — required; comma-separated 'field:agg_type[:avg_type]' specs
                  Examples: outTemp:avg:max, outTemp:avg:min, rain:avg:sum,
                            dewpoint:avg, rain:sum, windGust:max
      from      — optional; lower bound as epoch seconds (inclusive)
      to        — optional; upper bound as epoch seconds (exclusive)
      force_full_period — optional bool (default true); pads month/hour series
                          to 12/24 elements even when data is absent for some
                          buckets

    Response:
      {
        "data": {
          "labels": ["01", "02", ..., "12"],
          "series": {
            "outTemp:avg:max": [72.3, ...],
            "rain:avg:sum": [2.8, ...]
          }
        },
        "generatedAt": "2026-06-07T12:00:00Z"
      }

    Fields absent from the ColumnRegistry are silently omitted from series
    (self-hide rule).

    Unknown query parameters are rejected with 400 per security-baseline §3.5.
    """
    field_specs = _parse_field_specs(params.fields)

    registry = get_registry()

    grouped_data = get_grouped_archive(
        db=db,
        registry=registry,
        group_by=params.group_by,
        field_specs=field_specs,
        from_ts=params.from_,
        to_ts=params.to,
        force_full_period=params.force_full_period,
    )

    return {
        "data": grouped_data,
        "generatedAt": _now_utc_z(),
        "stationClock": build_station_clock().model_dump(),
        "freshness": build_freshness("current_observation").model_dump(),
    }
