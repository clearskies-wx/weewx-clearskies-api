"""Grouped aggregation service — general-purpose time-bucketed aggregation.

Supports arbitrary fields, time ranges, and group_by dimensions (month, day,
hour, year).  Replaces the narrowly-scoped /climatology/monthly endpoint with
a data-access primitive that the dashboard can drive from chart config.

Design principles (ADR-010):
  - No "climatology" concept here — this is grouped aggregation with an
    optional time range.
  - Each field carries its own aggregate_type + average_type.
  - API is general-purpose data access; no chart-specific logic.

Security model:
  Caller-supplied field names are validated against ColumnRegistry before any
  SQL is composed.  Only names present in the registry (a curated allowlist of
  known weewx archive column names) are passed to SQL helpers.  Column
  identifiers cannot be bound as SQL parameters, so post-validation
  interpolation is used with backtick-quoting (coding.md §1).

  Time bounds (from_ts, to_ts) are always bound as named parameters (:from_ts,
  :to_ts) — never interpolated into SQL text.

SQL note: all column names in SQL text come from ColumnRegistry-validated
allowlists or from hard-coded dialect constants.  No user-controlled data
reaches a SQL string.

ruff: noqa: N815  (canonical fields use weewx camelCase per ADR-010)
"""
# ruff: noqa: S608  (SQL f-strings are safe here — all column names come from
#                    hard-coded constants or registry-validated allowlists;
#                    no user input reaches the SQL string)

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.reflection import ColumnRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported group_by values
# ---------------------------------------------------------------------------

_VALID_GROUP_BY = frozenset({"month", "day", "hour", "year"})

# ---------------------------------------------------------------------------
# Dialect helpers — produce trusted SQL bucket expressions for each group_by.
# All returned strings are dialect constants, not user input.
# ---------------------------------------------------------------------------


def _bucket_sql(dialect_name: str, group_by: str) -> str:
    """Return a SQL expression that extracts a bucket value for the given group_by.

    The bucket is an integer or sortable string that identifies the group.

    month  → 1-12   (integer)
    day    → 1-366  (integer day-of-year)
    hour   → 0-23   (integer)
    year   → 4-digit year integer
    """
    if group_by == "month":
        if dialect_name == "sqlite":
            return "CAST(strftime('%m', datetime(dateTime, 'unixepoch')) AS INTEGER)"
        return "MONTH(FROM_UNIXTIME(dateTime))"

    if group_by == "day":
        # Day of year (1–366).
        if dialect_name == "sqlite":
            return "CAST(strftime('%j', datetime(dateTime, 'unixepoch')) AS INTEGER)"
        return "DAYOFYEAR(FROM_UNIXTIME(dateTime))"

    if group_by == "hour":
        if dialect_name == "sqlite":
            return "CAST(strftime('%H', datetime(dateTime, 'unixepoch')) AS INTEGER)"
        return "HOUR(FROM_UNIXTIME(dateTime))"

    if group_by == "year":
        if dialect_name == "sqlite":
            return "CAST(strftime('%Y', datetime(dateTime, 'unixepoch')) AS INTEGER)"
        return "YEAR(FROM_UNIXTIME(dateTime))"

    # Caller must validate group_by before this function is called.
    raise ValueError(f"Unsupported group_by: {group_by!r}")


def _day_bucket_sql(dialect_name: str) -> str:
    """Return a SQL expression for a calendar-day bucket (YYYY-MM-DD).

    Used in two-level aggregations: inner GROUP BY day then outer GROUP BY
    the requested dimension.
    """
    if dialect_name == "sqlite":
        return "strftime('%Y-%m-%d', datetime(dateTime, 'unixepoch'))"
    return "DATE(FROM_UNIXTIME(dateTime))"


def _outer_bucket_from_day_sql(dialect_name: str, group_by: str) -> str:
    """Return a SQL expression that extracts the outer bucket from a day_bucket value.

    day_bucket values produced by _day_bucket_sql():
      SQLite  → 'YYYY-MM-DD' string
      MariaDB → DATE value (displayable as 'YYYY-MM-DD')

    Extracts:
      month → month number 1-12
      day   → day-of-year 1-366
      hour  → (not applicable for two-level daily agg; should not be called)
      year  → 4-digit year
    """
    if group_by == "month":
        if dialect_name == "sqlite":
            # 'YYYY-MM-DD' → CAST(substr(6,2) AS INTEGER)
            return "CAST(SUBSTR(day_bucket, 6, 2) AS INTEGER)"
        return "MONTH(day_bucket)"

    if group_by == "day":
        if dialect_name == "sqlite":
            # strftime('%j', ...) on a date string.
            return "CAST(strftime('%j', day_bucket) AS INTEGER)"
        return "DAYOFYEAR(day_bucket)"

    if group_by == "year":
        if dialect_name == "sqlite":
            return "CAST(SUBSTR(day_bucket, 1, 4) AS INTEGER)"
        return "YEAR(day_bucket)"

    raise ValueError(
        f"_outer_bucket_from_day_sql: unsupported group_by={group_by!r}"
    )


