"""GET /alerts — active severe-weather alerts (ADR-016).

Behavior decision tree per brief §per-endpoint spec:

  1. No alerts provider in capability registry  → 200, alerts=[], source="none"
  2. Provider configured, NWS returns 200 + empty features → 200, alerts=[], source="nws"
  3. Provider configured, NWS returns 200 + features → normalize, filter, return 200
  4. Network failure / 5xx after retries → 502 ProviderProblem (TransientNetworkError)
  5. NWS returns 429 → 503 ProviderProblem (QuotaExhausted) + Retry-After
  6. NWS returns 401/403 → 502 ProviderProblem (KeyInvalid) [exotic; NWS is keyless]
  7. Pydantic validation failure on wire model → 502 ProviderProblem (ProviderProtocolError)

Severity filter (ADR-017 §Cache key — filter applied AFTER cache lookup):
  Cache stores the full canonical list (all severities), keyed by station lat/lon.
  Severity filter is applied by the endpoint handler — one cache entry per station,
  not one per filter value.  This avoids N × quota burn for N severity tiers.

No DB hit.  Alerts come from the provider, not weewx archive.

Operator lat/lon: from get_station_info() (services/station.py) per ADR-011
  (single-station scope).  No ?station= param.

Pydantic + Depends pattern (coding.md §1, security-baseline §3.5):
  Unknown query keys rejected with 422/400 via extra="forbid" + Depends wrapper.

Provider discovery: endpoint reads the capability registry at request time.
  wire_providers() at startup registers the configured provider's CAPABILITY;
  this endpoint checks the registry for an "alerts" domain entry.  Tests that
  need the NWS path call wire_providers([nws.CAPABILITY]) directly.

NWS user-agent contact: wired separately via wire_nws_user_agent_contact() in
  __main__.py after settings load.  Tests without a full startup use None (no
  contact), which triggers a one-time WARNING log but works correctly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from weewx_clearskies_api.models.params import SEVERITY_ORDER, AlertsQueryParams
from weewx_clearskies_api.models.responses import (
    AlertList,
    AlertListResponse,
    AlertRecord,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.station import get_station_info

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level NWS UA contact wiring (populated at startup)
# ---------------------------------------------------------------------------

_nws_user_agent_contact: str | None = None


def wire_nws_user_agent_contact(contact: str | None) -> None:
    """Store the NWS User-Agent contact string for use by the endpoint.

    Called from __main__.py after settings load.
    Tests that don't care about the UA leave this as None.
    """
    global _nws_user_agent_contact  # noqa: PLW0603
    _nws_user_agent_contact = contact


def wire_alerts_settings(settings: object) -> None:
    """Wire alerts-related settings from the Settings object.

    Convenience wrapper for __main__.py — extracts nws_user_agent_contact
    from settings.alerts and calls wire_nws_user_agent_contact().
    """
    contact = getattr(getattr(settings, "alerts", None), "nws_user_agent_contact", None)
    wire_nws_user_agent_contact(contact)


# ---------------------------------------------------------------------------
# Depends wrapper — Pydantic + Depends pattern (coding.md §1)
# ---------------------------------------------------------------------------


def _get_alerts_params(request: Request) -> AlertsQueryParams:
    """Extract and validate /alerts query parameters via Pydantic.

    Using Depends(model_validate(dict(request.query_params))) pattern so
    extra="forbid" actually fires for unknown query keys (coding.md §1,
    security-baseline §3.5).  Individual FastAPI Query() declarations
    silently ignore unknown keys — not acceptable.
    """
    try:
        return AlertsQueryParams.model_validate(dict(request.query_params))
    except Exception as exc:
        raise RequestValidationError(exc.errors() if hasattr(exc, "errors") else []) from exc  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Severity filter helper
# ---------------------------------------------------------------------------


def _filter_by_severity(
    records: list[AlertRecord], min_severity: str | None
) -> list[AlertRecord]:
    """Return alerts at or above the minimum severity level.

    advisory → return all (0+)
    watch    → return watch + warning (1+)
    warning  → return warning only (2+)
    None     → return all (no filter)
    """
    if min_severity is None:
        return records
    min_level = SEVERITY_ORDER.get(min_severity, 0)
    return [r for r in records if SEVERITY_ORDER.get(r.severity, 0) >= min_level]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/alerts",
    summary="Active severe-weather alerts",
    tags=["Alerts"],
    response_model=AlertListResponse,
)
def get_alerts(
    params: Annotated[AlertsQueryParams, Depends(_get_alerts_params)],
) -> AlertListResponse:
    """Return active severe-weather alerts from the configured provider.

    Reads the capability registry for the alerts domain at request time.
    Returns AlertList(alerts=[], source="none") when no provider is registered.
    Cache integration and severity filter happen transparently in this handler.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # --- Find the configured alerts provider in the capability registry ---
    provider_registry = get_provider_registry()
    alerts_providers = [p for p in provider_registry if p.domain == "alerts"]

    # --- Decision tree branch 1: no provider configured ---
    if not alerts_providers:
        logger.debug("No alerts provider in registry; returning empty list")
        return AlertListResponse(
            data=AlertList(
                alerts=[],
                retrievedAt=now_str,
                source="none",
            ),
            source="none",
            generatedAt=now_str,
        )

    # Single source per deploy per ADR-016; take the first (and only) entry.
    provider_id = alerts_providers[0].provider_id

    # --- Obtain station lat/lon (ADR-011: single-station, no ?station= param) ---
    try:
        station = get_station_info()
    except RuntimeError:
        # Defense-in-depth: station should always be wired before uvicorn starts.
        # This branch is theoretically unreachable if startup order is correct.
        # Logged as a 503 so orchestrators can detect and retry start.
        logger.error(
            "Station metadata not available at alerts endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(
            status_code=503,
            detail="Service starting",
        )

    # --- Dispatch to provider module ---
    if provider_id == "nws":
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        all_records = nws.fetch(
            lat=station.latitude,
            lon=station.longitude,
            user_agent_contact=_nws_user_agent_contact,
        )
    else:
        # Unknown provider should have been caught at startup by _wire_providers_from_config.
        # If we reach here, it means a bug in the startup sequence — treat as 502.
        logger.error("Unknown alerts provider at request time: %r", provider_id)
        raise HTTPException(status_code=502, detail=f"Unknown alerts provider: {provider_id!r}")

    # --- Apply severity filter AFTER cache lookup (ADR-017) ---
    filtered_records = _filter_by_severity(all_records, params.severity)

    return AlertListResponse(
        data=AlertList(
            alerts=filtered_records,
            retrievedAt=now_str,
            source=provider_id,
        ),
        source=provider_id,
        generatedAt=now_str,
    )
