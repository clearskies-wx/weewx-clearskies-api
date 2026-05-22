"""Prometheus metrics definitions per ADR-031.

Metrics are always created and always incremented (negligible ~50ns overhead)
regardless of CLEARSKIES_METRICS_ENABLED. This is a deliberate interpretation
of ADR-031 §Default ("no metrics endpoint by default"): the endpoint is gated,
but collection is always-on so metrics are immediately available when an
operator enables the flag without restarting.
The /metrics endpoint is only exposed when CLEARSKIES_METRICS_ENABLED=true.

Known gap: provider_calls_total{outcome="cache_hit"} is not emitted here.
Each provider module's fetch() would need to be instrumented individually
(~24 modules) to carry the provider_id and domain labels required by ADR-031.
The aggregate cache_hits_total{backend} counter covers this use-case.
This is documented here rather than in the ADR so the next implementation
round knows where the gap is and how to close it.
"""
from __future__ import annotations

import contextvars
import time as _time
from typing import Any

from prometheus_client import (  # type: ignore[attr-defined]
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

# ContextVar for passing the route template from the HTTP dependency
# to the SQLAlchemy event listeners (DB metrics need the endpoint label).
# Set by the app-level dependency _set_endpoint_context in app.py.
current_endpoint: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_endpoint", default="unknown"
)

# ---------------------------------------------------------------------------
# HTTP metrics (ADR-031 — auto-instrumented via MetricsMiddleware)
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL: Counter = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "endpoint", "status"],
)
HTTP_REQUEST_DURATION: Histogram = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "endpoint"],
)

# ---------------------------------------------------------------------------
# Provider metrics (ADR-031 — instrumented in providers/_common/http.py)
# Note: outcome="cache_hit" is not emitted — see module docstring.
# ---------------------------------------------------------------------------

PROVIDER_CALLS_TOTAL: Counter = Counter(
    "provider_calls_total",
    "Total provider calls.",
    ["provider_id", "domain", "outcome"],
)
PROVIDER_CALL_DURATION: Histogram = Histogram(
    "provider_call_duration_seconds",
    "Provider call duration in seconds (cache misses only).",
    ["provider_id", "domain"],
)

# ---------------------------------------------------------------------------
# Cache metrics (ADR-031 — instrumented in providers/_common/cache.py)
# ---------------------------------------------------------------------------

CACHE_HITS_TOTAL: Counter = Counter(
    "cache_hits_total",
    "Total cache hits.",
    ["backend"],
)
CACHE_MISSES_TOTAL: Counter = Counter(
    "cache_misses_total",
    "Total cache misses.",
    ["backend"],
)

# ---------------------------------------------------------------------------
# DB metrics (ADR-031 — instrumented via SQLAlchemy event listeners)
# ---------------------------------------------------------------------------

DB_QUERY_DURATION: Histogram = Histogram(
    "db_query_duration_seconds",
    "Database query duration in seconds.",
    ["endpoint"],
)


def metrics_response() -> tuple[bytes, str]:
    """Generate Prometheus exposition format response.

    Returns:
        Tuple of (body_bytes, content_type_string).
    """
    return generate_latest(), CONTENT_TYPE_LATEST


def wire_db_metrics(engine: Any) -> None:
    """Attach SQLAlchemy event listeners for DB query duration metrics.

    Uses before_cursor_execute / after_cursor_execute events (ADR-031 §Database).
    The endpoint label is read from the current_endpoint ContextVar, which is
    set by the app-level dependency _set_endpoint_context in app.py before the
    route handler runs.

    Args:
        engine: A SQLAlchemy Engine instance (typed as Any to avoid importing
                SQLAlchemy at module level — this module is imported before
                the engine is built in __main__.py startup order).
    """
    from sqlalchemy import event  # deferred import — SQLAlchemy is a runtime dep

    @event.listens_for(engine, "before_cursor_execute")
    def _before_execute(  # type: ignore[misc]
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        conn.info.setdefault("_query_start_times", []).append(_time.monotonic())

    @event.listens_for(engine, "after_cursor_execute")
    def _after_execute(  # type: ignore[misc]
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        starts = conn.info.get("_query_start_times")
        if starts:
            start = starts.pop()
            duration = _time.monotonic() - start
            endpoint = current_endpoint.get("unknown")
            DB_QUERY_DURATION.labels(endpoint=endpoint).observe(duration)
