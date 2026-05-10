"""Redaction filter per ADR-029 and security baseline §3.4.

Strips the following from every log record before it is formatted:
  - The value of Authorization headers
  - The value of X-Clearskies-Proxy-Auth headers
  - Query-string values for 'appid', 'apiKey', 'client_id', and 'client_secret'
  - SQL parameter values (logged query templates only — never the bound values)

This is defense-in-depth; the primary control is "don't log secrets in the
first place." But log aggregation pipelines are unforgiving, so we catch
accidental leaks here before they leave the process.

The filter modifies the record's message in-place. Because log records can be
shared across handlers we operate on a copy of the message string, not the
record object, and reassign record.msg.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

# Context variable shared between RequestIdMiddleware and the logging
# infrastructure. Middleware sets this; RequestIdFilter reads it per record.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

# Match Authorization header value (common formats: Bearer <token>, Basic <b64>).
# Allow internal whitespace so two-word "scheme credential" forms like
# "Bearer xyz" redact fully. Stop at newlines or structural JSON-ish delimiters
# so we don't gobble post-header content in mixed-form log lines.
_AUTH_HEADER_RE = re.compile(
    r"(Authorization\s*[:=]\s*)[^\r\n,\]}\"']+",
    re.IGNORECASE,
)

# Match X-Clearskies-Proxy-Auth header value
_PROXY_AUTH_RE = re.compile(
    r"(X-Clearskies-Proxy-Auth\s*[:=]\s*)[^\s,\]}\n\"']+",
    re.IGNORECASE,
)

# Match appid= query parameter value
_APPID_RE = re.compile(
    r"((?:^|[?&])appid=)[^&\s\n\"']+",
    re.IGNORECASE,
)

# Match client_id= query parameter value (F13 from 3b-1, fires on first keyed provider)
# Pattern mirrors _CLIENT_SECRET_RE; both are Aeris query-param credentials.
# Fires this round (3b-4) because Aeris is the first keyed provider on this project.
_CLIENT_ID_RE = re.compile(
    r"((?:^|[?&])client_id=)[^&\s\n\"']+",
    re.IGNORECASE,
)

# Match client_secret= query parameter value
_CLIENT_SECRET_RE = re.compile(
    r"((?:^|[?&])client_secret=)[^&\s\n\"']+",
    re.IGNORECASE,
)

# Match apiKey= query parameter value (Wunderground PWS API)
# Pattern mirrors _APPID_RE / _CLIENT_ID_RE shape; both are query-param
# credentials.  Fires this round (3b-6) because Wunderground is the third
# keyed provider on this project.  re.IGNORECASE covers apikey= and APIKEY=
# variants that may appear in URL-encoded log lines.
_APIKEY_RE = re.compile(
    r"((?:^|[?&])apiKey=)[^&\s\n\"']+",
    re.IGNORECASE,
)

# Match SQL quoted literals when someone accidentally logs a bound query.
# Catches patterns like: WHERE name = 'alice', VALUES (42, 'bob')
#
# Approach (a) — broad regex: redact ALL single- or double-quoted string
# literals anywhere in a log message. Trade-off: may over-redact legitimate
# quoted strings in error messages (e.g. "User 'admin' not found" → "User
# [REDACTED] not found"). Chosen because over-redaction is preferable to
# under-redaction. A future operator reading truncated error messages is
# better served than a log-aggregation pipeline that leaks SQL literals.
# If false-positive redaction causes observability pain, switch to approach
# (b) — scope to SQL context keywords (VALUES/WHERE/SET/IN/LIKE) — at the
# cost of a more fragile pattern that misses novel query forms.
_SQL_LITERAL_RE = re.compile(
    r"(?<![:%])(?:'[^']*'|\"[^\"]*\")",
)

_REDACTED = "[REDACTED]"

# Each entry is (pattern, replacement). The four header/query-param patterns
# preserve a leading prefix via group 1 ("Authorization: " stays, the value is
# replaced); the SQL pattern has no prefix to keep, so the whole match is
# replaced.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_AUTH_HEADER_RE, r"\g<1>" + _REDACTED),
    (_PROXY_AUTH_RE, r"\g<1>" + _REDACTED),
    (_APPID_RE, r"\g<1>" + _REDACTED),
    (_CLIENT_ID_RE, r"\g<1>" + _REDACTED),
    (_CLIENT_SECRET_RE, r"\g<1>" + _REDACTED),
    (_APIKEY_RE, r"\g<1>" + _REDACTED),
    (_SQL_LITERAL_RE, _REDACTED),
]


def _redact(text: str) -> str:
    """Apply all redaction patterns to a string and return the cleaned result."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Logging filter that redacts secrets from record.msg and record.args.

    Install at the root logger via setup_logging() so every handler benefits.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the message template.
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)

        # Redact positional format args (usually a tuple).
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(str(a)) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (_redact(str(v)) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }

        # Always allow the record through — we're filtering content, not records.
        return True


class RequestIdFilter(logging.Filter):
    """Injects the current request_id context variable into every log record.

    The request_id is set per-request by RequestIdMiddleware and stored in
    the request_id_var ContextVar. This filter makes it available as
    record.request_id so JsonFormatter can include it in the output.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")  # type: ignore[attr-defined]
        return True
