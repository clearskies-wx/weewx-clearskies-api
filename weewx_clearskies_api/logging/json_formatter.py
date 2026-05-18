"""JSON log formatter per ADR-029.

Produces one JSON object per line to stdout. Required fields per record:
  timestamp  — ISO-8601 UTC with Z suffix
  level      — DEBUG / INFO / WARNING / ERROR / CRITICAL
  logger     — Python logger name
  message    — human-readable
  request_id — present when in HTTP-request context; omitted otherwise

Additional structured fields (provider_id, endpoint, duration_ms, etc.)
are passed as keyword arguments via logger.info("msg", extra={"field": value})
and appear as top-level JSON keys.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any


class JsonFormatter(logging.Formatter):
    """Formats a LogRecord as a single-line JSON string to stdout."""

    def format(self, record: logging.LogRecord) -> str:
        # Base required fields (ADR-029).
        doc: dict[str, Any] = {
            "timestamp": self._utc_now(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # request_id is present when the logging context variable has been set
        # by RequestIdMiddleware. It is stored in the LogRecord via the
        # RequestIdFilter (see setup_logging).
        request_id = getattr(record, "request_id", None)
        if request_id:
            doc["request_id"] = request_id

        # Carry through any extra structured fields the caller attached.
        skip = {
            "args", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "message",
            "module", "msecs", "msg", "name", "pathname", "process",
            "processName", "relativeCreated", "stack_info", "thread",
            "threadName", "request_id",
        }
        for k, v in record.__dict__.items():
            if k not in skip:
                doc[k] = v

        # Exception info — stack trace goes to the operator log, never to the
        # user-facing error response (coding.md §3 "hide internals from users").
        if record.exc_info:
            doc["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            doc["exc_info"] = record.exc_text

        if record.stack_info:
            doc["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(doc, default=str)

    @staticmethod
    def _utc_now() -> str:
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
