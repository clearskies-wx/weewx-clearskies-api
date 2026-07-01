"""Correction engine SQLite persistence layer (ADR-079).

This module manages a SEPARATE SQLite database for forecast-observation pairs
and model metadata.  It is deliberately isolated from the weewx archive engine
(db/session.py) — the correction DB is writable and lives at an operator-
configured path, while the archive DB is read-only by architecture.

Public API
----------
init_engine(db_path)        Create engine, enable WAL mode, create tables.
wire_engine(engine)         Register a pre-built engine (used in tests).
get_engine()                Return the registered engine; raise if not wired.

insert_pair(...)            INSERT OR IGNORE a forecast-observation pair.
get_training_data(cutoff)   Pairs older than cutoff_epoch (training set).
get_validation_data(cutoff) Pairs from cutoff_epoch onward (validation set).
purge_old_records(before)   DELETE pairs older than before_epoch.
get_pair_count()            COUNT(*) of all pairs.
get_date_range()            (MIN, MAX) timestamp tuple.
save_model_metadata(...)    INSERT OR REPLACE singleton metadata row.
get_model_metadata()        SELECT the singleton metadata row or None.

All SQL uses text() with :param bind parameters — no f-strings in queries
(coding.md §1).  Explicit conn.commit() after every write because SQLAlchemy
2.x uses autobegin=True and does not auto-commit.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, event, text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level engine registry (same pattern as db/session.py)
# ---------------------------------------------------------------------------

_engine: Engine | None = None


def wire_engine(engine: Engine) -> None:
    """Register an externally-built engine.

    Used in tests that supply an in-memory SQLite engine without going through
    init_engine().  Production code calls init_engine() instead.
    """
    global _engine  # noqa: PLW0603 — intentional module-level registry
    _engine = engine


def get_engine() -> Engine:
    """Return the registered engine.

    Raises:
        RuntimeError: Engine has not been initialised.
    """
    if _engine is None:
        raise RuntimeError(
            "Correction engine is not initialised. "
            "init_engine() must be called before using the correction DB. "
            "This is a startup-sequence bug — check __main__.py."
        )
    return _engine


# ---------------------------------------------------------------------------
# Engine initialisation
# ---------------------------------------------------------------------------


def init_engine(db_path: str) -> Engine:
    """Create the correction SQLite engine, enable WAL mode, and create tables.

    Args:
        db_path: Filesystem path to the SQLite database file.  The directory
                 must already exist and be writable by the service user.

    Returns:
        The created (and module-level registered) Engine instance.
    """
    url = f"sqlite:///{db_path}"
    engine = create_engine(
        url,
        # SQLite does not benefit from connection pooling the same way MariaDB
        # does, but we do want connections to be reused within a request rather
        # than created per statement.  The default pool (StaticPool for
        # in-memory, NullPool would recreate each time) is fine here; for an
        # on-disk SQLite file the default SingletonThreadPool gives us one
        # connection per thread which matches the daemon-thread usage pattern.
        echo=False,
        # Ensure foreign key constraints are enforced when SQLite is used.
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_conn: object, _connection_record: object) -> None:
        """Enable WAL journal mode on each new connection.

        WAL mode allows concurrent readers alongside the single writer, which
        is important because the ForecastCollector background thread writes
        pairs while forecast endpoint threads may be reading metadata.
        """
        cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    wire_engine(engine)
    _create_tables(engine)
    logger.info("Correction engine initialised: %s", db_path)
    return engine


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def _create_tables(engine: Engine) -> None:
    """Create the correction tables if they do not yet exist.

    Uses raw SQL via text() to keep the schema visible and auditable without
    requiring SQLAlchemy ORM models.  All DDL is idempotent (IF NOT EXISTS).
    """
    ddl_pairs = text("""
        CREATE TABLE IF NOT EXISTS forecast_observation_pairs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         INTEGER NOT NULL UNIQUE,
            provider_id       TEXT    NOT NULL,
            month             INTEGER NOT NULL,
            hour              INTEGER NOT NULL,
            day_of_year       INTEGER NOT NULL,
            fcst_temp         REAL    NOT NULL,
            fcst_wind_dir     REAL,
            fcst_humidity     REAL,
            fcst_cloud_cover  REAL,
            fcst_wind_speed   REAL,
            actual_temp       REAL    NOT NULL,
            created_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        )
    """)

    ddl_metadata = text("""
        CREATE TABLE IF NOT EXISTS model_metadata (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            last_trained    TEXT,
            sample_count    INTEGER,
            mae_raw         REAL,
            mae_corrected   REAL,
            provider_score  REAL,
            correction_pct  REAL,
            model_path      TEXT,
            training_status TEXT DEFAULT 'idle'
        )
    """)

    idx_timestamp = text("""
        CREATE INDEX IF NOT EXISTS idx_pairs_timestamp
        ON forecast_observation_pairs(timestamp)
    """)

    idx_provider = text("""
        CREATE INDEX IF NOT EXISTS idx_pairs_provider
        ON forecast_observation_pairs(provider_id)
    """)

    with engine.connect() as conn:
        conn.execute(ddl_pairs)
        conn.execute(ddl_metadata)
        conn.execute(idx_timestamp)
        conn.execute(idx_provider)
        conn.commit()

    logger.debug("Correction tables verified/created")


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def insert_pair(
    *,
    timestamp: int,
    provider_id: str,
    month: int,
    hour: int,
    day_of_year: int,
    fcst_temp: float,
    fcst_wind_dir: float | None,
    fcst_humidity: float | None,
    fcst_cloud_cover: float | None,
    fcst_wind_speed: float | None,
    actual_temp: float,
) -> None:
    """Insert a forecast-observation pair.

    Uses INSERT OR IGNORE so duplicate timestamps (UNIQUE constraint) are
    silently skipped — the collector never errors on a re-seen pair.

    All parameters are keyword-only to prevent accidental positional swaps
    between the many numeric columns.
    """
    sql = text("""
        INSERT OR IGNORE INTO forecast_observation_pairs
            (timestamp, provider_id, month, hour, day_of_year,
             fcst_temp, fcst_wind_dir, fcst_humidity, fcst_cloud_cover,
             fcst_wind_speed, actual_temp)
        VALUES
            (:timestamp, :provider_id, :month, :hour, :day_of_year,
             :fcst_temp, :fcst_wind_dir, :fcst_humidity, :fcst_cloud_cover,
             :fcst_wind_speed, :actual_temp)
    """)
    with get_engine().connect() as conn:
        conn.execute(sql, {
            "timestamp": timestamp,
            "provider_id": provider_id,
            "month": month,
            "hour": hour,
            "day_of_year": day_of_year,
            "fcst_temp": fcst_temp,
            "fcst_wind_dir": fcst_wind_dir,
            "fcst_humidity": fcst_humidity,
            "fcst_cloud_cover": fcst_cloud_cover,
            "fcst_wind_speed": fcst_wind_speed,
            "actual_temp": actual_temp,
        })
        conn.commit()


def get_training_data(cutoff_epoch: int) -> list[dict]:
    """Return all pairs with timestamp < cutoff_epoch (training set).

    The training set is pairs older than 30 days; the caller computes the
    cutoff epoch from the current time minus 30 days.

    Returns:
        List of row dicts with all forecast_observation_pairs columns.
    """
    sql = text("""
        SELECT id, timestamp, provider_id, month, hour, day_of_year,
               fcst_temp, fcst_wind_dir, fcst_humidity, fcst_cloud_cover,
               fcst_wind_speed, actual_temp, created_at
        FROM forecast_observation_pairs
        WHERE timestamp < :cutoff
        ORDER BY timestamp ASC
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql, {"cutoff": cutoff_epoch})
        return [dict(row._mapping) for row in result]


