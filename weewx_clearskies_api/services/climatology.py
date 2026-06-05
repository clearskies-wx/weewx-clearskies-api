"""Climatology service — 12-month average values from the weewx archive.

Computes per-month averages (month number 1-12, collapsed across all years)
for temperature highs/lows, dewpoint, and rainfall.

Self-hide rule: if a canonical field is not in the ColumnRegistry's mapped
set, the corresponding key is omitted from the returned dict entirely.

SQL note: column identifiers come exclusively from the _CLIM_DB_COLS constant
— a module-level dict with hard-coded keys, not user-supplied values.  All
value bindings use SQLAlchemy named parameters.  No user-controlled data is
interpolated into query text.

For the generalized get_climatology_by_fields() path: caller-supplied field
names are validated against the ColumnRegistry before any SQL is composed.
Only names that appear in the registry (a curated allowlist of known weewx
archive column names) reach the SQL string. This prevents SQL injection while
allowing backtick-quoted identifier interpolation for column names, which
cannot be bound as SQL parameters.

ruff: noqa: N815  (canonical fields use weewx camelCase per ADR-010)
"""
# ruff: noqa: S608  (SQL f-strings are safe here — all column names come from
#                    hard-coded constants or registry-validated allowlists;
#                    no user input reaches the SQL string)

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.reflection import ColumnRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trusted constant: canonical name → archive DB column name.
# Keys and values are hard-coded; neither comes from HTTP request inputs.
# ---------------------------------------------------------------------------

_CLIM_DB_COLS: dict[str, str] = {
    "outTemp": "outTemp",
    "dewpoint": "dewpoint",
    "rain": "rain",
}

_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# Dialect helpers — produce trusted SQL month-number expressions.
# These are internal constants, not user input.
# ---------------------------------------------------------------------------


def _month_number_sql(dialect_name: str) -> str:
    """Return a SQL expression that extracts the month number (1-12) from
    the archive's `dateTime` epoch column.

    SQLite uses strftime; MySQL/MariaDB uses MONTH(FROM_UNIXTIME()).
    Both expressions are trusted dialect constants — not user input.
    """
    if dialect_name == "sqlite":
        return "CAST(strftime('%m', datetime(dateTime, 'unixepoch')) AS INTEGER)"
    # MariaDB/MySQL — no % format codes used directly in Python text() here.
    return "MONTH(FROM_UNIXTIME(dateTime))"


def _year_month_sql(dialect_name: str) -> str:
    """Return a SQL expression that extracts a year-month bucket for grouping.

    Used in the rainfall subquery to sum per calendar month across each year,
    before averaging those monthly totals by month number.

    SQLite: 'YYYY-MM' string (sortable, groups correctly).
    MariaDB/MySQL: DATE_FORMAT expression — %% required for pymysql pyformat driver.
    """
    if dialect_name == "sqlite":
        return "strftime('%Y-%m', datetime(dateTime, 'unixepoch'))"
    # %% because SQLAlchemy text() escapes % to %% for pymysql pyformat driver.
    return "DATE_FORMAT(FROM_UNIXTIME(dateTime), '%%Y-%%m')"


def _day_bucket_sql(dialect_name: str) -> str:
    """Return a SQL expression for a day bucket (YYYY-MM-DD).

    Used in the temperature subquery to find daily max/min before averaging.
    """
    if dialect_name == "sqlite":
        return "strftime('%Y-%m-%d', datetime(dateTime, 'unixepoch'))"
    return "DATE(FROM_UNIXTIME(dateTime))"


def _month_num_from_day_bucket(dialect_name: str) -> str:
    """Return a SQL expression that extracts month number (1-12) from the
    day_bucket column produced by _day_bucket_sql().

    Used in the outer temperature query to group daily highs/lows by month.
    """
    if dialect_name == "sqlite":
        # day_bucket is 'YYYY-MM-DD'; CAST(substr(...,6,2) AS INTEGER) → month number.
        return "CAST(SUBSTR(day_bucket, 6, 2) AS INTEGER)"
    # MariaDB/MySQL: day_bucket is a DATE value; MONTH() extracts the month.
    return "MONTH(day_bucket)"


