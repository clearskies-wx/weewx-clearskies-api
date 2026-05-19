"""AQI history service — reads from the weewx archive (ADR-013 corrected, P4-T3).

Path A: operators with AQI columns in their weewx archive configure column names
  in [aqi.history] of api.conf.  This service queries those columns and returns
  canonical AQIReading objects.

Path B: operators without archive AQI columns leave [aqi.history] unconfigured.
  All column fields default to empty string.  This service returns an empty list
  and a PageInfo with total=0.  No error is raised — this is the expected state.

SQL note: column identifiers come exclusively from AQIHistorySettings (trusted
  operator config constants, not user input).  All value bindings use named
  parameters (:name).  User-supplied values (from_ts, to_ts, limit, offset)
  are bound only via SQLAlchemy text() parameters — never string-interpolated.

ruff: noqa: N815  (canonical AQI field names use camelCase per ADR-010)
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from weewx_clearskies_api.config.settings import AQIHistorySettings
from weewx_clearskies_api.models.responses import AQIReading, PageInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical field → (archive alias, AQIReading field name)
# Maps each AQIHistorySettings column attribute to the corresponding canonical
# AQIReading field.  Built once; used by _build_column_map().
# ---------------------------------------------------------------------------

# (settings_attr, aqi_reading_field, sql_alias)
_FIELD_SPEC: list[tuple[str, str, str]] = [
    ("column_aqi",              "aqi",              "col_aqi"),
    ("column_aqi_category",     "aqiCategory",      "col_aqi_category"),
    ("column_aqi_main_pollutant", "aqiMainPollutant", "col_aqi_main_pollutant"),
    ("column_aqi_location",     "aqiLocation",      "col_aqi_location"),
    ("column_pm25",             "pollutantPM25",    "col_pm25"),
    ("column_pm10",             "pollutantPM10",    "col_pm10"),
    ("column_o3",               "pollutantO3",      "col_o3"),
    ("column_no2",              "pollutantNO2",     "col_no2"),
    ("column_so2",              "pollutantSO2",     "col_so2"),
    ("column_co",               "pollutantCO",      "col_co"),
]


# ---------------------------------------------------------------------------
# Cursor helpers (same encode/decode pattern as services/archive.py)
# ---------------------------------------------------------------------------


def encode_cursor(after_datetime: int) -> str:
    """Encode a cursor from a dateTime epoch value."""
    payload = json.dumps({"after_dateTime": after_datetime})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> int:
    """Decode a cursor and return the after_dateTime epoch value.

    Raises:
        ValueError: If the cursor is malformed or missing the required key.
    """
    try:
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(payload)
        after = data["after_dateTime"]
        if not isinstance(after, int):
            raise ValueError("after_dateTime must be an integer")
        return after
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


# ---------------------------------------------------------------------------
# Column mapping builder
# ---------------------------------------------------------------------------


def _build_column_map(
    hist: AQIHistorySettings,
) -> list[tuple[str, str, str]]:
    """Build a list of (archive_column, aqi_reading_field, sql_alias) triples.

    Only includes entries where the archive column is configured (non-empty).
    Returns an empty list when no columns are configured (Path B).

    Column names come from operator config (trusted constants), not user input.
    """
    result: list[tuple[str, str, str]] = []
    for settings_attr, reading_field, sql_alias in _FIELD_SPEC:
        col_name = getattr(hist, settings_attr, "")
        if col_name:
            result.append((col_name, reading_field, sql_alias))
    return result


# ---------------------------------------------------------------------------
# Epoch → ISO-8601 UTC Z
# ---------------------------------------------------------------------------


def _epoch_to_utc_z(epoch: int | float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Primary service function
# ---------------------------------------------------------------------------


def get_aqi_history(
    db: Session,
    hist: AQIHistorySettings,
    from_dt: datetime | None,
    to_dt: datetime | None,
    limit: int,
    cursor: str | None,
    page: int | None,
) -> tuple[list[AQIReading], PageInfo]:
    """Query the weewx archive for AQI readings and return (readings, page_info).

    Returns ([], PageInfo(total=0)) immediately when no AQI columns are configured
    (Path B operators).

    Args:
        db: SQLAlchemy Session (per-request, read-only).
        hist: AQIHistorySettings with archive column mappings.
        from_dt: Start of time window (inclusive).  None → 24 h ago.
        to_dt: End of time window (exclusive).  None → now.
        limit: Maximum rows to return.
        cursor: Opaque pagination cursor (mutually exclusive with page).
        page: 1-based page number (mutually exclusive with cursor).

    Returns:
        (readings, PageInfo) where readings is a list of AQIReading objects.
    """
    col_map = _build_column_map(hist)
    if not col_map:
        # Path B: no AQI columns configured.  Return empty result.
        logger.debug(
            "AQI history: no archive columns configured; returning empty result (Path B)"
        )
        return [], PageInfo(cursor=None, limit=limit, page=page, totalPages=None, totalRecords=0)

    now = datetime.now(tz=UTC)
    effective_to = to_dt if to_dt is not None else now
    effective_from = from_dt if from_dt is not None else (now - timedelta(hours=24))

    from_epoch = int(effective_from.timestamp())
    to_epoch = int(effective_to.timestamp())

    # Cursor takes priority; adjusts from_epoch forward past the cursor point.
    if cursor is not None:
        after_dt_epoch = decode_cursor(cursor)
        from_epoch = after_dt_epoch + 1

    offset = 0
    if page is not None and cursor is None:
        offset = (page - 1) * limit

    # Build the SELECT list from trusted config column names (not user input).
    # Each column gets a stable alias so row-to-model mapping is by alias name.
    # Pattern: `archive_column AS col_alias`
    col_select_parts = ", ".join(
        f"{archive_col} AS {sql_alias}"
        for archive_col, _field, sql_alias in col_map
    )

    # Column identifiers (archive_col, sql_alias) come from operator config
    # (trusted constants).  Value parameters (:from_ts etc.) are named bindings.
    sql = text(
        f"SELECT dateTime, {col_select_parts} "
        f"FROM archive "
        f"WHERE dateTime >= :from_ts AND dateTime < :to_ts "
        f"ORDER BY dateTime ASC "
        f"LIMIT :lim OFFSET :off"
    )
    rows = db.execute(
        sql,
        {"from_ts": from_epoch, "to_ts": to_epoch, "lim": limit + 1, "off": offset},
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    # Map rows to AQIReading objects.
    readings: list[AQIReading] = []
    for row in rows:
        row_dict = dict(row._mapping)  # noqa: SLF001
        dt_epoch = row_dict.get("dateTime")
        observed_at = _epoch_to_utc_z(dt_epoch) if dt_epoch is not None else ""

        kwargs: dict[str, object] = {
            "observedAt": observed_at,
            "source": "weewx",
        }
        for _archive_col, reading_field, sql_alias in col_map:
            val = row_dict.get(sql_alias)
            if val is not None:
                kwargs[reading_field] = val

        readings.append(AQIReading(**kwargs))

    # Build next_cursor for cursor-based pagination.
    next_cursor: str | None = None
    if has_more and rows:
        last_row_dict = dict(rows[-1]._mapping)  # noqa: SLF001
        last_epoch = last_row_dict.get("dateTime")
        if last_epoch is not None:
            next_cursor = encode_cursor(int(last_epoch))

    # Total count for page-number pagination.
    total_pages: int | None = None
    total_records: int | None = None

    if page is not None:
        count_sql = text(
            "SELECT COUNT(*) FROM archive "
            "WHERE dateTime >= :from_ts AND dateTime < :to_ts"
        )
        total_records = db.execute(
            count_sql, {"from_ts": from_epoch, "to_ts": to_epoch}
        ).scalar() or 0
        total_pages = max(1, (total_records + limit - 1) // limit)

    return readings, PageInfo(
        cursor=next_cursor,
        limit=limit,
        page=page,
        totalPages=total_pages,
        totalRecords=total_records,
    )
