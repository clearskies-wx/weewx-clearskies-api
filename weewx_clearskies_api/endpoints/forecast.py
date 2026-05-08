"""GET /forecast — forecast bundle (hourly + daily + discussion) (ADR-007).

Behavior decision tree per brief §per-endpoint spec:

  1. No forecast provider in capability registry  → 200, hourly=[], daily=[],
     discussion=null, source="none". No upstream call. No error.
  2. Provider configured, Open-Meteo returns 200 → normalize hourly + daily
     per canonical-data-model §4.1.2 / §4.1.3; slice to hours/days; return 200.
  3. Network failure / 5xx after retries → 502 ProviderProblem (TransientNetworkError)
  4. Open-Meteo returns 429 → 503 ProviderProblem (QuotaExhausted) + Retry-After
  5. Open-Meteo returns 400 with error envelope → 502 ProviderProblem (ProviderProtocolError)
  6. Pydantic validation failure on wire model → 502 ProviderProblem (ProviderProtocolError)

Slice-after-cache pattern (ADR-017 §Cache key):
  Cache stores the FULL bundle (every hourly + daily point Open-Meteo returned).
  Endpoint applies the operator's hours / days slice on the cached canonical bundle.
  One cache entry per (station, target_unit), not one per (hours, days) tuple.

No DB hit. Forecast comes from the provider, not weewx archive.

Operator lat/lon / target_unit / timezone source (ADR-011 single-station):
  Read from services/station.py StationInfo (lat, lon, timezone) and
  services/units.py get_target_unit() (target_unit).  No ?station= param.

Pydantic + Depends pattern (coding.md §1, security-baseline §3.5):
  Unknown query keys rejected with 422/400 via extra="forbid" + Depends wrapper.

Provider discovery: endpoint reads the capability registry at request time.
  _wire_providers_from_config() at startup registers the configured provider's
  CAPABILITY; this endpoint checks the registry for a "forecast" domain entry.
  Tests that need the openmeteo path call wire_providers([openmeteo.CAPABILITY]).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from weewx_clearskies_api.models.params import ForecastQueryParams
from weewx_clearskies_api.models.responses import (
    ForecastBundle,
    ForecastResponse,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.station import get_station_info
from weewx_clearskies_api.services.units import get_target_unit, get_units_block

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Depends wrapper — Pydantic + Depends pattern (coding.md §1)
# ---------------------------------------------------------------------------


def _get_forecast_params(request: Request) -> ForecastQueryParams:
    """Extract and validate /forecast query parameters via Pydantic.

    Using Depends(model_validate(dict(request.query_params))) pattern so
    extra="forbid" actually fires for unknown query keys (coding.md §1,
    security-baseline §3.5).  Individual FastAPI Query() declarations
    silently ignore unknown keys — not acceptable.
    """
    try:
        return ForecastQueryParams.model_validate(dict(request.query_params))
    except pydantic.ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/forecast",
    summary="Forecast bundle (hourly + daily + discussion)",
    tags=["Forecast"],
    response_model=ForecastResponse,
)
def get_forecast(
    params: Annotated[ForecastQueryParams, Depends(_get_forecast_params)],
) -> ForecastResponse:
    """Return forecast bundle from the configured provider.

    Reads the capability registry for the forecast domain at request time.
    Returns ForecastBundle(hourly=[], daily=[], discussion=None, source="none")
    when no provider is registered.
    Cache integration and hours/days slice happen transparently in this handler.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # --- Assemble units block (same wiring as observations + records) ---
    try:
        units = get_units_block()
        target_unit = get_target_unit()
    except RuntimeError:
        # Defense-in-depth: units should always be wired before uvicorn starts.
        # This branch is theoretically unreachable if startup order is correct.
        logger.error(
            "Units block not available at forecast endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(status_code=503, detail="Service starting")

    # --- Find the configured forecast provider in the capability registry ---
    provider_registry = get_provider_registry()
    forecast_providers = [p for p in provider_registry if p.domain == "forecast"]

    # --- Decision tree branch 1: no provider configured ---
    if not forecast_providers:
        logger.debug("No forecast provider in registry; returning empty bundle")
        return ForecastResponse(
            data=ForecastBundle(
                hourly=[],
                daily=[],
                discussion=None,
                source="none",
                generatedAt=now_str,
            ),
            units=units,
            source="none",
            generatedAt=now_str,
        )

    # Single source per deploy per ADR-007; take the first (and only) entry.
    provider_id = forecast_providers[0].provider_id

    # --- Obtain station lat/lon / timezone (ADR-011: single-station, no ?station= param) ---
    try:
        station = get_station_info()
    except RuntimeError:
        # Defense-in-depth: station should always be wired before uvicorn starts.
        logger.error(
            "Station metadata not available at forecast endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(status_code=503, detail="Service starting")

    # --- Dispatch to provider module ---
    if provider_id == "openmeteo":
        from weewx_clearskies_api.providers.forecast import openmeteo  # noqa: PLC0415

        # fetch() returns the FULL canonical bundle (all hours/days from Open-Meteo).
        # Cache stores the full bundle; slice is applied below after cache lookup.
        bundle = openmeteo.fetch(
            lat=station.latitude,
            lon=station.longitude,
            target_unit=target_unit,
            timezone=station.timezone,
        )
    else:
        # Unknown provider should have been caught at startup by _wire_providers_from_config.
        # If we reach here, it means a bug in the startup sequence — treat as 502.
        logger.error("Unknown forecast provider at request time: %r", provider_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unknown forecast provider: {provider_id!r}",
        )

    # --- Apply hours / days slice AFTER cache lookup (ADR-017, slice-after-cache) ---
    # Truncate from the head (first N points); Open-Meteo returns chronological order.
    # If the requested count exceeds what the provider returned, use all available.
    sliced_hourly = bundle.hourly[: params.hours]
    sliced_daily = bundle.daily[: params.days]

    sliced_bundle = ForecastBundle(
        hourly=sliced_hourly,
        daily=sliced_daily,
        discussion=bundle.discussion,
        source=provider_id,
        generatedAt=bundle.generatedAt,
    )

    return ForecastResponse(
        data=sliced_bundle,
        units=units,
        source=provider_id,
        generatedAt=now_str,
    )