def _month_num_from_ym_bucket(dialect_name: str) -> str:
    """Return a SQL expression that extracts month number (1-12) from the
    year_month bucket column produced by _year_month_sql().

    Used in the outer rainfall query to group monthly totals by month number.
    """
    if dialect_name == "sqlite":
        # ym_bucket is 'YYYY-MM'; CAST(substr(...,6,2) AS INTEGER) → month number.
        return "CAST(SUBSTR(ym_bucket, 6, 2) AS INTEGER)"
    # MariaDB/MySQL: CAST(... AS INTEGER) is not valid; use CAST(... AS SIGNED).
    return "CAST(SUBSTR(ym_bucket, 6, 2) AS SIGNED)"


# ---------------------------------------------------------------------------
# Per-metric query helpers
# ---------------------------------------------------------------------------


def _query_avg_temp_highs_lows(
    db: Session,
    db_col: str,          # hard-coded from _CLIM_DB_COLS — not user input
    dialect_name: str,
) -> dict[int, tuple[float | None, float | None]]:
    """Return {month_number: (avg_daily_high, avg_daily_low)} for months 1-12.

    Two-level aggregation:
      Inner: GROUP BY day_bucket → MAX(col) as daily_high, MIN(col) as daily_low
      Outer: GROUP BY month_number → AVG(daily_high), AVG(daily_low)

    Both db_col and all SQL fragments come from hard-coded constants.
    """
    day_bucket = _day_bucket_sql(dialect_name)
    month_from_day = _month_num_from_day_bucket(dialect_name)

    sql = text(
        f"SELECT {month_from_day} AS mnum, "
        f"       AVG(daily_high) AS avg_high, "
        f"       AVG(daily_low)  AS avg_low "
        f"FROM ("
        f"  SELECT {day_bucket} AS day_bucket, "
        f"         MAX({db_col}) AS daily_high, "
        f"         MIN({db_col}) AS daily_low "
        f"  FROM archive "
        f"  WHERE {db_col} IS NOT NULL "
        f"  GROUP BY day_bucket"
        f") sub "
        f"GROUP BY mnum "
        f"ORDER BY mnum ASC"
    )
    rows = db.execute(sql).fetchall()
    result: dict[int, tuple[float | None, float | None]] = {}
    for row in rows:
        mnum = int(row[0])
        avg_high = float(row[1]) if row[1] is not None else None
        avg_low = float(row[2]) if row[2] is not None else None
        result[mnum] = (avg_high, avg_low)
    return result


def _query_avg_dewpoint(
    db: Session,
    db_col: str,          # hard-coded from _CLIM_DB_COLS — not user input
    dialect_name: str,
) -> dict[int, float | None]:
    """Return {month_number: avg_dewpoint} for months 1-12.

    Straight AVG(dewpoint) grouped by month number — no daily bucketing needed.
    """
    month_num = _month_number_sql(dialect_name)

    sql = text(
        f"SELECT {month_num} AS mnum, "
        f"       AVG({db_col}) AS avg_dew "
        f"FROM archive "
        f"WHERE {db_col} IS NOT NULL "
        f"GROUP BY mnum "
        f"ORDER BY mnum ASC"
    )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        avg_dew = float(row[1]) if row[1] is not None else None
        result[mnum] = avg_dew
    return result


