"""Startup write-probe per ADR-012 / security-baseline §3.3.

Purpose:
    Verify that the DB user connected to the archive table has NO write
    privileges. If a write succeeds, the service refuses to start (log
    critical + sys.exit(1)). This is defense-in-depth: the DB-level GRANT
    is the primary control; this probe is the second layer.

Approach — INSERT inside an explicit ROLLBACK transaction:
    We attempt an INSERT into the archive table inside a transaction, then
    unconditionally call trans.rollback() in a finally block. This approach:

    1. Works for both SQLite and MariaDB — no dialect-specific introspection
       of INFORMATION_SCHEMA.PRIVILEGES (which varies across MariaDB versions
       and requires additional grants to query reliably).
    2. Does NOT leave a sentinel row behind — the transaction is always
       rolled back, whether the INSERT succeeded or failed.
    3. Detects write access accurately — see exception taxonomy below.

Exception taxonomy (critical for correctness on real production schemas):
    The production weewx archive table has usUnits and interval as NOT NULL
    columns with no default. Our probe INSERT supplies only dateTime, so a
    writable user will hit an IntegrityError (NOT NULL constraint violation)
    rather than a clean success. This is NOT the same as "permission denied."

    Three distinct outcomes are possible when the probe executes:

    a) INSERT succeeds outright (write_succeeded=True):
       No exception. User has INSERT privilege AND the schema accepted the row.
       → user is writable → log critical + sys.exit(1).

    b) INSERT raises IntegrityError (constraint violation):
       The DB engine accepted the statement and began evaluating it; the schema
       rejected the row content (NOT NULL, UNIQUE, FK, CHECK constraint).
       This means the user DID pass the privilege check — write access exists.
       → user is writable → log critical + sys.exit(1).

    c) INSERT raises OperationalError or ProgrammingError (privilege denied):
       MariaDB error 1142 ("INSERT command denied to user") surfaces as
       OperationalError. SQLite ?mode=ro surfaces as OperationalError with
       "attempt to write a readonly database". ProgrammingError covers other
       dialect-specific pre-execution rejections.
       → user is read-only → probe passes.

    Anything else (unexpected exception from connect() or execute()) is NOT
    silently swallowed. It propagates out of the probe so the caller can
    decide whether to abort (treated the same as DB unreachable).

Alternative considered: CREATE TABLE clearskies_probe_sentinel + DROP.
    Rejected: CREATE requires DDL privilege (GRANT CREATE), not DML INSERT.
    A user with SELECT+INSERT but no CREATE passes the CREATE test and still
    has dangerous write access. INSERT is the right sentinel operation.

SQLite special case (ADR-012):
    For SQLite, the URI must carry ?mode=ro. We check this before attempting
    the connection. The actual INSERT attempt also runs — SQLite's mode=ro
    returns an OperationalError before any data is written.
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

# Parameterized INSERT that supplies only the dateTime column.  On a real
# production weewx archive (which has usUnits + interval as NOT NULL), this
# will raise IntegrityError on a writable user — which is still evidence of
# write access and triggers the exit path.  All SQL is parameterized per
# coding.md §1 / ADR-012 (no f-string SQL, no string interpolation).
_PROBE_INSERT_SQL = text(
    "INSERT INTO archive (dateTime) VALUES (:ts)"
)
# Far-future sentinel value.  If a rollback somehow fails, this timestamp
# (9999-12-31 00:00:00 UTC) is identifiable and purgeable.
_PROBE_DATETIME_VALUE = 253402300800


def _critical_and_exit(reason: str) -> None:
    """Log a critical message and call sys.exit(1).

    Separated so tests can confirm both the log and the exit happen.
    """
    logger.critical(
        "FATAL: The database user has write access to the archive table. "
        "clearskies-api must connect with a SELECT-only user. "
        "Evidence of write access: %s. "
        "Action required: "
        "(1) Create a read-only database user per INSTALL.md, section "
        "'Database — read-only user setup'. "
        "(2) Set WEEWX_CLEARSKIES_DB_USER and WEEWX_CLEARSKIES_DB_PASSWORD "
        "in /etc/weewx-clearskies/secrets.env (mode 0600) to the new user. "
        "(3) Restart the service. "
        "Service will not start until a read-only user is configured.",
        reason,
    )
    sys.exit(1)


def run_write_probe(engine: Engine) -> None:
    """Attempt a write against the archive table and abort startup if it succeeds.

    Connects to the database, opens an explicit transaction, attempts an INSERT
    into the archive table, then ALWAYS rolls back via trans.rollback() in a
    finally block. Calls sys.exit(1) if write access is detected.

    For SQLite: also checks that the connection URL contains mode=ro.

    Args:
        engine: The SQLAlchemy Engine to probe.

    Side-effects:
        Calls sys.exit(1) if write access is detected. Logs critical message
        with explicit operator instructions before exiting.

    Raises:
        RuntimeError: The DB is unreachable (OperationalError on connect).
    """
    # --- SQLite URI check ---------------------------------------------------
    # For SQLite, ADR-012 requires mode=ro in the URL. The engine factory
    # enforces this at build time, but we verify here as defense-in-depth.
    db_url = str(engine.url)
    if engine.dialect.name == "sqlite":
        if "mode=ro" not in db_url:
            logger.critical(
                "FATAL: SQLite database URL does not contain '?mode=ro'. "
                "The engine was built without the read-only URI parameter "
                "required by ADR-012. "
                "Edit [database] path in api.conf to point at the .sdb file; "
                "the engine factory adds mode=ro automatically. "
                "See INSTALL.md for setup instructions. "
                "Service will not start."
            )
            sys.exit(1)

    # --- Write attempt ------------------------------------------------------
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(_PROBE_INSERT_SQL, {"ts": _PROBE_DATETIME_VALUE})
                # INSERT completed with no exception — user has INSERT privilege
                # and the row was tentatively accepted by the schema.
                _critical_and_exit("INSERT succeeded outright (no exception)")

            except IntegrityError:
                # Constraint violation (NOT NULL, UNIQUE, etc.).  The DB engine
                # accepted the statement and started evaluating it — privilege
                # check passed.  The user CAN write; the schema rejected the
                # row content, not the permission.
                _critical_and_exit(
                    "IntegrityError on INSERT — constraint violation means the DB "
                    "engine accepted the statement (privilege check passed); user "
                    "has write access"
                )

            except (OperationalError, ProgrammingError) as exc:
                # Permission denied or read-only-DB error.  These are the
                # expected exceptions for a correctly-configured read-only user:
                #   MariaDB: error 1142 "INSERT command denied to user"
                #             surfaces as OperationalError.
                #   SQLite:  "attempt to write a readonly database"
                #             surfaces as OperationalError.
                # ProgrammingError covers other pre-execution dialect rejections.
                detail = str(exc).split("\n")[0]
                logger.info(
                    "Write-probe passed: INSERT was denied — user has no write privilege.",
                    extra={"probe_denial_detail": detail},
                )

            finally:
                # Unconditional rollback — never commit.  sys.exit(1) in the
                # branches above still executes this finally before exiting,
                # cleaning up the (possibly committed-to-the-statement-level)
                # transaction.
                try:
                    trans.rollback()
                except (OperationalError, DatabaseError):
                    # Rollback can fail on a dead connection — acceptable here
                    # since we're already in the exit path.
                    pass

    except (OperationalError, DatabaseError) as exc:
        # engine.connect() itself failed — DB is unreachable.  This is a
        # readiness issue, not a permissions issue.  Raise so the caller can
        # surface it as a startup error distinct from write-access detection.
        raise RuntimeError(
            f"Database unreachable during write-probe: {exc}"
        ) from exc
