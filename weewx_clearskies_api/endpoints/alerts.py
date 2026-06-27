"""GET /alerts — active severe-weather alerts (ADR-016).

Behavior decision tree per brief §per-endpoint spec:

  1. No alerts provider in capability registry  → 200, alerts=[], source="none"
  2. Provider configured, NWS returns 200 + empty features → 200, alerts=[], source="nws"
  3. Provider configured, NWS returns 200 + features → normalize, filter, return 200
  4. Network failure / 5xx after retries → 502 ProviderProblem (TransientNetworkError)
  5. Provider returns 429 → 503 ProviderProblem (QuotaExhausted) + Retry-After
  6. Provider returns 401/403 → 502 ProviderProblem (KeyInvalid)
  7. Pydantic validation failure on wire model → 502 ProviderProblem (ProviderProtocolError)

minLevel filter (ADR-017 §Cache key, ADR-052 — filter applied AFTER cache lookup):
  Cache stores the full canonical list (all severity levels), keyed by station lat/lon.
  minLevel filter is applied by the endpoint handler — one cache entry per station,
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

Aeris credentials: wired via wire_aeris_credentials() called from
  wire_alerts_settings() at startup.  Tests that don't exercise the Aeris
  path leave these as None; aeris.fetch() will raise KeyInvalid if invoked.

OWM credentials: wired via wire_openweathermap_credentials() called from
  wire_alerts_settings() at startup (3b-8).  Mirror of endpoints/forecast.py
  wire_openweathermap_credentials(). Tests that don't exercise the OWM path
  leave _openweathermap_appid as None; openweathermap.fetch() will raise
  KeyInvalid if invoked.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from weewx_clearskies_api.models.params import AlertsQueryParams
from weewx_clearskies_api.models.responses import (
    AlertList,
    AlertListResponse,
    AlertRecord,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock, get_station_info

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


# ---------------------------------------------------------------------------
# Module-level Aeris credential wiring (populated at startup, 3b-7)
# ---------------------------------------------------------------------------

_aeris_client_id: str | None = None
_aeris_client_secret: str | None = None


def wire_aeris_credentials(client_id: str | None, client_secret: str | None) -> None:
    """Store Aeris credentials for the alerts endpoint dispatch.

    Called from wire_alerts_settings() at startup. Tests that don't exercise
    the Aeris path leave these as None; aeris.fetch() will raise KeyInvalid.
    Mirror of endpoints/forecast.py wire_aeris_credentials().
    """
    global _aeris_client_id, _aeris_client_secret  # noqa: PLW0603
    _aeris_client_id = client_id
    _aeris_client_secret = client_secret


# ---------------------------------------------------------------------------
# Module-level OWM credential wiring (populated at startup, 3b-8)
# ---------------------------------------------------------------------------

_openweathermap_appid: str | None = None


def wire_openweathermap_credentials(appid: str | None) -> None:
    """Store OWM appid for the alerts endpoint dispatch.

    Called from wire_alerts_settings() at startup. Tests that don't exercise
    the OWM path leave this as None; openweathermap.fetch() will raise KeyInvalid.
    Mirror of endpoints/forecast.py wire_openweathermap_credentials() (3b-5).
    """
    global _openweathermap_appid  # noqa: PLW0603
    _openweathermap_appid = appid


def wire_alerts_settings(settings: object) -> None:
    """Wire alerts-related settings from the Settings object.

    Convenience wrapper for __main__.py — extracts nws_user_agent_contact
    from settings.alerts and calls wire_nws_user_agent_contact(); also
    extracts Aeris credentials and calls wire_aeris_credentials(); also
    extracts OWM appid and calls wire_openweathermap_credentials().
    """
    alerts_section = getattr(settings, "alerts", None)
    contact = getattr(alerts_section, "nws_user_agent_contact", None)
    wire_nws_user_agent_contact(contact)

    aeris_id = getattr(alerts_section, "aeris_client_id", None)
    aeris_secret = getattr(alerts_section, "aeris_client_secret", None)
    wire_aeris_credentials(aeris_id, aeris_secret)

    owm_appid = getattr(alerts_section, "openweathermap_appid", None)
    wire_openweathermap_credentials(owm_appid)


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
    except pydantic.ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Severity filter helper
# ---------------------------------------------------------------------------


def _filter_by_min_level(
    records: list[AlertRecord], min_level: int | None
) -> list[AlertRecord]:
    """Return alerts at or above the minimum integer severity level.

    None     → return all records (no filter applied)
    1        → return all alerts (advisory+)
    2        → return watch, warning, and emergency alerts
    3        → return warning and emergency alerts
    4        → return emergency alerts only

    Records whose severityLevel is None (OWM passthrough providers that do
    not populate the ordinal) are included when no filter is specified and
    excluded when a minLevel IS specified, because None cannot be compared
    to an integer threshold.
    """
    if min_level is None:
        return records
    return [
        r for r in records
        if r.severityLevel is not None and r.severityLevel >= min_level
    ]


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
            stationClock=build_station_clock(),
            freshness=build_freshness("alerts"),
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
    elif provider_id == "aeris":
        from weewx_clearskies_api.providers.alerts import aeris  # noqa: PLC0415

        all_records = aeris.fetch(
            lat=station.latitude,
            lon=station.longitude,
            client_id=_aeris_client_id,
            client_secret=_aeris_client_secret,
        )
    elif provider_id == "openweathermap":
        from weewx_clearskies_api.providers.alerts import openweathermap  # noqa: PLC0415

        all_records = openweathermap.fetch(
            lat=station.latitude,
            lon=station.longitude,
            appid=_openweathermap_appid,
        )
    else:
        # Unknown provider should have been caught at startup by _wire_providers_from_config.
        # If we reach here, it means a bug in the startup sequence — treat as 502.
        logger.error("Unknown alerts provider at request time: %r", provider_id)
        raise HTTPException(status_code=502, detail=f"Unknown alerts provider: {provider_id!r}")

    # --- Apply minLevel filter AFTER cache lookup (ADR-017, ADR-052) ---
    filtered_records = _filter_by_min_level(all_records, params.minLevel)

    return AlertListResponse(
        data=AlertList(
            alerts=filtered_records,
            retrievedAt=now_str,
            source=provider_id,
        ),
        source=provider_id,
        generatedAt=now_str,
        stationClock=build_station_clock(),
        freshness=build_freshness("alerts"),
    )
