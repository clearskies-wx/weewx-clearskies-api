"""Observation endpoints: GET /current and GET /archive.

Per ADR-018: URL-path versioned under /api/v1/.
Per ADR-012: read-only, per-request sessions via get_db_session().
Per ADR-019: units block embedded in every response, no server-side conversion.
Per ADR-020: UTC ISO-8601 with Z on the wire.
Per security-baseline §3.5: query params validated via Pydantic with
  ConfigDict(extra="forbid"), enforced through Depends() + model_validate()
  on the raw query-param dict so FastAPI's routing layer does not silently
  discard unknown keys before the model sees them.

weatherText is always null in API responses per ADR-041; the BFF enrichment
pipeline populates it before serving the dashboard.

provider blending (Tasks 2a, A8): when the archive row has cloudcover=null,
snow=null, or snowRate=null, the /current endpoint attempts to fill those
fields from the configured forecast provider's fetch_current_conditions()
call (300 s cache, almost always a hit).  Errors are swallowed — fields
stay null rather than crashing the endpoint.

ruff: noqa: N815  (canonical field names are weewx camelCase per ADR-010)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from weewx_clearskies_api.db.registry import get_registry
from weewx_clearskies_api.db.session import get_db_session
from weewx_clearskies_api.models.params import ArchiveQueryParams
from weewx_clearskies_api.models.responses import (
    ArchiveResponse,
    Observation,
    ObservationResponse,
)
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.providers._common.dispatch import get_provider_module
from weewx_clearskies_api.services.archive import (
    decode_cursor,
    get_archive,
    get_current,
)
from weewx_clearskies_api.services.station import get_station_info
from weewx_clearskies_api.services.units import get_target_unit, get_units_block

# Alias kept for backwards-compatibility with tests that import ArchiveParams
# from this module (test_archive_params.py).  The class is defined in
# models/params.py and re-exported here under the original name.
ArchiveParams = ArchiveQueryParams

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Provider conditions blending helper
#
# When the archive row has cloudcover=null (no cloud sensor), snow=null, or
# snowRate=null (no snow hardware), /current fills those fields from whatever
# forecast provider is configured, using that provider's cached
# fetch_current_conditions() result (300 s TTL, almost always a cache hit).
#
# This is provider-agnostic: the code dispatches to whichever provider is
# registered for the "forecast" domain, never hard-coding a provider name.
#
# Credential state is read from endpoints/forecast.py module-level variables,
# which are populated at startup by wire_forecast_settings().  Importing them
# at call time (inside the function) avoids a circular-import at module load.
#
# All updates are collected into a single dict so one provider call feeds
# cloudcover, snow, and snowRate blending together.
#
# Errors are swallowed: affected fields stay null rather than crashing /current.
# ---------------------------------------------------------------------------


def _fill_cloudcover_from_provider(observation: Observation) -> Observation:
    """Return observation with provider-supplied fields blended in.

    Calls fetch_current_conditions() on the configured forecast provider and
    blends cloudcover, snow, and snowRate into the observation when those
    fields are None (no hardware on the station) and the provider supplies them.

    One provider call per invocation; all updates applied via a single
    model_copy(update=...) at the end so no field is missed.

    On any error (provider unavailable, station not wired, no provider
    configured) returns the observation unchanged.

    Args:
        observation: The archive observation.

    Returns:
        observation unchanged if fill is not possible, or a new Observation
        instance with cloudcover/snow/snowRate set from ProviderConditions.
    """
    provider_id: str = "unknown"
    try:
        provider_registry = get_provider_registry()
        forecast_providers = [p for p in provider_registry if p.domain == "forecast"]
        if not forecast_providers:
            return observation

        provider_id = forecast_providers[0].provider_id

        try:
            station = get_station_info()
        except RuntimeError:
            logger.debug(
                "Station metadata not available; skipping cloudcover fill"
            )
            return observation

        try:
            target_unit = get_target_unit()
        except RuntimeError:
            logger.debug(
                "Target unit not available; skipping cloudcover fill"
            )
            return observation

        try:
            provider_module = get_provider_module(domain="forecast", provider_id=provider_id)
        except KeyError:
            logger.warning(
                "Unknown forecast provider %r; skipping cloudcover fill",
                provider_id,
            )
            return observation

        # Import credential state from endpoints/forecast.py at call time to
        # avoid a circular import at module load.  The wire_forecast_settings()
        # function in that module populates these variables at startup.
        import weewx_clearskies_api.endpoints.forecast as _forecast_ep  # noqa: PLC0415

        if provider_id == "openmeteo":
            provider_conditions = provider_module.fetch_current_conditions(
                lat=station.latitude,
                lon=station.longitude,
                target_unit=target_unit,
                timezone=station.timezone,
            )
        elif provider_id == "nws":
            provider_conditions = provider_module.fetch_current_conditions(
                lat=station.latitude,
                lon=station.longitude,
                target_unit=target_unit,
                user_agent_contact=_forecast_ep._nws_user_agent_contact,
            )
        elif provider_id == "aeris":
            provider_conditions = provider_module.fetch_current_conditions(
                lat=station.latitude,
                lon=station.longitude,
                target_unit=target_unit,
                client_id=_forecast_ep._aeris_client_id,
                client_secret=_forecast_ep._aeris_client_secret,
            )
        elif provider_id == "openweathermap":
            provider_conditions = provider_module.fetch_current_conditions(
                lat=station.latitude,
                lon=station.longitude,
                target_unit=target_unit,
                appid=_forecast_ep._openweathermap_appid,
            )
        elif provider_id == "wunderground":
            provider_conditions = provider_module.fetch_current_conditions(
                lat=station.latitude,
                lon=station.longitude,
                target_unit=target_unit,
                api_key=_forecast_ep._wunderground_api_key,
                pws_station_id=_forecast_ep._wunderground_pws_station_id,
            )
        else:
            logger.debug(
                "No fetch_current_conditions dispatch for provider %r; "
                "skipping cloudcover fill",
                provider_id,
            )
            return observation

        if provider_conditions is not None:
            updates: dict[str, object] = {}
            if observation.cloudcover is None and provider_conditions.cloudCover is not None:
                updates["cloudcover"] = provider_conditions.cloudCover
            if observation.snow is None and provider_conditions.snow is not None:
                updates["snow"] = provider_conditions.snow
            if observation.snowRate is None and provider_conditions.snowRate is not None:
                updates["snowRate"] = provider_conditions.snowRate
            if updates:
                return observation.model_copy(update=updates)

    except Exception:  # noqa: BLE001
        logger.warning(
            "Provider conditions fill from provider %r failed; leaving fields null",
            provider_id,
            exc_info=True,
        )

    return observation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_z() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Dependency: parse + validate /archive query params
#
# FastAPI's routing layer extracts only the declared param names before
# populating a Depends() model, so extra="forbid" in a plain Depends(Model)
# call never fires for unknown HTTP keys.  The fix is to validate the full
# raw query-param dict via model_validate() inside a dependency function —
# that way Pydantic sees every key the client sent and raises ValidationError
# on any unknown one.  The ValidationError is caught here and surfaced as 400
# problem+json via FastAPI's RequestValidationError handler.
# ---------------------------------------------------------------------------


def _get_archive_params(request: Request) -> ArchiveQueryParams:
    """Dependency: parse and validate /archive query params from the raw dict.

    Calling model_validate(dict(request.query_params)) passes every HTTP key
    the client sent to Pydantic.  ConfigDict(extra="forbid") on ArchiveQueryParams
    then rejects any key not in the model's field set (security-baseline §3.5).
    """
    try:
        return ArchiveQueryParams.model_validate(dict(request.query_params))
    except ValidationError as exc:
        # Re-raise as RequestValidationError so the existing RFC 9457 error
        # handler shapes it as 400/422 problem+json.
        from fastapi.exceptions import RequestValidationError
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# GET /current
# ---------------------------------------------------------------------------


@router.get(
    "/current",
    summary="Most recent observation",
    tags=["Observations"],
    response_model=ObservationResponse,
)
def get_current_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
) -> ObservationResponse:
    """Return the most-recent archive row.

    Returns data: null when the archive is empty — not 404 (brief §1).

    weatherText is always null here; the BFF enrichment pipeline populates
    it per ADR-041 before serving the dashboard.
    """
    registry = get_registry()
    units = get_units_block()

    observation = get_current(db, registry)

    # Blend cloudcover, snow, and snowRate from the forecast provider cache
    # when the archive row lacks the hardware to supply those fields.
    # Almost always a cache hit (<1 ms); errors are swallowed so /current
    # never crashes due to a provider outage.
    if observation is not None and (
        observation.cloudcover is None
        or observation.snow is None
        or observation.snowRate is None
    ):
        observation = _fill_cloudcover_from_provider(observation)

    return ObservationResponse(
        data=observation,
        units=units,
        source="weewx",
        generatedAt=_now_utc_z(),
    )


# ---------------------------------------------------------------------------
# GET /archive
# ---------------------------------------------------------------------------


@router.get(
    "/archive",
    summary="Historical archive records",
    tags=["Observations"],
    response_model=ArchiveResponse,
)
def get_archive_endpoint(
    db: Annotated[Session, Depends(get_db_session)],
    params: Annotated[ArchiveQueryParams, Depends(_get_archive_params)],
) -> ArchiveResponse:
    """Return archive records within a time window.

    Supports raw / hour / day interval aggregation and cursor + page pagination.
    Unknown query parameters are rejected with 400 per security-baseline §3.5.
    """
    registry = get_registry()

    # Validate cursor if provided.
    if params.cursor is not None:
        try:
            decode_cursor(params.cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cursor: {exc}") from exc

    # Validate fields if provided.
    parsed_field_names: list[str] | None = None
    if params.fields is not None:
        names = [f.strip() for f in params.fields.split(",") if f.strip()]
        mapped_names = {
            info.canonical_name
            for info in registry.stock.values()
            if info.canonical_name is not None
        }
        unknown = [n for n in names if n not in mapped_names]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown field(s): {', '.join(unknown)}",
            )
        parsed_field_names = names

    units = get_units_block()

    try:
        records, page_info = get_archive(
            db=db,
            registry=registry,
            from_dt=params.from_,
            to_dt=params.to,
            interval=params.interval,
            fields=parsed_field_names,
            limit=params.limit,
            cursor=params.cursor,
            page=params.page,
            agg=params.agg,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ArchiveResponse(
        data=records,
        units=units,
        source="weewx",
        generatedAt=_now_utc_z(),
        page=page_info,
    )