def _query_avg_rainfall(
    db: Session,
    db_col: str,          # hard-coded from _CLIM_DB_COLS — not user input
    dialect_name: str,
) -> dict[int, float | None]:
    """Return {month_number: avg_monthly_total_rain} for months 1-12.

    Two-level aggregation:
      Inner: GROUP BY year+month → SUM(rain) as monthly_total
      Outer: GROUP BY month_number → AVG(monthly_total)

    MySQL/MariaDB uses YEAR()/MONTH() directly to avoid DATE_FORMAT percent-
    escaping issues with SQLAlchemy text() and pymysql's pyformat paramstyle.
    """
    if dialect_name == "sqlite":
        ym_bucket = _year_month_sql(dialect_name)
        month_from_ym = _month_num_from_ym_bucket(dialect_name)
        sql = text(
            f"SELECT {month_from_ym} AS mnum, "
            f"       AVG(monthly_total) AS avg_rain "
            f"FROM ("
            f"  SELECT {ym_bucket} AS ym_bucket, "
            f"         SUM({db_col}) AS monthly_total "
            f"  FROM archive "
            f"  WHERE {db_col} IS NOT NULL "
            f"  GROUP BY ym_bucket"
            f") sub "
            f"GROUP BY mnum "
            f"ORDER BY mnum ASC"
        )
    else:
        sql = text(
            f"SELECT mo AS mnum, "
            f"       AVG(monthly_total) AS avg_rain "
            f"FROM ("
            f"  SELECT YEAR(FROM_UNIXTIME(dateTime)) AS yr, "
            f"         MONTH(FROM_UNIXTIME(dateTime)) AS mo, "
            f"         SUM({db_col}) AS monthly_total "
            f"  FROM archive "
            f"  WHERE {db_col} IS NOT NULL "
            f"  GROUP BY yr, mo"
            f") sub "
            f"GROUP BY mnum "
            f"ORDER BY mnum ASC"
        )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        avg_rain = float(row[1]) if row[1] is not None else None
        result[mnum] = avg_rain
    return result


# ---------------------------------------------------------------------------
# Helper: dict → 12-element list (None for months with no data)
# ---------------------------------------------------------------------------


def _to_12_list(by_month: dict[int, float | None]) -> list[float | None]:
    """Convert a {month_number: value} dict to a 12-element list indexed 0-11."""
    return [by_month.get(m) for m in range(1, 13)]


def _to_12_list_pair(
    by_month: dict[int, tuple[float | None, float | None]],
    index: int,
) -> list[float | None]:
    """Extract one side (high=0, low=1) of a temp-pair dict as a 12-element list."""
    return [by_month.get(m, (None, None))[index] for m in range(1, 13)]


# ---------------------------------------------------------------------------
# Primary service function
# ---------------------------------------------------------------------------


def get_monthly_climatology(db: Session, registry: ColumnRegistry) -> dict:
    """Compute 12-month climatology averages from the weewx archive.

    Returns a dict with:
      - months: ["Jan", ..., "Dec"]
      - avgHighTemp (if outTemp in registry): 12-element list of floats/null
      - avgLowTemp  (if outTemp in registry): 12-element list of floats/null
      - avgDewpoint (if dewpoint in registry): 12-element list of floats/null
      - avgRainfall (if rain in registry): 12-element list of floats/null

    Fields whose backing column is absent from the registry are omitted
    entirely (self-hide rule, consistent with records service).

    All SQL is parameterised; column names come from _CLIM_DB_COLS (a
    module-level constant with hard-coded values, not user input).
    """
    mapped_fields = set(registry.stock.keys())
    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    result: dict = {"months": _MONTH_NAMES}

    # outTemp → avgHighTemp + avgLowTemp
    if "outTemp" in mapped_fields:
        db_col = _CLIM_DB_COLS["outTemp"]
        try:
            temp_data = _query_avg_temp_highs_lows(db, db_col, dialect_name)
            result["avgHighTemp"] = _to_12_list_pair(temp_data, 0)
            result["avgLowTemp"] = _to_12_list_pair(temp_data, 1)
        except Exception:
            logger.exception("climatology: failed to query outTemp averages")

    # dewpoint → avgDewpoint
    if "dewpoint" in mapped_fields:
        db_col = _CLIM_DB_COLS["dewpoint"]
        try:
            dew_data = _query_avg_dewpoint(db, db_col, dialect_name)
            result["avgDewpoint"] = _to_12_list(dew_data)
        except Exception:
            logger.exception("climatology: failed to query dewpoint averages")

    # rain → avgRainfall
    if "rain" in mapped_fields:
        db_col = _CLIM_DB_COLS["rain"]
        try:
            rain_data = _query_avg_rainfall(db, db_col, dialect_name)
            result["avgRainfall"] = _to_12_list(rain_data)
        except Exception:
            logger.exception("climatology: failed to query rainfall averages")

    return result


