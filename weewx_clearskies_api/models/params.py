"""Pydantic parameter models for the DB-backed endpoints.

All models use ConfigDict(extra="forbid") per security-baseline §3.5 —
unknown query keys are rejected with 422 (reshaped to 400 problem+json by
the error handler).

ruff: noqa: N815  (canonical field names use weewx camelCase per ADR-010)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# /archive query params
# ---------------------------------------------------------------------------

_ARCHIVE_INTERVAL_CHOICES = frozenset({"raw", "hour", "day"})


class ArchiveQueryParams(BaseModel):
    """Validated query parameters for GET /archive."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    interval: str = "raw"
    fields: str | None = None
    limit: int = Field(default=1000, ge=1, le=10000)
    cursor: str | None = None
    page: int | None = Field(default=None, ge=1)

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        if v not in _ARCHIVE_INTERVAL_CHOICES:
            raise ValueError(
                f"interval must be one of {sorted(_ARCHIVE_INTERVAL_CHOICES)}"
            )
        return v

    @model_validator(mode="after")
    def check_cursor_page_exclusive(self) -> "ArchiveQueryParams":
        if self.cursor is not None and self.page is not None:
            raise ValueError("cursor and page are mutually exclusive")
        return self


# ---------------------------------------------------------------------------
# /records query params
# ---------------------------------------------------------------------------

_VALID_SECTIONS = frozenset({
    "temperature", "wind", "rain", "humidity", "barometer",
    "sun", "aqi", "inside-temp", "custom",
})

_YEAR_RE = re.compile(r"^\d{4}$")


class RecordsQueryParams(BaseModel):
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
            # Optionally reject future years.
            current_year = datetime.now(tz=UTC).year
            if year > current_year:
                raise ValueError(f"Year {year} is in the future")
            return v
        raise ValueError(
            "period must be 'ytd', 'all-time', or a 4-digit year (e.g. '2025')"
        )

    @field_validator("section")
    @classmethod
    def validate_section(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_SECTIONS:
            raise ValueError(
                f"section must be one of: {', '.join(sorted(_VALID_SECTIONS))}"
            )
        return v


# ---------------------------------------------------------------------------
# /reports/{year}/{month} path params
# ---------------------------------------------------------------------------


class ReportMonthlyParams(BaseModel):
    """Validated path parameters for GET /reports/{year}/{month}."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=1900)
    month: int = Field(ge=1, le=12)


# ---------------------------------------------------------------------------
# /reports/{year} path params
# ---------------------------------------------------------------------------


class ReportYearlyParams(BaseModel):
    """Validated path parameters for GET /reports/{year}."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=1900)
