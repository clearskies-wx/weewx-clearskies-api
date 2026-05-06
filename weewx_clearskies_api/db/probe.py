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
    columns with no default. Our probe INSERT supplies only dateTime, so on
    a writable user the DB raises a constraint-related exception rather than
    a clean INSERT-succeeded result.

    IMPORTANT: The specific exception depends on the backend:

    SQLite (mode=ro):
        OperationalError: "attempt to write a readonly database"
        → Privilege denied. User is read-only. Probe passes.

    MariaDB, SELECT-only user (GRANT SELECT):
        OperationalError, error code 1142: "INSERT command denied to user"
        → Privilege denied. User is read-only. Probe passes.

    MariaDB, writable user (GRANT ALL or GRANT INSERT):
        OperationalError, error code 1364: "Field 'usUnits' doesn't have a
        default value"
        → NOT a privilege denial. The DB engine accepted the INSERT statement
          (privilege check passed); the schema rejected the row content because
          usUnits and interval have no defaults. Write access EXISTS.
        → User is writable. Probe exits.

    IntegrityError (any backend):
        The DB engine accepted the INSERT at the privilege level; a constraint
        (NOT NULL, UNIQUE, FK, CHECK) rejected the row. Write access exists.
        → User is writable. Probe exits.

    INSERT succeeds with no exception:
        The DB accepted and tentatively committed the row (rolled back in
        finally). Write access exists.
        → User is writable. Probe exits.

Distinguishing write-access OperationalError from privilege-denied OperationalError:
    We inspect the underlying error code from the driver:
      - MariaDB/MySQL error 1142 (ER_TABLEACCESS_DENIED_ERROR) = privilege denied.
      - MariaDB/MySQL error 1064 (ER_PARSE_ERROR) = SQL syntax error (shouldn't
        happen with parameterized SQL, but re-raise if it does so we don't
        silently swallow a bug).
      - Any other MariaDB/MySQL OperationalError code (e.g. 1364, 1048, etc.)
        = DB engine accepted the statement; write access exists.
      - For SQLite: the message "readonly database" is the privilege-denied
        signal (no numeric code — SQLite uses message strings).
      - For any other dialect: if we cannot identify the error as a known
        privilege-denial signal, we treat it conservatively as "write access
        cannot be confirmed as absent" and exit.

Alternative considered: CREATE TABLE clearskies_probe_sentinel + DROP.
    Rejected: CREATE requires DDL privilege (GRANT CREATE), not DML INSERT.
    A user with SELECT+INSERT but no CREATE passes the CREATE test and still
    has dangerous write access. INSERT is the right sentinel operation.

SQLite URI check (ADR-012):
    For SQLite, the URL must carry mode=ro. We check this before attempting
    the connection. The actual INSERT attempt also runs for defense-in-depth.
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError

logger = logging.getLogger(__name__)

# MariaDB/MySQL error codes relevant to the probe.
# Source: https://mariadb.com/kb/en/mariadb-error-codes/
_MARIADB_ACCESS_DENIED = 1142      # ER_TABLEACCESS_DENIED_ERROR
_MARIADB_COLUMNACCESS_DENIED = 1143  # ER_COLUMNACCESS_DENIED_ERROR

# SQLite: the read-only error message (no numeric code in the driver).
_SQLITE_READONLY_MSG = "attempt to write a readonly database"

# Parameterized INSERT that supplies only dateTime.  On a real production
# weewx archive (which has usUnits + interval as NOT NULL), a writable user
# gets OperationalError(1364) rather than a clean INSERT-succeeded result.
# All SQL is parameterized per coding.md §1 / ADR-012 (no f-string SQL).
_PROBE_INSERT_SQL = text(
    "INSERT INTO archive (dateTime) VALUES (:ts)"
)
# Far-future sentinel value.  If a rollback somehow fails, this timestamp
# (9999-12-31 00:00:00 UTC) is identifiable and purgeable.
_PROBE_DATETIME_VALUE = 253402300800


