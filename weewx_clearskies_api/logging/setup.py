"""Logging setup entry point.

Called once at startup (before any other logger activity) to configure the
root logger for JSON output per ADR-029. Also reconfigures uvicorn's access
log to use the same formatter so all log lines are parseable JSON.
"""

from __future__ import annotations

import logging
import sys

from weewx_clearskies_api.logging.json_formatter import JsonFormatter
from weewx_clearskies_api.logging.redaction_filter import RedactionFilter, RequestIdFilter


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger: JSON to stdout + redaction + request-id injection.

    Args:
        level: Log level string (DEBUG / INFO / WARNING / ERROR / CRITICAL).
               Can be overridden by CLEARSKIES_LOG_LEVEL env var — the caller
               (Settings) already resolves the env override before calling here.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any handlers already attached (e.g. uvicorn's defaults) so we
    # don't get duplicate lines.
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Install filters globally on the root logger so every child logger
    # inherits them automatically.
    root.addFilter(RedactionFilter())
    root.addFilter(RequestIdFilter())

    # Reconfigure uvicorn loggers to use the same handler.
    # uvicorn.error and uvicorn.access both exist when uvicorn is running.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True  # Let records flow to the root handler above.

    logging.getLogger(__name__).debug("Logging configured at level %s", level)
