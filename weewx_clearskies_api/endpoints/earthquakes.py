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

wire_earthquakes_settings(settings) extracts default_radius_km, min_magnitude,
  and default_days from settings.earthquakes for use as per-request fallbacks.

GeoNet note: GeoNet does not support server-side radius filtering; all events
  returned and the endpoint's radius filter applies post-fetch at the canonical
  layer. Other providers (USGS, EMSC, ReNaSS) pass radius_km to the provider.

Distance + unit conversion (T7.1/T7.2): every record gets a haversine
  distance-from-station (services/station.py StationInfo lat/lon). Depth and
  distance both participate in the group_distance unit system (unlike
  magnitude/coordinates, which stay unit-system-invariant) — converted to the
  operator's configured group_distance unit (mile or km) via
  units/conversion.py's convert(), never a hand-rolled factor. See
  API-MANUAL.md §2 "Earthquake fields".
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from weewx_clearskies_api.models.params import EarthquakesQueryParams
from weewx_clearskies_api.models.responses import (
    EarthquakeListResponse,
    EarthquakeRecord,
    utc_isoformat,
)
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import get_provider_registry
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock, get_station_info
from weewx_clearskies_api.services.units import get_units_block
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level settings wiring (populated at startup)
# ---------------------------------------------------------------------------

_default_radius_km: float = 100.0    # fallback if wire_earthquakes_settings not called
_default_min_magnitude: float = 2.0  # fallback min magnitude from config
_default_days: int = 7               # fallback lookback window in days
_configured_provider: str | None = None  # provider id from config (for /config endpoint)


def wire_earthquakes_settings(settings: object) -> None:
    """Store earthquakes settings for use by the endpoint.

    Extracts default_radius_km, min_magnitude, and default_days from
    settings.earthquakes. Called from __main__.py after settings load.
    Tests that don't care about these values leave them at the module defaults.
    """
    global _default_radius_km, _default_min_magnitude, _default_days, _configured_provider  # noqa: PLW0603
    eq_section = getattr(settings, "earthquakes", None)
    if eq_section is not None:
        _default_radius_km = float(getattr(eq_section, "default_radius_km", 100.0))
        _default_min_magnitude = float(getattr(eq_section, "min_magnitude", 2.0))
        _default_days = int(getattr(eq_section, "default_days", 7))
        _configured_provider = getattr(eq_section, "provider", None)


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
# Distance-from-station + group_distance unit conversion (T7.1 / T7.2)
# ---------------------------------------------------------------------------


def _resolve_distance_unit() -> str:
    """Return the operator's configured group_distance unit: "mile" or "km".

    Reads the units block populated at startup (services/units.py), keyed by
    a group_distance member field ("windrun") rather than inferring from the
    temperature-based target-unit-system check. This is authoritative for
    every case: it reflects any [StdReport][[Units]][[Groups]] override the
    operator applied specifically to group_distance, independent of the
    US/METRIC/METRICWX inference used for other purposes. "windrun" is always
    present in the units block (§6 unit groups; group_distance always has a
    system default), so no fallback branch is needed.
    """
    return get_units_block()["windrun"]


