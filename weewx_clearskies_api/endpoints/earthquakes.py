"""GET /earthquakes — recent earthquakes within configured radius (ADR-040).

Behavior decision tree per brief §per-endpoint spec:

  1. No earthquakes provider in capability registry  → 200, data=[], source="none"
  2. Provider configured, returns 200 + empty features → 200, data=[], source=<id>
  3. Provider configured, returns 200 + features → normalize, filter, return 200
  4. Network failure / 5xx after retries → 502 ProviderProblem (TransientNetworkError)
  5. Provider returns 429 → 503 ProviderProblem (QuotaExhausted) + Retry-After
  6. Provider returns 401/403 → 502 ProviderProblem (KeyInvalid)
  7. Pydantic validation failure on wire model → 502 ProviderProblem (ProviderProtocolError)

Magnitude filter (ADR-017 §Cache key — filter applied AFTER cache lookup):
  Cache stores the full canonical list (all magnitudes), keyed by station lat/lon,
  radius, and time window. Magnitude filter applied by the endpoint handler.

No DB hit.  Earthquakes come from the provider, not weewx archive.

Operator lat/lon: from get_station_info() (services/station.py) per ADR-011
  (single-station scope).  No ?station= param.

Pydantic + Depends pattern (coding.md §1, security-baseline §3.5):
  Unknown query keys rejected with 422/400 via extra="forbid" + Depends wrapper.

Provider discovery: endpoint reads the capability registry at request time.
  wire_providers() at startup registers the configured provider's CAPABILITY;
  this endpoint checks the registry for an "earthquakes" domain entry.

All four providers are keyless (per ADR-040) — no credential wiring functions
needed here (no wire_*_credentials() calls; no module-level credential storage).

wire_earthquakes_settings(settings) extracts default_radius_km from
  settings.earthquakes for use as the per-request radius fallback when
  ?radius_km is not supplied.

GeoNet note: GeoNet does not support server-side radius filtering; all events
  returned and the endpoint's radius filter applies post-fetch at the canonical
  layer. Other providers (USGS, EMSC, ReNaSS) pass radius_km to the provider.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Annotated

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from weewx_clearskies_api.models.params import EarthquakesQueryParams
from weewx_clearskies_api.models.responses import EarthquakeListResponse, EarthquakeRecord, utc_isoformat
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.station import get_station_info

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup)
# ---------------------------------------------------------------------------

_default_radius_km: float = 100.0  # fallback if wire_earthquakes_settings not called


def wire_earthquakes_settings(settings: object) -> None:
    """Store earthquakes settings for use by the endpoint.

    Extracts default_radius_km from settings.earthquakes. Called from
    __main__.py after settings load. Tests that don't care about the
    configured radius leave this at the module default of 100 km.
    """
    global _default_radius_km  # noqa: PLW0603
    eq_section = getattr(settings, "earthquakes", None)
    if eq_section is not None:
        _default_radius_km = float(getattr(eq_section, "default_radius_km", 100.0))


# ---------------------------------------------------------------------------
# Depends wrapper — Pydantic + Depends pattern (coding.md §1)
# ---------------------------------------------------------------------------


def _get_earthquakes_params(request: Request) -> EarthquakesQueryParams:
    """Extract and validate /earthquakes query parameters via Pydantic.

    Using Depends(model_validate(dict(request.query_params))) pattern so
    extra="forbid" actually fires for unknown query keys (coding.md §1,
    security-baseline §3.5).  Individual FastAPI Query() declarations
    silently ignore unknown keys — not acceptable.
    """
    try:
        return EarthquakesQueryParams.model_validate(dict(request.query_params))
    except pydantic.ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# ---------------------------------------------------------------------------
# Magnitude filter helper
# ---------------------------------------------------------------------------


def _filter_by_magnitude(
    records: list[EarthquakeRecord], min_magnitude: float | None
) -> list[EarthquakeRecord]:
    """Return earthquakes at or above the minimum magnitude.

    None -> return all (no filter).
    Applied post-cache per ADR-017 so the cache entry is operator-uniform
    (one entry per station + radius + time window, not one per magnitude filter).
    """
    if min_magnitude is None:
        return records
    return [r for r in records if r.magnitude >= min_magnitude]


# ---------------------------------------------------------------------------
# GeoNet radius filter (post-fetch; GeoNet doesn't accept lat/lon/radius params)
# ---------------------------------------------------------------------------

# Earth radius in km (WGS84 mean spherical approximation).
_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _filter_by_radius(
    records: list[EarthquakeRecord],
    station_lat: float,
    station_lon: float,
    radius_km: float,
) -> list[EarthquakeRecord]:
    """Return only earthquakes within radius_km of the station.

    Used for GeoNet, which returns all NZ events (no server-side radius).
    Other providers perform server-side radius filtering; this is a no-op for them
    (already limited by the server's maxradiuskm param).
    """
    return [
        r
        for r in records
        if _haversine_km(station_lat, station_lon, r.latitude, r.longitude) <= radius_km
    ]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/earthquakes",
    summary="Recent earthquakes within configured radius",
    tags=["Earthquakes"],
    response_model=EarthquakeListResponse,
)
def get_earthquakes(
    params: Annotated[EarthquakesQueryParams, Depends(_get_earthquakes_params)],
) -> EarthquakeListResponse:
    """Return recent earthquakes from the configured provider.

    Reads the capability registry for the earthquakes domain at request time.
    Returns EarthquakeListResponse(data=[], source="none") when no provider is
    registered (ADR-040 §Single source per deploy).

    Magnitude filter and radius filter are applied post-cache (ADR-017).
    GeoNet post-fetch radius filter is applied here since GeoNet does not
    accept server-side lat/lon/radius params.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # --- Find the configured earthquakes provider in the capability registry ---
    provider_registry = get_provider_registry()
    earthquakes_providers = [p for p in provider_registry if p.domain == "earthquakes"]

    # --- Decision tree branch 1: no provider configured ---
    if not earthquakes_providers:
        logger.debug("No earthquakes provider in registry; returning empty list")
        return EarthquakeListResponse(
            data=[],
            source="none",
            generatedAt=now_str,
        )

    # Single source per deploy per ADR-040; take the first (and only) entry.
    provider_id = earthquakes_providers[0].provider_id

    # --- Obtain station lat/lon (ADR-011: single-station, no ?station= param) ---
    try:
        station = get_station_info()
    except RuntimeError:
        # Defense-in-depth: station should always be wired before uvicorn starts.
        logger.error(
            "Station metadata not available at earthquakes endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(
            status_code=503,
            detail="Service starting",
        )

    # Resolve effective radius: ?radius_km overrides configured default.
    effective_radius_km = params.radius_km if params.radius_km is not None else _default_radius_km

    # --- Dispatch to provider module ---
    if provider_id == "usgs":
        from weewx_clearskies_api.providers.earthquakes import usgs  # noqa: PLC0415

        all_records = usgs.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=params.from_,
            to_dt=params.to,
        )
    elif provider_id == "geonet":
        from weewx_clearskies_api.providers.earthquakes import geonet  # noqa: PLC0415

        all_records = geonet.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=params.from_,
            to_dt=params.to,
        )
        # GeoNet returns all NZ events — apply radius filter post-fetch.
        all_records = _filter_by_radius(
            all_records, station.latitude, station.longitude, effective_radius_km
        )
    elif provider_id == "emsc":
        from weewx_clearskies_api.providers.earthquakes import emsc  # noqa: PLC0415

        all_records = emsc.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=params.from_,
            to_dt=params.to,
        )
    elif provider_id == "renass":
        from weewx_clearskies_api.providers.earthquakes import renass  # noqa: PLC0415

        all_records = renass.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=params.from_,
            to_dt=params.to,
        )
    else:
        # Unknown provider should have been caught at startup by _wire_providers_from_config.
        logger.error("Unknown earthquakes provider at request time: %r", provider_id)
        raise HTTPException(
            status_code=502, detail=f"Unknown earthquakes provider: {provider_id!r}"
        )

    # --- Apply magnitude filter AFTER cache lookup + GeoNet radius filter (ADR-017) ---
    filtered_records = _filter_by_magnitude(all_records, params.min_magnitude)

    return EarthquakeListResponse(
        data=filtered_records,
        source=provider_id,
        generatedAt=now_str,
    )