def get_validation_data(cutoff_epoch: int) -> list[dict]:
    """Return all pairs with timestamp >= cutoff_epoch (validation set).

    The validation set is the last 30 days of pairs; the caller computes the
    cutoff epoch from the current time minus 30 days.

    Returns:
        List of row dicts with all forecast_observation_pairs columns.
    """
    sql = text("""
        SELECT id, timestamp, provider_id, month, hour, day_of_year,
               fcst_temp, fcst_wind_dir, fcst_humidity, fcst_cloud_cover,
               fcst_wind_speed, actual_temp, created_at
        FROM forecast_observation_pairs
        WHERE timestamp >= :cutoff
        ORDER BY timestamp ASC
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql, {"cutoff": cutoff_epoch})
        return [dict(row._mapping) for row in result]


def purge_old_records(before_epoch: int) -> int:
    """Delete all pairs with timestamp < before_epoch.

    Called at the start of each training run to enforce the retention_years
    rolling window.

    Args:
        before_epoch: Unix timestamp; records strictly older than this are deleted.

    Returns:
        Number of rows deleted.
    """
    sql = text("""
        DELETE FROM forecast_observation_pairs
        WHERE timestamp < :before
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql, {"before": before_epoch})
        conn.commit()
        deleted: int = result.rowcount
    logger.info("Purged %d correction pairs older than epoch %d", deleted, before_epoch)
    return deleted