def _apply_distance_conversion(
    records: list[EarthquakeRecord],
    station_lat: float,
    station_lon: float,
    distance_unit: str,
) -> None:
    """Compute distance-from-station and convert depth/distance in place.

    Distance is computed via haversine (always in km first), then both depth
    and distance are converted to *distance_unit* ("mile" or "km") using the
    canonical conversion registry (units/conversion.py) — never a hand-rolled
    factor, per API-MANUAL §6 "Conversion factor accuracy".
    """
    for record in records:
        distance_km = _haversine_km(
            station_lat, station_lon, record.latitude, record.longitude
        )
        record.distance = _convert_unit(distance_km, "km", distance_unit)
        record.depth = _convert_unit(record.depth, "km", distance_unit)


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

    # --- Resolve operator's group_distance unit (T7.2) ---
    # "mile" or "km" (weewx-internal name, needed for the convert() calls below).
    distance_unit = _resolve_distance_unit()
    # Short display symbol for the units envelope, matching the project's
    # existing short-symbol convention for this endpoint (e.g. "km", not the
    # ConvertedValue " miles" label used elsewhere).
    distance_display_unit = "mi" if distance_unit == "mile" else "km"
    units_block = {
        "depth": distance_display_unit,
        "distance": distance_display_unit,
        "magnitude": "",
    }

    # --- Find the configured earthquakes provider in the capability registry ---
    provider_registry = get_provider_registry()
    earthquakes_providers = [p for p in provider_registry if p.domain == "earthquakes"]

    # --- Decision tree branch 1: no provider configured ---
    if not earthquakes_providers:
        logger.debug("No earthquakes provider in registry; returning empty list")
        return EarthquakeListResponse(
            data=[],
            units=units_block,
            source="none",
            generatedAt=now_str,
            stationClock=build_station_clock(),
            freshness=build_freshness("earthquakes"),
        )

    # Single source per deploy per ADR-040; take the first (and only) entry.
    provider_cap = earthquakes_providers[0]
    provider_id = provider_cap.provider_id

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

    # Resolve effective time window: ?from overrides configured default_days lookback.
    # When from_ is absent, compute starttime as now - default_days so the upstream
    # provider call is bounded (avoids open-ended queries against providers that may
    # return huge result sets).
    effective_from = params.from_
    if effective_from is None:
        effective_from = datetime.now(tz=UTC) - timedelta(days=_default_days)

    # Resolve effective min_magnitude: ?min_magnitude overrides configured default.
    # When absent, use _default_min_magnitude from config (applied post-cache per ADR-017).
    effective_min_magnitude = (
        params.min_magnitude if params.min_magnitude is not None else _default_min_magnitude
    )

    # --- Dispatch to provider module ---
    if provider_id == "usgs":
        from weewx_clearskies_api.providers.earthquakes import usgs  # noqa: PLC0415

        all_records = usgs.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=effective_from,
            to_dt=params.to,
        )
    elif provider_id == "geonet":
        from weewx_clearskies_api.providers.earthquakes import geonet  # noqa: PLC0415

        all_records = geonet.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=effective_from,
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
            from_dt=effective_from,
            to_dt=params.to,
        )
    elif provider_id == "renass":
        from weewx_clearskies_api.providers.earthquakes import renass  # noqa: PLC0415

        all_records = renass.fetch(
            lat=station.latitude,
            lon=station.longitude,
            radius_km=effective_radius_km,
            from_dt=effective_from,
            to_dt=params.to,
        )
    else:
        # Unknown provider should have been caught at startup by _wire_providers_from_config.
        logger.error("Unknown earthquakes provider at request time: %r", provider_id)
        raise HTTPException(
            status_code=502, detail=f"Unknown earthquakes provider: {provider_id!r}"
        )

    # --- Compute distance-from-station and convert depth/distance (T7.1/T7.2) ---
    # Applied to the full pre-filter list; magnitude filtering below doesn't
    # care about depth/distance, and this keeps a single conversion pass.
    _apply_distance_conversion(all_records, station.latitude, station.longitude, distance_unit)

    # --- Apply magnitude filter AFTER cache lookup + GeoNet radius filter (ADR-017) ---
    filtered_records = _filter_by_magnitude(all_records, effective_min_magnitude)

    return EarthquakeListResponse(
        data=filtered_records,
        units=units_block,
        source=provider_id,
        generatedAt=now_str,
        stationClock=build_station_clock(),
        freshness=build_freshness(
            "earthquakes", provider_refresh_interval=provider_cap.refresh_interval
        ),
    )


# ---------------------------------------------------------------------------
# GET /earthquakes/config
# ---------------------------------------------------------------------------


@router.get(
    "/earthquakes/config",
    summary="Current seismic configuration",
    tags=["Earthquakes"],
)
def get_earthquakes_config() -> dict:
    """Return the current seismic configuration from api.conf.

    Includes the configured provider, radius, minMagnitude, and defaultDays.
    The provider value reflects what is wired at startup; it may be None when
    no earthquakes provider is configured.
    """
    now_str = utc_isoformat(datetime.now(tz=UTC))

    # Derive the active provider id from the registry (consistent with /earthquakes).
    provider_registry = get_provider_registry()
    earthquakes_providers = [p for p in provider_registry if p.domain == "earthquakes"]
    provider_id = earthquakes_providers[0].provider_id if earthquakes_providers else (
        _configured_provider or "none"
    )

    return {
        "data": {
            "provider": provider_id,
            "radiusKm": _default_radius_km,
            "minMagnitude": _default_min_magnitude,
            "defaultDays": _default_days,
        },
        "generatedAt": now_str,
    }


# ---------------------------------------------------------------------------
# GET /earthquakes/faults
# ---------------------------------------------------------------------------


@router.get(
    "/earthquakes/faults",
    summary="Active fault lines within configured radius",
    tags=["Earthquakes"],
)
def get_faults() -> dict:
    """Return GEM Active Faults GeoJSON clipped to the configured radius.

    Faults are loaded from data/gem_active_faults.geojson (GEM Global Active
    Faults Database, CC-BY-SA 4.0). Only fault features with at least one
    vertex within the configured radius of the station are included.

    When the GEM data file is absent, an empty FeatureCollection is returned.
    """
    from weewx_clearskies_api.services.faults import get_faults_within_radius  # noqa: PLC0415

    now_str = utc_isoformat(datetime.now(tz=UTC))

    try:
        station = get_station_info()
    except RuntimeError:
        logger.error(
            "Station metadata not available at faults endpoint — "
            "this should not happen after successful startup"
        )
        raise HTTPException(status_code=503, detail="Service starting")

    # Cache-check-first guard (ADR-045).  The warmer pre-computes faults for
    # the station location and configured radius on a 6-hour interval.
    try:
        cached = get_cache().get("warmer:earthquakes:faults")
        if cached is not None:
            logger.debug("faults cache hit")
            return {
                "data": cached,
                "attribution": "Active faults: GEM Global Active Faults Database, CC-BY-SA 4.0",
                "generatedAt": now_str,
            }
    except Exception:
        logger.debug("faults cache miss or error", exc_info=True)

    data = get_faults_within_radius(station.latitude, station.longitude, _default_radius_km)

    return {
        "data": data,
        "attribution": "Active faults: GEM Global Active Faults Database, CC-BY-SA 4.0",
        "generatedAt": now_str,
    }