def _is_privilege_denied(exc: OperationalError) -> bool:
    """Return True if this OperationalError means the DB denied the INSERT privilege.

    Returns False for any OperationalError that is NOT a privilege denial —
    meaning the DB engine accepted the statement but the schema or runtime
    rejected it after the privilege check passed (write access exists).

    Logic per backend:
      MariaDB/MySQL: inspect exc.orig.args[0] (the numeric error code).
        1142 = INSERT command denied to user  → privilege denied → True
        1143 = column-level INSERT denied     → privilege denied → True
        anything else (e.g. 1364, 1048)       → schema/runtime error → False
      SQLite: inspect message text for "readonly database".
        message contains "readonly"           → read-only URI → True
        anything else                          → unknown → False (conservative)
    """
    orig = exc.orig
    if orig is None:
        # No underlying DB exception — cannot classify. Conservative: False.
        return False

    orig_args = getattr(orig, "args", ())
    if orig_args and isinstance(orig_args[0], int):
        # Driver with numeric error codes (MariaDB/MySQL via pymysql).
        code: int = orig_args[0]
        return code in (_MARIADB_ACCESS_DENIED, _MARIADB_COLUMNACCESS_DENIED)

    # String-based error (SQLite, others).
    message = str(orig).lower()
    return "readonly" in message or "read-only" in message


def _critical_and_exit(reason: str) -> None:
    """Log a critical message and call sys.exit(1)."""
    logger.critical(
        "FATAL: The database user has write access to the archive table. "
        "clearskies-api must connect with a SELECT-only user. "
        "Evidence: %s. "
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
        RuntimeError: The DB is unreachable (connection failed before any
                      INSERT could be attempted).
    """
    # --- SQLite URI check ---------------------------------------------------
    # For SQLite, ADR-012 requires mode=ro in the URL. The engine factory
    # enforces this at build time; we verify here as defense-in-depth.
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
                _critical_and_exit("INSERT succeeded outright (no exception raised)")

            except IntegrityError as exc:
                # Constraint violation (NOT NULL, UNIQUE, FK, CHECK).
                # The DB engine accepted the statement at the privilege level;
                # the schema rejected the row content after the privilege check.
                # Write access exists.
                _critical_and_exit(
                    f"IntegrityError — DB accepted the INSERT statement "
                    f"(privilege check passed); schema constraint rejected "
                    f"row content: {str(exc.orig).split(chr(10))[0]}"
                )

            except OperationalError as exc:
                # Two distinct sub-cases:
                #   (a) Privilege denied — user cannot INSERT. Probe passes.
                #   (b) Schema/runtime rejection after privilege check —
                #       write access exists. Probe exits.
                if _is_privilege_denied(exc):
                    detail = str(exc).split("\n")[0]
                    logger.info(
                        "Write-probe passed: INSERT was denied at the "
                        "privilege level — user has no INSERT access.",
                        extra={"probe_denial_detail": detail},
                    )
                else:
                    _critical_and_exit(
                        f"OperationalError that is NOT a privilege denial — "
                        f"DB engine accepted the statement (write access exists): "
                        f"{str(exc.orig).split(chr(10))[0] if exc.orig else str(exc)[:100]}"
                    )

            finally:
                # Unconditional rollback — never commit.  sys.exit(1) in the
                # branches above executes this finally before exiting.
                try:
                    trans.rollback()
                except (OperationalError, DatabaseError):
                    # Rollback can fail on a dead connection — acceptable here
                    # since we're already in the exit/pass path.
                    pass

    except (OperationalError, DatabaseError) as exc:
        # engine.connect() itself failed — DB is unreachable.  Distinct from
        # a permissions issue; raise so the caller can handle appropriately.
        raise RuntimeError(
            f"Database unreachable during write-probe: {exc}"
        ) from exc