def get_pair_count() -> int:
    """Return the total number of stored forecast-observation pairs."""
    sql = text("SELECT COUNT(*) FROM forecast_observation_pairs")
    with get_engine().connect() as conn:
        result = conn.execute(sql)
        row = result.fetchone()
        return int(row[0]) if row else 0


def get_date_range() -> tuple[int | None, int | None]:
    """Return (MIN timestamp, MAX timestamp) of all stored pairs.

    Returns:
        Tuple of (min_epoch, max_epoch).  Both are None when the table is empty.
    """
    sql = text("""
        SELECT MIN(timestamp), MAX(timestamp)
        FROM forecast_observation_pairs
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql)
        row = result.fetchone()
        if row is None:
            return (None, None)
        min_ts = int(row[0]) if row[0] is not None else None
        max_ts = int(row[1]) if row[1] is not None else None
        return (min_ts, max_ts)


def save_model_metadata(
    *,
    last_trained: str | None,
    sample_count: int | None,
    mae_raw: float | None,
    mae_corrected: float | None,
    provider_score: float | None,
    correction_pct: float | None,
    model_path: str | None,
    training_status: str,
) -> None:
    """Upsert the singleton model_metadata row (id=1).

    Uses INSERT OR REPLACE so the first call inserts and subsequent calls
    update.  The CHECK (id = 1) constraint on the table ensures only one row
    can ever exist.

    All parameters are keyword-only to prevent positional swaps.
    """
    sql = text("""
        INSERT OR REPLACE INTO model_metadata
            (id, last_trained, sample_count, mae_raw, mae_corrected,
             provider_score, correction_pct, model_path, training_status)
        VALUES
            (1, :last_trained, :sample_count, :mae_raw, :mae_corrected,
             :provider_score, :correction_pct, :model_path, :training_status)
    """)
    with get_engine().connect() as conn:
        conn.execute(sql, {
            "last_trained": last_trained,
            "sample_count": sample_count,
            "mae_raw": mae_raw,
            "mae_corrected": mae_corrected,
            "provider_score": provider_score,
            "correction_pct": correction_pct,
            "model_path": model_path,
            "training_status": training_status,
        })
        conn.commit()


def get_model_metadata() -> dict | None:
    """Return the singleton model_metadata row as a dict, or None if absent."""
    sql = text("""
        SELECT id, last_trained, sample_count, mae_raw, mae_corrected,
               provider_score, correction_pct, model_path, training_status
        FROM model_metadata
        WHERE id = 1
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql)
        row = result.fetchone()
        if row is None:
            return None
        return dict(row._mapping)