def _ym_bucket_sql(dialect_name: str) -> str:
    """Return a SQL expression for a year-month bucket.

    Used in avg-of-monthly-total aggregation.
    SQLite: 'YYYY-MM' string.
    MariaDB: DATE_FORMAT — %% escaping required for pymysql pyformat driver.
    """
    if dialect_name == "sqlite":
        return "strftime('%Y-%m', datetime(dateTime, 'unixepoch'))"
    return "DATE_FORMAT(FROM_UNIXTIME(dateTime), '%%Y-%%m')"


def _outer_bucket_from_ym_sql(dialect_name: str, group_by: str) -> str:
    """Extract outer bucket from ym_bucket (year-month string 'YYYY-MM').

    group_by=month → month number 1-12
    group_by=year  → 4-digit year integer
    """
    if group_by == "month":
        if dialect_name == "sqlite":
            return "CAST(SUBSTR(ym_bucket, 6, 2) AS INTEGER)"
        # MariaDB: ym_bucket is a string 'YYYY-MM'; CAST AS SIGNED for integer.
        return "CAST(SUBSTR(ym_bucket, 6, 2) AS SIGNED)"

    if group_by == "year":
        if dialect_name == "sqlite":
            return "CAST(SUBSTR(ym_bucket, 1, 4) AS INTEGER)"
        return "CAST(SUBSTR(ym_bucket, 1, 4) AS SIGNED)"

    raise ValueError(
        f"_outer_bucket_from_ym_sql: unsupported group_by={group_by!r} for ym_bucket"
    )


# ---------------------------------------------------------------------------
# Time-filter clause builder
#
# from_ts and to_ts are epoch integers bound as named parameters — never
# interpolated into SQL text.
# ---------------------------------------------------------------------------


def _time_filter_clause(from_ts: int | None, to_ts: int | None) -> str:
    """Return a SQL WHERE clause fragment for optional time bounds.

    Returns '' (empty string) when both bounds are None.
    Callers must place this after any existing WHERE clause fragment.
    """
    parts: list[str] = []
    if from_ts is not None:
        parts.append("dateTime >= :from_ts")
    if to_ts is not None:
        parts.append("dateTime < :to_ts")
    if not parts:
        return ""
    return " AND ".join(parts)


def _bind_params(from_ts: int | None, to_ts: int | None) -> dict[str, int]:
    """Return the bind-param dict for time filter clauses."""
    params: dict[str, int] = {}
    if from_ts is not None:
        params["from_ts"] = from_ts
    if to_ts is not None:
        params["to_ts"] = to_ts
    return params


# ---------------------------------------------------------------------------
# Per-field SQL query helpers
#
# All accept:
#   db          — SQLAlchemy session
#   column      — validated archive column name (ColumnRegistry allowlist)
#   group_by    — validated group_by dimension
#   dialect_name — 'sqlite' or 'mysql'/'mariadb'
#   from_ts/to_ts — optional epoch int bounds (bound via named params)
#
# All return dict[int, float | None]: bucket_number → aggregate value.
# ---------------------------------------------------------------------------