# ---------------------------------------------------------------------------
# Generalized query helpers (accept any validated column name)
#
# Security model: column names are validated against ColumnRegistry before
# reaching these functions. Only names present in the registry (a curated
# allowlist of known weewx archive column names) are passed here. Column
# identifiers cannot be bound as SQL parameters, so post-validation
# interpolation is used. Backtick-quoting handles reserved words per
# coding.md §1 (e.g. `interval` is a MariaDB reserved word).
# ---------------------------------------------------------------------------


_VALID_AGG_TYPES = frozenset({"avg_max", "avg_min", "avg", "avg_monthly_total", "sum"})


def _query_avg_of_daily_agg(
    db: Session,
    column: str,      # validated against ColumnRegistry — not user-supplied verbatim
    daily_agg: str,   # "MAX" or "MIN" — caller-controlled constant, not user input
    dialect_name: str,
) -> dict[int, float | None]:
    """Average of daily MAX or MIN for any validated column.

    Generalises _query_avg_temp_highs_lows for a single aggregation side.

    Two-level aggregation:
      Inner: GROUP BY day_bucket → daily_agg(column) AS daily_val
      Outer: GROUP BY month_number → AVG(daily_val)

    Column name is validated against ColumnRegistry before use — only known
    weewx column names reach the SQL string.
    """
    day_bucket = _day_bucket_sql(dialect_name)
    month_from_day = _month_num_from_day_bucket(dialect_name)

    sql = text(
        f"SELECT {month_from_day} AS mnum, "
        f"       AVG(daily_val) AS avg_val "
        f"FROM ("
        f"  SELECT {day_bucket} AS day_bucket, "
        f"         {daily_agg}(`{column}`) AS daily_val "
        f"  FROM archive "
        f"  WHERE `{column}` IS NOT NULL "
        f"  GROUP BY day_bucket"
        f") sub "
        f"GROUP BY mnum "
        f"ORDER BY mnum ASC"
    )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        avg_val = float(row[1]) if row[1] is not None else None
        result[mnum] = avg_val
    return result


def _query_straight_avg(
    db: Session,
    column: str,      # validated against ColumnRegistry — not user-supplied verbatim
    dialect_name: str,
) -> dict[int, float | None]:
    """Straight monthly average for any validated column.

    Generalises _query_avg_dewpoint.

    Column name is validated against ColumnRegistry before use — only known
    weewx column names reach the SQL string.
    """
    month_num = _month_number_sql(dialect_name)

    sql = text(
        f"SELECT {month_num} AS mnum, "
        f"       AVG(`{column}`) AS avg_val "
        f"FROM archive "
        f"WHERE `{column}` IS NOT NULL "
        f"GROUP BY mnum "
        f"ORDER BY mnum ASC"
    )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        avg_val = float(row[1]) if row[1] is not None else None
        result[mnum] = avg_val
    return result


