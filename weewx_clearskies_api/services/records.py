"""Records service — highs and lows per ADR-024.

The section mapping is lead-confirmed (see phase-2-task-3a-1-brief.md
"Per-section field mapping").  Baked here as a constant; operator
customisation is Phase 4 per ADR-027.

Self-hide rule: if none of a section's canonical fields are in the
ColumnRegistry's mapped set, the section key is omitted from the response
entirely (not returned as an empty list).

The `custom` section always returns [] at v0.1 — operator mapping UI is
Phase 4 per ADR-027.

The `aqi` section self-hides this round — AQI columns are operator-custom
and the Phase 4 mapping UI hasn't shipped yet (ADR-013 / ADR-035).

SQL note: column identifiers in query text come exclusively from
_CANONICAL_TO_DB — a module-level constant whose keys are hard-coded strings,
not user-supplied values.  All value bindings use named parameters (:name).
The "no f-string SQL" rule in coding.md §1 targets user-controlled data; these
are trusted internal identifiers compiled from code, not from HTTP inputs.

ruff: noqa: N815  (canonical fields use weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.reflection import ColumnRegistry
from weewx_clearskies_api.models.responses import RecordEntry, RecordsBundle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section field-mapping constant (lead-confirmed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RecordSpec:
    label: str
    canonicalField: str  # noqa: N815 — weewx camelCase
    kind: str  # "high" | "low"
    aggregator: str  # "max"|"min"|"sum-by-day-then-max"|"sum-by-month-then-max"|"sum-by-hour-then-max"


SECTION_MAP: dict[str, list[_RecordSpec]] = {
    "temperature": [
        _RecordSpec("High temperature", "outTemp", "high", "max"),
        _RecordSpec("Low temperature", "outTemp", "low", "min"),
        _RecordSpec("High dewpoint", "dewpoint", "high", "max"),
        _RecordSpec("Low dewpoint", "dewpoint", "low", "min"),
        _RecordSpec("High heat index", "heatindex", "high", "max"),
        _RecordSpec("Low wind chill", "windchill", "low", "min"),
    ],
    "wind": [
        _RecordSpec("High wind speed", "windSpeed", "high", "max"),
        _RecordSpec("High wind gust", "windGust", "high", "max"),
    ],
    "rain": [
        _RecordSpec("High daily rainfall", "rain", "high", "sum-by-day-then-max"),
        _RecordSpec("High monthly rainfall", "rain", "high", "sum-by-month-then-max"),
        _RecordSpec("Most rain in 1 hour", "rain", "high", "sum-by-hour-then-max"),
        _RecordSpec("Highest rain rate", "rainRate", "high", "max"),
    ],
    "humidity": [
        _RecordSpec("High humidity", "outHumidity", "high", "max"),
        _RecordSpec("Low humidity", "outHumidity", "low", "min"),
    ],
    "barometer": [
        _RecordSpec("High barometer", "barometer", "high", "max"),
        _RecordSpec("Low barometer", "barometer", "low", "min"),
    ],
    "sun": [
        _RecordSpec("High solar radiation", "radiation", "high", "max"),
        _RecordSpec("High UV index", "UV", "high", "max"),
    ],
    # aqi self-hides this round — Phase 4 / ADR-013.
    "aqi": [],
    "inside-temp": [
        _RecordSpec("High indoor temperature", "inTemp", "high", "max"),
        _RecordSpec("Low indoor temperature", "inTemp", "low", "min"),
        _RecordSpec("High indoor humidity", "inHumidity", "high", "max"),
        _RecordSpec("Low indoor humidity", "inHumidity", "low", "min"),
    ],
    # custom always returns [] at v0.1 — Phase 4 per ADR-027.
    "custom": [],
}

# Trusted constant: canonical field name → archive DB column name.
# Keys and values are hard-coded; neither comes from HTTP request inputs.
_CANONICAL_TO_DB: dict[str, str] = {
    "outTemp": "outTemp",
    "dewpoint": "dewpoint",
    "windchill": "windchill",
    "heatindex": "heatindex",
    "windSpeed": "windSpeed",
    "windGust": "windGust",
    "rain": "rain",
    "rainRate": "rainRate",
    "outHumidity": "outHumidity",
    "barometer": "barometer",
    "radiation": "radiation",
    "UV": "UV",
    "inTemp": "inTemp",
    "inHumidity": "inHumidity",
}


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def _period_clause(period: str) -> tuple[str, dict[str, Any]]:
    """Return (WHERE fragment, named params dict) for a period string."""
    now = datetime.now(tz=UTC)
    if period == "ytd":
        jan_1 = datetime(now.year, 1, 1, tzinfo=UTC)
        return "dateTime >= :period_from", {"period_from": int(jan_1.timestamp())}
    if period == "all-time":
        return "", {}
    year = int(period)
    jan_1 = datetime(year, 1, 1, tzinfo=UTC)
    jan_1_next = datetime(year + 1, 1, 1, tzinfo=UTC)
    return (
        "dateTime >= :period_from AND dateTime < :period_to",
        {"period_from": int(jan_1.timestamp()), "period_to": int(jan_1_next.timestamp())},
    )


def _where(clause: str) -> str:
    return f"WHERE {clause}" if clause else ""


# ---------------------------------------------------------------------------
# Dialect helpers — produce trusted SQL bucket expressions.
# These are internal constants, not user input.
# ---------------------------------------------------------------------------


def _day_bucket_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return "strftime('%Y-%m-%d', datetime(dateTime, 'unixepoch'))"
    return "DATE(FROM_UNIXTIME(dateTime))"


def _month_bucket_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return "strftime('%Y-%m', datetime(dateTime, 'unixepoch'))"
    return "DATE_FORMAT(FROM_UNIXTIME(dateTime), '%Y-%m')"


def _hour_bucket_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return "strftime('%Y-%m-%d %H:00:00', datetime(dateTime, 'unixepoch'))"
    return "FROM_UNIXTIME(dateTime, '%Y-%m-%d %H:00:00')"


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def _simple_max_min(
    db: Session,
    db_col: str,       # trusted internal constant — not user input
    agg: str,          # "max" or "min"
    period_clause: str,
    period_params: dict[str, Any],
) -> tuple[float | None, int | None]:
    """Return (extreme_value, dateTime_epoch) for MAX or MIN of db_col."""
    # db_col comes from _CANONICAL_TO_DB — a hardcoded constant dict.
    # Named params (:period_from / :period_to) carry all user-controlled values.
    where = _where(period_clause)
    agg_fn = "MAX" if agg == "max" else "MIN"

    # Step 1: find the extreme value.
    val_sql = text(f"SELECT {agg_fn}({db_col}) AS val FROM archive {where}")
    val_row = db.execute(val_sql, period_params).fetchone()
    if val_row is None or val_row[0] is None:
        return None, None
    extreme_val = val_row[0]

    # Step 2: find the epoch of the first archive row matching that value.
    if period_clause:
        ts_sql = text(
            f"SELECT dateTime FROM archive WHERE {period_clause} "
            f"AND {db_col} = :extreme_val ORDER BY dateTime ASC LIMIT 1"
        )
    else:
        ts_sql = text(
            f"SELECT dateTime FROM archive WHERE {db_col} = :extreme_val "
            f"ORDER BY dateTime ASC LIMIT 1"
        )
    ts_params = {**period_params, "extreme_val": extreme_val}
    ts_row = db.execute(ts_sql, ts_params).fetchone()
    ts = int(ts_row[0]) if ts_row is not None else None

    return float(extreme_val), ts


def _sum_by_bucket_then_max(
    db: Session,
    db_col: str,
    period_clause: str,
    period_params: dict[str, Any],
    bucket_sql: str,   # trusted dialect constant — not user input
) -> tuple[float | None, int | None]:
    """Sum db_col per bucket (day/month/hour); return (max_bucket_sum, epoch).

    bucket_sql is produced by one of the _*_bucket_sql() helpers above —
    a trusted dialect expression, not user-controlled data.
    """
    where = _where(period_clause)
    sql = text(
        f"SELECT bucket_sum, bucket_ts FROM ("
        f"  SELECT SUM({db_col}) AS bucket_sum, "
        f"         MIN(dateTime) AS bucket_ts, "
        f"         {bucket_sql} AS bucket "
        f"  FROM archive {where} "
        f"  GROUP BY bucket"
        f") sub "
        f"ORDER BY bucket_sum DESC "
        f"LIMIT 1"
    )
    row = db.execute(sql, period_params).fetchone()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), int(row[1])


def _epoch_to_utc_z(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_broken_in_last_30_days(observed_epoch: int | None) -> bool:
    if observed_epoch is None:
        return False
    observed = datetime.fromtimestamp(observed_epoch, tz=UTC)
    cutoff = datetime.now(tz=UTC) - timedelta(days=30)
    return observed >= cutoff


# ---------------------------------------------------------------------------
# Primary service function
# ---------------------------------------------------------------------------


def get_records(
    db: Session,
    registry: ColumnRegistry,
    period: str,
    section_filter: str | None,
) -> RecordsBundle:
    """Query the archive for highs/lows and return a RecordsBundle."""
    mapped_fields = set(registry.stock.keys())
    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    period_clause, period_params = _period_clause(period)

    sections_to_build = (
        [section_filter]
        if section_filter is not None
        else list(SECTION_MAP.keys())
    )

    sections: dict[str, list[RecordEntry]] = {}

    for section_name in sections_to_build:
        specs = SECTION_MAP.get(section_name, [])

        if section_name == "custom":
            sections[section_name] = []
            continue

        # aqi self-hides this round.
        if section_name == "aqi":
            continue

        # sun: section appears if either radiation or UV is mapped.
        if section_name == "sun":
            if "radiation" not in mapped_fields and "UV" not in mapped_fields:
                continue

        available_specs = [s for s in specs if s.canonicalField in mapped_fields]

        # Self-hide if no fields from this section are mapped.
        if not available_specs:
            continue

        entries: list[RecordEntry] = []

        for spec in available_specs:
            db_col = _CANONICAL_TO_DB.get(spec.canonicalField, spec.canonicalField)

            if spec.aggregator == "max":
                val, ts = _simple_max_min(db, db_col, "max", period_clause, period_params)
            elif spec.aggregator == "min":
                val, ts = _simple_max_min(db, db_col, "min", period_clause, period_params)
            elif spec.aggregator == "sum-by-day-then-max":
                val, ts = _sum_by_bucket_then_max(
                    db, db_col, period_clause, period_params,
                    _day_bucket_sql(dialect_name),
                )
            elif spec.aggregator == "sum-by-month-then-max":
                val, ts = _sum_by_bucket_then_max(
                    db, db_col, period_clause, period_params,
                    _month_bucket_sql(dialect_name),
                )
            elif spec.aggregator == "sum-by-hour-then-max":
                val, ts = _sum_by_bucket_then_max(
                    db, db_col, period_clause, period_params,
                    _hour_bucket_sql(dialect_name),
                )
            else:
                logger.error(
                    "Unknown aggregator %r for record spec %r",
                    spec.aggregator,
                    spec.label,
                )
                val, ts = None, None

            entries.append(
                RecordEntry(
                    label=spec.label,
                    canonicalField=spec.canonicalField,
                    value=float(val) if val is not None else None,
                    observedAt=_epoch_to_utc_z(ts) if ts is not None else None,
                    brokenInLast30Days=_is_broken_in_last_30_days(ts),
                )
            )

        sections[section_name] = entries

    return RecordsBundle(period=period, sections=sections)
