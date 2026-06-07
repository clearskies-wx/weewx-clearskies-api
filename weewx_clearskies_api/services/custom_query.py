"""Custom SQL query validation and execution service.

Security model (ADR-038 / security-baseline):
  - SQL queries are loaded from charts.conf on disk (operator-controlled).
  - SQL never crosses the HTTP boundary; the endpoint accepts only series_id.
  - Defense-in-depth:
      1. DDL keyword blocklist at startup (word-boundary regex).
      2. EXPLAIN validation at startup (query parses against real schema).
      3. Read-only DB connection (enforced at connection level by ADR-012).
      4. Query timeout (best-effort, MariaDB only).

The module exposes two public callables:
  validate_custom_queries(db) — called once from __main__.py at startup.
  execute_custom_query(db, series_id, ...) — called per HTTP request.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from weewx_clearskies_api.services.charts_config import get_charts_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL keyword blocklist — word-boundary matched to avoid false positives on
# column names that contain blocklisted substrings (e.g. "updated" vs "UPDATE").
# ---------------------------------------------------------------------------

_DDL_BLOCKLIST = {
    "DROP",
    "CREATE",
    "ALTER",
    "DELETE",
    "INSERT",
    "UPDATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "REPLACE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "LOAD",
    "MERGE",
    "RENAME",
    "LOCK",
    "UNLOCK",
}

# Pre-compiled pattern — case-insensitive, whole-word matching.
_DDL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _DDL_BLOCKLIST) + r")\b",
    re.IGNORECASE,
)

# Dummy epoch values used when substituting :from_ts/:to_ts for EXPLAIN.
_EXPLAIN_FROM_TS = 0
_EXPLAIN_TO_TS = 2_147_483_647

# Timeout for MariaDB queries (milliseconds).
_MARIADB_TIMEOUT_MS = 10_000


# ---------------------------------------------------------------------------
# Validated query cache — populated at startup, read per-request.
# ---------------------------------------------------------------------------


@dataclass
class ValidatedQuery:
    """A custom SQL query that has passed startup validation."""

    sql: str
    x_column: str
    y_column: str
    has_from_param: bool  # True if :from_ts appears in query
    has_to_param: bool  # True if :to_ts appears in query


_validated_queries: dict[str, ValidatedQuery] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_ddl_blocklist(sql: str, series_id: str) -> bool:
    """Return True if the query is clean (no DDL keywords).

    Logs a warning and returns False if a blocked keyword is found.
    """
    match = _DDL_PATTERN.search(sql)
    if match:
        logger.warning(
            "Custom SQL for series %r contains blocked keyword %r — skipping",
            series_id,
            match.group(0),
        )
        return False
    return True


def _explain_query(db: Session, sql: str, series_id: str) -> bool:
    """Run EXPLAIN against the query to verify it parses.

    Substitutes any :from_ts / :to_ts parameters with dummy epoch values so
    EXPLAIN sees valid SQL.

    Returns True if EXPLAIN succeeds; logs a warning and returns False on
    any error (syntax error, missing table, etc.).
    """
    # Substitute dummy values for named params before EXPLAIN.
    # EXPLAIN does not execute the query so the values are irrelevant;
    # they just need to be syntactically valid integers.
    explain_sql: str
    params: dict[str, int] = {}

    if ":from_ts" in sql:
        params["from_ts"] = _EXPLAIN_FROM_TS
    if ":to_ts" in sql:
        params["to_ts"] = _EXPLAIN_TO_TS

    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    explain_sql = "EXPLAIN QUERY PLAN " + sql if dialect_name == "sqlite" else "EXPLAIN " + sql

    try:
        db.execute(text(explain_sql), params)
    except Exception as exc:  # noqa: BLE001 — broad catch intentional; we want to warn + skip
        logger.warning(
            "Custom SQL for series %r failed EXPLAIN validation (%s: %s) — skipping",
            series_id,
            type(exc).__name__,
            exc,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Public: startup validation
# ---------------------------------------------------------------------------


def validate_custom_queries(db: Session) -> None:
    """Validate all custom SQL queries from charts config at startup.

    For each series with use_custom_sql=True:
      1. Check DDL blocklist.
      2. Run EXPLAIN to verify query parses against the live schema.
      3. Cache the ValidatedQuery.

    Invalid queries are logged as warnings and skipped (not cached).  An
    operator with one bad query should not lose all custom charts.

    Clears the module-level cache before populating it so this function can
    be called again safely in tests.
    """
    global _validated_queries  # noqa: PLW0603
    _validated_queries = {}

    charts_config = get_charts_config()

    candidate_count = 0
    validated_count = 0

    for group in charts_config.groups:
        for chart in group.charts:
            for series in chart.series:
                if not series.use_custom_sql:
                    continue

                series_id = series.series_id
                sql = series.custom_sql_query
                x_column = series.x_column
                y_column = series.y_column

                candidate_count += 1

                # Guard against incomplete config entries.
                if not sql:
                    logger.warning(
                        "Series %r has use_custom_sql=True but no custom_sql_query — skipping",
                        series_id,
                    )
                    continue
                if not x_column:
                    logger.warning(
                        "Series %r has use_custom_sql=True but no x_column — skipping",
                        series_id,
                    )
                    continue
                if not y_column:
                    logger.warning(
                        "Series %r has use_custom_sql=True but no y_column — skipping",
                        series_id,
                    )
                    continue

                if not _check_ddl_blocklist(sql, series_id):
                    continue

                if not _explain_query(db, sql, series_id):
                    continue

                _validated_queries[series_id] = ValidatedQuery(
                    sql=sql,
                    x_column=x_column,
                    y_column=y_column,
                    has_from_param=":from_ts" in sql,
                    has_to_param=":to_ts" in sql,
                )
                validated_count += 1
                logger.debug(
                    "Custom SQL series %r validated OK (x=%r, y=%r, from_param=%s, to_param=%s)",
                    series_id,
                    x_column,
                    y_column,
                    _validated_queries[series_id].has_from_param,
                    _validated_queries[series_id].has_to_param,
                )

    logger.info(
        "Custom SQL validation complete: %d/%d series validated",
        validated_count,
        candidate_count,
    )


def get_validated_series_ids() -> list[str]:
    """Return the list of series_ids that passed validation.

    Intended for diagnostics / the endpoint's 404 message.
    """
    return list(_validated_queries.keys())


# ---------------------------------------------------------------------------
# Public: per-request execution
# ---------------------------------------------------------------------------


def execute_custom_query(
    db: Session,
    series_id: str,
    from_epoch: float | None = None,
    to_epoch: float | None = None,
) -> list[dict[str, float | int | str | None]]:
    """Execute a validated custom SQL query.

    Args:
        db: Active SQLAlchemy session.
        series_id: Identifies the pre-validated query in the cache.
        from_epoch: Optional Unix epoch for :from_ts substitution.
        to_epoch: Optional Unix epoch for :to_ts substitution.

    Returns:
        List of {"x": <value>, "y": <value>} dicts, one per result row.

    Raises:
        KeyError: series_id is not in the validated query cache.
        Exception: Query execution failed (caller converts to 500).
    """
    validated = _validated_queries[series_id]  # raises KeyError if absent

    params: dict[str, float | int] = {}
    if validated.has_from_param:
        params["from_ts"] = from_epoch if from_epoch is not None else _EXPLAIN_FROM_TS
    if validated.has_to_param:
        params["to_ts"] = to_epoch if to_epoch is not None else _EXPLAIN_TO_TS

    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    if dialect_name == "mysql":
        # Best-effort timeout — limits long-running SELECTs.
        # MariaDB uses max_statement_time (seconds); MySQL uses max_execution_time (ms).
        # Try MariaDB syntax first, fall back to MySQL.
        timeout_s = _MARIADB_TIMEOUT_MS // 1000
        try:
            db.execute(text(f"SET SESSION max_statement_time = {timeout_s}"))  # noqa: S608
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                db.execute(text(f"SET SESSION max_execution_time = {_MARIADB_TIMEOUT_MS}"))  # noqa: S608

    rows = db.execute(text(validated.sql), params).fetchall()

    x_col = validated.x_column
    y_col = validated.y_column

    return [
        {"x": row._mapping[x_col], "y": row._mapping[y_col]}  # noqa: SLF001
        for row in rows
    ]
