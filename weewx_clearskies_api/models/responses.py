"""Response envelope and data models for DB-backed endpoints.

Per ADR-010: camelCase field names everywhere (identical in Python and JSON).
Per ADR-020: datetime fields are UTC ISO-8601 with Z suffix.

Pydantic v2 models with ConfigDict(extra="forbid") on all request/param models.
Response models use extra="ignore" so the serialisation layer doesn't reject
extra DB columns (they route to `extras`).

ruff: noqa: N815  (canonical fields use weewx camelCase: outTemp, windSpeed, etc.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def utc_isoformat(dt: datetime) -> str:
    """Serialise a UTC datetime to ISO-8601 with Z suffix (ADR-020)."""
    # Pydantic serialises datetime to "+00:00" by default; we want "Z".
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Observation / ArchiveRecord
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """Canonical observation (ADR-010 §3.1).

    All numeric observation fields are Optional — weather data is genuinely
    missing sometimes.  `extras` is always present (may be empty).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    timestamp: str  # UTC ISO-8601 with Z
    outTemp: float | None = None
    outHumidity: float | None = None
    windSpeed: float | None = None
    windDir: float | None = None
    windGust: float | None = None
    windGustDir: float | None = None
    barometer: float | None = None
    pressure: float | None = None
    altimeter: float | None = None
    dewpoint: float | None = None
    windchill: float | None = None
    heatindex: float | None = None
    rainRate: float | None = None
    rain: float | None = None
    radiation: float | None = None
    UV: float | None = None
    inTemp: float | None = None
    inHumidity: float | None = None
    ET: float | None = None
    hail: float | None = None
    hailRate: float | None = None
    extras: dict[str, Any] = {}
    source: str = "weewx"


class ArchiveRecord(Observation):
    """ArchiveRecord = Observation + interval (ADR-010 §3.2)."""

    interval: int


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class PageInfo(BaseModel):
    """Pagination metadata matching OpenAPI PageInfo schema."""

    cursor: str | None = None
    next: str | None = None
    previous: str | None = None
    limit: int
    page: int | None = None
    totalPages: int | None = None
    totalRecords: int | None = None


class ObservationResponse(BaseModel):
    """ObservationResponse envelope."""

    data: Observation | None
    units: dict[str, str]
    source: str
    generatedAt: str  # UTC ISO-8601 with Z


class ArchiveResponse(BaseModel):
    """ArchiveResponse envelope."""

    data: list[ArchiveRecord]
    units: dict[str, str]
    source: str
    generatedAt: str
    page: PageInfo


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class RecordEntry(BaseModel):
    """One named record (e.g. "All-time high outTemp")."""

    label: str
    canonicalField: str
    value: float | None = None
    observedAt: str | None = None  # UTC ISO-8601 with Z
    brokenInLast30Days: bool = False


class RecordsBundle(BaseModel):
    """Records grouped by section."""

    period: str
    sections: dict[str, list[RecordEntry]]


class RecordsResponse(BaseModel):
    """RecordsResponse envelope."""

    data: RecordsBundle
    units: dict[str, str]
    source: str
    generatedAt: str


# ---------------------------------------------------------------------------
# Reports (NOAA)
# ---------------------------------------------------------------------------


class ReportEntry(BaseModel):
    """One NOAA report file entry (monthly or yearly)."""

    kind: str  # "monthly" | "yearly"
    year: int
    month: int | None = None
    filename: str
    modifiedAt: str  # UTC ISO-8601 with Z


class ReportIndex(BaseModel):
    """Index of available NOAA reports."""

    reports: list[ReportEntry]


class ReportIndexResponse(BaseModel):
    """ReportIndexResponse envelope."""

    data: ReportIndex
    generatedAt: str


class NOAAReport(BaseModel):
    """Raw monthly NOAA report text."""

    year: int
    month: int
    filename: str
    rawText: str
    modifiedAt: str  # UTC ISO-8601 with Z


class NOAAYearlyReport(BaseModel):
    """Raw yearly NOAA report text."""

    year: int
    filename: str
    rawText: str
    modifiedAt: str  # UTC ISO-8601 with Z


class ReportResponse(BaseModel):
    """ReportResponse envelope."""

    data: NOAAReport
    generatedAt: str


class YearlyReportResponse(BaseModel):
    """YearlyReportResponse envelope."""

    data: NOAAYearlyReport
    generatedAt: str