def _query_avg_of_daily_agg(
    db: Session,
    column: str,
    daily_agg: str,   # "MAX" or "MIN" — caller-controlled constant
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Average of daily MAX or MIN for any validated column and group_by.

    Two-level aggregation:
      Inner: GROUP BY day_bucket → daily_agg(column) AS daily_val
      Outer: GROUP BY bucket     → AVG(daily_val)

    Supports group_by=month|day|year (not hour — hourly daily-max is not
    semantically meaningful; hour group_by uses _query_straight_avg instead).

    Column name is validated against ColumnRegistry before use.
    """
    day_bucket = _day_bucket_sql(dialect_name)
    outer_bucket = _outer_bucket_from_day_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    sql = text(
        f"SELECT {outer_bucket} AS bucket, "
        f"       AVG(daily_val) AS agg_val "
        f"FROM ("
        f"  SELECT {day_bucket} AS day_bucket, "
        f"         {daily_agg}(`{column}`) AS daily_val "
        f"  FROM archive "
        f"  WHERE {where_col_notnull}{time_and} "
        f"  GROUP BY day_bucket"
        f") sub "
        f"GROUP BY bucket "
        f"ORDER BY bucket ASC"
    )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


def _query_straight_avg(
    db: Session,
    column: str,
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Straight average grouped by bucket dimension.

    Single-level aggregation: GROUP BY bucket → AVG(column).
    Works for all group_by values (month/day/hour/year).

    Column name is validated against ColumnRegistry before use.
    """
    bucket_expr = _bucket_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    sql = text(
        f"SELECT {bucket_expr} AS bucket, "
        f"       AVG(`{column}`) AS agg_val "
        f"FROM archive "
        f"WHERE {where_col_notnull}{time_and} "
        f"GROUP BY bucket "
        f"ORDER BY bucket ASC"
    )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


def _query_avg_of_period_total(
    db: Session,
    column: str,
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Average of per-period totals for any validated column.

    Two-level aggregation:
      Inner: GROUP BY ym_bucket → SUM(column) AS period_total
      Outer: GROUP BY outer_bucket → AVG(period_total)

    This is meaningful for group_by=month (avg of monthly totals) or
    group_by=year (avg of yearly totals — effectively the per-year sum,
    since each year appears once in the inner group).

    For group_by=day or hour the inner ym_bucket (year-month) is coarser
    than the outer bucket, so the semantics break. Callers should use
    _query_period_sum for those cases instead.

    Column name is validated against ColumnRegistry before use.
    """
    ym_bucket = _ym_bucket_sql(dialect_name)
    outer_bucket = _outer_bucket_from_ym_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    if dialect_name == "sqlite":
        sql = text(
            f"SELECT {outer_bucket} AS bucket, "
            f"       AVG(period_total) AS agg_val "
            f"FROM ("
            f"  SELECT {ym_bucket} AS ym_bucket, "
            f"         SUM(`{column}`) AS period_total "
            f"  FROM archive "
            f"  WHERE {where_col_notnull}{time_and} "
            f"  GROUP BY ym_bucket"
            f") sub "
            f"GROUP BY bucket "
            f"ORDER BY bucket ASC"
        )
    else:
        # MariaDB: avoid DATE_FORMAT in outer query; extract from ym_bucket string.
        sql = text(
            f"SELECT {outer_bucket} AS bucket, "
            f"       AVG(period_total) AS agg_val "
            f"FROM ("
            f"  SELECT {ym_bucket} AS ym_bucket, "
            f"         SUM(`{column}`) AS period_total "
            f"  FROM archive "
            f"  WHERE {where_col_notnull}{time_and} "
            f"  GROUP BY ym_bucket"
            f") sub "
            f"GROUP BY bucket "
            f"ORDER BY bucket ASC"
        )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


def _query_period_sum(
    db: Session,
    column: str,
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Straight sum grouped by bucket dimension.

    Single-level aggregation: GROUP BY bucket → SUM(column).
    Works for all group_by values (month/day/hour/year).

    Column name is validated against ColumnRegistry before use.
    """
    bucket_expr = _bucket_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    sql = text(
        f"SELECT {bucket_expr} AS bucket, "
        f"       SUM(`{column}`) AS agg_val "
        f"FROM archive "
        f"WHERE {where_col_notnull}{time_and} "
        f"GROUP BY bucket "
        f"ORDER BY bucket ASC"
    )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


def _query_period_max(
    db: Session,
    column: str,
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Maximum grouped by bucket dimension.

    Single-level aggregation: GROUP BY bucket → MAX(column).
    Works for all group_by values (month/day/hour/year).

    Column name is validated against ColumnRegistry before use.
    """
    bucket_expr = _bucket_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    sql = text(
        f"SELECT {bucket_expr} AS bucket, "
        f"       MAX(`{column}`) AS agg_val "
        f"FROM archive "
        f"WHERE {where_col_notnull}{time_and} "
        f"GROUP BY bucket "
        f"ORDER BY bucket ASC"
    )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


def _query_period_min(
    db: Session,
    column: str,
    group_by: str,
    dialect_name: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict[int, float | None]:
    """Minimum grouped by bucket dimension.

    Single-level aggregation: GROUP BY bucket → MIN(column).
    Works for all group_by values (month/day/hour/year).

    Column name is validated against ColumnRegistry before use.
    """
    bucket_expr = _bucket_sql(dialect_name, group_by)
    time_clause = _time_filter_clause(from_ts, to_ts)
    where_col_notnull = f"`{column}` IS NOT NULL"
    time_and = f" AND {time_clause}" if time_clause else ""

    sql = text(
        f"SELECT {bucket_expr} AS bucket, "
        f"       MIN(`{column}`) AS agg_val "
        f"FROM archive "
        f"WHERE {where_col_notnull}{time_and} "
        f"GROUP BY bucket "
        f"ORDER BY bucket ASC"
    )
    params = _bind_params(from_ts, to_ts)
    rows = db.execute(sql, params).fetchall()
    return _rows_to_dict(rows)


# ---------------------------------------------------------------------------
# Row-result helper
# ---------------------------------------------------------------------------


def _rows_to_dict(rows: Sequence[Any]) -> dict[int, float | None]:
    """Convert (bucket, value) row pairs to {bucket_int: float | None}."""
    result: dict[int, float | None] = {}
    for row in rows:
        bucket = int(row[0])
        value = float(row[1]) if row[1] is not None else None
        result[bucket] = value
    return result


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------


def _make_labels_from_range(group_by: str, bucket_range: list[int]) -> list[str]:
    """Produce labels from an explicit sorted bucket range."""
    if group_by == "month":
        return [f"{m:02d}" for m in bucket_range]
    if group_by == "hour":
        return [f"{h:02d}" for h in bucket_range]
    if group_by == "day":
        return [f"{d:03d}" for d in bucket_range]
    if group_by == "year":
        return [str(y) for y in bucket_range]
    raise ValueError(f"Unsupported group_by: {group_by!r}")


# ---------------------------------------------------------------------------
# Series-key builder
# ---------------------------------------------------------------------------


def _series_key(field: str, agg_type: str, avg_type: str | None) -> str:
    """Build the series dict key as 'field:agg_type[:avg_type]'."""
    if avg_type:
        return f"{field}:{agg_type}:{avg_type}"
    return f"{field}:{agg_type}"


# ---------------------------------------------------------------------------
# Aggregation dispatch
# ---------------------------------------------------------------------------


def _dispatch_query(
    db: Session,
    column: str,
    agg_type: str,
    avg_type: str | None,
    group_by: str,
    dialect_name: str,
    from_ts: int | None,
    to_ts: int | None,
) -> dict[int, float | None]:
    """Dispatch to the appropriate SQL helper based on agg_type + avg_type.

    Dispatch table (from task brief):

    agg_type  avg_type  → helper
    --------  --------  -------
    avg       max       → _query_avg_of_daily_agg(..., "MAX")
    avg       min       → _query_avg_of_daily_agg(..., "MIN")
    avg       sum       → _query_avg_of_period_total(...)
    avg       (none)    → _query_straight_avg(...)
    sum       -         → _query_period_sum(...)
    max       -         → _query_period_max(...)
    min       -         → _query_period_min(...)

    For avg:max and avg:min with group_by=hour, the two-level daily aggregation
    is semantically odd (hourly buckets of daily maxes), so we fall back to
    _query_straight_avg — consistent with how hour-level data should work.
    """
    if agg_type == "avg":
        if avg_type == "max":
            if group_by == "hour":
                # Two-level daily agg doesn't apply at the hour dimension.
                return _query_straight_avg(
                    db, column, group_by, dialect_name, from_ts, to_ts
                )
            return _query_avg_of_daily_agg(
                db, column, "MAX", group_by, dialect_name, from_ts, to_ts
            )
        if avg_type == "min":
            if group_by == "hour":
                return _query_straight_avg(
                    db, column, group_by, dialect_name, from_ts, to_ts
                )
            return _query_avg_of_daily_agg(
                db, column, "MIN", group_by, dialect_name, from_ts, to_ts
            )
        if avg_type == "sum":
            if group_by in ("day", "hour"):
                # avg-of-period-total is month/year semantics;
                # for day/hour use plain sum.
                return _query_period_sum(
                    db, column, group_by, dialect_name, from_ts, to_ts
                )
            return _query_avg_of_period_total(
                db, column, group_by, dialect_name, from_ts, to_ts
            )
        # avg with no avg_type → straight average
        return _query_straight_avg(
            db, column, group_by, dialect_name, from_ts, to_ts
        )

    if agg_type == "sum":
        return _query_period_sum(db, column, group_by, dialect_name, from_ts, to_ts)

    if agg_type == "max":
        return _query_period_max(db, column, group_by, dialect_name, from_ts, to_ts)

    if agg_type == "min":
        return _query_period_min(db, column, group_by, dialect_name, from_ts, to_ts)

    # Caller validates agg_type before this function is called.
    raise ValueError(f"Unsupported agg_type: {agg_type!r}")


# ---------------------------------------------------------------------------
# Null-padding helpers
# ---------------------------------------------------------------------------


def _pad_series(
    by_bucket: dict[int, float | None],
    bucket_range: list[int],
) -> list[float | None]:
    """Map a {bucket: value} dict onto a fixed ordered bucket range.

    Buckets absent from by_bucket are filled with None.
    """
    return [by_bucket.get(b) for b in bucket_range]


def _full_period_range(group_by: str, observed_buckets: set[int]) -> list[int]:
    """Return the canonical full-period bucket range for null-padding.

    month → [1, 2, ..., 12]
    hour  → [0, 1, ..., 23]
    day   → observed range (can't know full year without knowing which year)
    year  → observed range (open-ended by design)
    """
    if group_by == "month":
        return list(range(1, 13))
    if group_by == "hour":
        return list(range(0, 24))
    # day and year: use observed buckets sorted
    return sorted(observed_buckets)


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


def get_grouped_archive(
    db: Session,
    registry: ColumnRegistry,
    group_by: str,
    field_specs: list[tuple[str, str, str | None]],  # (field, agg_type, avg_type)
    from_ts: int | None = None,
    to_ts: int | None = None,
    force_full_period: bool = True,
) -> dict[str, Any]:
    """Compute grouped aggregations from the weewx archive.

    Args:
        db: SQLAlchemy session.
        registry: Column registry for field validation (allowlist).
        group_by: Grouping dimension — one of 'month', 'day', 'hour', 'year'.
        field_specs: List of (field, agg_type, avg_type) tuples.
            field: canonical archive field name (validated against registry).
            agg_type: 'avg' | 'sum' | 'max' | 'min'
            avg_type: 'max' | 'min' | 'sum' | None
        from_ts: Optional lower epoch bound (inclusive).  Bound as named param.
        to_ts: Optional upper epoch bound (exclusive).  Bound as named param.
        force_full_period: When True, pad to the full canonical range for the
            group_by dimension (12 months, 24 hours).  Default True.

    Returns:
        {
            "labels": ["01", "02", ..., "12"],  # for month
            "series": {
                "outTemp:avg:max": [72.3, 68.1, ...],
                "rain:avg:sum": [2.8, 3.5, ...],
            }
        }

    Fields absent from the registry are skipped with a warning (self-hide rule).
    Unknown group_by or agg_type values are rejected by the endpoint layer
    before this function is called.

    All SQL is parameterised for time bounds.  Column names come from
    ColumnRegistry-validated allowlists; no user input reaches the SQL string.
    """
    # Validate group_by (belt-and-suspenders; endpoint also validates).
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(
            f"Invalid group_by {group_by!r}; must be one of "
            f"{sorted(_VALID_GROUP_BY)}"
        )

    mapped_field_names = set(registry.stock.keys()) | set(registry.unmapped.keys())
    dialect_name: str = db.bind.dialect.name  # type: ignore[union-attr]

    # Collect per-series results as bucket dicts first; align to labels after.
    raw_series: dict[str, dict[int, float | None]] = {}
    all_observed_buckets: set[int] = set()

    for field, agg_type, avg_type in field_specs:
        # Validate field against ColumnRegistry allowlist.
        if field not in mapped_field_names:
            logger.warning(
                "archive_grouped: field %r not in registry — skipping", field
            )
            continue

        key = _series_key(field, agg_type, avg_type)
        try:
            by_bucket = _dispatch_query(
                db=db,
                column=field,
                agg_type=agg_type,
                avg_type=avg_type,
                group_by=group_by,
                dialect_name=dialect_name,
                from_ts=from_ts,
                to_ts=to_ts,
            )
        except Exception:
            logger.exception(
                "archive_grouped: failed to query field=%r agg_type=%r avg_type=%r",
                field,
                agg_type,
                avg_type,
            )
            continue

        all_observed_buckets.update(by_bucket.keys())
        raw_series[key] = by_bucket

    # Determine bucket range for labels and padding.
    bucket_range = _full_period_range(group_by, all_observed_buckets)
    if not force_full_period or group_by in ("day", "year"):
        # Variable dimensions always use observed buckets only.
        bucket_range = sorted(all_observed_buckets)

    labels = _make_labels_from_range(group_by, bucket_range)

    # Align each series to the bucket range.
    aligned_series: dict[str, list[float | None]] = {
        key: _pad_series(by_bucket, bucket_range)
        for key, by_bucket in raw_series.items()
    }

    return {
        "labels": labels,
        "series": aligned_series,
    }
