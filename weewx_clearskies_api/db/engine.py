"""SQLAlchemy 2.x engine factory (ADR-012).

One config knob selects the backend:
  kind = sqlite  → uri mode, mandatory ?mode=ro&uri=true (ADR-012)
  kind = mysql   → MariaDB/MySQL via pymysql driver

Credentials come from env vars only (ADR-027 §3):
  WEEWX_CLEARSKIES_DB_USER
  WEEWX_CLEARSKIES_DB_PASSWORD

Connection pool (ADR-012):
  Default pool_size=5, max_overflow=10. Configurable via [database] section.

IPv4/IPv6 dual-stack (coding.md §1):
  DB host validated with ipaddress.ip_address when it looks like a bare IP.
  Hostname strings pass directly to the driver, which resolves via getaddrinfo.
  We never call gethostbyname.

Why pymysql instead of mysqlclient (MySQL-Connector-Python)?
  pymysql is a pure-Python implementation with no native build step. It works
  out of the box on every platform without a C extension and a system MySQL
  client library. mysqlclient requires a system libmysqlclient; mysql-connector
  ships its own C extension and has had licensing friction on PyPI.  pymysql is
  the standard choice for SQLAlchemy + MariaDB in small-to-medium Python
  services with no special throughput requirements.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool, QueuePool

from weewx_clearskies_api.config.settings import DatabaseSettings

logger = logging.getLogger(__name__)

# Environment-variable names per ADR-027 §3 / etc/api.conf.example.
_ENV_DB_USER = "WEEWX_CLEARSKIES_DB_USER"
_ENV_DB_PASSWORD = "WEEWX_CLEARSKIES_DB_PASSWORD"


def _validate_db_host(host: str) -> None:
    """Validate the DB host; reject obvious typos with actionable error messages.

    Applies ipaddress.ip_address (coding.md §1) only when the value looks like
    an IP literal. Hostname strings are passed through — the driver resolves
    them via getaddrinfo at connect time.

    Heuristics (tightened from the naive "any colon = IPv6"):

    IPv6 literal: two or more colons, OR the substring "::".
        Correct:  "::1", "2001:db8::1", "[::1]"
        Not IPv6: "db.example.com:3306" (one colon = host:port typo)

    IPv4 literal: exactly three dots AND every dot-separated segment is
        all-digits. This avoids misclassifying rare-but-legal all-digit
        hostnames (e.g. "12345") as IPv4 literals.
        Correct:  "192.168.1.5", "127.0.0.1"
        Not IPv4: "hostname.com" (has letters), "12345" (zero dots)

    host:port typo: one colon in a non-bracket-wrapped value. This is the
        common mistake of putting "db.host:3306" in the host field. Raise
        with a message pointing at [database] port.

    Anything else: treat as a hostname, skip ipaddress validation.

    Raises:
        ValueError: Malformed IP literal or host:port typo.
    """
    stripped = host.strip("[]")  # strip brackets in case caller passed [::1]
    colon_count = stripped.count(":")

    # Catch host:port typo before the IPv6 check.
    if colon_count == 1 and not stripped.startswith("["):
        raise ValueError(
            f"[database] host {host!r} looks like a host:port combination. "
            "The port belongs in [database] port, not in host. "
            f"Example: host = {stripped.split(':')[0]}, "
            f"port = {stripped.split(':')[1]}"
        )

    # IPv6 detection: two or more colons, or the "::" shorthand.
    is_ipv6 = colon_count >= 2 or "::" in stripped
    # IPv4 detection: exactly three dots with all-digit segments.
    parts = stripped.split(".")
    is_ipv4 = (
        len(parts) == 4
        and all(p.isdigit() for p in parts)
    )

    if is_ipv6 or is_ipv4:
        # Validate with ipaddress — raises ValueError on malformed literals.
        ipaddress.ip_address(stripped)


def _build_sqlite_url(settings: DatabaseSettings) -> str:
    """Return a read-only SQLite URL per ADR-012.

    URL format: sqlite+pysqlite:///file:///absolute/path?mode=ro&uri=true

    Why this specific format:
      - sqlite+pysqlite:// names the dialect (sqlite) and the DBAPI driver
        (pysqlite, which is the stdlib sqlite3 module).  Naming it explicitly
        avoids ambiguity.
      - file:///path is the SQLite-native URI scheme.  SQLAlchemy passes it
        through to sqlite3.connect() when the URL contains the pysqlite driver
        name.  This 'file:' scheme is required for SQLAlchemy's MetaData.reflect()
        to work correctly with mode=ro — the simpler sqlite:////path format
        works for engine.connect() and queries but fails for reflection because
        SQLAlchemy's SQLite dialect does not pass the mode= parameter through
        the catalog introspection path unless the 'file:' URI scheme is present.
      - mode=ro makes the connection read-only at the SQLite level.
      - uri=true tells the pysqlite driver to treat the connection string as a
        SQLite URI (which activates mode= and other SQLite URI parameters).

    The path is taken verbatim from settings.path; the caller is responsible
    for the file existing. SQLite raises on first connection if the file is
    absent, which the probe layer catches.
    """
    path = settings.path
    return f"sqlite+pysqlite:///file:///{path}?mode=ro&uri=true"


def _build_mysql_url(settings: DatabaseSettings) -> str:
    """Return a pymysql connection URL from settings + env-var credentials.

    Host is validated with ipaddress.ip_address when it looks like a bare IP
    (coding.md §1). Hostname strings pass through to the driver, which uses
    getaddrinfo internally.

    IPv6 literal hosts are wrapped in brackets in the URL per RFC 3986
    (urllib.parse does this; we do it manually here since we build the URL
    ourselves to keep the password out of logs).

    Raises:
        ValueError: Missing credentials or invalid host.
    """
    user = os.environ.get(_ENV_DB_USER, "").strip()
    password = os.environ.get(_ENV_DB_PASSWORD, "").strip()
    if not user:
        raise ValueError(
            f"{_ENV_DB_USER} is not set. "
            "Create a read-only DB user and export its credentials before starting: "
            "MariaDB — GRANT SELECT ON <database>.* TO 'clearskies_ro'@'localhost'; "
            f"then set {_ENV_DB_USER}=clearskies_ro in "
            "/etc/weewx-clearskies/secrets.env (mode 0600). "
            "See etc/api.conf.example for the full config layout."
        )
    if not password:
        raise ValueError(
            f"{_ENV_DB_PASSWORD} is not set. "
            f"Set {_ENV_DB_PASSWORD}=<password> in "
            "/etc/weewx-clearskies/secrets.env (mode 0600) alongside "
            f"{_ENV_DB_USER}. See etc/api.conf.example for the config layout."
        )

    host = settings.host
    _validate_db_host(host)

    # Wrap IPv6 literal in brackets for the URL (RFC 3986 §3.2.2).
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
        if addr.version == 6:
            host_in_url = f"[{addr.compressed}]"
        else:
            host_in_url = addr.compressed
    except ValueError:
        # Hostname string — pass through as-is.
        host_in_url = host

    # URL-encode user/password; the password in particular may contain
    # special characters.  Never log the password.
    encoded_user = quote_plus(user)
    encoded_password = quote_plus(password)
    db_name = settings.name

    return (
        f"mysql+pymysql://{encoded_user}:{encoded_password}"
        f"@{host_in_url}:{settings.port}/{db_name}"
        "?charset=utf8mb4"
    )


def build_engine(settings: DatabaseSettings) -> Engine:
    """Build and return a SQLAlchemy 2.x Engine per ADR-012.

    Args:
        settings: DatabaseSettings from the parsed config file.

    Returns:
        Configured Engine. The engine is not connected on return; the first
        actual query triggers connection checkout from the pool.

    Raises:
        ValueError: Invalid settings or missing credentials.
        sqlalchemy.exc.OperationalError: On first connection attempt if the
            DB is unreachable (propagated by the caller — probe layer).
    """
    kind = settings.kind.lower()

    if kind == "sqlite":
        url = _build_sqlite_url(settings)
        # NullPool for SQLite: file-locking semantics make persistent pools
        # unreliable and SQLite has no real connection overhead.
        engine = create_engine(
            url,
            poolclass=NullPool,
            future=True,  # SQLAlchemy 2.x behaviour
            echo=False,
        )
        logger.info(
            "SQLite engine created (read-only URI mode)",
            extra={"db_path": settings.path},
        )
        return engine

    if kind == "mysql":
        url = _build_mysql_url(settings)
        pool_size = settings.pool_size
        max_overflow = settings.max_overflow
        engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # validate connections on checkout
            future=True,
            echo=False,
        )
        # Log host/db but not credentials.
        logger.info(
            "MySQL/MariaDB engine created",
            extra={
                "db_host": settings.host,
                "db_port": settings.port,
                "db_name": settings.name,
                "pool_size": pool_size,
                "max_overflow": max_overflow,
            },
        )
        return engine

    raise ValueError(
        f"Unsupported database kind: {kind!r}. "
        "Supported values: 'sqlite', 'mysql'. "
        "Check [database] kind in api.conf."
    )
