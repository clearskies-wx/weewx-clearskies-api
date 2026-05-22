"""Health check endpoints per ADR-030.

/health/live  — liveness: always 200 if the process is running.
/health/ready — readiness: 200 (ok or degraded) or 503 (unhealthy).

These endpoints are mounted on a SEPARATE FastAPI app that binds to a
separate loopback port (default 8081). They are NOT part of the public API.

Why a separate app on a separate port (rather than routes on the main app)?

  1. Security (ADR-030): health probes are unauthenticated. Putting them
     on the public port would either expose them to the internet (bad) or
     require special-casing in the ProxyAuthMiddleware (fragile). Separate
     port bound to loopback means the OS-level network boundary IS the
     access control, without any middleware complexity.
  2. Operator ergonomics: Docker HEALTHCHECK and k8s probes can target
     8081 directly without traversing the proxy.
  3. Log hygiene (ADR-029): probe traffic doesn't pollute the main access log.

Readiness probe registration:
  Probes are callables registered via register_readiness_probe().
  Each probe returns a ProbeResult. DB probe is registered in Task 2.

  Example:
      from weewx_clearskies_api.health import register_readiness_probe, ProbeResult
      def db_probe() -> ProbeResult:
          try:
              db.execute("SELECT 1")
              return ProbeResult(name="database", status="ok")
          except Exception as exc:
              return ProbeResult(name="database", status="warning",
                                 messages=[str(exc)])
      register_readiness_probe(db_probe)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Probe result types
# ---------------------------------------------------------------------------

StatusLiteral = Literal["ok", "warning", "unhealthy"]


@dataclass
class ProbeResult:
    """Result from a single readiness probe."""

    name: str
    status: StatusLiteral
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Probe registry
# ---------------------------------------------------------------------------

_readiness_probes: list[Callable[[], ProbeResult]] = []


def register_readiness_probe(probe: Callable[[], ProbeResult]) -> None:
    """Register a readiness probe.

    Probes are called synchronously in /health/ready. Keep them fast
    (connection ping only, not a full query). Phase 2 Task 2 registers
    the DB probe here.
    """
    _readiness_probes.append(probe)


def _run_readiness_probes() -> tuple[StatusLiteral, dict[str, object]]:
    """Run all registered probes and aggregate their results.

    Aggregation rules:
      - Any 'unhealthy' result → overall 'unhealthy' (→ 503).
      - Any 'warning' result   → overall 'degraded'  (→ 200).
      - All 'ok'               → overall 'ok'         (→ 200).
    """
    checks: dict[str, object] = {}
    overall: StatusLiteral = "ok"

    for probe in _readiness_probes:
        try:
            result = probe()
        except Exception as exc:
            logger.error("Readiness probe raised an exception: %s", exc)
            result = ProbeResult(
                name="unknown",
                status="unhealthy",
                messages=[f"Probe raised {type(exc).__name__}"],
            )

        checks[result.name] = {
            "status": result.status,
            **({"messages": result.messages} if result.messages else {}),
        }

        if result.status == "unhealthy":
            overall = "unhealthy"
        elif result.status == "warning" and overall != "unhealthy":
            overall = "warning"

    # Normalise "warning" aggregate to "degraded" per ADR-030 response body shape.
    display_status: str = "degraded" if overall == "warning" else overall
    return overall, {"status": display_status, "checks": checks}


# ---------------------------------------------------------------------------
# Health app factory
# ---------------------------------------------------------------------------


def create_health_app(*, metrics_enabled: bool = False) -> FastAPI:
    """Create the health FastAPI app (mounts on a separate loopback port).

    This app has no middleware. It is intentionally unauthenticated per
    ADR-030 — access control is the loopback bind.

    Args:
        metrics_enabled: When True, register GET /metrics (ADR-031).
            Reads from settings.health.metrics_enabled, which is set by
            CLEARSKIES_METRICS_ENABLED env var (or metrics_enabled = true in
            the [health] section of api.conf).
    """
    health_app = FastAPI(
        title="weewx-clearskies-api health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @health_app.get("/health/live")
    async def live() -> JSONResponse:
        """Liveness probe. Returns 200 if the process is responding."""
        return JSONResponse({"status": "ok"})

    @health_app.get("/health/ready")
    async def ready() -> JSONResponse:
        """Readiness probe. Returns 200 (ok/degraded) or 503 (unhealthy)."""
        overall_internal, body = _run_readiness_probes()
        http_status = 503 if overall_internal == "unhealthy" else 200
        return JSONResponse(body, status_code=http_status)

    if metrics_enabled:
        # ADR-031: Prometheus /metrics on the health port.
        # Same loopback-only, unauthenticated posture as /health/live and /health/ready.
        from weewx_clearskies_api.metrics import metrics_response  # deferred to avoid cycle

        @health_app.get("/metrics")
        async def metrics() -> Response:
            """Prometheus metrics in text exposition format (ADR-031)."""
            body, content_type = metrics_response()
            return Response(content=body, media_type=content_type)

    return health_app