def _query_avg_of_monthly_total(
    db: Session,
    column: str,      # validated against ColumnRegistry — not user-supplied verbatim
    dialect_name: str,
) -> dict[int, float | None]:
    """Average of monthly totals for any validated column.

    Generalises _query_avg_rainfall.

    Column name is validated against ColumnRegistry before use — only known
    weewx column names reach the SQL string.
    """
    if dialect_name == "sqlite":
        ym_bucket = _year_month_sql(dialect_name)
        month_from_ym = _month_num_from_ym_bucket(dialect_name)
        sql = text(
            f"SELECT {month_from_ym} AS mnum, "
            f"       AVG(monthly_total) AS avg_val "
            f"FROM ("
            f"  SELECT {ym_bucket} AS ym_bucket, "
            f"         SUM(`{column}`) AS monthly_total "
            f"  FROM archive "
            f"  WHERE `{column}` IS NOT NULL "
            f"  GROUP BY ym_bucket"
            f") sub "
            f"GROUP BY mnum "
            f"ORDER BY mnum ASC"
        )
    else:
        sql = text(
            f"SELECT mo AS mnum, "
            f"       AVG(monthly_total) AS avg_val "
            f"FROM ("
            f"  SELECT YEAR(FROM_UNIXTIME(dateTime)) AS yr, "
            f"         MONTH(FROM_UNIXTIME(dateTime)) AS mo, "
            f"         SUM(`{column}`) AS monthly_total "
            f"  FROM archive "
            f"  WHERE `{column}` IS NOT NULL "
            f"  GROUP BY yr, mo"
            f") sub "
            f"GROUP BY mnum "
            f"ORDER BY mnum ASC"
        )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        avg_val = float(row[1]) if row[1] is not None else None
        result[mnum] = avg_val
    return result


def _query_monthly_sum(
    db: Session,
    column: str,      # validated against ColumnRegistry — not user-supplied verbatim
    dialect_name: str,
) -> dict[int, float | None]:
    """Straight monthly sum for any validated column.

    Column name is validated against ColumnRegistry before use — only known
    weewx column names reach the SQL string.
    """
    month_num = _month_number_sql(dialect_name)

    sql = text(
        f"SELECT {month_num} AS mnum, "
        f"       SUM(`{column}`) AS sum_val "
        f"FROM archive "
        f"WHERE `{column}` IS NOT NULL "
        f"GROUP BY mnum "
        f"ORDER BY mnum ASC"
    )
    rows = db.execute(sql).fetchall()
    result: dict[int, float | None] = {}
    for row in rows:
        mnum = int(row[0])
        sum_val = float(row[1]) if row[1] is not None else None
        result[mnum] = sum_val
    return result


# ---------------------------------------------------------------------------
# Generalized public service function
# ---------------------------------------------------------------------------


def get_climatology_by_fields(
    db: Session,
    registry: ColumnRegistry,
    fields: list[str],
    agg: str,
) -> dict[str, Any]:
    """Compute monthly climatology for arbitrary fields with specified aggregation.

    Args:
        db: SQLAlchemy session.
        registry: Column registry for field validation (allowlist).
        fields: List of canonical field names to aggregate.
        agg: Aggregation type — one of avg_max, avg_min, avg, avg_monthly_total, sum.

    Returns:
        Dict with 'months' (12-element name list) and 'results' (field → 12-element list).

    Fields absent from the registry are skipped with a warning (self-hide rule).
    Unknown agg values are rejected by the endpoint before this function is called.
    """
    mapped_fields = set(registry.stock.keys())
    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    results: dict[str, list[float | None]] = {}

    for field in fields:
        if field not in mapped_fields:
            logger.warning(
                "climatology: field %r not in registry — skipping", field
            )
            continue

        # Column name validated against registry allowlist above.
        # Only known weewx archive column names reach the SQL helpers.
        try:
            if agg == "avg_max":
                by_month = _query_avg_of_daily_agg(db, field, "MAX", dialect_name)
                results[field] = _to_12_list(by_month)
            elif agg == "avg_min":
                by_month = _query_avg_of_daily_agg(db, field, "MIN", dialect_name)
                results[field] = _to_12_list(by_month)
            elif agg == "avg":
                by_month = _query_straight_avg(db, field, dialect_name)
                results[field] = _to_12_list(by_month)
            elif agg == "avg_monthly_total":
                by_month = _query_avg_of_monthly_total(db, field, dialect_name)
                results[field] = _to_12_list(by_month)
            elif agg == "sum":
                by_month = _query_monthly_sum(db, field, dialect_name)
                results[field] = _to_12_list(by_month)
            # No else branch: agg is validated at the endpoint layer before this call.
        except Exception:
            logger.exception(
                "climatology: failed to query field=%r agg=%r", field, agg
            )

    return {"months": _MONTH_NAMES, "results": results}
