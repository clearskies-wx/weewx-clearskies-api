"""Setup endpoints (ADR-038 §secure channel).

Six endpoints that let the config UI wizard pair with the API over TLS during
initial setup.  After initial setup, endpoints accept re-runs authenticated via
X-Clearskies-Proxy-Auth (the same shared secret used for normal API requests).

Endpoints:
  POST /setup/handshake                  — exchange trust token for a session_id
  GET  /setup/db-defaults                — return weewx.conf DB connection defaults
  POST /setup/db-test                    — test a DB connection with supplied credentials
  GET  /setup/schema                     — reflect DB schema using stored db_params
  GET  /setup/station                    — return weewx.conf station identity
  POST /setup/apply                      — write api.conf + secrets.env, mark setup complete;
                                            also pushes the marine config subset to
                                            {marine_service_url}/config when configured (T6.4)
  GET  /setup/marine/config              — return the marine config subset (same payload
                                            /setup/apply pushes); authenticated with
                                            MARINE_SERVICE_SECRET, called by the marine
                                            service on startup config recovery (T6.4b)
  GET  /setup/marine/eccodes-check       — probe whether a GRIB2 backend (eccodes or
                                            pygrib) is installed, before the wizard lets
                                            the operator enable marine features
                                            (Marine Remediation Plan T3.6)
  GET  /setup/marine/swan-check          — probe whether the SWAN Fortran binary is
                                            installed; returns version, path, and CPU
                                            core count for the wizard's SWAN step
                                            (T4.4 / SWAN-CORRECTIONS-PLAN T4.1); C-54
                                            pass-through to
                                            {marine_service_url}/discovery/swan-check
                                            — SWAN runs on the marine service host,
                                            not this one
  GET  /setup/marine/discover-stations   — discover nearby NDBC/CO-OPS stations (T6.3)
  GET  /setup/marine/species             — species checklist for a coordinate + fishing
                                            target category, keyed by biogeographic region
                                            (T2.5)
  GET  /setup/marine/species-database    — dump the full loaded species reference data
                                            (regions, species-by-region, per-species scoring
                                            profiles, seasonal behavior) for admin/wizard
                                            reference (T8.2 — data externalized to
                                            data/species.yaml)
  GET  /setup/marine/coverage             — data source coverage panel for a coordinate
                                            (T3.6); bathymetry block is a C-48 pass-through
                                            to {marine_service_url}/discovery/bathymetry-coverage
  GET  /setup/marine/compute-estimate    — runtime estimate for the current spot
                                            configuration (Phase 16); C-48 pass-through to
                                            {marine_service_url}/discovery/compute-estimate
  GET  /setup/marine/discover-structures — discover nearby coastal structures (jetties,
                                            piers, breakwaters, seawalls, groins) via the
                                            OpenStreetMap Overpass API, for surf spot
                                            wave-physics setup (T5.2)
  POST /setup/marine/bathymetry/upload      — accept operator-supplied GeoTIFF bathymetry
                                            file; validate, save, return per-level coverage
                                            (SWAN-FIXES-PLAN Phase 24, T24.1)
  POST /setup/providers/test-marine         — test connectivity to the marine service
                                            (T7.3, LC-P7-4): authenticated GET /health,
                                            plus GET /discovery/grib-availability when a
                                            secret is supplied (proves the secret, since
                                            /health is auth-exempt); returns {ok, version,
                                            spots, last_run, status, error}
  GET  /setup/marine/health              — admin status page pass-through (Marine Model
                                            Restoration Plan B4): GET {marine_service_url}/health
                                            verbatim, for the admin status page (the marine
                                            service may only be reached through the API).
                                            Returns {reachable, error, health} where health is
                                            the marine service's health body unmodified — never
                                            cherry-picked or re-typed

No /api/v1 prefix — setup is a separate surface registered without a prefix
in app.py.  All endpoints live directly under /setup/...
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import configobj
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from weewx_clearskies_api.config.marine_config import (
    _COMPASS_DIRECTIONS,
    _VALID_ACTIVITIES,
    _VALID_BOTTOM_TYPES,
    _VALID_STRUCTURE_MATERIALS,
    _VALID_STRUCTURE_TYPES,
    _VALID_TARGET_CATEGORIES,
    _VALID_TOPOGRAPHIC_FEATURES,
)
from weewx_clearskies_api.correction.models import (
    CorrectionStatusResponse,
    CorrectionToggleRequest,
    CorrectionToggleResponse,
    RetrainResponse,
)
from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, SchemaReflector
from weewx_clearskies_api.enrichment.fishing_species import (
    BIOGEOGRAPHIC_REGIONS,
    SEASONAL_BEHAVIOR,
    SPECIES_BY_REGION,
    SPECIES_PROFILES,
    classify_region as _classify_fishing_region,
)
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.errors import ProviderError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.services.companion_proxy import (
    MarineDiscoveryError,
    MarineDiscoveryUnconfiguredError,
    marine_discovery_get,
)
from weewx_clearskies_api.services.station import _get_str_field, _parse_altitude
from weewx_clearskies_api.services.weewx_conf import WeewxConfLoadError, get_weewx_conf, load_weewx_conf
from weewx_clearskies_api.services.weewx_metadata import get_unit_for_group
from weewx_clearskies_api.trust import TrustManager, _read_secrets_env, _write_secrets_env
from weewx_clearskies_api.units.conversion import convert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])

# ---------------------------------------------------------------------------
# Marine discovery pass-throughs (C-42, MARINE-SEP-CONCERNS.md).
#
# The wizard used to import providers/{ocean,buoy,tides,marine}/ directly to
# answer four setup-time questions. Per the operator ruling, the API is the
# marine service's only client — these are now proxied lookups via
# services/companion_proxy.py's marine_discovery_get() rather than provider
# imports. Response shapes below match the deleted provider functions'
# return values field-for-field (same names, same nullability) so every
# caller in this file needed zero changes beyond the call site itself.
# ---------------------------------------------------------------------------


def _marine_discovery_http_exception(exc: MarineDiscoveryError) -> HTTPException:
    """Convert a marine discovery failure into the wizard-facing 503.

    Both branches are 503 (the marine service is not currently able to
    answer), but the ``detail`` text is deliberately different — an
    operator who hasn't installed the marine service needs "marine
    features require the marine service," not a message implying an
    outage. Never returns 200 with an empty result for either case.
    """
    if isinstance(exc, MarineDiscoveryUnconfiguredError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=503, detail=f"Marine service discovery failed: {exc}")


def _discover_ofs_model(lat: float, lon: float) -> tuple[str | None, str | None]:
    """C-42 pass-through for the deleted ``providers.ocean.ofs.find_ofs_model``.

    GET {marine_service_url}/discovery/ofs-model?lat&lon -> a bare 2-element
    JSON array ``[primary, fallback]`` (pinned contract, positional — the
    underlying ``find_ofs_model()`` returned an unnamed tuple, so there were
    no field names to preserve; consumed positionally, NOT as
    ``{"primary": ..., "fallback": ...}``). Example live responses: ``[null,
    null]`` (no OFS coverage), ``["SFBOFS", "WCOFS"]`` (San Francisco Bay).
    """
    body = marine_discovery_get("/discovery/ofs-model", {"lat": lat, "lon": lon})
    primary, fallback = body
    return primary, fallback


def _discover_ndbc_stations(lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
    """C-42 pass-through for the deleted ``providers.buoy.ndbc.discover_stations``.

    GET {marine_service_url}/discovery/buoy-stations?lat&lon&radius_km ->
    list[{stationId, name, lat, lon, type, capabilityGuess, distanceKm}]
    (pinned contract — same shape ``ndbc.discover_stations`` returned).
    """
    body = marine_discovery_get(
        "/discovery/buoy-stations", {"lat": lat, "lon": lon, "radius_km": radius_km}
    )
    return body if isinstance(body, list) else []


def _discover_coops_stations(lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
    """C-42 pass-through for the deleted ``providers.tides.coops.discover_stations``.

    GET {marine_service_url}/discovery/tide-stations?lat&lon&radius_km ->
    list[{id, name, lat, lon, distance_km, products}] (pinned contract —
    same shape ``coops.discover_stations`` returned).
    """
    body = marine_discovery_get(
        "/discovery/tide-stations", {"lat": lat, "lon": lon, "radius_km": radius_km}
    )
    return body if isinstance(body, list) else []


def _discover_grib_availability() -> tuple[bool, str | None]:
    """C-42 pass-through for the deleted ``providers.marine.grib_processor``
    GRIB2-backend probe (``GRIB_AVAILABLE`` / ``check_grib_available()``).

    GET {marine_service_url}/discovery/grib-availability -> {"available":
    bool, "install_instructions": str|null}. GRIB2 processing now happens
    entirely in the marine service's process, so the availability question
    is inherently about that process, not this one.
    """
    body = marine_discovery_get("/discovery/grib-availability", {})
    return bool(body.get("available")), body.get("install_instructions")


# ---------------------------------------------------------------------------
# Forecast correction module-level state (wired at startup by __main__.py)
# ---------------------------------------------------------------------------

#: ForecastCorrectionSettings instance; set by wire_forecast_correction_settings().
_forecast_correction_settings: object | None = None

#: Runtime override for collection_enabled (toggled via /setup/forecast-correction/toggle).
#: None means "use the value from _forecast_correction_settings".
_collection_enabled_override: bool | None = None


def wire_forecast_correction_settings(settings: object) -> None:
    """Store the ForecastCorrectionSettings for use by the admin endpoints.

    Called from __main__.py step 6m½ so the three forecast-correction admin
    endpoints can access settings without importing them at module load time.
    Mirrors wire_forecast_settings() in endpoints/forecast.py.

    Args:
        settings: ForecastCorrectionSettings instance from config.
    """
    global _forecast_correction_settings  # noqa: PLW0603
    _forecast_correction_settings = settings


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _check_proxy_auth(request: Request) -> bool:
    """Return True if the request carries a valid X-Clearskies-Proxy-Auth header.

    Mirrors ProxyAuthMiddleware logic exactly — constant-time comparison against
    WEEWX_CLEARSKIES_PROXY_SECRET.  Returns False (not raises) when the secret
    env var is unset so callers can decide how to handle that case.
    """
    secret = os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip()
    if not secret:
        return False
    provided = request.headers.get("X-Clearskies-Proxy-Auth", "")
    # Both empty strings would hmac-match, but an empty secret means "not configured"
    # which is already guarded above.  An absent header must not match a set secret.
    if not provided:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), provided.encode("utf-8"))


async def require_setup_active(request: Request) -> TrustManager:
    """Gate access to setup endpoints.

    - Setup NOT complete: passes through (trust session required by the individual
      endpoint via require_setup_session).
    - Setup IS complete: requires a valid X-Clearskies-Proxy-Auth header so that
      re-configuration (e.g. credential rotation) remains possible without 410.
      Raises 401 if the header is absent or wrong, 503 if the secret is not
      configured (admin access not possible without it).
    """
    tm: TrustManager = request.app.state.trust_manager
    if tm.setup_complete:
        secret_configured = bool(os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip())
        if not secret_configured:
            raise HTTPException(
                503,
                detail="Setup complete; proxy secret not configured — admin re-run unavailable.",
            )
        if not _check_proxy_auth(request):
            raise HTTPException(401, detail="Admin re-run requires valid X-Clearskies-Proxy-Auth")
    return tm


async def require_setup_session(request: Request) -> TrustManager:
    """Ensure the caller is authorised to drive a setup step.

    - Setup NOT complete: require Bearer setup-session token (issued by handshake).
    - Setup IS complete: require valid X-Clearskies-Proxy-Auth header (same check
      as require_setup_active — re-run case already passed that gate, but we
      validate again here for defence-in-depth).
    """
    tm: TrustManager = request.app.state.trust_manager
    if tm.setup_complete:
        # require_setup_active already validated proxy auth; call it again to keep
        # the dependency chain explicit and guard against direct endpoint calls.
        await require_setup_active(request)
        return tm

    # Setup not yet complete — fall through to trust-session check.
    tm = await require_setup_active(request)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, detail="Setup session required")
    session_id = auth[7:]
    if not tm.validate_session(session_id):
        raise HTTPException(401, detail="Invalid or expired setup session")
    return tm


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class HandshakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class HandshakeResponse(BaseModel):
    session_id: str


class DbDefaultsResponse(BaseModel):
    kind: str = "mysql"  # "sqlite" or "mysql"
    host: str = ""       # MySQL only
    port: int = 0        # MySQL only
    user: str = ""       # MySQL only
    name: str = ""       # MySQL only
    path: str = ""       # SQLite only — path to .sdb file
    conf_path: str = ""  # weewx.conf path


class DbTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "mysql"  # "sqlite" or "mysql"
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    name: str = ""
    path: str = ""       # SQLite only


class DbTestResponse(BaseModel):
    success: bool
    version: str | None = None
    error: str | None = None


class ColumnEntry(BaseModel):
    name: str
    db_type: str
    stock: bool
    canonical: str | None
    auto_detected_group: str | None = None
    auto_detected_unit: str | None = None
    suggested_group: str | None = None
    suggested_unit: str | None = None
    unit_source: str | None = None


class SchemaResponse(BaseModel):
    columns: list[ColumnEntry]
    stock_count: int
    unmapped_count: int


class StationResponse(BaseModel):
    station_name: str
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
    altitude_unit: str = "meter"  # "foot" or "meter" — matches weewx.conf unit string
    station_type: str | None = None


class DatabaseApplyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "mysql"  # "sqlite" or "mysql"
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    name: str = ""
    path: str = ""       # SQLite only


class StationApplyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
    timezone: str | None = None
    default_locale: str | None = None


class ProviderConfig(BaseModel):
    """Configuration for a single provider domain (forecast, aqi, alerts, radar, earthquakes).

    Non-secret fields (provider id, nws_user_agent_contact, iframe_url) go to api.conf.
    Credential fields (api_key, api_secret, pws_station_id) go to secrets.env using the
    exact env var names that settings.py reads at startup (provider-scoped, per ADR-027 §3).

    Credential naming follows existing settings.py conventions (not domain-scoped):
      aeris      → WEEWX_CLEARSKIES_AERIS_CLIENT_ID / WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET
      openweathermap → WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID
      iqair      → WEEWX_CLEARSKIES_IQAIR_KEY
      nws        → no credentials; nws_user_agent_contact is non-secret (api.conf)
    """

    model_config = ConfigDict(extra="forbid")

    #: Provider id (e.g. "nws", "aeris", "openweathermap", "iqair",
    #: "openmeteo", "rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet",
    #: "dwd_radolan", "usgs", "geonet", "emsc", "renass", "iframe").
    provider: str
    #: Primary credential. Maps to provider-specific env var (see class docstring).
    api_key: str | None = None
    #: Secondary credential (Aeris only: client_secret).
    api_secret: str | None = None
    #: NWS User-Agent contact email or URL (non-secret; written to api.conf).
    nws_user_agent_contact: str | None = None
    #: Radar iframe embed URL (non-secret; written to api.conf [radar] section).
    iframe_url: str | None = None
    #: Aeris forecast model selection: "standard" or "xcast" (ADR-063).
    #: Written to api.conf [forecast] aeris_forecast_model.
    aeris_forecast_model: str | None = None
    #: LibreWxR API endpoint (non-secret; written to api.conf [radar] section).
    librewxr_endpoint: str | None = None
    #: LibreWxR geographic bounds "south,west,north,east" (non-secret; api.conf).
    librewxr_bounds: str | None = None
    #: Marine alert radius in miles (ADR-089). Written to api.conf [alerts]
    #: marine_alert_radius_miles. Only sent for the "alerts" domain.
    marine_alert_radius_miles: int | None = None
    #: Pre-discovered marine zone IDs (ADR-089). Written to api.conf [alerts]
    #: marine_alert_zone_ids. Populated by the API at apply time via zone
    #: discovery — the wizard does not send this field directly.
    marine_alert_zone_ids: list[str] | None = None


class BrandingApplyConfig(BaseModel):
    """Branding fields for the [branding] section of api.conf."""

    model_config = ConfigDict(extra="forbid")

    site_title: str | None = None
    copyright_entity: str | None = None
    logo_light_url: str | None = None
    logo_dark_url: str | None = None
    #: Alt text for the logo image (WCAG 2.1 AA, ADR-022 §5.5).
    #: When omitted the service falls back to "<site_title> logo" or
    #: "Weather station logo" so no logo is ever rendered with empty alt.
    logo_alt: str | None = None
    favicon_url: str | None = None
    accent: str | None = None
    default_theme_mode: str | None = None
    custom_css_url: str | None = None


class SocialApplyConfig(BaseModel):
    """Social media URL fields for the [social] section of api.conf."""

    model_config = ConfigDict(extra="forbid")

    facebook_url: str | None = None
    twitter_url: str | None = None
    instagram_url: str | None = None
    youtube_url: str | None = None


class EarthquakeApplyConfig(BaseModel):
    """Earthquake settings for the [earthquakes] section of api.conf.

    These are the seismic-specific knobs beyond the provider id (which is
    handled by the ProviderConfig mechanism in the providers dict).

    All fields are optional — absent means "leave existing value unchanged"
    (same pattern as BrandingApplyConfig and SocialApplyConfig).
    """

    model_config = ConfigDict(extra="forbid")

    #: Default radius in km from station for earthquake queries.
    default_radius_km: float | None = None
    #: Minimum magnitude filter applied when ?minmagnitude not supplied.
    min_magnitude: float | None = None
    #: Lookback window in days used to compute starttime when ?from not supplied.
    default_days: int | None = None


_NDBC_STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{3,10}$")
_COOPS_STATION_ID_PATTERN = re.compile(r"^\d{5,8}$")
_NWS_MARINE_ZONE_ID_PATTERN = re.compile(r"^(AMZ|GMZ|PZZ|ANZ|PKZ|PHZ)\d{3}$")
_MARINE_LOCATION_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


class MarineBathymetryPointApplyConfig(BaseModel):
    """One ``{distance_m, depth_m}`` point of an operator-supplied or
    CUDEM-downloaded bathymetric profile."""

    model_config = ConfigDict(extra="forbid")

    distance_m: float = Field(ge=0)
    depth_m: float = Field(ge=0)


class MarineStructureApplyConfig(BaseModel):
    """A coastal structure (jetty/pier/breakwater/seawall/groin) near a surf spot."""

    model_config = ConfigDict(extra="forbid")

    type: str
    material: str
    length_m: float = Field(gt=0)
    bearing_degrees: float = Field(ge=0, lt=360)
    distance_m: float = Field(gt=0)
    #: Real structure outline from the wizard's Overpass API discovery
    #: (E13 / ADR-095 Decision 3), ``[[lon, lat], ...]`` — the marine
    #: ``StructureConfig.coordinates`` contract. Optional: absent when no
    #: discovery geometry was captured (e.g. hand-entered structure).
    coordinates: list[list[float]] | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in _VALID_STRUCTURE_TYPES:
            raise ValueError(f"structure type {v!r} not in {sorted(_VALID_STRUCTURE_TYPES)}")
        return v

    @field_validator("material")
    @classmethod
    def _validate_material(cls, v: str) -> str:
        if v not in _VALID_STRUCTURE_MATERIALS:
            raise ValueError(f"structure material {v!r} not in {sorted(_VALID_STRUCTURE_MATERIALS)}")
        return v


class MarineSurfSpotApplyConfig(BaseModel):
    """``[[[[surf]]]]`` sub-block for one marine location (PROVIDER-MANUAL §14,
    OPERATIONS-MANUAL.md "Marine location configuration").

    T2.1 (SURF-1D-IMPLEMENTATION-PLAN Phase 2): The old single-pin fields
    (``beach_facing_degrees``, ``spot_lat``, ``spot_lon``) are replaced by a
    shoreline segment.  ``beach_facing_degrees`` is now computed from the
    segment geometry at config load time — it is NOT accepted in the apply
    payload and NOT written to api.conf.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Shoreline segment (T2.1) ---
    #: Latitude of the segment start endpoint (degrees WGS84).
    segment_start_lat: float = Field(ge=-90, le=90)
    #: Longitude of the segment start endpoint (degrees WGS84).
    segment_start_lon: float = Field(ge=-180, le=180)
    #: Latitude of the segment end endpoint (degrees WGS84).
    segment_end_lat: float = Field(ge=-90, le=90)
    #: Longitude of the segment end endpoint (degrees WGS84).
    segment_end_lon: float = Field(ge=-180, le=180)
    #: Spacing between parallel cross-shore transects (metres).
    #: Default 10m matches L3 grid resolution so each transect aligns with a
    #: SWAN grid node (SURF-ZONE-MODEL-BRIEF §2.2.4).
    #: None (omitted from payload) is config-file-owned: preserved from the
    #: existing api.conf value, or defaulted to 10.0 for a brand-new
    #: location (operator ruling 2026-08-03, decision item 8).
    transect_spacing_m: float | None = Field(default=None, gt=0)

    # --- Other surf config fields ---
    bottom_type: str
    topographic_feature: str
    directional_exposure: dict[str, bool] | None = None
    #: Nested-subsection shape when written to api.conf — see
    #: _build_marine_conf_section() for the T6.3 divergence note (the round
    #: brief's flat-CSV example does not match config/marine_config.py's
    #: SurfSpotConfig.__init__ loader, which is authoritative).
    bathymetric_profile: list[MarineBathymetryPointApplyConfig] | None = None
    structures: list[MarineStructureApplyConfig] | None = None
    #: Breaker height formula for this spot (T2.6, T4.4 wizard).
    #: "komar_gaughan" (default) — Komar & Gaughan (1973), general purpose.
    #: "caldwell" — Caldwell & Aucan (2007) H1/10 for steep volcanic coasts.
    #: None (omitted from payload) is config-file-owned: preserved from the
    #: existing api.conf value (operator ruling 2026-08-03, decision item 8).
    breaker_formula: str | None = None
    #: Display convention for surf height (T2.6, T4.4 wizard).
    #: "face" (default) — trough-to-crest face height (Western scale).
    #: "hawaiian" — back-of-wave scale (≈ face × 0.5, traditional Hawaii/Australia).
    #: None (omitted from payload) is config-file-owned: preserved from the
    #: existing api.conf value (operator ruling 2026-08-03, decision item 8).
    surf_height_display: str | None = None
    #: Operator override for L3 (fine) nested grid in SWAN.
    #: "auto" (default) — enable L3 only when structures are present.
    #: "on" — always enable L3.  "off" — never enable L3.
    #: None (omitted from payload) is config-file-owned: preserved from the
    #: existing api.conf value, or defaulted to "auto" for a brand-new
    #: location (operator ruling 2026-08-03, decision item 8).
    l3_enabled: str | None = None
    #: Bottom friction coefficient (cfjon) for SWAN FRICTION JON.
    #: Default 0.038 (JONSWAP swell value).  Always enabled in production —
    #: frictionless is not valid for nearshore wave modelling.
    friction_coefficient: float = 0.038
    #: Enable SurfBeat strip for infragravity / set timing predictions.
    #: Default True.  When False, setTimingMinutes / igWaveHeightM are null.
    surfbeat_enabled: bool = True
    #: Hours between SurfBeat strip SWAN runs (1 per cadence hour, default 3).
    #: Intermediate forecast hours carry forward the last strip result.
    surfbeat_cadence_hours: int = 3
    #: Maximum expected significant wave height (m) for this spot (T4A.3 Do
    #: step 11, API-MANUAL §17). Drives SwellTrack's fine-zone sizing
    #: (T4A.2) and the L3 viability check's breaking-depth expression
    #: (ADR-093 Amendment 2). Default 4.0m.
    #: None (omitted from payload) is config-file-owned: preserved from the
    #: existing api.conf value, or defaulted to 4.0 for a brand-new location
    #: (operator ruling 2026-08-03, decision item 8).
    max_hs_m: float | None = Field(default=None, gt=0, le=30)

    @field_validator("bottom_type")
    @classmethod
    def _validate_bottom_type(cls, v: str) -> str:
        if v not in _VALID_BOTTOM_TYPES:
            raise ValueError(f"bottom_type {v!r} not in {sorted(_VALID_BOTTOM_TYPES)}")
        return v

    @field_validator("topographic_feature")
    @classmethod
    def _validate_topographic_feature(cls, v: str) -> str:
        if v not in _VALID_TOPOGRAPHIC_FEATURES:
            raise ValueError(
                f"topographic_feature {v!r} not in {sorted(_VALID_TOPOGRAPHIC_FEATURES)}"
            )
        return v

    @field_validator("directional_exposure")
    @classmethod
    def _validate_directional_exposure(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if v is None:
            return v
        bad = sorted(k for k in v if k not in _COMPASS_DIRECTIONS)
        if bad:
            raise ValueError(f"directional_exposure keys {bad!r} not in {list(_COMPASS_DIRECTIONS)}")
        return v

    @field_validator("breaker_formula")
    @classmethod
    def _validate_breaker_formula(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from weewx_clearskies_api.config.marine_config import _VALID_BREAKER_FORMULAS
        if v not in _VALID_BREAKER_FORMULAS:
            raise ValueError(f"breaker_formula {v!r} not in {sorted(_VALID_BREAKER_FORMULAS)}")
        return v

    @field_validator("surf_height_display")
    @classmethod
    def _validate_surf_height_display(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from weewx_clearskies_api.config.marine_config import _VALID_SURF_HEIGHT_DISPLAYS
        if v not in _VALID_SURF_HEIGHT_DISPLAYS:
            raise ValueError(
                f"surf_height_display {v!r} not in {sorted(_VALID_SURF_HEIGHT_DISPLAYS)}"
            )
        return v

    @field_validator("l3_enabled")
    @classmethod
    def _validate_l3_enabled(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from weewx_clearskies_api.config.marine_config import _VALID_L3_ENABLED
        if v not in _VALID_L3_ENABLED:
            raise ValueError(f"l3_enabled {v!r} not in {sorted(_VALID_L3_ENABLED)}")
        return v


class MarineFishingSpotApplyConfig(BaseModel):
    """``[[[[fishing]]]]`` sub-block for one marine location.

    ``target_categories`` (T6.3, 2026-07-11) replaces the earlier single
    ``target_category: str`` — anglers may target species from multiple
    categories at the same spot. Accepts either a bare string (from an
    older wizard/admin client, or a single-category selection) or a list;
    the validator normalizes a bare string to a one-element list.
    """

    model_config = ConfigDict(extra="forbid")

    target_categories: list[str] | str
    #: Auto-classified from coordinates when omitted (OPERATIONS-MANUAL.md);
    #: the wizard may also send an operator-confirmed value.
    biogeographic_region: str | None = None
    #: Operator-provided species names targeted at this spot (T2.1, 2026-07-15).
    species: list[str] = []

    @field_validator("target_categories")
    @classmethod
    def _validate_target_categories(cls, v: list[str] | str) -> list[str]:
        categories = [v] if isinstance(v, str) else v
        if not categories:
            raise ValueError("target_categories must not be empty")
        bad = sorted(c for c in categories if c not in _VALID_TARGET_CATEGORIES)
        if bad:
            raise ValueError(f"target_categories {bad!r} not in {sorted(_VALID_TARGET_CATEGORIES)}")
        return categories


class MarineExternalLinkApplyConfig(BaseModel):
    """One operator-provided informational link (beach safety resources)."""

    model_config = ConfigDict(extra="forbid")

    label: str
    url: str


class MarineBeachSafetyApplyConfig(BaseModel):
    """``[[[[beach_safety]]]]`` sub-block for one marine location."""

    model_config = ConfigDict(extra="forbid")

    external_links: list[MarineExternalLinkApplyConfig] | None = None


class MarineLocationApplyConfig(BaseModel):
    """One ``[[[<id>]]]`` entry under ``[marine][[locations]]``.

    ``id`` becomes the configobj section key — must be a lowercase slug
    (letters, digits, hyphen, underscore).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    activities: list[str]
    ndbc_station_ids: list[str] = []
    coops_station_ids: list[str] = []
    nws_marine_zone_id: str | None = None
    surf: MarineSurfSpotApplyConfig | None = None
    fishing: MarineFishingSpotApplyConfig | None = None
    beach_safety: MarineBeachSafetyApplyConfig | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _MARINE_LOCATION_ID_PATTERN.match(v):
            raise ValueError(
                f"marine location id {v!r} must be a lowercase slug "
                "(letters, digits, hyphen, underscore; 1-64 chars)"
            )
        return v

    @field_validator("activities")
    @classmethod
    def _validate_activities(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("activities must not be empty")
        bad = sorted(a for a in v if a not in _VALID_ACTIVITIES)
        if bad:
            raise ValueError(f"activities {bad!r} not in {sorted(_VALID_ACTIVITIES)}")
        return v

    @field_validator("ndbc_station_ids")
    @classmethod
    def _validate_ndbc_ids(cls, v: list[str]) -> list[str]:
        bad = sorted(s for s in v if not _NDBC_STATION_ID_PATTERN.match(s))
        if bad:
            raise ValueError(f"ndbc_station_ids {bad!r} are not valid NDBC station id formats")
        return v

    @field_validator("coops_station_ids")
    @classmethod
    def _validate_coops_ids(cls, v: list[str]) -> list[str]:
        bad = sorted(s for s in v if not _COOPS_STATION_ID_PATTERN.match(s))
        if bad:
            raise ValueError(f"coops_station_ids {bad!r} are not valid CO-OPS station id formats")
        return v

    @field_validator("nws_marine_zone_id")
    @classmethod
    def _validate_zone_id(cls, v: str | None) -> str | None:
        if v is not None and not _NWS_MARINE_ZONE_ID_PATTERN.match(v):
            raise ValueError(f"nws_marine_zone_id {v!r} does not match a known marine zone prefix")
        return v


class MarineApplyConfig(BaseModel):
    """Top-level ``[marine]`` apply payload (T6.3).

    Additive/optional — omitting this field (or sending an empty
    ``locations`` list) leaves any existing ``[marine]`` section in api.conf
    untouched (see the apply handler: the section is only rewritten when
    ``apply.marine is not None``).
    """

    model_config = ConfigDict(extra="forbid")

    locations: list[MarineLocationApplyConfig] = []

    @field_validator("locations")
    @classmethod
    def _validate_unique_ids(
        cls, v: list[MarineLocationApplyConfig]
    ) -> list[MarineLocationApplyConfig]:
        ids = [loc.id for loc in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate marine location ids: {dupes}")
        return v


class SurfScoringWeightsApplyConfig(BaseModel):
    """System-wide surf score weights for the top-level ``[surf_scoring]``
    section of api.conf (ADR-101 Consequences; EYEBALL-FIX-PLAN-2026-08-04.md
    §S3, operator-approved 2026-08-04 "adr 101 approved").

    Whole-section-or-absent (unlike EarthquakeApplyConfig's per-field
    optionality): the five weights are always sent together or not at all —
    the admin form rejects zero/negative/malformed at the field level via
    ``gt=0``. Absent ``surf_scoring`` on the apply request leaves any
    existing ``[surf_scoring]`` section in api.conf untouched (same "None
    means don't touch this" convention as ``EarthquakeApplyConfig``/
    ``SocialApplyConfig``). Written TOP-LEVEL, not nested under
    ``[marine]``, so the existing replace-whole-``[marine]``-section save
    can never clobber it.
    """

    model_config = ConfigDict(extra="forbid")

    weight_size: float = Field(gt=0)
    weight_shape: float = Field(gt=0)
    weight_conditions: float = Field(gt=0)
    weight_power: float = Field(gt=0)
    weight_consistency: float = Field(gt=0)


class SwanApplyConfig(BaseModel):
    """``[swan]`` section for api.conf (T4.2 / T4.4 wizard).

    Written by the wizard's SWAN step.  All fields have defaults
    so existing wizard clients that don't send this block are unaffected.

    ``service_url`` is REMOVED (T7.2, LC-P7-3): ``marine_service_url`` +
    ``marine_verify_tls`` (top-level ``ApplyRequest`` fields, below) are
    the single connection the wizard now configures, replacing both the
    legacy ``surf_compute_host`` and this section's old ``service_url``.
    The rest of ``[swan]`` stays — the marine service's ported config
    loader still reads it today (API-MANUAL §19.5); folding it away
    entirely is a later phase.

    omp_num_threads:
      Number of OpenMP threads for SWAN.  0 = all available cores (default).
      Use a positive integer to limit SWAN's CPU usage on shared hosts.

    outer_grid_resolution_km:
      Outer SWAN grid resolution in kilometres (continental shelf approach
      domain).  Default 3.0 km.  Valid range: 1.0 – 10.0 km.  Coarser
      values reduce memory and runtime at the cost of shelf-scale accuracy.

    inner_nest_resolution_m:
      Inner nest SWAN grid resolution in metres (tight nearshore domain
      around surf spots).  Default 200 m.  Valid range: 50 – 1000 m.
      Finer resolution improves accuracy at higher CPU cost.
    """

    model_config = ConfigDict(extra="forbid")

    omp_num_threads: int = Field(default=0, ge=0)
    outer_grid_resolution_km: float = Field(default=3.0, ge=1.0, le=10.0)
    inner_nest_resolution_m: int = Field(default=200, ge=50, le=1000)


class UnitsApplyConfig(BaseModel):
    """Unit configuration for api.conf [units] (ADR-042).

    Each subsection mirrors the matching weewx unit-system concept:
    - groups: maps unit-group names to unit names (e.g. group_temperature → degree_F)
    - string_formats: maps unit names to printf-style format strings (e.g. degree_F → %.1f)
    - labels: maps unit names to display labels (e.g. degree_F → °F)
    - ordinates: ordered list of 16 compass-direction labels (N, NNE, … NNW)

    This is the single unit authority for the API (T2A.5).  On re-run the
    entire [units] section is replaced.
    """

    model_config = ConfigDict(extra="forbid")

    groups: dict[str, str] | None = None
    string_formats: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    ordinates: list[str] | None = None


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: DatabaseApplyConfig
    column_mapping: dict[str, str] = {}
    #: Operator-confirmed unit for each mapped column (e.g. ``outTemp`` →
    #: ``degree_F``).  Written to ``[column_units]`` in api.conf.  On re-run
    #: the entire section is replaced (stale entries from unmapped columns
    #: are not carried over).
    column_units: dict[str, str] = {}
    station: StationApplyConfig = StationApplyConfig()
    weewx_conf_path: str | None = None
    #: Provider configurations keyed by domain: "forecast", "aqi", "alerts",
    #: "radar", "earthquakes".  Each entry sets the provider id in api.conf and
    #: writes any credential to secrets.env using provider-scoped env var names.
    providers: dict[str, ProviderConfig] | None = None
    #: MQTT/realtime proxy shared secret.  Written to secrets.env as
    #: WEEWX_CLEARSKIES_PROXY_SECRET.
    proxy_secret: str | None = None
    #: Optional skin.conf payload.  When present, written to
    #: /etc/weewx/skins/ClearSkies/skin.conf (ADR-043).  Old wizard versions
    #: that do not send this field are unaffected (None → skip).
    skin_conf: dict[str, Any] | None = None
    #: Optional branding configuration.  When present, written to the
    #: [branding] section of api.conf.  Old wizard versions that do not send
    #: this field are unaffected (None → skip).
    branding: BrandingApplyConfig | None = None
    #: Optional social media URLs.  When present, written to the [social]
    #: section of api.conf.  None → skip.
    social: SocialApplyConfig | None = None
    #: Optional earthquake settings.  When present, written to the [earthquakes]
    #: section of api.conf.  None → skip (leaves existing values unchanged).
    earthquakes: EarthquakeApplyConfig | None = None
    #: Optional unit configuration.  When present, written to the [units]
    #: section of api.conf with subsections [[groups]], [[string_formats]],
    #: [[labels]], [[ordinates]].  This is the single unit authority (T2A.5).
    #: Old wizard versions that do not send this field are unaffected (None → skip).
    units: UnitsApplyConfig | None = None
    #: OpenAQ API key for calibration bootstrap and AQI provider.  Written to
    #: secrets.env as WEEWX_CLEARSKIES_OPENAQ_API_KEY.
    openaq_api_key: str | None = None
    #: Optional marine location configuration (T6.3).  When present, written to
    #: the [marine] section of api.conf (additive — a station with no marine
    #: locations configured behaves identically to a non-marine installation,
    #: per API-MANUAL §18 "Capability gating").
    marine: MarineApplyConfig | None = None
    #: Optional system-wide surf score weights (Round S, ADR-101). When
    #: present, written TOP-LEVEL to the [surf_scoring] section of api.conf
    #: (NOT nested under [marine]). None → skip (leaves any existing
    #: [surf_scoring] section unchanged, same convention as swan/earthquakes).
    surf_scoring: SurfScoringWeightsApplyConfig | None = None
    #: Optional SWAN configuration (T4.2 / T4.4 wizard).  When present,
    #: written to the [swan] section of api.conf.  None → skip (leaves any
    #: existing [swan] section unchanged, preserving manually-set values).
    swan: SwanApplyConfig | None = None
    # T6.8: surf_compute_host / surf_compute_secret / surf_compute_verify_tls
    # removed — marine_service_url is the single key that replaces the legacy
    # compute-offload connection (API-MANUAL §19.2).
    #: Base URL of the marine service (T7.2, LC-P7-3), e.g.
    #: ``https://localhost:8780``.  Written to api.conf [providers]
    #: marine_service_url.  None (the default) means no marine service is
    #: connected — replaces both the legacy surf_compute_host and the old
    #: [swan] service_url (API-MANUAL §19.2).
    marine_service_url: str | None = None
    #: Verify TLS certificate on marine service requests.  Written to
    #: api.conf [providers] marine_verify_tls.  Default True (secure
    #: default); set False for a self-signed cert on the same VLAN.
    marine_verify_tls: bool = True
    #: Bearer secret shared with the marine service.  Written to
    #: secrets.env as MARINE_SERVICE_SECRET — never to api.conf.  None →
    #: skip (leaves any existing secret unchanged, same "None means don't
    #: touch this credential" convention as openaq_api_key/proxy_secret
    #: below).
    marine_service_secret: str | None = None


class MarineConfigPushResult(BaseModel):
    """Outcome of the marine config push attempted at the end of /setup/apply
    (T7.6). Surfaced to the wizard/admin so a push failure is visible in the
    UI instead of only living in the API's own ERROR log — API-MANUAL §19.5's
    failure handling is unchanged by this: the push never fails the apply
    itself, this field only reports what happened."""

    #: False when marine_service_url is not configured — nothing was
    #: attempted, and ok/error are not meaningful.
    attempted: bool
    #: True when the marine service accepted the push (2xx). Only
    #: meaningful when attempted is True.
    ok: bool
    #: Human-readable failure reason when attempted is True and ok is
    #: False (unreachable, non-2xx, or MARINE_SERVICE_SECRET unset).
    error: str | None = None


class ApplyResponse(BaseModel):
    success: bool
    message: str
    #: One-time token valid for a single POST /setup/restart call within 60 s.
    #: Issued so the wizard can trigger a restart immediately after apply even
    #: before WEEWX_CLEARSKIES_PROXY_SECRET is loaded into the running process's
    #: environment (the secret was just written to secrets.env by this call).
    restart_token: str | None = None
    #: Outcome of the marine config push (T7.6). Always present —
    #: attempted=False (ok/error not meaningful) when no marine_service_url
    #: is configured, so the wizard doesn't need a separate "was it even
    #: attempted" check beyond that field.
    marine_config_push: MarineConfigPushResult


class RestartResponse(BaseModel):
    status: str


class CurrentConfigDatabaseSection(BaseModel):
    kind: str = "mysql"  # "sqlite" or "mysql"
    host: str
    port: int
    user: str
    password: str
    name: str
    path: str = ""       # SQLite only — path to .sdb file


class CurrentConfigProviderCredentials(BaseModel):
    """Credential fields for a single provider (all fields optional)."""

    client_id: str | None = None       # Aeris: AERIS_CLIENT_ID
    client_secret: str | None = None   # Aeris: AERIS_CLIENT_SECRET
    appid: str | None = None           # OpenWeatherMap: OPENWEATHERMAP_APPID
    key: str | None = None             # IQAir: IQAIR_KEY


class CurrentConfigProviderSection(BaseModel):
    provider: str
    credentials: CurrentConfigProviderCredentials
    librewxr_endpoint: str | None = None
    librewxr_bounds: str | None = None
    iframe_url: str | None = None
    aeris_forecast_model: str | None = None
    nws_user_agent_contact: str | None = None
    marine_alert_radius_miles: int | None = None
    marine_alert_zone_ids: list[str] | None = None


class CurrentConfigStationSection(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
    altitude_unit: str = "meter"
    timezone: str | None = None
    default_locale: str | None = None


class CurrentConfigBrandingSection(BaseModel):
    site_title: str = ""
    copyright_entity: str = ""
    logo_light_url: str = ""
    logo_dark_url: str = ""
    #: Alt text for the logo (WCAG 2.1 AA, ADR-022 §5.5).  Empty = not yet set.
    logo_alt: str = ""
    favicon_url: str = ""
    accent: str = ""
    default_theme_mode: str = ""
    custom_css_url: str = ""


class CurrentConfigSocialSection(BaseModel):
    facebook_url: str = ""
    twitter_url: str = ""
    instagram_url: str = ""
    youtube_url: str = ""


class CurrentConfigEarthquakeSection(BaseModel):
    """Earthquake-specific knobs from the [earthquakes] section of api.conf."""

    default_radius_km: float | None = None
    min_magnitude: float | None = None
    default_days: int | None = None


class CurrentConfigUnitsSection(BaseModel):
    """Unit configuration from the [units] section of api.conf (ADR-042).

    Mirrors UnitsApplyConfig: populated on re-run from api.conf so the
    wizard can pre-populate its unit-configuration step.
    """

    groups: dict[str, str] | None = None
    string_formats: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    ordinates: list[str] | None = None


class CurrentConfigResponse(BaseModel):
    database: CurrentConfigDatabaseSection
    providers: dict[str, CurrentConfigProviderSection]
    station: CurrentConfigStationSection
    branding: CurrentConfigBrandingSection = CurrentConfigBrandingSection()
    social: CurrentConfigSocialSection = CurrentConfigSocialSection()
    earthquakes: CurrentConfigEarthquakeSection = CurrentConfigEarthquakeSection()
    units: CurrentConfigUnitsSection | None = None
    column_mapping: dict[str, str] | None = None
    column_units: dict[str, str] | None = None
    openaq_api_key: str | None = None
    marine: dict[str, Any] | None = None
    #: System-wide surf score weights (Round S, ADR-101), read from the
    #: TOP-LEVEL [surf_scoring] section (not nested under marine above) so
    #: the admin form can pre-fill via this authoritative read path —
    #: split-host deployments must not read api.conf directly (marine
    #: admin code's own note). None when the section is absent/unconfigured
    #: (code defaults apply, same "absent means use defaults" convention as
    #: the apply side).
    surf_scoring: dict[str, float] | None = None
    #: Marine service connection (T7.2, LC-P7-3), so a wizard re-run
    #: pre-fills the operator's existing settings without re-entry. Same
    #: round-trip convention as openaq_api_key above — the raw secret is
    #: returned unmasked (CLAUDE.md "never hide operator secrets from the
    #: operator"; this endpoint already requires proxy auth to reach).
    marine_service_url: str | None = None
    marine_verify_tls: bool = True
    marine_service_secret: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Canonical provider name aliases.  The wizard (and other callers) may send
# variant names; these are normalised before being written to api.conf so that
# settings.py always sees the canonical form.
_PROVIDER_ALIASES: dict[str, str] = {
    "nws_alerts": "nws",
}


def _canonical_provider(name: str) -> str:
    """Return the canonical provider name, resolving any known aliases."""
    return _PROVIDER_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Heuristic unit-group suggestions for custom/extension columns (T2.4)
# ---------------------------------------------------------------------------

# Patterns are tried in order; first match wins.  Group names are validated
# against weewx.units.obs_group_dict values on the production weewx host.
_HEURISTIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)pm[_]?2[_.]?5"), "group_concentration"),
    (re.compile(r"(?i)pm[_]?10"), "group_concentration"),
    (re.compile(r"(?i)pm[_]?1(?!\d)"), "group_concentration"),
    (re.compile(r"(?i)(?:no2|so2|o3|co(?!ol|unt|n)|nh3)"), "group_fraction"),
    (re.compile(r"(?i)temp"), "group_temperature"),
    (re.compile(r"(?i)humid"), "group_percent"),
    (re.compile(r"(?i)press|barom"), "group_pressure"),
    (re.compile(r"(?i)rain(?!bow)"), "group_rain"),
    (re.compile(r"(?i)wind.*(?:speed|gust)"), "group_speed"),
    (re.compile(r"(?i)wind.*dir"), "group_direction"),
]


def _suggest_group(column_name: str) -> str | None:
    """Pattern-match a column name to a likely weewx unit group.

    Returns the first matching group name, or None if no pattern matches.
    Used as a lower-confidence fallback when weewx auto-detection (obs_group_dict)
    returns nothing for custom/extension columns.
    """
    for pattern, group in _HEURISTIC_PATTERNS:
        if pattern.search(column_name):
            return group
    return None


def _build_temp_mysql_url(host: str, port: int, user: str, password: str, name: str) -> str:
    """Build a pymysql URL for a one-shot test connection.

    IPv6 literals are wrapped in brackets per RFC 3986.
    """
    # Wrap IPv6 literals.
    try:
        stripped = host.strip("[]")
        addr = ipaddress.ip_address(stripped)
        if addr.version == 6:
            host_in_url = f"[{addr.compressed}]"
        else:
            host_in_url = addr.compressed
    except ValueError:
        host_in_url = host

    encoded_user = quote_plus(user)
    encoded_password = quote_plus(password)
    return (
        f"mysql+pymysql://{encoded_user}:{encoded_password}"
        f"@{host_in_url}:{port}/{name}"
        "?charset=utf8mb4"
    )


def _build_temp_sqlite_url(path: str) -> str:
    """Build a read-only SQLite URL for a one-shot test connection or schema reflection.

    Mirrors db/engine.py's _build_sqlite_url() (ADR-012) format exactly: the
    file: URI scheme is required for SQLAlchemy's MetaData.reflect() to work
    correctly with mode=ro — the simpler sqlite:////path format works for
    engine.connect()/queries but fails for reflection.
    """
    return f"sqlite+pysqlite:///file:///{path}?mode=ro&uri=true"


def _load_weewx_conf_for_setup(weewx_conf_path: str) -> configobj.ConfigObj | None:
    """Load weewx.conf for setup endpoints; return None on any error (non-fatal here)."""
    try:
        return load_weewx_conf(weewx_conf_path)
    except WeewxConfLoadError:
        return None


def _provider_secrets(domain: str, pc: ProviderConfig) -> dict[str, str]:
    """Return env var entries for a provider config, using the naming conventions
    from settings.py (provider-scoped, not domain-scoped per ADR-027 §3 deviation).

    Only non-empty credential values are included so existing secrets.env entries
    are not overwritten with empty strings.
    """
    secrets: dict[str, str] = {}
    p = _canonical_provider(pc.provider.lower())

    if p == "aeris":
        # Provider-scoped: same key works for forecast / alerts / aqi / radar.
        if pc.api_key:
            secrets["WEEWX_CLEARSKIES_AERIS_CLIENT_ID"] = pc.api_key
        if pc.api_secret:
            secrets["WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET"] = pc.api_secret

    elif p == "openweathermap":
        # Provider-scoped; long-form appid naming per 3b-5 user decision.
        if pc.api_key:
            secrets["WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID"] = pc.api_key

    elif p == "iqair":
        # Domain-scoped (AQI-only provider; Q1 user decision 2026-05-11).
        if pc.api_key:
            secrets["WEEWX_CLEARSKIES_IQAIR_KEY"] = pc.api_key

    # nws, openmeteo, rainviewer, iem_nexrad, noaa_mrms, msc_geomet, dwd_radolan,
    # usgs, geonet, emsc, renass, iframe — all keyless; no env vars to write.
    # (nws_user_agent_contact and iframe_url are non-secret; written to api.conf.)

    return secrets



def _build_marine_conf_section(
    marine: MarineApplyConfig,
) -> dict[str, Any]:
    """Build the ``[marine]`` configobj section dict from the validated apply payload.

    Mirrors config/marine_config.py's loader shape exactly (also documented in
    OPERATIONS-MANUAL.md "Marine location configuration"): ``surf`` / ``fishing``
    / ``beach_safety`` sub-blocks nest INSIDE each location's own
    ``[[[<id>]]]`` section as ``[[[[surf]]]]`` / ``[[[[fishing]]]]`` /
    ``[[[[beach_safety]]]]`` — there is no separate top-level
    ``[[surf_spots]]``/``[[fishing_spots]]`` section, and
    ``bathymetric_profile``/``structures`` are nested subsections (one per
    point/structure), not flat CSV strings. (T6.3 divergence from the round
    brief's illustrative api.conf example — config/marine_config.py's
    ``SurfSpotConfig.__init__``/``MarineLocation.__init__`` loader is
    authoritative per lead ruling 2026-07-10.) A ``[[weather]]`` section is
    intentionally NOT written: it is not part of MarineConfig's loader and has
    no runtime consumer (also lead-confirmed 2026-07-10) — MarineWeatherConfig's
    hardcoded defaults are used until an operator-configurable loader exists.
    """
    locations: dict[str, Any] = {}
    for loc in marine.locations:
        loc_section: dict[str, Any] = {
            "name": loc.name,
            "lat": str(loc.lat),
            "lon": str(loc.lon),
            "activities": list(loc.activities),
        }
        if loc.ndbc_station_ids:
            loc_section["ndbc_station_ids"] = list(loc.ndbc_station_ids)
        if loc.coops_station_ids:
            loc_section["coops_station_ids"] = list(loc.coops_station_ids)
        if loc.nws_marine_zone_id:
            loc_section["nws_marine_zone_id"] = loc.nws_marine_zone_id

        # C-42: OFS model annotation is config-write-time, not a live
        # wizard query the operator is looking at — a marine-service outage
        # here must not abort the whole /setup/apply (matches
        # _push_marine_service_config()'s "a marine outage never blocks
        # setup" contract). Left unset on failure, same effective result as
        # "no OFS coverage" before this was a network call; the marine
        # service can resolve OFS coverage itself from lat/lon when it
        # actually needs ocean data.
        try:
            ofs_primary, ofs_fallback = _discover_ofs_model(loc.lat, loc.lon)
        except MarineDiscoveryError as exc:
            logger.warning(
                "Marine config build: OFS model discovery failed for location %r: %s",
                loc.name, exc,
            )
            ofs_primary, ofs_fallback = None, None
        if ofs_primary:
            loc_section["ofs_model"] = ofs_primary
        if ofs_fallback:
            loc_section["ofs_fallback"] = ofs_fallback

        if loc.surf is not None:
            surf = loc.surf
            surf_section: dict[str, Any] = {
                # Shoreline segment (T2.1) — beach_facing_degrees is computed
                # at load time from segment geometry, NOT written to api.conf.
                "segment_start_lat": str(surf.segment_start_lat),
                "segment_start_lon": str(surf.segment_start_lon),
                "segment_end_lat": str(surf.segment_end_lat),
                "segment_end_lon": str(surf.segment_end_lon),
                "bottom_type": surf.bottom_type,
                "topographic_feature": surf.topographic_feature,
                # SwellTrack friction (SURF-MODEL-FIX-PLAN T5.2).
                "friction_coefficient": str(surf.friction_coefficient),
                # SurfBeat IG strip config (SURF-MODEL-FIX-PLAN T5.1 / T5.2).
                "surfbeat_enabled": str(surf.surfbeat_enabled).lower(),
                "surfbeat_cadence_hours": str(surf.surfbeat_cadence_hours),
            }
            # Config-file-owned surf fields (operator ruling 2026-08-03,
            # decision item 8): the SAVING surface may legitimately omit
            # these (max_hs_m/beach_slope have no UI on either surface;
            # breaker_formula/surf_height_display live on the TruShore
            # step, not the marine page; transect_spacing_m/l3_enabled have
            # admin UI that omits-when-default — aud-r1 F2 truth-fix), so they
            # are written only when the payload explicitly supplies a value.
            # Absence here is visible to the marine merge block, which
            # preserves the existing api.conf value (or applies today's
            # default for the three that carried one).
            if surf.transect_spacing_m is not None:
                surf_section["transect_spacing_m"] = str(surf.transect_spacing_m)
            if surf.breaker_formula is not None:
                surf_section["breaker_formula"] = surf.breaker_formula
            if surf.surf_height_display is not None:
                surf_section["surf_height_display"] = surf.surf_height_display
            if surf.l3_enabled is not None:
                surf_section["l3_enabled"] = surf.l3_enabled
            if surf.max_hs_m is not None:
                surf_section["max_hs_m"] = str(surf.max_hs_m)
            if surf.directional_exposure:
                surf_section["directional_exposure"] = [
                    f"{direction}:{str(value).lower()}"
                    for direction, value in surf.directional_exposure.items()
                ]
            # Operator-supplied bathymetric profile (optional).
            profile_points = surf.bathymetric_profile
            if profile_points:
                surf_section["bathymetric_profile"] = {
                    str(i): {"distance_m": str(p.distance_m), "depth_m": str(p.depth_m)}
                    for i, p in enumerate(profile_points)
                }
            if surf.structures:
                structures_section: dict[str, Any] = {}
                for i, s in enumerate(surf.structures):
                    struct_entry: dict[str, Any] = {
                        "type": s.type,
                        "material": s.material,
                        "length_m": str(s.length_m),
                        "bearing_degrees": str(s.bearing_degrees),
                        "distance_m": str(s.distance_m),
                    }
                    # E13 / ADR-095 Decision 3: discovery outline, JSON-string
                    # encoded — configobj cannot round-trip a native nested
                    # list into a shape the marine reader's
                    # ``[float(c[0]), float(c[1])] for c in ...]`` can parse
                    # (empirically verified: configobj yields a list of
                    # per-element strings, not nested lists).
                    if s.coordinates:
                        struct_entry["coordinates"] = json.dumps(
                            [[float(c[0]), float(c[1])] for c in s.coordinates]
                        )
                    structures_section[str(i)] = struct_entry
                surf_section["structures"] = structures_section
            loc_section["surf"] = surf_section

        if loc.fishing is not None:
            fishing_section: dict[str, Any] = {
                "target_categories": list(loc.fishing.target_categories)
            }
            if loc.fishing.biogeographic_region:
                fishing_section["biogeographic_region"] = loc.fishing.biogeographic_region
            if loc.fishing.species:
                fishing_section["species"] = list(loc.fishing.species)
            loc_section["fishing"] = fishing_section

        if loc.beach_safety is not None and loc.beach_safety.external_links:
            loc_section["beach_safety"] = {
                "external_links": {
                    link.label: {"label": link.label, "url": link.url}
                    for link in loc.beach_safety.external_links
                }
            }

        locations[loc.id] = loc_section

    return {"locations": locations}


# ---------------------------------------------------------------------------
# Marine service config push/pull (T6.4 / T6.4b)
#
# One serializer, both paths (API-MANUAL.md §19.5): _build_marine_service_config_payload()
# is called both by the POST {marine_service_url}/config push at the end of
# /setup/apply and by GET /setup/marine/config (the marine service's own
# startup recovery pull). Both read the SAME source — the api.conf just
# persisted to config_dir — rather than the in-memory ApplyRequest body, so
# a pull days later returns byte-identical results to the push that
# accompanied the apply call that produced the current on-disk state. This
# also sidesteps a subtlety in _write_api_conf(): the [marine] section merge
# there preserves station IDs from the prior config that the new request
# didn't send, so the *persisted* config can differ from the raw request body.
# ---------------------------------------------------------------------------


def _cfg_opt_float(section: dict[str, Any], key: str) -> float | None:
    """Return a float from a configobj section value, or None if absent/blank."""
    raw = section.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    return float(raw)


def _cfg_float(section: dict[str, Any], key: str, default: float) -> float:
    value = _cfg_opt_float(section, key)
    return default if value is None else value


def _cfg_int(section: dict[str, Any], key: str, default: int) -> int:
    raw = section.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(float(raw))


def _cfg_bool(section: dict[str, Any], key: str, default: bool) -> bool:
    raw = section.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def _cfg_opt_str(section: dict[str, Any], key: str) -> str | None:
    raw = section.get(key)
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _serialize_directional_exposure(raw: Any) -> dict[str, bool] | None:
    """Convert api.conf's on-disk ``directional_exposure`` storage format —
    a list of ``"DIR:bool"`` strings, the shape ``configobj`` produces for a
    flat multi-value key — into the pinned wire shape: a JSON-native dict
    (``{"N": false, "NE": false, ...}``).

    C-23 decision: the wire payload always uses the dict shape, because the
    payload is JSON and a dict of 8 known keys is the natural JSON
    representation of "one bool per compass direction" — no reader has to
    know a delimiter convention to parse it. api.conf's own on-disk format
    is unrelated and unchanged by this (that's an INI-family storage
    concern, not the API<->marine wire contract this function serialises
    for). The marine service's parser accordingly only accepts the dict
    shape now (config/marine_config.py's list-of-"DIR:bool" tolerance
    branch was deleted as unused — rules/coding.md §3).
    """
    if not raw:
        return None
    items = raw if isinstance(raw, list) else [raw]
    result: dict[str, bool] = {}
    for item in items:
        item_str = str(item).strip()
        if ":" not in item_str:
            continue
        direction, _, value = item_str.partition(":")
        direction = direction.strip()
        if direction:
            result[direction] = str(value).strip().lower() in ("true", "1", "yes")
    return result or None


def _serialize_marine_locations_section(marine_section: dict[str, Any]) -> dict[str, Any]:
    """Build the ``marine.locations`` subtree of the push/pull payload from
    api.conf's persisted ``[marine][[locations]]`` section.

    Field-for-field mirror of what ``config/marine_config.py``'s
    ``MarineLocation`` / ``SurfSpotConfig`` / ``FishingSpotConfig`` /
    ``BeachSafetyConfig`` loaders on the marine side actually read (verified
    against the ported copy in repos/weewx-clearskies-marine) — no field is
    added here that has no consumer there, and no field the marine loader
    reads is left out. ``[[weather]]`` is intentionally never written (see
    ``_build_marine_conf_section`` docstring) so it is not serialised here
    either — the marine side's ``MarineWeatherConfig`` defaults apply.

    Station timezone and elevation are deliberately NOT included — the
    marine service's fishing endpoint has no consumer for them yet
    (MARINE-SEP-CONCERNS.md C-27, awaiting operator decision).
    """
    raw_locations = marine_section.get("locations", {})
    if not isinstance(raw_locations, dict):
        return {}

    locations: dict[str, Any] = {}
    for loc_id, raw_loc in raw_locations.items():
        if not isinstance(raw_loc, dict):
            continue

        loc: dict[str, Any] = {
            "name": str(raw_loc.get("name", "")).strip(),
            "lat": _cfg_float(raw_loc, "lat", 0.0),
            "lon": _cfg_float(raw_loc, "lon", 0.0),
        }
        raw_activities = raw_loc.get("activities", [])
        loc["activities"] = (
            list(raw_activities) if isinstance(raw_activities, list)
            else ([str(raw_activities)] if raw_activities else [])
        )
        for list_key in ("ndbc_station_ids", "coops_station_ids"):
            raw_val = raw_loc.get(list_key, [])
            if isinstance(raw_val, list):
                loc[list_key] = list(raw_val)
            elif raw_val:
                loc[list_key] = [str(raw_val)]
        for opt_key in ("nws_marine_zone_id", "nws_srf_zone_id", "nws_srf_wfo", "ofs_model",
                        "ofs_fallback", "ofs_region"):
            val = _cfg_opt_str(raw_loc, opt_key)
            if val is not None:
                loc[opt_key] = val

        raw_surf = raw_loc.get("surf")
        if isinstance(raw_surf, dict):
            surf: dict[str, Any] = {
                "segment_start_lat": _cfg_float(raw_surf, "segment_start_lat", 0.0),
                "segment_start_lon": _cfg_float(raw_surf, "segment_start_lon", 0.0),
                "segment_end_lat": _cfg_float(raw_surf, "segment_end_lat", 0.0),
                "segment_end_lon": _cfg_float(raw_surf, "segment_end_lon", 0.0),
                "transect_spacing_m": _cfg_float(raw_surf, "transect_spacing_m", 10.0),
                "bottom_type": str(raw_surf.get("bottom_type", "")).strip(),
                "topographic_feature": str(raw_surf.get("topographic_feature", "")).strip(),
                "breaker_formula": str(raw_surf.get("breaker_formula", "komar_gaughan")).strip(),
                "surf_height_display": str(raw_surf.get("surf_height_display", "face")).strip(),
                "l3_enabled": str(raw_surf.get("l3_enabled", "auto")).strip().lower(),
                "friction_coefficient": _cfg_float(raw_surf, "friction_coefficient", 0.038),
                "surfbeat_enabled": _cfg_bool(raw_surf, "surfbeat_enabled", True),
                "surfbeat_cadence_hours": _cfg_int(raw_surf, "surfbeat_cadence_hours", 3),
                "max_hs_m": _cfg_float(raw_surf, "max_hs_m", 4.0),
            }
            beach_slope = _cfg_opt_float(raw_surf, "beach_slope")
            if beach_slope is not None:
                surf["beach_slope"] = beach_slope
            directional_exposure = _serialize_directional_exposure(
                raw_surf.get("directional_exposure")
            )
            if directional_exposure is not None:
                surf["directional_exposure"] = directional_exposure

            raw_bathy = raw_surf.get("bathymetric_profile")
            if isinstance(raw_bathy, dict) and raw_bathy:
                surf["bathymetric_profile"] = {
                    idx: {
                        "distance_m": _cfg_float(pt, "distance_m", 0.0),
                        "depth_m": _cfg_float(pt, "depth_m", 0.0),
                    }
                    for idx, pt in raw_bathy.items() if isinstance(pt, dict)
                }

            raw_structures = raw_surf.get("structures")
            if isinstance(raw_structures, dict) and raw_structures:
                structures: dict[str, Any] = {}
                for idx, s in raw_structures.items():
                    if not isinstance(s, dict):
                        continue
                    entry: dict[str, Any] = {
                        "type": str(s.get("type", "")).strip(),
                        "material": str(s.get("material", "")).strip(),
                        "length_m": _cfg_float(s, "length_m", 0.0),
                        "bearing_degrees": _cfg_float(s, "bearing_degrees", 0.0),
                        "distance_m": _cfg_float(s, "distance_m", 0.0),
                    }
                    raw_coords = s.get("coordinates")
                    # E13 / ADR-095 Decision 3: on-disk value is the
                    # JSON-string _build_marine_conf_section() writes;
                    # decode it before the shape check below. The
                    # isinstance(list) branch is a tolerant fallback for an
                    # already-native list (e.g. a hand-built section).
                    if isinstance(raw_coords, str) and raw_coords.strip():
                        raw_coords = json.loads(raw_coords)
                    if isinstance(raw_coords, list) and raw_coords:
                        entry["coordinates"] = [
                            [float(c[0]), float(c[1])] for c in raw_coords
                        ]
                    structures[idx] = entry
                surf["structures"] = structures

            loc["surf"] = surf

        raw_fishing = raw_loc.get("fishing")
        if isinstance(raw_fishing, dict):
            raw_categories = raw_fishing.get("target_categories", [])
            categories = (
                list(raw_categories) if isinstance(raw_categories, list)
                else ([str(raw_categories)] if raw_categories else [])
            )
            fishing: dict[str, Any] = {"target_categories": categories}
            region = _cfg_opt_str(raw_fishing, "biogeographic_region")
            if region is not None:
                fishing["biogeographic_region"] = region
            raw_species = raw_fishing.get("species", [])
            if isinstance(raw_species, list) and raw_species:
                fishing["species"] = list(raw_species)
            elif raw_species:
                fishing["species"] = [str(raw_species)]
            loc["fishing"] = fishing

        raw_beach_safety = raw_loc.get("beach_safety")
        if isinstance(raw_beach_safety, dict):
            raw_links = raw_beach_safety.get("external_links")
            if isinstance(raw_links, dict) and raw_links:
                loc["beach_safety"] = {
                    "external_links": {
                        key: {
                            "label": str(link.get("label", "")).strip(),
                            "url": str(link.get("url", "")).strip(),
                        }
                        for key, link in raw_links.items() if isinstance(link, dict)
                    }
                }

        locations[loc_id] = loc

    return {"locations": locations}


def _serialize_swan_section(swan_section: dict[str, Any]) -> dict[str, Any]:
    """Build the top-level ``swan`` payload key from api.conf's ``[swan]``
    section — every field ``config/marine_config.py``'s ``SwanConfig``
    reads on the marine side (still a live consumer today; ADR-099's
    replacement of this section with ``marine_service_url`` alone is a
    later Phase 7 task, not this one).

    ``service_url`` is REMOVED from this payload (T7.2, LC-P7-3):
    ``marine_service_url`` (top-level, ``_build_marine_service_config_payload``
    does not carry it either — it is a connection detail the marine service
    doesn't need to know about itself) is the single connection key now.
    Verified safe on the marine side: ``SwanConfig.service_url`` defaults to
    the bundled sentinel when the key is absent, and ``is_remote`` has no
    consumer anywhere in the marine service beyond its own ``validate()`` —
    so an absent key is the correct "SWAN runs here" state.
    """
    payload: dict[str, Any] = {
        "omp_num_threads": _cfg_int(swan_section, "omp_num_threads", 0),
        "outer_grid_resolution_km": _cfg_float(swan_section, "outer_grid_resolution_km", 3.0),
        "inner_nest_resolution_m": _cfg_float(swan_section, "inner_nest_resolution_m", 200.0),
    }
    # verify_tls: only api.conf keys _write_api_conf() actually writes are
    # read here; it does not currently write [swan] verify_tls, so this is
    # normally absent and the marine-side SwanConfig applies its own
    # documented default (True). Included when present so an operator who
    # hand-edits api.conf still has it honoured.
    if "verify_tls" in swan_section:
        payload["verify_tls"] = _cfg_bool(swan_section, "verify_tls", True)
    return payload


def _build_marine_service_config_payload(config_dir: Path) -> dict[str, Any]:
    """Build the marine service config push/pull payload from the persisted
    api.conf (T6.4 push, T6.4b pull — API-MANUAL.md §19.5 "one serializer,
    both paths").

    Returns an empty dict when api.conf does not exist yet, or has neither
    a ``[marine]`` nor a ``[swan]`` nor a ``[providers]`` compute-offload
    section — i.e. nothing marine-relevant has ever been configured.

    When ``[marine]`` locations exist, also attaches ``"station": {"lat",
    "lon"}`` from the weewx station's own metadata (C-51) — best-effort,
    omitted rather than defaulted when station metadata isn't loaded.
    """
    conf_path = config_dir / "api.conf"
    if not conf_path.exists():
        return {}
    try:
        cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
    except Exception:  # noqa: BLE001
        logger.error("Failed to parse api.conf while building marine service config payload")
        return {}

    payload: dict[str, Any] = {}

    marine_section = cfg.get("marine")
    if isinstance(marine_section, dict):
        payload["marine"] = _serialize_marine_locations_section(marine_section)

        # C-51: the marine service's is_station_served() spatial test (used
        # by C-47's t=0 station-wind fetch) needs the weewx station's own
        # coordinates to know which marine locations fall within
        # dedup_radius_km. Best-effort — station metadata is normally
        # loaded at API startup and this call happens well after that, but
        # this serializer must not assume it (it is also invoked from
        # GET /setup/marine/config, a plain request handler). Absence is a
        # normal state, never an error and never a substituted default
        # (0,0 is a real ocean coordinate): the marine side treats a
        # missing "station" key exactly like every location being outside
        # dedup_radius_km — every location takes the forecast-provider
        # path, same as before this change.
        try:
            from weewx_clearskies_api.services.station import get_station_info  # noqa: PLC0415

            station_info = get_station_info()
            payload["station"] = {"lat": station_info.latitude, "lon": station_info.longitude}
        except RuntimeError:
            logger.warning(
                "Station metadata not loaded while building marine service config "
                "payload — omitting station coordinates (is_station_served() will "
                "treat every location as not station-served until the next push)"
            )

    swan_section = cfg.get("swan")
    if isinstance(swan_section, dict):
        payload["swan"] = _serialize_swan_section(swan_section)

    # [surf_scoring] — Round S, ADR-101. TOP-LEVEL section (not nested
    # under [marine]), attached independently of the marine_section check
    # above so a location-less install can still carry operator-adjusted
    # weights. One serializer, both paths (push and pull, per the module
    # docstring) — this is the ONE call site that attaches it.
    surf_scoring_section = cfg.get("surf_scoring")
    if isinstance(surf_scoring_section, dict):
        payload["surf_scoring"] = {
            "weight_size": _cfg_float(surf_scoring_section, "weight_size", 0.0),
            "weight_shape": _cfg_float(surf_scoring_section, "weight_shape", 0.0),
            "weight_conditions": _cfg_float(surf_scoring_section, "weight_conditions", 0.0),
            "weight_power": _cfg_float(surf_scoring_section, "weight_power", 0.0),
            "weight_consistency": _cfg_float(surf_scoring_section, "weight_consistency", 0.0),
        }

    # T6.8: the legacy [providers] surf_compute_host / surf_compute_verify_tls
    # compute-offload keys are removed from the push payload — marine_service_url
    # is the single key that replaces them (API-MANUAL §19.2).

    return payload


def _read_marine_service_connection(config_dir: Path) -> tuple[str | None, bool]:
    """Return ``(marine_service_url, marine_verify_tls)`` from the persisted
    api.conf ``[providers]`` section. ``marine_service_url`` is ``None`` when
    unconfigured (marine service disabled — T6.4 does nothing in that case).
    ``marine_verify_tls`` defaults to ``True`` (secure default; documented in
    OPERATIONS-MANUAL.md "Marine service TLS")."""
    conf_path = config_dir / "api.conf"
    if not conf_path.exists():
        return None, True
    try:
        cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
    except Exception:  # noqa: BLE001
        return None, True
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return None, True
    url = _cfg_opt_str(providers, "marine_service_url")
    verify_tls = _cfg_bool(providers, "marine_verify_tls", True)
    return url, verify_tls


async def _push_marine_service_config(config_dir: Path) -> MarineConfigPushResult:
    """POST the marine config subset to the marine service (T6.4).

    Called from /setup/apply after api.conf AND secrets.env have both been
    written (T7.6 reordered this call to run after the secrets.env write —
    previously it ran first, which meant a MARINE_SERVICE_SECRET supplied
    in the *same* apply request via ``marine_service_secret`` was not yet
    in ``os.environ`` when the push fired, making the wizard's first-ever
    apply always report "secret not set" even though it had just sent one.
    This is a same-request-visibility ordering fix, not a new capability —
    the secret still only ever lands in secrets.env / os.environ, never
    api.conf).

    Never raises: an unreachable or erroring marine service logs ERROR and
    the apply still succeeds (API-MANUAL.md §19.5 "config push failure
    handling" — the marine service picks the config up on the next apply,
    or via its own startup recovery pull, T6.4b). The outcome is returned
    (T7.6, not just logged) so /setup/apply can surface it to the wizard —
    see ``ApplyResponse.marine_config_push``.
    """
    marine_service_url, verify_tls = _read_marine_service_connection(config_dir)
    if not marine_service_url:
        return MarineConfigPushResult(attempted=False, ok=False)

    secret = os.environ.get("MARINE_SERVICE_SECRET", "").strip()
    if not secret:
        error = (
            "marine_service_url is configured but MARINE_SERVICE_SECRET is not set "
            "in the environment; cannot push marine config"
        )
        logger.error(error)
        return MarineConfigPushResult(attempted=True, ok=False, error=error)

    payload = _build_marine_service_config_payload(config_dir)
    push_url = marine_service_url.rstrip("/") + "/config"

    import httpx  # noqa: PLC0415 — lazy import; mirrors providers_test_marine()

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=verify_tls) as client:
            resp = await client.post(
                push_url,
                json=payload,
                headers={"Authorization": f"Bearer {secret}"},
            )
    except httpx.HTTPError as exc:
        error = f"Failed to reach {push_url}: {type(exc).__name__}"
        logger.error("Failed to push marine config to %s: %s", push_url, type(exc).__name__)
        return MarineConfigPushResult(attempted=True, ok=False, error=error)

    if resp.status_code >= 400:
        error = f"Marine service rejected config push (HTTP {resp.status_code})"
        logger.error(
            "Marine service rejected config push to %s (HTTP %d)",
            push_url, resp.status_code,
        )
        return MarineConfigPushResult(attempted=True, ok=False, error=error)

    return MarineConfigPushResult(attempted=True, ok=True)


def _write_api_conf(
    config_dir: Path,
    apply: ApplyRequest,
) -> None:
    """Write (or update) api.conf in config_dir with non-secret settings from apply."""
    conf_path = config_dir / "api.conf"

    # Load existing config if present so we preserve other sections.
    if conf_path.exists():
        cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
    else:
        cfg = configobj.ConfigObj(interpolation=False)
        cfg.filename = str(conf_path)

    # [database] — non-secret fields only; password goes to secrets.env.
    if "database" not in cfg:
        cfg["database"] = {}
    cfg["database"]["kind"] = apply.database.kind
    if apply.database.kind == "sqlite":
        cfg["database"]["path"] = apply.database.path
    else:
        cfg["database"]["host"] = apply.database.host
        cfg["database"]["port"] = str(apply.database.port)
        cfg["database"]["name"] = apply.database.name

    # [weewx] — config_path if supplied.
    if apply.weewx_conf_path:
        if "weewx" not in cfg:
            cfg["weewx"] = {}
        cfg["weewx"]["config_path"] = apply.weewx_conf_path

    # [station] — non-secret station overrides.
    st = apply.station
    if "station" not in cfg:
        cfg["station"] = {}
    if st.timezone:
        cfg["station"]["timezone"] = st.timezone
    # Also persist the other station identity fields so /setup/current-config can
    # return them on re-run without having to parse weewx.conf again.
    if st.name:
        cfg["station"]["name"] = st.name
    if st.latitude is not None:
        cfg["station"]["latitude"] = str(st.latitude)
    if st.longitude is not None:
        cfg["station"]["longitude"] = str(st.longitude)
    if st.altitude_meters is not None:
        cfg["station"]["altitude_meters"] = str(st.altitude_meters)
    if st.default_locale:
        cfg["station"]["default_locale"] = st.default_locale

    # [column_mapping] — operator-supplied canonical → archive column pairs.
    # Replace the entire section so removed mappings don't persist from prior runs.
    if apply.column_mapping is not None:
        cfg["column_mapping"] = dict(apply.column_mapping)

    # [column_units] — operator-confirmed unit for each mapped column.
    # Replace the entire section so stale units from columns that were unmapped
    # on re-run don't persist (same pattern as column_mapping above).
    if apply.column_units is not None:
        cfg["column_units"] = dict(apply.column_units)

    # [forecast] / [aqi] / [alerts] / [radar] / [earthquakes] — non-secret provider
    # fields only.  Credentials are written to secrets.env by the apply handler.
    if apply.providers:
        for domain, pc in apply.providers.items():
            section = domain.lower()
            if section not in cfg:
                cfg[section] = {}
            cfg[section]["provider"] = _canonical_provider(pc.provider)

            # NWS contact email/URL (non-secret; stored in api.conf per settings.py).
            # Valid for forecast and alerts domains.
            if pc.nws_user_agent_contact and section in ("forecast", "alerts"):
                cfg[section]["nws_user_agent_contact"] = pc.nws_user_agent_contact

            # Radar iframe URL (non-secret; stored in api.conf per settings.py).
            if pc.iframe_url and section == "radar":
                cfg[section]["iframe_url"] = pc.iframe_url

            # LibreWxR endpoint and bounds (non-secret; api.conf [radar]).
            if pc.librewxr_endpoint and section == "radar":
                cfg[section]["librewxr_endpoint"] = pc.librewxr_endpoint
            if pc.librewxr_bounds and section == "radar":
                cfg[section]["librewxr_bounds"] = pc.librewxr_bounds

            # Aeris forecast model (ADR-063; non-secret).
            if pc.aeris_forecast_model and section == "forecast":
                cfg[section]["aeris_forecast_model"] = pc.aeris_forecast_model

            # Marine alert radius + zone discovery (ADR-089; alerts domain only).
            if section == "alerts" and pc.marine_alert_radius_miles is not None:
                cfg[section]["marine_alert_radius_miles"] = str(pc.marine_alert_radius_miles)
                if pc.marine_alert_radius_miles > 0:
                    from weewx_clearskies_api.providers._common.nws_zones import (  # noqa: PLC0415
                        discover_marine_zones,
                    )

                    station_cfg = apply.station
                    if station_cfg.latitude and station_cfg.longitude:
                        try:
                            zones = discover_marine_zones(
                                station_cfg.latitude,
                                station_cfg.longitude,
                                radius_miles=float(pc.marine_alert_radius_miles),
                            )
                            zone_ids = [z["zone_id"] for z in zones]
                            cfg[section]["marine_alert_zone_ids"] = ", ".join(zone_ids)
                            logger.info(
                                "Marine zone discovery: %d zone(s) within %d miles",
                                len(zone_ids),
                                pc.marine_alert_radius_miles,
                            )
                        except Exception:
                            logger.warning(
                                "Marine zone discovery failed; marine_alert_zone_ids not set",
                                exc_info=True,
                            )
                else:
                    cfg[section]["marine_alert_zone_ids"] = ""

    # [branding] — optional; only written when wizard sends this block.
    if apply.branding is not None:
        if "branding" not in cfg:
            cfg["branding"] = {}
        br = apply.branding
        if br.site_title is not None:
            cfg["branding"]["site_title"] = br.site_title
        if br.copyright_entity is not None:
            cfg["branding"]["copyright_entity"] = br.copyright_entity
        if br.logo_light_url is not None:
            cfg["branding"]["logo_light_url"] = br.logo_light_url
        if br.logo_dark_url is not None:
            cfg["branding"]["logo_dark_url"] = br.logo_dark_url
        if br.logo_alt is not None:
            cfg["branding"]["logo_alt"] = br.logo_alt
        if br.favicon_url is not None:
            cfg["branding"]["favicon_url"] = br.favicon_url
        if br.accent is not None:
            cfg["branding"]["accent"] = br.accent
        if br.default_theme_mode is not None:
            cfg["branding"]["default_theme_mode"] = br.default_theme_mode
        if br.custom_css_url is not None:
            cfg["branding"]["custom_css_url"] = br.custom_css_url

    # [social] — optional; only written when wizard sends this block.
    if apply.social is not None:
        if "social" not in cfg:
            cfg["social"] = {}
        so = apply.social
        if so.facebook_url is not None:
            cfg["social"]["facebook_url"] = so.facebook_url
        if so.twitter_url is not None:
            cfg["social"]["twitter_url"] = so.twitter_url
        if so.instagram_url is not None:
            cfg["social"]["instagram_url"] = so.instagram_url
        if so.youtube_url is not None:
            cfg["social"]["youtube_url"] = so.youtube_url

    # [earthquakes] — optional seismic knobs; only written when wizard sends this block.
    # Provider id is handled by the providers dict above; these are the extra knobs.
    if apply.earthquakes is not None:
        if "earthquakes" not in cfg:
            cfg["earthquakes"] = {}
        eq = apply.earthquakes
        if eq.default_radius_km is not None:
            cfg["earthquakes"]["default_radius_km"] = str(eq.default_radius_km)
        if eq.min_magnitude is not None:
            cfg["earthquakes"]["min_magnitude"] = str(eq.min_magnitude)
        if eq.default_days is not None:
            cfg["earthquakes"]["default_days"] = str(eq.default_days)

    # [units] — optional; written when wizard sends unit configuration.
    # Subsections mirror weewx skin.conf [Units] structure (ADR-042).
    # On re-run the entire [units] section is replaced so stale group
    # overrides from prior runs don't persist.
    if apply.units is not None:
        cfg["units"] = {}
        u = apply.units
        if u.groups is not None:
            cfg["units"]["groups"] = dict(u.groups)
        if u.string_formats is not None:
            cfg["units"]["string_formats"] = dict(u.string_formats)
        if u.labels is not None:
            cfg["units"]["labels"] = dict(u.labels)
        if u.ordinates is not None:
            cfg["units"]["ordinates"] = list(u.ordinates)

    # [marine] — optional; only written when the wizard sends this block (T6.3).
    # Replace-whole-section on re-run so stale locations don't persist, but
    # preserve station IDs (ndbc_station_ids, coops_station_ids,
    # nws_marine_zone_id) from the existing config when the new payload
    # doesn't include them — the wizard has no UI for these fields, so a
    # re-run would silently drop manually-configured station IDs without
    # this merge.
    if apply.marine is not None:
        new_marine = _build_marine_conf_section(apply.marine)
        existing_marine = cfg.get("marine")
        if isinstance(existing_marine, dict):
            existing_locs = existing_marine.get("locations", {})
            if isinstance(existing_locs, dict):
                _PRESERVE_KEYS = ("ndbc_station_ids", "coops_station_ids", "nws_marine_zone_id")
                # Surf-level fields the saving surface may omit are config-file-
                # owned (operator ruling 2026-08-03, decision item 8):
                # preserved from the existing config when absent from the
                # payload. Payload-present still wins; a location removed
                # from the payload stays removed (no resurrection of any of
                # its keys).
                _SURF_PRESERVE_KEYS = (
                    "max_hs_m",
                    "beach_slope",
                    "transect_spacing_m",
                    "l3_enabled",
                    "breaker_formula",
                    "surf_height_display",
                )
                for loc_id, new_loc in new_marine.get("locations", {}).items():
                    old_loc = existing_locs.get(loc_id)
                    if isinstance(old_loc, dict):
                        for key in _PRESERVE_KEYS:
                            if key not in new_loc and key in old_loc:
                                new_loc[key] = old_loc[key]
                        new_surf = new_loc.get("surf")
                        old_surf = old_loc.get("surf")
                        if isinstance(new_surf, dict) and isinstance(old_surf, dict):
                            for key in _SURF_PRESERVE_KEYS:
                                if key not in new_surf and key in old_surf:
                                    new_surf[key] = old_surf[key]
        # Fresh-install fallback: a surf field absent from both the payload
        # and the existing config still gets today's documented default for
        # every field that carried a hard model default before this change
        # (max_hs_m, transect_spacing_m, l3_enabled, breaker_formula,
        # surf_height_display) — so a brand-new location's api.conf write is
        # byte-identical to pre-change output (aud-r1 F1: the first cut
        # omitted breaker_formula/surf_height_display, which pre-change code
        # always wrote explicitly). beach_slope alone has no write-time
        # default — pre-change code never wrote it either (it was never on
        # the model); absence falls through to the read-time default in
        # _serialize_marine_locations_section.
        _SURF_FRESH_DEFAULTS = {
            "max_hs_m": "4.0",
            "transect_spacing_m": "10.0",
            "l3_enabled": "auto",
            "breaker_formula": "komar_gaughan",
            "surf_height_display": "face",
        }
        for new_loc in new_marine.get("locations", {}).values():
            new_surf = new_loc.get("surf")
            if isinstance(new_surf, dict):
                for key, default_val in _SURF_FRESH_DEFAULTS.items():
                    if key not in new_surf:
                        new_surf[key] = default_val
        cfg["marine"] = new_marine

    # [surf_scoring] — optional; TOP-LEVEL (Round S, ADR-101), NOT nested
    # under [marine] -- written independently of the [marine] block above
    # so the existing replace-whole-[marine]-section save can never clobber
    # it. Whole-section-or-absent: SurfScoringWeightsApplyConfig requires
    # all five weights together, so there is no partial-section merge case
    # to handle here (unlike [earthquakes]' per-field optionality).
    if apply.surf_scoring is not None:
        ss = apply.surf_scoring
        cfg["surf_scoring"] = {
            "weight_size": str(ss.weight_size),
            "weight_shape": str(ss.weight_shape),
            "weight_conditions": str(ss.weight_conditions),
            "weight_power": str(ss.weight_power),
            "weight_consistency": str(ss.weight_consistency),
        }

    # [swan] — optional; written when the wizard's SWAN step sends
    # this block (T4.2 / T4.4).  None → skip (existing values unchanged).
    # service_url is REMOVED (T7.2, LC-P7-3) — marine_service_url below is
    # the single connection key now; [swan] no longer carries a URL.
    if apply.swan is not None:
        ts = apply.swan
        if "swan" not in cfg:
            cfg["swan"] = {}
        cfg["swan"]["omp_num_threads"] = str(ts.omp_num_threads)
        cfg["swan"]["outer_grid_resolution_km"] = str(ts.outer_grid_resolution_km)
        cfg["swan"]["inner_nest_resolution_m"] = str(ts.inner_nest_resolution_m)

    # [providers] marine_service_url / marine_verify_tls — the single
    # marine-service connection (T7.2, LC-P7-3), replacing the legacy
    # surf_compute_host/surf_compute_verify_tls (T6.8) and [swan]
    # service_url (removed above). Both keys are already READ by
    # _read_marine_service_connection() and config/settings.py; this is
    # the write path that was previously missing (marine_service_url could
    # be hand-edited into api.conf but never set by the wizard).
    # None → write nothing, leaving any existing value on disk unchanged —
    # same "don't touch what wasn't sent" convention as [swan] above.
    if apply.marine_service_url is not None:
        if "providers" not in cfg:
            cfg["providers"] = {}
        cfg["providers"]["marine_service_url"] = apply.marine_service_url
        cfg["providers"]["marine_verify_tls"] = str(apply.marine_verify_tls)

    if conf_path.exists():
        shutil.copy2(conf_path, conf_path.with_suffix(conf_path.suffix + ".bak"))

    cfg.write()


_SKIN_CONF_UNITS_SUBSECTIONS: dict[str, str] = {
    "groups": "Groups",
    "string_formats": "StringFormats",
    "labels": "Labels",
    "ordinates": "Ordinates",
    "time_formats": "TimeFormats",
    "degree_days": "DegreeDays",
    "trend": "Trend",
}


def _write_skin_conf(skin_data: dict[str, Any]) -> Path:
    """Write /etc/weewx/skins/ClearSkies/skin.conf (ADR-043).

    Reads SKIN_ROOT from the cached weewx.conf ([StdReport] SKIN_ROOT).
    Falls back to /etc/weewx/skins if weewx.conf has not been loaded yet
    (first-run wizard path, before weewx_conf_path is committed).
    """
    try:
        wconf = get_weewx_conf()
        raw = wconf.get("StdReport", {}).get("SKIN_ROOT", "skins")
        skin_root = Path(raw)
        if not skin_root.is_absolute():
            skin_root = Path(wconf.filename).parent / skin_root
    except RuntimeError:
        skin_root = Path("/etc/weewx/skins")

    skin_dir = skin_root / "ClearSkies"
    skin_dir.mkdir(parents=True, exist_ok=True)
    skin_path = skin_dir / "skin.conf"

    cfg = configobj.ConfigObj(indent_type="    ", encoding="utf-8")
    cfg.filename = str(skin_path)
    cfg.initial_comment = [
        "skin.conf for Clear Skies - generated by the setup wizard.",
        "Do not edit manually; re-run the wizard to update.",
    ]

    # [Units] — subsections mapped from snake_case payload to CamelCase configobj keys
    units = skin_data.get("units", {})
    if units:
        cfg["Units"] = {}
        for payload_key, section_name in _SKIN_CONF_UNITS_SUBSECTIONS.items():
            if payload_key in units and units[payload_key]:
                cfg["Units"][section_name] = units[payload_key]

    # [Labels][[Generic]]
    labels = skin_data.get("labels", {})
    if labels and labels.get("generic"):
        cfg["Labels"] = {"Generic": labels["generic"]}

    # [Extras] — freeform key-value
    extras = skin_data.get("extras")
    if extras:
        cfg["Extras"] = extras

    # [Almanac]
    almanac = skin_data.get("almanac")
    if almanac:
        cfg["Almanac"] = almanac

    cfg.write()
    logger.info("Wrote skin.conf to %s", skin_path)
    return skin_path


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/handshake", response_model=HandshakeResponse)
async def handshake(body: HandshakeRequest, request: Request) -> HandshakeResponse:
    """Exchange the trust token for a setup session_id."""
    tm = await require_setup_active(request)
    session_id = tm.create_session(body.token)
    if session_id is None:
        raise HTTPException(401, detail="Invalid trust token")
    return HandshakeResponse(session_id=session_id)


@router.get("/db-defaults", response_model=DbDefaultsResponse)
async def db_defaults(request: Request) -> DbDefaultsResponse:
    """Return DB connection defaults from weewx.conf. Password is never included.

    Detection sequence:
      1. [DataBindings][[wx_binding]] database — the active database stanza name
         (usually "archive_sqlite" or "archive_mysql").
      2. [Databases][[<stanza>]] database_type — "SQLite" or "MySQL".
      3. SQLite: read SQLITE_ROOT from [DatabaseTypes][[SQLite]] and
         database_name from [Databases][[<stanza>]]; join into a full path.
      4. MySQL: read [DatabaseTypes][[MySQL]] for host/port/user, and
         database_name from the matching [Databases] stanza (existing behavior).
    """
    tm = await require_setup_session(request)  # noqa: F841 — side-effect: auth check
    settings = request.app.state.settings
    weewx_conf_path: str = settings.weewx.config_path

    cfg = _load_weewx_conf_for_setup(weewx_conf_path)

    kind = "mysql"
    host = "localhost"
    port = 3306
    user = "weewx"
    db_name = "weewx"
    sqlite_path = ""

    if cfg is not None:
        # 1. Find the active database stanza via [DataBindings][[wx_binding]].
        data_bindings = cfg.get("DataBindings", {})
        stanza_name = "archive_mysql"
        if isinstance(data_bindings, dict):
            wx_binding = data_bindings.get("wx_binding", {})
            if isinstance(wx_binding, dict) and wx_binding.get("database"):
                stanza_name = str(wx_binding["database"])

        # 2. Look up database_type for that stanza under [Databases].
        databases_section = cfg.get("Databases", {})
        stanza: dict[str, Any] = {}
        if isinstance(databases_section, dict):
            raw_stanza = databases_section.get(stanza_name)
            if isinstance(raw_stanza, dict):
                stanza = raw_stanza

        db_type = str(stanza.get("database_type", "")).lower()

        if db_type == "sqlite":
            kind = "sqlite"
            db_types_section = cfg.get("DatabaseTypes", {})
            sqlite_section: dict[str, Any] = {}
            if isinstance(db_types_section, dict):
                raw_sqlite = db_types_section.get("SQLite")
                if isinstance(raw_sqlite, dict):
                    sqlite_section = raw_sqlite
            sqlite_root = str(sqlite_section.get("SQLITE_ROOT", ""))
            database_name = str(stanza.get("database_name", ""))
            if sqlite_root and database_name:
                sqlite_path = str(Path(sqlite_root) / database_name)
            elif database_name:
                sqlite_path = database_name
        else:
            # MySQL (or unresolved stanza — fall back to legacy MySQL-only detection).
            kind = "mysql"
            db_types_section = cfg.get("DatabaseTypes", {})
            mysql_section: dict[str, Any] = {}
            if isinstance(db_types_section, dict):
                raw_mysql = db_types_section.get("MySQL")
                if isinstance(raw_mysql, dict):
                    mysql_section = raw_mysql

            if mysql_section:
                host = str(mysql_section.get("host", host))
                try:
                    port = int(mysql_section.get("port", port))
                except (ValueError, TypeError):
                    pass
                user = str(mysql_section.get("user", user))

            if stanza.get("database_name"):
                db_name = str(stanza["database_name"])
            elif isinstance(databases_section, dict):
                # Fall back: scan all stanzas for the first MySQL one (legacy behavior
                # when [DataBindings][[wx_binding]] is absent or points elsewhere).
                for _db_key, db_val in databases_section.items():
                    if isinstance(db_val, dict):
                        candidate_type = str(db_val.get("database_type", "")).lower()
                        if candidate_type == "mysql":
                            db_name = str(db_val.get("database_name", db_name))
                            break

    if kind == "sqlite":
        return DbDefaultsResponse(
            kind="sqlite",
            path=sqlite_path,
            conf_path=weewx_conf_path,
        )

    return DbDefaultsResponse(
        kind="mysql",
        host=host,
        port=port,
        user=user,
        name=db_name,
        conf_path=weewx_conf_path,
    )


@router.post("/db-test", response_model=DbTestResponse)
async def db_test(body: DbTestRequest, request: Request) -> DbTestResponse:
    """Test a DB connection and store params in session data on success."""
    tm = await require_setup_session(request)

    if body.kind == "sqlite":
        db_path = Path(body.path)
        if not db_path.exists():
            return DbTestResponse(
                success=False,
                error=f"SQLite database file not found: {body.path}",
            )
        if not db_path.is_file():
            return DbTestResponse(success=False, error=f"Path is not a file: {body.path}")

        url = _build_temp_sqlite_url(body.path)
        engine = create_engine(url, poolclass=NullPool, future=True, echo=False)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError as exc:
            logger.debug("db-test SQLite OperationalError: %s", exc)
            return DbTestResponse(success=False, error="Unable to open SQLite database file")
        except Exception as exc:  # noqa: BLE001
            logger.error("db-test unexpected error (sqlite): %s", type(exc).__name__)
            return DbTestResponse(success=False, error="Connection test failed")
        finally:
            engine.dispose()

        tm.set_session_data("db_params", {
            "kind": "sqlite",
            "path": body.path,
        })
        return DbTestResponse(success=True, version="SQLite")

    url = _build_temp_mysql_url(
        host=body.host,
        port=body.port,
        user=body.user,
        password=body.password,
        name=body.name,
    )

    # 5-second connect timeout via connect_args.
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
        future=True,
        echo=False,
    )

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT VERSION()"))
            version_str = str(result.scalar() or "")
    except OperationalError as exc:
        # Map pymysql error codes to generic messages — never forward raw driver
        # output to the client, as it may contain DB host or username.
        error_code: int | None = None
        if exc.orig is not None and exc.orig.args:
            try:
                error_code = int(exc.orig.args[0])
            except (TypeError, ValueError):
                pass
        _DB_ERROR_MAP = {
            1045: "Authentication failed",
            2003: "Cannot reach database host",
            2005: "Unknown database host",
            1049: "Unknown database",
            2013: "Connection timed out",
        }
        client_msg = _DB_ERROR_MAP.get(error_code, "Connection failed")
        logger.debug("db-test OperationalError (code=%s): %s", error_code, exc)
        return DbTestResponse(success=False, error=client_msg)
    except Exception as exc:  # noqa: BLE001
        logger.error("db-test unexpected error: %s", type(exc).__name__)
        return DbTestResponse(success=False, error="Connection test failed")
    finally:
        engine.dispose()

    # Store validated params in session for /setup/schema and /setup/apply.
    tm.set_session_data("db_params", {
        "kind": "mysql",
        "host": body.host,
        "port": body.port,
        "user": body.user,
        "password": body.password,
        "name": body.name,
    })

    return DbTestResponse(success=True, version=version_str)


@router.get("/schema", response_model=SchemaResponse)
async def schema(request: Request) -> SchemaResponse:
    """Reflect the DB schema using stored db_params from a prior db-test call."""
    tm = await require_setup_session(request)

    session_data = tm.get_session_data()
    db_params: dict[str, Any] | None = session_data.get("db_params")
    if db_params is None:
        raise HTTPException(409, detail="Test database connection first")

    if db_params.get("kind") == "sqlite":
        url = _build_temp_sqlite_url(db_params["path"])
    else:
        url = _build_temp_mysql_url(
            host=db_params["host"],
            port=db_params["port"],
            user=db_params["user"],
            password=db_params["password"],
            name=db_params["name"],
        )

    engine = create_engine(url, poolclass=NullPool, future=True, echo=False)
    try:
        reflector = SchemaReflector(engine)
        registry = reflector.reflect()

        # Read the unit system from the first archive record so we can resolve
        # unit groups to concrete unit strings (e.g. group_temperature → degree_F).
        # Returns None when the archive table is empty (fresh install).
        with engine.connect() as conn:
            us_units: int | None = conn.execute(
                text("SELECT usUnits FROM archive LIMIT 1"),
            ).scalar()
    except RuntimeError as exc:
        logger.error("Schema reflection failed during setup: %s", type(exc).__name__)
        raise HTTPException(502, detail="Schema reflection failed. Verify database and archive table exist.") from exc
    except OperationalError as exc:
        logger.error("DB error during setup schema reflection: %s", type(exc).__name__)
        raise HTTPException(502, detail="Database connection error during schema reflection.") from exc
    finally:
        engine.dispose()

    columns: list[ColumnEntry] = []
    for col_info in registry.all_columns():
        # db_type from SQLAlchemy reflection comes via column.type — SchemaReflector
        # doesn't currently store the raw SQL type string in ColumnInfo. We can
        # re-derive it by re-reflecting; instead, we use the stock map to classify
        # and note "INTEGER" for dateTime / usUnits / interval, "REAL" otherwise as
        # a reasonable default for setup UI purposes.  The setup wizard only needs
        # to know stock/unmapped, not the precise SQL type for now.
        if col_info.db_name in ("dateTime", "usUnits", "interval"):
            db_type = "INTEGER"
        else:
            db_type = "REAL"

        auto_unit: str | None = None
        if col_info.auto_detected_group and us_units is not None:
            auto_unit = get_unit_for_group(col_info.auto_detected_group, us_units)

        # Determine unit_source and heuristic suggestion fields.
        # Priority: weewx auto-detection > heuristic pattern match > nothing.
        suggested_group: str | None = None
        suggested_unit: str | None = None
        unit_source: str | None = None

        if col_info.auto_detected_group:
            unit_source = "weewx"
        else:
            heuristic_group = _suggest_group(col_info.db_name)
            if heuristic_group:
                unit_source = "heuristic"
                suggested_group = heuristic_group
                if us_units is not None:
                    suggested_unit = get_unit_for_group(heuristic_group, us_units)

        columns.append(ColumnEntry(
            name=col_info.db_name,
            db_type=db_type,
            stock=col_info.is_stock,
            canonical=col_info.canonical_name,
            auto_detected_group=col_info.auto_detected_group,
            auto_detected_unit=auto_unit,
            suggested_group=suggested_group,
            suggested_unit=suggested_unit,
            unit_source=unit_source,
        ))

    return SchemaResponse(
        columns=columns,
        stock_count=len(registry.stock),
        unmapped_count=len(registry.unmapped),
    )


@router.get("/station", response_model=StationResponse)
async def station(request: Request) -> StationResponse:
    """Return station identity from weewx.conf [Station]."""
    tm = await require_setup_session(request)  # noqa: F841 — auth check
    settings = request.app.state.settings
    weewx_conf_path: str = settings.weewx.config_path

    cfg = _load_weewx_conf_for_setup(weewx_conf_path)
    if cfg is None:
        raise HTTPException(502, detail="Cannot read weather station configuration file.")

    station_section = cfg.get("Station")
    if not isinstance(station_section, dict):
        raise HTTPException(502, detail="Station section missing from weather station configuration.")

    station_name = _get_str_field(station_section, "location")

    latitude: float | None = None
    raw_lat = station_section.get("latitude", "")
    if isinstance(raw_lat, str):
        raw_lat = raw_lat.strip()
    if raw_lat:
        try:
            latitude = float(str(raw_lat))
        except (ValueError, TypeError):
            pass

    longitude: float | None = None
    raw_lon = station_section.get("longitude", "")
    if isinstance(raw_lon, str):
        raw_lon = raw_lon.strip()
    if raw_lon:
        try:
            longitude = float(str(raw_lon))
        except (ValueError, TypeError):
            pass

    altitude_meters: float | None = None
    altitude_unit: str = "meter"
    raw_altitude_val = station_section.get("altitude", "")
    if isinstance(raw_altitude_val, list):
        raw_altitude = ", ".join(str(x) for x in raw_altitude_val)
    else:
        raw_altitude = str(raw_altitude_val).strip()
    if raw_altitude:
        try:
            altitude_meters = _parse_altitude(raw_altitude)
            # _parse_altitude returns the raw numeric value unchanged; parse the
            # unit string from the same field so the wizard can display and
            # convert correctly (weewx.conf: "altitude = 700, foot" or "200, meter").
            parts = raw_altitude.split(",", 1)
            if len(parts) == 2:
                unit_str = parts[1].strip().lower()
                if "foot" in unit_str or "feet" in unit_str or unit_str == "ft":
                    altitude_unit = "foot"
                else:
                    altitude_unit = "meter"
        except Exception:  # noqa: BLE001
            pass

    station_type = _get_str_field(station_section, "station_type") or None

    return StationResponse(
        station_name=station_name,
        latitude=latitude,
        longitude=longitude,
        altitude_meters=altitude_meters,
        altitude_unit=altitude_unit,
        station_type=station_type,
    )


@router.post("/apply", response_model=ApplyResponse)
async def apply(body: ApplyRequest, request: Request) -> ApplyResponse:
    """Write api.conf and secrets.env, then mark setup complete."""
    tm = await require_setup_session(request)

    config_dir: Path = request.app.state.config_dir

    # 0. Validate weewx_conf_path before touching the filesystem.
    if body.weewx_conf_path is not None:
        wcp = body.weewx_conf_path
        if not wcp.startswith("/") or not wcp.endswith(".conf") or not Path(wcp).exists():
            raise HTTPException(422, detail="Invalid weewx.conf path")

    # 1. Write non-secret settings to api.conf.
    try:
        _write_api_conf(config_dir, body)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write api.conf during setup apply: %s", type(exc).__name__)
        raise HTTPException(500, detail="Failed to write configuration file.") from exc

    # 1c. C-41 (2026-07-25, executed): the CUDEM download / grid sizing /
    # profile generation chain moved to the marine service — it runs there
    # on config receipt (POST /config, weewx-clearskies-marine's
    # services/grid_sizing_chain.py), triggered by the config push below.
    # It is purely an internal SWAN/marine-service function; it sat here
    # only because SWAN itself used to run inside the API process.

    # --- Step 1b: write skin.conf (ADR-043) ---
    if body.skin_conf:
        try:
            _write_skin_conf(body.skin_conf)
        except OSError:
            logger.exception("Failed to write skin.conf to weewx skins directory")
            raise HTTPException(
                status_code=500,
                detail="Wrote api.conf but failed to write skin.conf — check weewx skins directory permissions.",
            )

    # 2. Write secrets to secrets.env.
    secrets_path = config_dir / "secrets.env"
    try:
        existing = _read_secrets_env(secrets_path)
        existing["WEEWX_CLEARSKIES_DB_PASSWORD"] = body.database.password
        existing["WEEWX_CLEARSKIES_DB_USER"] = body.database.user

        # Provider credentials — written using provider-scoped env var names that
        # match what settings.py reads at startup (see _provider_secrets docstring).
        if body.providers:
            for domain, pc in body.providers.items():
                existing.update(_provider_secrets(domain, pc))

        # MQTT/realtime proxy shared secret.
        if body.proxy_secret:
            existing["WEEWX_CLEARSKIES_PROXY_SECRET"] = body.proxy_secret

        # OpenAQ API key (calibration bootstrap + AQI provider).
        if body.openaq_api_key:
            existing["WEEWX_CLEARSKIES_OPENAQ_API_KEY"] = body.openaq_api_key

        # T6.8: SURF_COMPUTE_SECRET removed — MARINE_SERVICE_SECRET
        # (companion_proxy.py) is the single secret that replaces it.
        # T7.2/LC-P7-3: the wizard can now write it via apply, same
        # None-means-don't-touch convention as the OpenAQ key above.
        if body.marine_service_secret:
            existing["MARINE_SERVICE_SECRET"] = body.marine_service_secret

        _write_secrets_env(secrets_path, existing)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write secrets.env during setup apply: %s", type(exc).__name__)
        raise HTTPException(500, detail="Failed to write secrets file.") from exc

    # 2b. Mirror any newly-written secrets into the running process's environment
    # so that the restart endpoint can authenticate the immediately-following restart
    # call without waiting for a process restart to reload secrets.env.
    # This is safe: apply is already authenticated; we are only surfacing values that
    # were just persisted to disk.
    if body.proxy_secret:
        os.environ["WEEWX_CLEARSKIES_PROXY_SECRET"] = body.proxy_secret
    # T7.6: mirror MARINE_SERVICE_SECRET too, and BEFORE the push below —
    # otherwise a secret supplied for the first time in this same apply
    # request would not yet be visible to _push_marine_service_config()'s
    # os.environ read, and the wizard's very first apply would always
    # report "secret not set" even though it just sent one.
    if body.marine_service_secret:
        os.environ["MARINE_SERVICE_SECRET"] = body.marine_service_secret

    # 1d. T6.4 — push the marine config subset to the marine service, if
    # marine_service_url is configured. Failure logs ERROR but does not
    # fail the apply (API-MANUAL.md §19.5). Runs after secrets.env has
    # been written and mirrored (T7.6 reorder, see _push_marine_service_
    # config()'s docstring) so a same-request secret is actually usable.
    marine_config_push = await _push_marine_service_config(config_dir)

    # 3. Mark setup complete — consumes trust token and invalidates session.
    # Skip on re-run (setup already complete) to avoid redundant file writes and
    # to preserve the proxy-auth-based session that authorised this re-run.
    if not tm.setup_complete:
        tm.mark_setup_complete()

    # 4. Issue a one-time restart token so the wizard can call /setup/restart
    # immediately after apply.  The token is valid for 60 s and is consumed on
    # first use.  This handles the case where no proxy secret exists (same-host
    # topology) and the wizard has no proxy auth to send.
    restart_token = secrets.token_hex(32)
    request.app.state.restart_token = restart_token
    request.app.state.restart_token_expires = time.monotonic() + 60.0

    return ApplyResponse(
        success=True,
        message="Configuration saved. Restart the API to apply.",
        restart_token=restart_token,
        marine_config_push=marine_config_push,
    )


@router.get("/current-config", response_model=CurrentConfigResponse)
async def current_config(request: Request) -> CurrentConfigResponse:
    """Return the full current configuration including secrets.

    Requires proxy auth (X-Clearskies-Proxy-Auth header).  Called by the config
    UI wizard in re-run mode to pre-populate all fields so the operator does not
    have to re-enter every password and API key.

    Secrets are read from secrets.env (already written by a prior /setup/apply
    call).  Non-secret fields come from api.conf.  The response includes the DB
    password, provider API keys, and the DB username — everything the wizard
    needs to populate its state without user re-entry.
    """
    # require_setup_active enforces proxy auth when setup is complete, which is
    # the only case this endpoint is reachable (setup must have been done to have
    # a proxy secret in the first place).
    await require_setup_active(request)

    config_dir: Path = request.app.state.config_dir
    secrets_path = config_dir / "secrets.env"
    secrets = _read_secrets_env(secrets_path)

    # --- Database ---
    db_kind = "mysql"
    db_host = "localhost"
    db_port = 3306
    db_user = ""
    db_name = "weewx"
    db_path = ""

    conf_path = config_dir / "api.conf"
    api_cfg: configobj.ConfigObj | None = None
    if conf_path.exists():
        try:
            api_cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
        except Exception:  # noqa: BLE001
            api_cfg = None

    if api_cfg is not None:
        db_section = api_cfg.get("database", {})
        if isinstance(db_section, dict):
            if db_section.get("kind"):
                db_kind = str(db_section["kind"])
            if db_section.get("host"):
                db_host = str(db_section["host"])
            if db_section.get("port"):
                try:
                    db_port = int(db_section["port"])
                except (ValueError, TypeError):
                    pass
            if db_section.get("name"):
                db_name = str(db_section["name"])
            if db_kind == "sqlite" and db_section.get("path"):
                db_path = str(db_section["path"])

    # DB user and password come from secrets.env (the authoritative source for
    # credentials; api.conf only stores the non-secret DB fields).
    db_user = secrets.get("WEEWX_CLEARSKIES_DB_USER", db_user)
    db_password = secrets.get("WEEWX_CLEARSKIES_DB_PASSWORD", "")

    database = CurrentConfigDatabaseSection(
        kind=db_kind,
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        name=db_name,
        path=db_path,
    )

    # --- Providers ---
    # Non-secret fields (provider id) come from api.conf.
    # Credentials come from secrets.env using the provider-scoped naming that
    # _provider_secrets() wrote at apply time.
    # "imagery" added 2026-08-03 (LM-3 finding 3): without it, a saved
    # [imagery] provider never pre-filled on wizard re-run — the same
    # silent-reset defect class as the study-area wizard bug. Imagery is
    # keyless here (provider value only; its api_key is a plain api.conf
    # field read by ImagerySettings, not a secrets.env credential).
    _PROVIDER_DOMAINS = ("forecast", "aqi", "alerts", "radar", "earthquakes", "imagery")
    providers: dict[str, CurrentConfigProviderSection] = {}
    for domain in _PROVIDER_DOMAINS:
        domain_section = {}
        if api_cfg is not None:
            raw = api_cfg.get(domain, {})
            if isinstance(raw, dict):
                domain_section = raw
        provider_id = str(domain_section.get("provider", "")).strip()
        if not provider_id:
            continue
        p = _canonical_provider(provider_id.lower())
        creds = CurrentConfigProviderCredentials()
        if p == "aeris":
            creds.client_id = secrets.get("WEEWX_CLEARSKIES_AERIS_CLIENT_ID") or None
            creds.client_secret = secrets.get("WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET") or None
        elif p == "openweathermap":
            creds.appid = secrets.get("WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID") or None
        elif p == "iqair":
            creds.key = secrets.get("WEEWX_CLEARSKIES_IQAIR_KEY") or None
        # Keyless providers (nws, openmeteo, rainviewer, etc.) have no credential fields.

        # Provider-specific non-secret config fields from api.conf.
        extra: dict[str, str | None] = {}
        if domain == "radar":
            extra["librewxr_endpoint"] = str(domain_section.get("librewxr_endpoint", "")).strip() or None
            extra["librewxr_bounds"] = str(domain_section.get("librewxr_bounds", "")).strip() or None
            extra["iframe_url"] = str(domain_section.get("iframe_url", "")).strip() or None
        if domain == "forecast":
            extra["aeris_forecast_model"] = str(domain_section.get("aeris_forecast_model", "")).strip() or None
        if domain in ("forecast", "alerts"):
            extra["nws_user_agent_contact"] = str(domain_section.get("nws_user_agent_contact", "")).strip() or None
        if domain == "alerts":
            raw_radius = str(domain_section.get("marine_alert_radius_miles", "0")).strip()
            try:
                extra["marine_alert_radius_miles"] = int(raw_radius) if raw_radius else 0
            except (ValueError, TypeError):
                extra["marine_alert_radius_miles"] = 0
            raw_zones = str(domain_section.get("marine_alert_zone_ids", "")).strip()
            extra["marine_alert_zone_ids"] = [z.strip() for z in raw_zones.split(",") if z.strip()] if raw_zones else []

        providers[domain] = CurrentConfigProviderSection(
            provider=provider_id,
            credentials=creds,
            **extra,
        )

    # --- Station ---
    station = CurrentConfigStationSection()
    if api_cfg is not None:
        st_section = api_cfg.get("station", {})
        if isinstance(st_section, dict):
            if st_section.get("timezone"):
                station.timezone = str(st_section["timezone"])
        # Station name, lat, lon, altitude come from [station] in api.conf if
        # the apply call persisted them there.  For installs where these live only
        # in weewx.conf and were never explicitly stored in api.conf, the wizard
        # will fall back to the existing /setup/station endpoint.
        if isinstance(st_section, dict):
            if st_section.get("name"):
                station.name = str(st_section["name"])
            for float_key, attr in (
                ("latitude", "latitude"),
                ("longitude", "longitude"),
                ("altitude_meters", "altitude_meters"),
            ):
                raw_val = st_section.get(float_key)
                if raw_val:
                    try:
                        setattr(station, attr, float(str(raw_val)))
                    except (ValueError, TypeError):
                        pass
            if st_section.get("altitude_unit"):
                station.altitude_unit = str(st_section["altitude_unit"])
            if st_section.get("default_locale"):
                station.default_locale = str(st_section["default_locale"])

    # --- Branding ---
    branding = CurrentConfigBrandingSection()
    if api_cfg is not None:
        br_section = api_cfg.get("branding", {})
        if isinstance(br_section, dict):
            if br_section.get("site_title"):
                branding.site_title = str(br_section["site_title"])
            if br_section.get("copyright_entity"):
                branding.copyright_entity = str(br_section["copyright_entity"])
            if br_section.get("logo_light_url"):
                branding.logo_light_url = str(br_section["logo_light_url"])
            if br_section.get("logo_dark_url"):
                branding.logo_dark_url = str(br_section["logo_dark_url"])
            if br_section.get("logo_alt") is not None:
                # Intentionally use `is not None` (not truthiness): an operator
                # who explicitly clears the alt text should have that honoured
                # and get the fallback at render time rather than having the
                # wizard silently skip the field.
                branding.logo_alt = str(br_section["logo_alt"])
            if br_section.get("favicon_url"):
                branding.favicon_url = str(br_section["favicon_url"])
            if br_section.get("accent"):
                branding.accent = str(br_section["accent"])
            if br_section.get("default_theme_mode"):
                branding.default_theme_mode = str(br_section["default_theme_mode"])
            if br_section.get("custom_css_url"):
                branding.custom_css_url = str(br_section["custom_css_url"])

    # --- Social ---
    social = CurrentConfigSocialSection()
    if api_cfg is not None:
        so_section = api_cfg.get("social", {})
        if isinstance(so_section, dict):
            if so_section.get("facebook_url"):
                social.facebook_url = str(so_section["facebook_url"])
            if so_section.get("twitter_url"):
                social.twitter_url = str(so_section["twitter_url"])
            if so_section.get("instagram_url"):
                social.instagram_url = str(so_section["instagram_url"])
            if so_section.get("youtube_url"):
                social.youtube_url = str(so_section["youtube_url"])

    # --- Earthquakes (seismic knobs) ---
    earthquakes_config = CurrentConfigEarthquakeSection()
    if api_cfg is not None:
        eq_section = api_cfg.get("earthquakes", {})
        if isinstance(eq_section, dict):
            raw_radius = eq_section.get("default_radius_km")
            if raw_radius:
                try:
                    earthquakes_config.default_radius_km = float(str(raw_radius))
                except (ValueError, TypeError):
                    pass
            raw_min_mag = eq_section.get("min_magnitude")
            if raw_min_mag:
                try:
                    earthquakes_config.min_magnitude = float(str(raw_min_mag))
                except (ValueError, TypeError):
                    pass
            raw_days = eq_section.get("default_days")
            if raw_days:
                try:
                    earthquakes_config.default_days = int(str(raw_days))
                except (ValueError, TypeError):
                    pass

    # --- Units ---
    units_config: CurrentConfigUnitsSection | None = None
    if api_cfg is not None:
        u_section = api_cfg.get("units", {})
        if isinstance(u_section, dict) and u_section:
            u_groups: dict[str, str] | None = None
            raw_groups = u_section.get("groups")
            if isinstance(raw_groups, dict) and raw_groups:
                u_groups = {str(k): str(v) for k, v in raw_groups.items() if v}

            u_string_formats: dict[str, str] | None = None
            raw_string_formats = u_section.get("string_formats")
            if isinstance(raw_string_formats, dict) and raw_string_formats:
                u_string_formats = {str(k): str(v) for k, v in raw_string_formats.items() if v}

            u_labels: dict[str, str] | None = None
            raw_labels = u_section.get("labels")
            if isinstance(raw_labels, dict) and raw_labels:
                u_labels = {str(k): str(v) for k, v in raw_labels.items() if v}

            u_ordinates: list[str] | None = None
            raw_ordinates = u_section.get("ordinates")
            if isinstance(raw_ordinates, (list, tuple)) and raw_ordinates:
                u_ordinates = [str(o) for o in raw_ordinates]

            if any(x is not None for x in (u_groups, u_string_formats, u_labels, u_ordinates)):
                units_config = CurrentConfigUnitsSection(
                    groups=u_groups,
                    string_formats=u_string_formats,
                    labels=u_labels,
                    ordinates=u_ordinates,
                )

    # --- Column mapping ---
    col_mapping: dict[str, str] | None = None
    if api_cfg is not None:
        cm_section = api_cfg.get("column_mapping", {})
        if isinstance(cm_section, dict) and cm_section:
            col_mapping = {
                str(k): str(v) for k, v in cm_section.items()
                if v and k != "_excluded"
            }

    # --- Column units ---
    col_units: dict[str, str] | None = None
    if api_cfg is not None:
        cu_section = api_cfg.get("column_units", {})
        if isinstance(cu_section, dict) and cu_section:
            col_units = {str(k): str(v) for k, v in cu_section.items() if v}

    # --- OpenAQ API key (bootstrap + AQI provider) ---
    openaq_key = secrets.get("WEEWX_CLEARSKIES_OPENAQ_API_KEY") or None

    # --- Marine (nested ConfigObj dict, returned as-is for admin UI) ---
    marine_config: dict[str, Any] | None = None
    if api_cfg is not None:
        marine_section = api_cfg.get("marine")
        if isinstance(marine_section, dict) and marine_section:
            marine_config = dict(marine_section)

    # T6.8: surf_compute_host / surf_compute_verify_tls removed — superseded
    # by marine_service_url (API-MANUAL §19.2).

    # --- Surf scoring weights (Round S, ADR-101) ---
    # TOP-LEVEL [surf_scoring] section, read independently of [marine]
    # above — this is the authoritative read path the admin form pre-fills
    # from (split-host deployments must not read api.conf directly).
    surf_scoring_config: dict[str, float] | None = None
    if api_cfg is not None:
        surf_scoring_section = api_cfg.get("surf_scoring")
        if isinstance(surf_scoring_section, dict) and surf_scoring_section:
            surf_scoring_config = {
                "weight_size": _cfg_float(surf_scoring_section, "weight_size", 0.0),
                "weight_shape": _cfg_float(surf_scoring_section, "weight_shape", 0.0),
                "weight_conditions": _cfg_float(surf_scoring_section, "weight_conditions", 0.0),
                "weight_power": _cfg_float(surf_scoring_section, "weight_power", 0.0),
                "weight_consistency": _cfg_float(surf_scoring_section, "weight_consistency", 0.0),
            }

    # --- Marine service connection (T7.2, LC-P7-3) ---
    # Read via the same helper _push_marine_service_config() uses, so the
    # wizard's re-run pre-fill and the actual push read the same source.
    marine_service_url, marine_verify_tls = _read_marine_service_connection(config_dir)
    marine_service_secret = secrets.get("MARINE_SERVICE_SECRET") or None

    return CurrentConfigResponse(
        database=database,
        providers=providers,
        station=station,
        branding=branding,
        social=social,
        earthquakes=earthquakes_config,
        units=units_config,
        column_mapping=col_mapping,
        column_units=col_units,
        openaq_api_key=openaq_key,
        marine=marine_config,
        surf_scoring=surf_scoring_config,
        marine_service_url=marine_service_url,
        marine_verify_tls=marine_verify_tls,
        marine_service_secret=marine_service_secret,
    )


@router.get("/skin-file")
async def get_skin_file(
    skin: str,
    path: str,
    request: Request,
) -> FileResponse:
    """Serve a file from a weewx skin directory (ADR-043 image import).

    Used by the wizard to fetch image assets (logos, favicons) from an
    existing skin when importing its skin.conf.
    """
    await require_setup_session(request)

    try:
        wconf = get_weewx_conf()
        raw = wconf.get("StdReport", {}).get("SKIN_ROOT", "skins")
        skin_root = Path(raw)
        if not skin_root.is_absolute():
            skin_root = Path(wconf.filename).parent / skin_root
    except RuntimeError:
        skin_root = Path("/etc/weewx/skins")

    # Validate skin name — no path separators or traversal sequences.
    if "/" in skin or "\\" in skin or ".." in skin:
        raise HTTPException(400, detail="Invalid skin name")

    # Build and validate full path — prevent directory traversal.
    skin_dir = (skin_root / skin).resolve()
    file_path = (skin_dir / path).resolve()

    # Ensure the resolved file_path is strictly inside skin_dir.
    # The os.sep suffix prevents prefix attacks, e.g. /skins/Foo matching
    # /skins/FooBar/secret when skin_dir is /skins/Foo.
    if not str(file_path).startswith(str(skin_dir) + os.sep) and file_path != skin_dir:
        raise HTTPException(400, detail="Invalid path")

    if not file_path.is_file():
        raise HTTPException(404, detail="File not found in skin directory")

    return FileResponse(file_path)


# ---------------------------------------------------------------------------
# Marine setup endpoints (T6.3)
# ---------------------------------------------------------------------------


def _check_marine_service_auth(request: Request) -> None:
    """Raise on missing/invalid marine-service credentials.

    T6.4b — GET /setup/marine/config is called by the marine service itself
    (startup config recovery), not by the wizard/admin, so it uses its own
    auth: Authorization: Bearer {MARINE_SERVICE_SECRET} — the same shared
    secret the API sends *to* the marine service on every push (T6.4), not
    the setup-session or X-Clearskies-Proxy-Auth mechanisms the rest of
    /setup/ uses. 503 when the secret is not configured (deployment fault,
    matches the marine service's own MARINE_SERVICE_SECRET-unset handling
    per OPERATIONS-MANUAL.md); 401 for a missing or wrong token.
    """
    secret = os.environ.get("MARINE_SERVICE_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, detail="MARINE_SERVICE_SECRET not configured")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(
        secret.encode("utf-8"), auth[7:].encode("utf-8")
    ):
        raise HTTPException(401, detail="Invalid or missing marine service credentials")


@router.get("/marine/config")
async def get_marine_service_config(request: Request) -> dict[str, Any]:
    """Return the marine service config subset (T6.4b — config recovery on
    restart).

    Returns exactly what POST {marine_service_url}/config pushes (T6.4) —
    both are built by the same _build_marine_service_config_payload()
    (API-MANUAL.md §19.5 "one serializer, both paths"). Called by the
    marine service on startup when it has no local config
    (config/__init__.py's fetch-if-missing path); not part of the
    wizard/admin flow.
    """
    _check_marine_service_auth(request)
    config_dir: Path = request.app.state.config_dir
    return _build_marine_service_config_payload(config_dir)


class MarineEccodesCheckResponse(BaseModel):
    #: True when a GRIB2 backend (eccodes or the pygrib fallback) is
    #: importable in the marine service's process. Required for HRRR wind
    #: field ingestion that drives the SWAN nearshore model.
    available: bool
    #: Platform-agnostic install instructions targeting the marine service
    #: host. None when available is True.
    install_instructions: str | None = None


@router.get("/marine/eccodes-check", response_model=MarineEccodesCheckResponse)
async def marine_eccodes_check(request: Request) -> MarineEccodesCheckResponse:
    """Probe whether a GRIB2 backend is installed (Marine Remediation Plan T3.6).

    Called by the wizard's marine step on load, before the operator is
    allowed to enable marine features — GRIB2 processing (eccodes or the
    pygrib fallback) is required for HRRR wind field ingestion that drives
    the SWAN nearshore model (ADR-093). C-42: this used to probe
    ``providers/marine/grib_processor.py``'s process-local ``GRIB_AVAILABLE``
    state directly; GRIB2 processing now happens entirely in the marine
    service's process, so this is a pass-through to
    ``GET {marine_service_url}/discovery/grib-availability``.

    A marine-service outage or missing configuration must not be reported
    as ``available: false`` with eccodes install instructions — that would
    tell the operator to install a library on the wrong host for the wrong
    reason. Both cases raise 503 instead (distinct detail text — see
    ``_marine_discovery_http_exception()``).
    """
    await require_setup_session(request)

    try:
        available, install_instructions = _discover_grib_availability()
    except MarineDiscoveryError as exc:
        raise _marine_discovery_http_exception(exc) from exc

    return MarineEccodesCheckResponse(available=available, install_instructions=install_instructions)


class MarineSwanCheckResponse(BaseModel):
    """Response shape for ``GET /setup/marine/swan-check`` (T4.4 / T4.1; C-54).

    The wizard's SWAN step calls this endpoint on load to determine
    whether the SWAN Fortran binary is installed and to show the operator how
    many CPU cores are available for the ``omp_num_threads`` slider.
    """

    #: True when the SWAN binary is found on the marine service host and
    #: can be executed there.
    available: bool
    #: SWAN version string (e.g. ``"41.45"``), or None when the binary is
    #: unavailable or version detection fails.
    version: str | None = None
    #: Absolute path of the ``swan`` binary on the marine service host, or
    #: None when unavailable.
    path: str | None = None
    #: Number of logical CPU cores on the marine service host (from
    #: os.cpu_count() there, not this API process).
    cpu_cores: int


@router.get("/marine/swan-check", response_model=MarineSwanCheckResponse)
async def marine_swan_check(request: Request) -> MarineSwanCheckResponse:
    """Pass-through to the marine service's SWAN binary probe (C-54).

    GET {marine_service_url}/discovery/swan-check ->
    {available, version, path, cpu_cores} (pinned contract, LC-P7-1/C-54
    pattern). SWAN runs on the marine service host, never on this API
    host — the endpoint's former implementation ran ``shutil.which("swan")``
    locally, which meant a correctly-deployed system reported SWAN missing
    when it was installed and running on the marine service. The marine
    service is now the only place that can answer this question honestly,
    including the CPU core count (that host's cores, not this one's, and
    never the configured ``omp_num_threads`` thread budget in its place).

    Called by the wizard's SWAN setup step to:
      - Determine whether SWAN is available (blocks the step with install
        instructions when ``available`` is False).
      - Show the operator the marine service host's CPU core count for the
        ``omp_num_threads`` configuration slider.
      - Display the installed SWAN version for information/debugging.

    Hard 503 on ``MarineDiscoveryError`` (LC-P7-2) — this is a live wizard
    query, same policy as ``/setup/marine/compute-estimate`` and
    ``/setup/marine/discover-stations``: an unreachable marine service must
    say so, never be reported as "SWAN not installed."
    """
    await require_setup_session(request)

    try:
        body = marine_discovery_get("/discovery/swan-check", {})
    except MarineDiscoveryError as exc:
        raise _marine_discovery_http_exception(exc) from exc

    return MarineSwanCheckResponse(
        available=bool(body.get("available")),
        version=body.get("version"),
        path=body.get("path"),
        cpu_cores=body.get("cpu_cores", 1),
    )


class MarineHealthResponse(BaseModel):
    """Response shape for ``GET /setup/marine/health`` (Marine Model Restoration
    Plan B4, part A).

    Carries the marine service's own ``GET /health`` body through to the admin
    status page. **``health`` is an opaque pass-through** — the marine service's
    health payload is still evolving (B3 is adding ``reasons``/``inputs``/
    ``invariants`` to the existing ``status``/``version``/``last_run``/``spots``/
    ``run_in_progress`` keys), and this response model does not name, validate,
    or reshape any of its inner keys. Do not add a nested schema for ``health``
    later — that would silently drop any key the marine service adds that this
    model doesn't already know about, which is the exact defect this endpoint
    exists to avoid (see ``TestMarineResponse``'s pinned six-field shape for the
    contrast: that one intentionally narrows, this one intentionally does not).
    """

    #: True when the marine service's /health responded 2xx with a JSON object.
    reachable: bool
    #: Human-readable reason when reachable is False. None when reachable.
    error: str | None = None
    #: The marine service's parsed /health JSON body, verbatim. None when
    #: reachable is False.
    health: dict[str, Any] | None = None


@router.get("/marine/health", response_model=MarineHealthResponse)
async def marine_health(request: Request) -> MarineHealthResponse:
    """Pass-through to the marine service's own health state (Marine Model
    Restoration Plan B4, part A).

    GET {marine_service_url}/health -> {reachable, error, health}. The marine
    service's own /health is auth-exempt (API-MANUAL §19.7), so no bearer
    token is sent. Feeds the admin status page (stack repo), which is the
    only consumer — nothing in Clear Skies may contact the marine service
    except through the API (ARCHITECTURE.md "The marine service is an add-on
    reached only through the API — INVARIANT").

    Always HTTP 200 (except an auth failure from require_setup_session): an
    unreachable marine service is itself a status the operator needs to see
    rendered, not an error page. Never raises past this handler.
    """
    await require_setup_session(request)

    config_dir: Path = request.app.state.config_dir
    marine_service_url, verify_tls = _read_marine_service_connection(config_dir)
    if not marine_service_url:
        return MarineHealthResponse(
            reachable=False, error="Marine service is not configured", health=None
        )

    import httpx  # noqa: PLC0415 — lazy import; mirrors providers_test_marine()

    health_url = marine_service_url.rstrip("/") + "/health"

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=verify_tls) as client:
            resp = await client.get(health_url)
    except httpx.ConnectError as exc:
        logger.debug("marine_health ConnectError for %s: %s", health_url, exc)
        return MarineHealthResponse(reachable=False, error="Connection refused", health=None)
    except httpx.TimeoutException:
        return MarineHealthResponse(reachable=False, error="Connection timed out", health=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("marine_health unexpected error for %s: %s", health_url, type(exc).__name__)
        return MarineHealthResponse(
            reachable=False, error=f"Connection failed: {type(exc).__name__}", health=None
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        return MarineHealthResponse(
            reachable=False,
            error=f"Marine service returned HTTP {resp.status_code}",
            health=None,
        )

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None

    if not isinstance(body, dict):
        return MarineHealthResponse(
            reachable=False,
            error="Marine service returned a non-JSON response",
            health=None,
        )

    return MarineHealthResponse(reachable=True, error=None, health=body)


class MarineNdbcStationEntry(BaseModel):
    station_id: str
    name: str
    lat: float
    lon: float
    distance_miles: float
    capabilities: list[str]
    quality: str


class MarineCoopsStationEntry(BaseModel):
    station_id: str
    name: str
    lat: float
    lon: float
    distance_miles: float
    products: list[str]
    quality: str


class MarineStationDiscoveryResponse(BaseModel):
    ndbc_stations: list[MarineNdbcStationEntry]
    coops_stations: list[MarineCoopsStationEntry]


def _ndbc_quality(distance_miles: float) -> str:
    """NDBC wave buoy quality tier (PROVIDER-MANUAL §14 round brief spec)."""
    if distance_miles <= 25:
        return "excellent"
    if distance_miles <= 50:
        return "good"
    return "fair"


def _coops_quality(distance_miles: float) -> str:
    """CO-OPS tide station quality tier (PROVIDER-MANUAL §14 round brief spec)."""
    if distance_miles <= 20:
        return "excellent"
    if distance_miles <= 40:
        return "good"
    return "fair"


def _ndbc_capabilities(capability_guess: str) -> list[str]:
    """Map ndbc.py's best-effort capabilityGuess to a capability-string list.

    ndbc.discover_stations() already filters out stations with no atmospheric
    data (capabilityGuess == "none" never reaches this function).
    """
    if capability_guess == "atmospheric_only":
        return ["atmospheric"]
    if capability_guess == "wave_atmospheric":
        return ["waves", "atmospheric", "water_temp"]
    return []


@router.get("/marine/discover-stations", response_model=MarineStationDiscoveryResponse)
async def marine_discover_stations(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_miles: float = Query(50.0, gt=0, le=500),
) -> MarineStationDiscoveryResponse:
    """Discover nearby NDBC buoy and CO-OPS tide/water-level stations for
    marine location setup (OPERATIONS-MANUAL.md "Marine location setup
    procedure" steps 3-4; PROVIDER-MANUAL §14.1, §14.2).

    C-42: this used to delegate directly to the provider discovery
    functions (ndbc.discover_stations / coops.discover_stations). Both are
    now pass-throughs to the marine service's
    ``/discovery/buoy-stations`` / ``/discovery/tide-stations`` — this
    endpoint only converts units and applies the quality-tier scoring, same
    as before. This is the wizard's live "nearby stations" query, so a
    marine-service outage or missing configuration must surface as an
    honest 503 (``_marine_discovery_http_exception()``) — never as an empty
    station list, which would read as "there are no buoys near you."
    """
    await require_setup_session(request)

    radius_km = convert(radius_miles, "mile", "km") or 0.0

    try:
        ndbc_raw = _discover_ndbc_stations(lat, lon, radius_km)
        coops_raw = _discover_coops_stations(lat, lon, radius_km)
    except MarineDiscoveryError as exc:
        raise _marine_discovery_http_exception(exc) from exc

    ndbc_stations: list[MarineNdbcStationEntry] = []
    for s in ndbc_raw:
        distance_miles = round(convert(float(s["distanceKm"]), "km", "mile") or 0.0, 1)
        ndbc_stations.append(
            MarineNdbcStationEntry(
                station_id=str(s["stationId"]),
                name=str(s["name"]),
                lat=float(s["lat"]),
                lon=float(s["lon"]),
                distance_miles=distance_miles,
                capabilities=_ndbc_capabilities(str(s["capabilityGuess"])),
                quality=_ndbc_quality(distance_miles),
            )
        )

    coops_stations: list[MarineCoopsStationEntry] = []
    for s in coops_raw:
        distance_miles = round(convert(float(s["distance_km"]), "km", "mile") or 0.0, 1)
        coops_stations.append(
            MarineCoopsStationEntry(
                station_id=str(s["id"]),
                name=str(s["name"] or s["id"]),
                lat=float(s["lat"]),
                lon=float(s["lon"]),
                distance_miles=distance_miles,
                products=list(s["products"]),
                quality=_coops_quality(distance_miles),
            )
        )

    return MarineStationDiscoveryResponse(ndbc_stations=ndbc_stations, coops_stations=coops_stations)


class MarineSpeciesResponse(BaseModel):
    region: str
    species: list[str]


@router.get("/marine/species", response_model=MarineSpeciesResponse)
async def marine_species(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, description="Spot latitude"),
    lon: float = Query(..., ge=-180, le=180, description="Spot longitude"),
    category: str = Query(
        "saltwater_inshore",
        description=(
            "Target fishing category. Accepts a comma-separated list "
            "(T6.3) — e.g. 'saltwater_inshore,bottom_fish' — to select "
            "species from multiple categories at once."
        ),
    ),
) -> MarineSpeciesResponse:
    """Return available species for a coordinate + one or more fishing
    target categories (T2.5; multi-category union added T6.3).

    Used by the wizard to populate species checkboxes for a marine fishing
    spot, based on the biogeographic region covering the spot's coordinates.
    Delegates entirely to the existing hardcoded lookup tables in
    ``enrichment/fishing_species.py`` (API-MANUAL §17: "Species data is
    hardcoded lookup tables ... keyed by biogeographic region and target
    category. No external API.") — this endpoint performs no I/O.

    ``category`` accepts a comma-separated list of categories; the response
    is the deduplicated union of species across all of them, order
    preserved by first appearance.
    """
    await require_setup_session(request)

    categories = [c.strip() for c in category.split(",") if c.strip()]
    if not categories:
        raise HTTPException(422, detail="category must not be empty")
    bad = sorted(c for c in categories if c not in _VALID_TARGET_CATEGORIES)
    if bad:
        raise HTTPException(
            422,
            detail=f"category {bad!r} not in {sorted(_VALID_TARGET_CATEGORIES)}",
        )

    region = _classify_fishing_region(lat, lon)
    region_species = SPECIES_BY_REGION.get(region, {})
    species: list[str] = []
    seen: set[str] = set()
    for cat in categories:
        for name in region_species.get(cat, []):
            if name not in seen:
                seen.add(name)
                species.append(name)
    return MarineSpeciesResponse(region=region, species=species)


class MarineSpeciesDatabaseResponse(BaseModel):
    regions: dict[str, dict[str, float]]
    species_by_region: dict[str, dict[str, list[str]]]
    species_profiles: dict[str, dict[str, Any]]
    seasonal_behavior: dict[str, dict[int, dict[str, Any]]]
    source_path: str


@router.get("/marine/species-database", response_model=MarineSpeciesDatabaseResponse)
async def marine_species_database(request: Request) -> MarineSpeciesDatabaseResponse:
    """Dump the full loaded species reference data for admin/wizard reference (T8.2).

    Externalizing ``enrichment/fishing_species.py``'s data tables from
    hardcoded Python dicts to an operator-editable ``data/species.yaml``
    file (T8.2) means an admin/wizard client has no other way to inspect
    what's currently loaded (regions, species-by-region, per-species
    scoring profiles, seasonal behavior) short of reading the YAML file on
    disk directly. This endpoint returns exactly what the module loaded at
    process start — no re-read from disk, no transformation beyond the
    response model's field naming.

    ``source_path`` is the bundled default location relative to the
    package root (``data/species.yaml``). A future config override
    (``api.conf [fishing] species_data_path``) is documented in
    ``fishing_species.py``'s module docstring but not implemented yet — the
    loader always uses the bundled default today, so this is always
    ``"data/species.yaml"`` for now.
    """
    await require_setup_session(request)

    return MarineSpeciesDatabaseResponse(
        regions=BIOGEOGRAPHIC_REGIONS,
        species_by_region=SPECIES_BY_REGION,
        species_profiles=SPECIES_PROFILES,
        seasonal_behavior=SEASONAL_BEHAVIOR,
        source_path="data/species.yaml",
    )


# ---------------------------------------------------------------------------
# Coastal structure discovery via OpenStreetMap Overpass API (T5.2)
#
# Not a dispatch-registered provider module (no CAPABILITY, no
# PROVIDER_MODULES entry) — same "supporting component" category as
# enrichment/bathymetry.py (PROVIDER-MANUAL §14.7) and
# providers/_common/nws_zones.py (PROVIDER-MANUAL §14.8): a setup-time-only
# data-access helper. It still goes through ProviderHTTPClient (retry/
# backoff/canonical error taxonomy) and get_cache() (pluggable memory/Redis
# backend, ADR-017) rather than raw httpx or a hand-rolled Redis client —
# PROVIDER-MANUAL §1 "ProviderHTTPClient" and §3 "Cache key construction"
# apply to every outbound HTTP call in this codebase, not just
# dispatch-registered provider modules (lead-confirmed 2026-07-11, this
# round: the original brief's "use httpx directly" + raw-Redis-key
# instructions predated that check and were superseded).
# ---------------------------------------------------------------------------

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_OVERPASS_USER_AGENT = "ClearSkies-WeatherStation/1.0 (structure-discovery)"
_OVERPASS_PROVIDER_ID = "overpass"
_OVERPASS_DOMAIN = "marine"
_STRUCTURE_DISCOVERY_CACHE_TTL_SECONDS = 86400  # 24h — coastal structures rarely change
_MIN_STRUCTURE_LENGTH_M = 5.0  # filter out sub-5m ways as digitisation noise
_EARTH_RADIUS_M = 6371000.0

# OSM tag value -> our StructureConfig.type (config/marine_config.py
# _VALID_STRUCTURE_TYPES). "seawall" is reachable via two different OSM tag
# spellings (wall=seawall, man_made=dyke); osm_type on the response preserves
# which raw OSM value produced the match.
_OSM_STRUCTURE_TYPE_MAP: dict[str, str] = {
    "breakwater": "breakwater",
    "groyne": "groin",
    "pier": "pier",
    "seawall": "seawall",
    "dyke": "seawall",
}

# OSM material tag -> our StructureConfig.material (config/marine_config.py
# _VALID_STRUCTURE_MATERIALS). Values not present here (missing tag, or an
# OSM value we don't recognise) map to None — the operator must choose.
_OSM_MATERIAL_MAP: dict[str, str] = {
    "concrete": "impermeable",
    "rock": "semi_permeable",
    "stone": "semi_permeable",
    "wood": "permeable",
    "metal": "semi_permeable",
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters.

    Local to this module — no project-wide haversine helper exists (every
    other module that needs one, e.g. providers/buoy/ndbc.py, providers/
    tides/coops.py, providers/_common/nws_zones.py, services/faults.py,
    endpoints/earthquakes.py, implements its own private copy; same
    established pattern, see rules/coding.md §3 DRY rule discussion in
    nws_zones.py's module docstring).
    """
    lat1_r, lon1_r, lat2_r, lon2_r = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


def _initial_bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in degrees [0, 360).

    Standard forward-azimuth (geodesic bearing) formula: 0=N, 90=E, 180=S,
    270=W.
    """
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    x = math.sin(dlon_r) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360.0) % 360.0


_overpass_rate_limiter = RateLimiter(
    # Polite-use guard for the free, shared overpass-api.de instance — this
    # is a setup-time-only endpoint (called once per surf spot, then cached
    # 24h), so 1 req/s never trips in normal use. Same "be polite" rationale
    # as providers/buoy/ndbc.py's NDBC limiter and enrichment/bathymetry.py's
    # ncei-cudem limiter.
    name="overpass-structures",
    provider_id=_OVERPASS_PROVIDER_ID,
    domain=_OVERPASS_DOMAIN,
    max_calls=1,
    window_seconds=1,
)

_overpass_http_client: ProviderHTTPClient | None = None


def _get_overpass_http_client() -> ProviderHTTPClient:
    """Return the module-level HTTP client (one instance, not per-request)."""
    global _overpass_http_client  # noqa: PLW0603
    if _overpass_http_client is None:
        _overpass_http_client = ProviderHTTPClient(
            provider_id=_OVERPASS_PROVIDER_ID,
            domain=_OVERPASS_DOMAIN,
            user_agent=_OVERPASS_USER_AGENT,
            # Overpass queries can take a few seconds server-side even with
            # the query's own [timeout:10]; give the HTTP layer headroom
            # beyond that so we see the server's own timeout response rather
            # than cutting it off client-side first.
            read_timeout=15.0,
        )
    return _overpass_http_client


def _reset_overpass_http_client_for_tests() -> None:
    """Reset the module-level HTTP client. Used in tests only."""
    global _overpass_http_client  # noqa: PLW0603
    _overpass_http_client = None


def _build_overpass_structure_query(
    lat: float,
    lon: float,
    radius_m: int,
    *,
    bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """Build the Overpass QL query for coastal structures.

    When *bbox* is provided (south, west, north, east), uses a bbox filter
    instead of a radius-around filter. The bbox aligns with the Level 3 grid
    domain computed by the marine service's ``swan_domain.py`` (C-48, moved
    out of this repo), ensuring structure discovery covers the exact area
    SWAN will model.
    """
    if bbox is not None:
        south, west, north, east = bbox
        area_filter = f"{south},{west},{north},{east}"
        return (
            "[out:json][timeout:10];\n"
            "(\n"
            f'  way["man_made"~"breakwater|groyne|pier"]({area_filter});\n'
            f'  way["wall"="seawall"]({area_filter});\n'
            f'  way["man_made"="dyke"]({area_filter});\n'
            ");\n"
            "out body geom;"
        )
    return (
        "[out:json][timeout:10];\n"
        "(\n"
        f'  way["man_made"~"breakwater|groyne|pier"](around:{radius_m},{lat},{lon});\n'
        f'  way["wall"="seawall"](around:{radius_m},{lat},{lon});\n'
        f'  way["man_made"="dyke"](around:{radius_m},{lat},{lon});\n'
        ");\n"
        "out body geom;"
    )


def _build_structure_discovery_cache_key(lat: float, lon: float, radius_m: int) -> str:
    """Deterministic cache key: hash of (provider_id, endpoint, normalized_params).

    Same construction as providers/_common/nws_zones.py's cache key builders
    (PROVIDER-MANUAL §3 "Cache key construction") — lat/lon rounded to 4
    decimal places, not the brief's original 3dp, so structure-discovery
    cache entries follow the one established convention used everywhere else
    in this codebase rather than a bespoke one for this endpoint alone.
    """
    payload = json.dumps(
        {
            "provider_id": _OVERPASS_PROVIDER_ID,
            "endpoint": "structure_discovery",
            "params": {"lat4": round(lat, 4), "lon4": round(lon, 4), "radius_m": radius_m},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _classify_osm_way(tags: dict[str, Any]) -> str | None:
    """Return the raw OSM tag value identifying a coastal structure way, or
    None if the way's tags don't match any of the query filters (defensive
    — the Overpass query itself already filters server-side, but a
    conservative client-side re-check costs nothing and guards against a
    future query-string change drifting out of sync with this parser).
    """
    man_made = tags.get("man_made")
    if man_made in ("breakwater", "groyne", "pier", "dyke"):
        return str(man_made)
    if tags.get("wall") == "seawall":
        return "seawall"
    return None


class MarineDiscoveredStructure(BaseModel):
    osm_id: int
    #: Raw OSM tag value that matched (e.g. "breakwater", "groyne", "pier",
    #: "seawall", "dyke") — distinct from `type` when the OSM vocabulary and
    #: our canonical vocabulary diverge (groyne -> groin; dyke -> seawall).
    osm_type: str
    name: str | None = None
    #: Mapped to config/marine_config.py's StructureConfig.type vocabulary.
    type: str
    #: Mapped to config/marine_config.py's StructureConfig.material
    #: vocabulary; null when the OSM `material` tag is missing or not one we
    #: recognise — the operator must choose in that case.
    material: str | None = None
    #: "osm" when `material` was mapped from an OSM tag, "operator" when the
    #: operator must supply it themselves.
    material_source: str
    length_m: float
    bearing_degrees: float
    distance_m: float
    #: [[lat, lon], ...] — the way's node coordinates in OSM order.
    geometry: list[list[float]]


class MarineStructureDiscoveryResponse(BaseModel):
    structures: list[MarineDiscoveredStructure]
    query_radius_m: int
    source: str = "openstreetmap_overpass"
    #: Populated (structures left empty) when the Overpass call failed —
    #: timeout, rate limit, or server error. Never a 500; graceful
    #: degradation mirrors enrichment/bathymetry.py's "best-effort
    #: setup-time convenience" pattern (PROVIDER-MANUAL §14.7).
    error: str | None = None


def _parse_overpass_structures(
    elements: list[dict[str, Any]], lat: float, lon: float
) -> list[MarineDiscoveredStructure]:
    """Translate Overpass `elements` into MarineDiscoveredStructure entries.

    Filters out `floating=yes` ways (marina dock fingers — irrelevant to
    wave physics) and ways shorter than _MIN_STRUCTURE_LENGTH_M (digitisation
    noise). Sorted by distance_m ascending (nearest first) by the caller.
    """
    structures: list[MarineDiscoveredStructure] = []
    for el in elements:
        if el.get("type") != "way":
            continue

        tags = el.get("tags") or {}
        if str(tags.get("floating", "")).strip().lower() == "yes":
            continue

        raw_type = _classify_osm_way(tags)
        if raw_type is None:
            continue

        geometry_raw = el.get("geometry") or []
        points: list[tuple[float, float]] = [
            (float(pt["lat"]), float(pt["lon"]))
            for pt in geometry_raw
            if isinstance(pt, dict) and "lat" in pt and "lon" in pt
        ]
        if len(points) < 2:
            continue

        # Detect closed polygons (area outlines like pier footprints).
        is_closed = (
            len(points) > 3
            and abs(points[0][0] - points[-1][0]) < 1e-7
            and abs(points[0][1] - points[-1][1]) < 1e-7
        )

        if is_closed:
            # For closed polygons, find the two most distant vertices
            # (the principal/long axis) to get meaningful length and bearing.
            max_dist = 0.0
            p_a, p_b = points[0], points[1]
            for pi in range(len(points)):
                for pj in range(pi + 1, len(points)):
                    d = _haversine_m(
                        points[pi][0], points[pi][1],
                        points[pj][0], points[pj][1],
                    )
                    if d > max_dist:
                        max_dist = d
                        p_a, p_b = points[pi], points[pj]
            length_m = max_dist
            bearing_degrees = _initial_bearing_degrees(
                p_a[0], p_a[1], p_b[0], p_b[1]
            )
        else:
            length_m = sum(
                _haversine_m(lat_a, lon_a, lat_b, lon_b)
                for (lat_a, lon_a), (lat_b, lon_b) in zip(
                    points, points[1:], strict=False
                )
            )
            bearing_degrees = _initial_bearing_degrees(
                points[0][0], points[0][1], points[-1][0], points[-1][1]
            )

        if length_m < _MIN_STRUCTURE_LENGTH_M:
            continue
        nearest_point = min(
            points, key=lambda p: _haversine_m(lat, lon, p[0], p[1])
        )
        distance_m = _haversine_m(lat, lon, nearest_point[0], nearest_point[1])

        raw_material = str(tags.get("material", "")).strip().lower()
        material = _OSM_MATERIAL_MAP.get(raw_material)

        name = tags.get("name")

        structures.append(
            MarineDiscoveredStructure(
                osm_id=int(el.get("id", 0)),
                osm_type=raw_type,
                name=str(name) if name else None,
                type=_OSM_STRUCTURE_TYPE_MAP[raw_type],
                material=material,
                material_source="osm" if material is not None else "operator",
                length_m=round(length_m, 1),
                bearing_degrees=round(bearing_degrees, 1),
                distance_m=round(distance_m, 1),
                geometry=[[p_lat, p_lon] for p_lat, p_lon in points],
            )
        )

    structures.sort(key=lambda s: s.distance_m)
    return structures


@router.get("/marine/discover-structures", response_model=MarineStructureDiscoveryResponse)
async def marine_discover_structures(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, description="Surf spot latitude"),
    lon: float = Query(..., ge=-180, le=180, description="Surf spot longitude"),
    radius_m: int = Query(2000, gt=0, le=20000, description="Search radius in meters"),
    bbox_south: float | None = Query(None, ge=-90, le=90, description="Bbox south (Level 3 grid)"),
    bbox_west: float | None = Query(None, ge=-180, le=180, description="Bbox west"),
    bbox_north: float | None = Query(None, ge=-90, le=90, description="Bbox north"),
    bbox_east: float | None = Query(None, ge=-180, le=180, description="Bbox east"),
) -> MarineStructureDiscoveryResponse:
    """Discover nearby coastal structures (jetties, piers, breakwaters,
    seawalls, groins) via the OpenStreetMap Overpass API (T5.2).

    Used by the wizard to auto-populate the `structures` list of a surf
    spot's config/marine_config.py StructureConfig entries, which feed the
    wave_transform coastal-structure transmission/reflection correction
    (API-MANUAL §17 "Supplement 2 — Coastal structure effects").

    When bbox parameters are provided (computed from the Level 3 grid
    extent by the wizard), uses a bbox Overpass query. Otherwise falls back
    to radius-based discovery around lat/lon.

    Results are cached 24h (structures rarely change) via get_cache() — see
    _build_structure_discovery_cache_key() docstring for the key
    construction. A cache hit skips the Overpass call entirely.

    Graceful degradation: any ProviderError from the Overpass call (timeout,
    quota, 5xx after retries, unexpected response shape) is caught here and
    returns 200 with an empty `structures` list and `error` populated —
    never a 500. Not cached, so the next call retries live.
    """
    await require_setup_session(request)

    bbox: tuple[float, float, float, float] | None = None
    if all(v is not None for v in (bbox_south, bbox_west, bbox_north, bbox_east)):
        bbox = (bbox_south, bbox_west, bbox_north, bbox_east)  # type: ignore[arg-type]

    # Include bbox in cache key when provided so bbox queries don't return
    # cached radius-based results (audit F4).
    if bbox is not None:
        cache_key = _build_structure_discovery_cache_key(
            lat, lon, radius_m
        ) + f"_bbox_{bbox[0]:.4f}_{bbox[1]:.4f}_{bbox[2]:.4f}_{bbox[3]:.4f}"
    else:
        cache_key = _build_structure_discovery_cache_key(lat, lon, radius_m)
    cached = get_cache().get(cache_key)
    if cached is not None:
        return MarineStructureDiscoveryResponse.model_validate(cached)

    query = _build_overpass_structure_query(lat, lon, radius_m, bbox=bbox)
    client = _get_overpass_http_client()

    try:
        _overpass_rate_limiter.acquire()
        response = client.get(_OVERPASS_URL, params={"data": query})
        wire = response.json()
    except ProviderError as exc:
        logger.warning(
            "Overpass structure discovery failed for lat=%s,lon=%s,radius_m=%d (%s: %s)",
            lat,
            lon,
            radius_m,
            type(exc).__name__,
            exc,
        )
        return MarineStructureDiscoveryResponse(
            structures=[],
            query_radius_m=radius_m,
            error=f"Structure discovery unavailable: {exc}",
        )
    except ValueError as exc:
        # response.json() decode failure — Overpass returned a non-JSON body
        # (e.g. an HTML rate-limit page on a 200 response).
        logger.warning("Overpass response JSON decode failed: %s", exc)
        return MarineStructureDiscoveryResponse(
            structures=[],
            query_radius_m=radius_m,
            error="Structure discovery unavailable: invalid response from Overpass API.",
        )

    elements = wire.get("elements", []) if isinstance(wire, dict) else []
    structures = _parse_overpass_structures(elements, lat, lon)

    result = MarineStructureDiscoveryResponse(structures=structures, query_radius_m=radius_m)
    get_cache().set(
        cache_key, result.model_dump(), ttl_seconds=_STRUCTURE_DISCOVERY_CACHE_TTL_SECONDS
    )
    return result


def _check_restart_token(request: Request) -> bool:
    """Return True if the request carries a valid one-time restart token.

    The token is issued by /setup/apply (stored in app.state) and is valid for
    60 s.  It is consumed on first use so it cannot be replayed.  This lets the
    wizard trigger a restart immediately after apply before the process has
    reloaded its environment (the proxy secret was just written to disk).
    """
    provided = request.headers.get("X-Clearskies-Restart-Token", "").strip()
    if not provided:
        return False
    stored = getattr(request.app.state, "restart_token", None)
    if not stored:
        return False
    expires = getattr(request.app.state, "restart_token_expires", 0.0)
    if time.monotonic() > expires:
        # Expired — clear so it cannot be used again even if timing is borderline.
        request.app.state.restart_token = None
        return False
    if not hmac.compare_digest(stored.encode("utf-8"), provided.encode("utf-8")):
        return False
    # Consume: token is single-use.
    request.app.state.restart_token = None
    return True


@router.post("/restart", response_model=RestartResponse)
async def restart(request: Request, background_tasks: BackgroundTasks) -> RestartResponse:
    """Trigger a graceful service restart.

    Accepts two authentication mechanisms (in priority order):

    1. **One-time restart token** (``X-Clearskies-Restart-Token`` header):
       Issued by /setup/apply, valid for 60 s, single-use.  Used by the wizard
       to restart the API immediately after the first-run apply, before the
       running process has reloaded its environment with the newly-written
       WEEWX_CLEARSKIES_PROXY_SECRET.

    2. **Proxy auth** (``X-Clearskies-Proxy-Auth`` header):
       The normal mechanism for restarts triggered outside the wizard apply
       flow (e.g. admin re-runs, external tooling).  Requires
       WEEWX_CLEARSKIES_PROXY_SECRET to be set in the process environment.

    After the 200 response is sent, a background task waits 1.5 s then sends
    SIGTERM to the running process.  Uvicorn handles SIGTERM gracefully (flushes
    in-flight requests, shuts down the event loop).  The process supervisor
    (systemd Restart=always or Docker restart: unless-stopped) brings the process
    back with fresh config loaded from disk.

    Security: an unauthenticated restart endpoint would be a DoS vector.  Both
    auth paths are constant-time compared.
    """
    authed_via_token = _check_restart_token(request)
    if not authed_via_token:
        secret_configured = bool(os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip())
        if not secret_configured:
            raise HTTPException(
                503,
                detail="Proxy secret not configured — restart endpoint unavailable. "
                "Use the wizard to complete setup, which issues a one-time restart token.",
            )
        if not _check_proxy_auth(request):
            raise HTTPException(401, detail="Valid X-Clearskies-Proxy-Auth or X-Clearskies-Restart-Token header required")

    logger.warning(
        "Restart requested via /setup/restart from %s (auth: %s) — scheduling graceful shutdown",
        request.client.host if request.client else "unknown",
        "restart-token" if authed_via_token else "proxy-auth",
    )

    async def _deferred_sigterm() -> None:
        await asyncio.sleep(1.5)
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(_deferred_sigterm)
    return RestartResponse(status="restarting")


# ---------------------------------------------------------------------------
# Forecast correction admin endpoints (ADR-079 Phase 5)
# ---------------------------------------------------------------------------


@router.get("/forecast-correction/status")
def get_forecast_correction_status(
    _: TrustManager = Depends(require_setup_active),
) -> CorrectionStatusResponse:
    """Return the current state of the forecast correction engine.

    Reports model availability, enabled/active flags, pair-count statistics,
    and the metrics from the last training run.  Requires proxy auth.
    """
    from weewx_clearskies_api.correction import db as correction_db  # noqa: PLC0415
    from weewx_clearskies_api.correction.corrector import is_active, get_enabled  # noqa: PLC0415

    fc_settings = _forecast_correction_settings

    # DB may not be initialised when both enabled and collection_enabled are False.
    # In that case return zeroed-out stats rather than raising a 500.
    try:
        metadata = correction_db.get_model_metadata()
        pair_count = correction_db.get_pair_count()
        date_start, date_end = correction_db.get_date_range()
    except RuntimeError:
        metadata = None
        pair_count = 0
        date_start = None
        date_end = None

    # Read enabled from the corrector's runtime state, not the settings object.
    # The toggle endpoint changes the runtime flag; the settings object reflects
    # what was in api.conf at startup and is not updated by the toggle.
    enabled: bool = get_enabled()

    # Resolve collection_enabled: runtime override wins, then settings default.
    if _collection_enabled_override is not None:
        collection_enabled: bool = _collection_enabled_override
    else:
        collection_enabled = bool(
            getattr(fc_settings, "collection_enabled", False)
            if fc_settings is not None
            else False
        )

    retrain_schedule: str = str(
        getattr(fc_settings, "retrain_schedule", "manual")
        if fc_settings is not None
        else "manual"
    )

    _model_path = metadata.get("model_path") if metadata else None
    return CorrectionStatusResponse(
        model_available=(
            _model_path is not None and os.path.exists(_model_path)
        ),
        is_active=is_active(),
        enabled=enabled,
        collection_enabled=collection_enabled,
        retrain_schedule=retrain_schedule,
        pair_count=pair_count,
        date_range_start=date_start,
        date_range_end=date_end,
        last_trained=metadata.get("last_trained") if metadata else None,
        sample_count=metadata.get("sample_count") if metadata else None,
        mae_raw=metadata.get("mae_raw") if metadata else None,
        mae_corrected=metadata.get("mae_corrected") if metadata else None,
        provider_score=metadata.get("provider_score") if metadata else None,
        correction_score=metadata.get("correction_score") if metadata else None,
        training_status=metadata.get("training_status") if metadata else None,
    )


@router.post("/forecast-correction/toggle")
def toggle_forecast_correction(
    request: Request,
    body: CorrectionToggleRequest,
    _: TrustManager = Depends(require_setup_active),
) -> CorrectionToggleResponse:
    """Toggle correction and/or collection on or off at runtime.

    Updates the corrector module's enabled state immediately AND persists
    the new values to ``api.conf`` so they survive service restarts.  The
    collection_enabled toggle signals the live collector thread via
    ``set_collection_enabled()`` — the next tick respects the new state.

    Either or both fields may be provided.  Providing neither is a no-op that
    returns the current state.  Requires proxy auth.
    """
    global _collection_enabled_override  # noqa: PLW0603

    import weewx_clearskies_api.correction.corrector as _corrector_mod  # noqa: PLC0415

    if body.enabled is not None:
        _corrector_mod.set_enabled(body.enabled)
        current_enabled: bool = body.enabled
    else:
        current_enabled = _corrector_mod.get_enabled()

    if body.collection_enabled is not None:
        _collection_enabled_override = body.collection_enabled
        current_collection_enabled: bool = body.collection_enabled
        from weewx_clearskies_api.correction.collector import set_collection_enabled  # noqa: PLC0415
        set_collection_enabled(body.collection_enabled)
    else:
        # Return current effective collection_enabled.
        if _collection_enabled_override is not None:
            current_collection_enabled = _collection_enabled_override
        else:
            fc_settings = _forecast_correction_settings
            current_collection_enabled = bool(
                getattr(fc_settings, "collection_enabled", False)
                if fc_settings is not None
                else False
            )

    # Persist to api.conf so the toggle survives service restarts.
    try:
        config_dir: Path = request.app.state.config_dir
        conf_path = config_dir / "api.conf"
        if conf_path.exists():
            cfg = configobj.ConfigObj(str(conf_path), interpolation=False)
            if "forecast_correction" not in cfg:
                cfg["forecast_correction"] = {}
            if body.enabled is not None:
                cfg["forecast_correction"]["enabled"] = str(current_enabled).lower()
            if body.collection_enabled is not None:
                cfg["forecast_correction"]["collection_enabled"] = str(current_collection_enabled).lower()
            cfg.write()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist toggle to api.conf: %s", exc)

    return CorrectionToggleResponse(
        enabled=current_enabled,
        collection_enabled=current_collection_enabled,
    )


@router.post("/forecast-correction/retrain")
def retrain_forecast_correction(
    _: TrustManager = Depends(require_setup_active),
) -> RetrainResponse:
    """Trigger an immediate model retrain.

    Runs train_model() synchronously on the request thread (training typically
    takes < 5 s on datasets up to ~50 k pairs).  On success, hot-reloads the
    new model via reload_model() so subsequent forecast requests use it without
    a process restart.

    Returns success=False (not an HTTP error) when the min_samples gate is not
    met — this is a normal operational state.  Requires proxy auth.
    """
    from weewx_clearskies_api.correction.trainer import train_model  # noqa: PLC0415
    from weewx_clearskies_api.correction.corrector import reload_model  # noqa: PLC0415

    fc_settings = _forecast_correction_settings
    if fc_settings is None:
        return RetrainResponse(
            success=False,
            message="Forecast correction is not configured; cannot retrain.",
        )

    try:
        result = train_model(fc_settings)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Forecast correction retrain endpoint: train_model raised an exception: %s",
            type(exc).__name__,
            exc_info=True,
        )
        return RetrainResponse(
            success=False,
            message=f"Training failed: {type(exc).__name__}",
        )

    if result.get("success"):
        reload_model()

    return RetrainResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        sample_count=result.get("sample_count"),
        mae_raw=result.get("mae_raw"),
        mae_corrected=result.get("mae_corrected"),
        provider_score=result.get("provider_score"),
        correction_score=result.get("correction_score"),
    )


# ---------------------------------------------------------------------------
# GET /setup/marine/coverage — data source coverage panel (T3.6)
# ---------------------------------------------------------------------------


class _CoverageNearestStation(BaseModel):
    station_id: str
    name: str
    distance_miles: float
    products: list[str] | None = None
    capabilities: list[str] | None = None


class _BathymetryLevelCoverage(BaseModel):
    """Per-SWAN-level bathymetry coverage detail (T22.1)."""

    level: int
    source: str  # "ncei_regional" | "usgs_great_lakes" | "crm" | "operator"
    source_name: str  # Human-readable source name
    resolution_m: float | None = None
    quality: str  # "high" | "degraded"
    quality_label: str  # Human-readable quality label
    # T4.2 — Datum warning fields.  datum_warning is True when the source has
    # an unknown or unverifiable vertical datum (CRM fallback path).
    # vertical_datum is None when the datum is not known at coverage-check time
    # (the actual value is determined during the SWAN depth-grid download).
    # i18n for warning text is deferred; English text is used directly here.
    vertical_datum: str | None = None
    datum_warning: bool = False


class _BathymetryCoverage(BaseModel):
    """Bathymetry coverage summary for a location (T22.1)."""

    overall_quality: str  # min quality across L2 and L3
    levels: list[_BathymetryLevelCoverage]
    warning: str | None = None  # Present when overall_quality == "degraded"
    # T4.2 — True when any level has datum_warning=True.
    datum_warning: bool = False


class MarineCoverageResponse(BaseModel):
    """Response for GET /setup/marine/coverage (T3.6, WATER-TEMPERATURE-DATA-SOURCE-BRIEF)."""

    ofs_model: str | None = None
    ofs_model_resolution_deg: float | None = None
    ofs_fallback: str | None = None
    coverage_tier: str
    available_data: list[str]
    nearest_coops_station: _CoverageNearestStation | None = None
    nearest_ndbc_buoy: _CoverageNearestStation | None = None
    nws_marine_zone: str | None = None
    on_premises_sensor: str
    bathymetry: _BathymetryCoverage | None = None


def _coverage_tier_capabilities(tier: str) -> list[str]:
    """Derive available data capabilities from the coverage tier."""
    if tier == "ofs":
        return [
            "surface_temp",
            "water_column",
            "currents",
            "salinity",
            "modeled_water_levels",
            "forecast",
        ]
    if tier == "regional_erddap":
        return ["surface_temp", "water_column", "currents", "forecast"]
    if tier == "rtofs":
        return ["surface_temp", "water_column", "currents", "salinity", "forecast"]
    if tier == "mur_sst":
        return ["surface_temp"]
    return []


@router.get("/marine/coverage", response_model=MarineCoverageResponse)
async def marine_coverage(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> MarineCoverageResponse:
    """Return data source coverage information for a marine location (T3.6).

    Checks OFS model assignment, nearest stations, NWS zone, and
    on-premises sensor proximity. Used by the wizard and admin to show what
    data sources are available at a given coordinate before/after configuration.

    C-42: OFS assignment, NDBC discovery, and CO-OPS discovery are now
    pass-throughs to the marine service (``services/companion_proxy.py``'s
    ``marine_discovery_get()``) instead of direct provider imports. OFS
    assignment determines this response's ``coverage_tier`` (the primary
    signal this endpoint exists to report), so a marine-service failure
    there fails the whole request honestly (503) rather than silently
    defaulting to "mur_sst" coverage — the same "no wrong answer dressed as
    valid" principle C-42 requires for the station-list endpoint above.
    Nearest-station lookups keep their pre-existing tolerant degrade
    behaviour (log + leave the field null) — that was already the contract
    for any discovery failure here, not a new relaxation introduced by this
    pass-through.
    """
    await require_setup_session(request)

    # --- OFS model assignment ---
    try:
        ofs_primary, ofs_fallback = _discover_ofs_model(lat, lon)
    except MarineDiscoveryError as exc:
        raise _marine_discovery_http_exception(exc) from exc
    # Model-resolution metadata (providers/ocean/ofs.py's _MODEL_RESOLUTION_DEG)
    # moved to the marine service with the rest of the OFS provider code.
    # Best-effort: read it back if the marine service's discovery response
    # includes it; None (the response field's existing default) otherwise.
    ofs_resolution = None

    # --- Coverage tier ---
    if ofs_primary:
        coverage_tier = "ofs"
    else:
        coverage_tier = "mur_sst"

    available_data = _coverage_tier_capabilities(coverage_tier)

    # --- Nearest NDBC buoy ---
    nearest_ndbc: _CoverageNearestStation | None = None
    try:
        ndbc_raw = _discover_ndbc_stations(lat, lon, radius_km=80.5)
        if ndbc_raw:
            s = ndbc_raw[0]
            distance_miles = round(convert(float(s["distanceKm"]), "km", "mile") or 0.0, 1)
            nearest_ndbc = _CoverageNearestStation(
                station_id=str(s["stationId"]),
                name=str(s["name"]),
                distance_miles=distance_miles,
                capabilities=_ndbc_capabilities(str(s["capabilityGuess"])),
            )
    except Exception:
        # Broad on purpose (rules/coding.md notwithstanding): this block
        # tolerates both MarineDiscoveryError (service outage) and a
        # malformed response body the same way it always tolerated a
        # malformed ProviderError-adjacent payload — nearest-station is a
        # bonus field on this response, not its primary signal (see OFS
        # handling above, which does fail the request).
        logger.warning("Coverage: NDBC discovery failed at (%.4f, %.4f)", lat, lon, exc_info=True)

    # --- Nearest CO-OPS station ---
    nearest_coops: _CoverageNearestStation | None = None
    try:
        coops_raw = _discover_coops_stations(lat, lon, radius_km=80.5)
        if coops_raw:
            s = coops_raw[0]
            distance_miles = round(convert(float(s["distance_km"]), "km", "mile") or 0.0, 1)
            nearest_coops = _CoverageNearestStation(
                station_id=str(s["id"]),
                name=str(s["name"] or s["id"]),
                distance_miles=distance_miles,
                products=list(s["products"]),
            )
    except Exception:
        logger.warning("Coverage: CO-OPS discovery failed at (%.4f, %.4f)", lat, lon, exc_info=True)

    # --- NWS marine zone ---
    nws_zone: str | None = None
    try:
        from weewx_clearskies_api.providers._common.nws_zones import (  # noqa: PLC0415
            discover_marine_zones,
        )

        zones = discover_marine_zones(lat, lon, radius_miles=50, user_agent_contact=None)
        if zones:
            nws_zone = zones[0].zone_id
    except Exception:
        logger.warning("Coverage: NWS zone discovery failed at (%.4f, %.4f)", lat, lon, exc_info=True)

    # --- On-premises sensor proximity ---
    try:
        from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
            _haversine_km,
            _dedup_radius_km,
        )
        from weewx_clearskies_api.services.station import get_station_info  # noqa: PLC0415

        station_info = get_station_info()
        if station_info and station_info.latitude and station_info.longitude:
            dist_km = _haversine_km(lat, lon, station_info.latitude, station_info.longitude)
            if dist_km <= _dedup_radius_km:
                on_premises = "within_threshold"
            else:
                on_premises = "too_far"
        else:
            on_premises = "not_configured"
    except Exception:
        on_premises = "not_configured"

    # --- Bathymetry coverage (T22.1; C-48 pass-through) ---
    # GET {marine_service_url}/discovery/bathymetry-coverage?lat&lon ->
    # {overall_quality, levels, warning, datum_warning} (pinned contract,
    # LC-P7-1 — field names, nullability, and the source/quality/warning
    # string constants are lifted verbatim from this endpoint's former
    # in-API computation, so this response deserialises into the same
    # _BathymetryLevelCoverage / _BathymetryCoverage models unchanged).
    # Tolerant degrade (log WARNING, leave bathymetry null) — this was
    # already the contract for this bonus field before the pass-through
    # (see the OFS-assignment handling above, which fails the whole
    # request instead, because it is the primary signal this endpoint
    # exists to report; LC-P7-2 keeps that split).
    bathymetry: _BathymetryCoverage | None = None
    try:
        body = marine_discovery_get(
            "/discovery/bathymetry-coverage", {"lat": lat, "lon": lon}
        )
        bathymetry = _BathymetryCoverage(
            overall_quality=body["overall_quality"],
            levels=[_BathymetryLevelCoverage(**lvl) for lvl in body.get("levels", [])],
            warning=body.get("warning"),
            datum_warning=body.get("datum_warning", False),
        )
    except MarineDiscoveryError:
        logger.warning(
            "Coverage: bathymetry discovery failed at (%.4f, %.4f)", lat, lon, exc_info=True
        )
    except Exception:
        logger.warning(
            "Coverage: bathymetry detection failed at (%.4f, %.4f)", lat, lon, exc_info=True
        )

    return MarineCoverageResponse(
        ofs_model=ofs_primary,
        ofs_model_resolution_deg=ofs_resolution,
        ofs_fallback=ofs_fallback,
        coverage_tier=coverage_tier,
        available_data=available_data,
        nearest_coops_station=nearest_coops,
        nearest_ndbc_buoy=nearest_ndbc,
        nws_marine_zone=nws_zone,
        on_premises_sensor=on_premises,
        bathymetry=bathymetry,
    )


# ---------------------------------------------------------------------------
# Compute estimate (Phase 16 — SWAN-FIXES-PLAN)
# ---------------------------------------------------------------------------


class _ComputeEstimateLevel(BaseModel):
    level: int
    resolution_m: float
    cells: int
    estimated_seconds: float


class _ComputeEstimateCluster(BaseModel):
    spot_ids: list[str]
    cells: int
    estimated_seconds: float


class ComputeEstimateResponse(BaseModel):
    levels: list[_ComputeEstimateLevel]
    clusters: list[_ComputeEstimateCluster]
    total_cells: int
    total_estimated_seconds: float
    cores: int


@router.get("/marine/compute-estimate", response_model=ComputeEstimateResponse)
async def marine_compute_estimate(
    request: Request,
    cores: int = Query(6, ge=1, le=128, description="Available CPU cores for SWAN"),
) -> ComputeEstimateResponse:
    """Pass-through to the marine service's compute-runtime estimate (C-48).

    GET {marine_service_url}/discovery/compute-estimate?cores= ->
    {levels, clusters, total_cells, total_estimated_seconds, cores}
    (pinned contract, LC-P7-1). The marine service owns the domain-sizing
    algorithm (``swan_domain.compute_domains``, moved there in Phase 5/6)
    and the current spot configuration (pushed via ``POST /config``), so
    it is the only place that can answer this question now.

    Called by the wizard/admin marine page to show before/after compute
    cost as the operator adds, removes, or moves surf spots. Hard 503 on
    ``MarineDiscoveryError`` (LC-P7-2) — this is a live wizard query, same
    policy as ``/setup/marine/discover-stations``, never a silently empty
    estimate standing in for "the marine service is unreachable."

    **Pre-existing bug fixed by this rework, not before:** this endpoint
    called ``get_settings()`` without importing it anywhere in this module —
    a ``NameError`` on every request. Confirmed via ``git show`` that the
    endpoint has never worked in its current (pre-rework) form (C-48). The
    rework replaces the whole body, so the dead call is simply gone rather
    than patched.
    """
    await require_setup_session(request)

    try:
        body = marine_discovery_get("/discovery/compute-estimate", {"cores": cores})
    except MarineDiscoveryError as exc:
        raise _marine_discovery_http_exception(exc) from exc

    return ComputeEstimateResponse(
        levels=[_ComputeEstimateLevel(**lvl) for lvl in body.get("levels", [])],
        clusters=[_ComputeEstimateCluster(**c) for c in body.get("clusters", [])],
        total_cells=body.get("total_cells", 0),
        total_estimated_seconds=body.get("total_estimated_seconds", 0.0),
        cores=body.get("cores", cores),
    )


# ---------------------------------------------------------------------------
# Bathymetry upload (Phase 24, T24.1)
# ---------------------------------------------------------------------------

# Accepted datums for operator uploads — must match CO-OPS-supported datums so the
# SWAN pipeline can fetch tide predictions in the same datum (match-at-source, ADR-098).
# LAT, EGM2008, and "Other with manual offset" are NOT accepted in v1; operators must
# convert to one of the supported datums before uploading (e.g. via VDatum or QGIS).
_VALID_UPLOAD_DATUMS: frozenset[str] = frozenset({
    "NAVD88", "MLLW", "MHW", "MHHW", "MSL",
})

# Only GeoTIFF for v1; NetCDF and ASCII XYZ deferred to a future update.
_ACCEPTED_BATHY_EXTENSIONS: frozenset[str] = frozenset({".tif", ".tiff"})

_OPERATOR_BATHY_DIR: Path = Path("/etc/weewx-clearskies/operator_bathymetry")
_OPERATOR_BATHY_TIF: Path = _OPERATOR_BATHY_DIR / "operator.tif"
_OPERATOR_BATHY_META: Path = _OPERATOR_BATHY_DIR / "operator_meta.json"

# Grid level resolution thresholds in metres.
# A file whose native resolution is <= the threshold gives "full" coverage for
# that level; <= 10x the threshold gives "partial"; coarser gives "none".
_BATHY_LEVEL_THRESHOLDS: dict[int, float] = {
    1: 1000.0,   # Level 1: ~1 km grid
    2: 100.0,    # Level 2: ~100 m nearshore
    3: 10.0,     # Level 3: ~10 m surf zone
}


class _BathyUploadLevelCoverage(BaseModel):
    """Per-SWAN-level coverage result for the uploaded bathymetry file."""

    level: int
    coverage: str        # "full" | "partial" | "none"
    coverage_label: str  # locale-resolved string


class BathymetryUploadResponse(BaseModel):
    """Validation result returned by POST /setup/marine/bathymetry/upload."""

    accepted: bool
    status_label: str
    bbox: list[float] | None = None        # [lon_min, lat_min, lon_max, lat_max]
    resolution_m: float | None = None
    crs: str | None = None
    datum: str | None = None
    datum_applied_label: str | None = None
    levels: list[_BathyUploadLevelCoverage] = []
    rejection_reason: str | None = None


def _bathy_coverage_for_resolution(res_m: float, threshold_m: float) -> str:
    """Return coverage tier for *res_m* relative to a level *threshold_m*.

    "full"    — file resolution <= threshold (adequate for this grid level)
    "partial" — file resolution <= 10× threshold (usable but degraded)
    "none"    — file is too coarse to be useful at this level
    """
    if res_m <= threshold_m:
        return "full"
    if res_m <= threshold_m * 10.0:
        return "partial"
    return "none"


@router.post("/marine/bathymetry/upload", response_model=BathymetryUploadResponse)
async def marine_bathymetry_upload(
    request: Request,
    file: UploadFile = File(...),
    datum: str = Form(...),
) -> BathymetryUploadResponse:
    """Accept an operator-supplied GeoTIFF bathymetry file (T24.1, ADR-098).

    Validates the uploaded GeoTIFF using rasterio, saves it to the operator
    bathymetry directory alongside a JSON metadata file that records the
    chosen vertical datum. Returns a validation response with the file bbox,
    native resolution, and per-level coverage assessment.

    Accepted formats (v1): GeoTIFF (.tif, .tiff).  NetCDF and ASCII XYZ
    support is planned for a future update.

    Accepted datums: NAVD88, MLLW, MHW, MHHW, MSL.  These are the datums
    supported by CO-OPS tidal predictions so the SWAN pipeline can fetch
    water level data in the same datum as the bathymetry (match-at-source,
    ADR-098).  If the operator's data is in a different datum (LAT, EGM2008,
    etc.), they must convert it before uploading using VDatum or QGIS.
    No local datum conversion is performed at upload or at SWAN run time.
    """
    await require_setup_session(request)

    from weewx_clearskies_api import i18n  # noqa: PLC0415

    # --- Validate datum input ---
    # Accepted datums must match CO-OPS-supported values so the SWAN pipeline can
    # request tide predictions in the same datum (match-at-source, ADR-098).
    datum = datum.strip()
    if datum not in _VALID_UPLOAD_DATUMS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"datum must be one of: {sorted(_VALID_UPLOAD_DATUMS)}. "
                f"Got: {datum!r}. "
                f"If your data uses a different datum (LAT, EGM2008, etc.), "
                f"convert it to a supported datum before uploading."
            ),
        )

    # --- Validate file extension (v1: GeoTIFF only) ---
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in _ACCEPTED_BATHY_EXTENSIONS:
        return BathymetryUploadResponse(
            accepted=False,
            status_label=i18n.t("marine.bathymetry.upload.rejected"),
            datum=datum,
            rejection_reason=i18n.t("marine.bathymetry.upload.format_unsupported"),
        )

    # --- Require rasterio for GeoTIFF validation ---
    try:
        import rasterio  # type: ignore[import-untyped]
        import rasterio.crs  # type: ignore[import-untyped]
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=(
                "rasterio is not installed — GeoTIFF validation is unavailable. "
                "Install rasterio (add to [nearshore] extra) to enable operator "
                "bathymetry upload."
            ),
        )

    # --- Read the uploaded bytes ---
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")

    # --- Validate the GeoTIFF by opening it in a temp file ---
    import tempfile  # noqa: PLC0415

    bounds = None
    crs_str: str | None = None
    res_m: float | None = None
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            with rasterio.open(str(tmp_path)) as src:
                bounds = src.bounds
                if src.crs:
                    crs_str = src.crs.to_string()
                transform = src.transform
                pixel_width_deg = abs(float(transform.a))
                pixel_height_deg = abs(float(transform.e))
                center_lat = (bounds.bottom + bounds.top) / 2.0
                cos_lat = abs(math.cos(math.radians(center_lat)))
                # Approximate ground resolution (metres) — coarser of x/y
                res_m_x = pixel_width_deg * 111_320.0 * (cos_lat if cos_lat > 1e-6 else 1.0)
                res_m_y = pixel_height_deg * 111_320.0
                res_m = max(res_m_x, res_m_y)
        except Exception as exc:
            return BathymetryUploadResponse(
                accepted=False,
                status_label=i18n.t("marine.bathymetry.upload.rejected"),
                datum=datum,
                rejection_reason=f"GeoTIFF validation failed: {str(exc)[:200]}",
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    # --- Persist to operator directory ---
    try:
        _OPERATOR_BATHY_DIR.mkdir(parents=True, exist_ok=True)
        _OPERATOR_BATHY_TIF.write_bytes(content)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save bathymetry file: {exc}",
        )

    # --- Persist datum metadata ---
    # Key is "vertical_datum" (ADR-098). The SWAN pipeline reads this to determine
    # which datum to request from CO-OPS (match-at-source strategy).
    meta: dict[str, Any] = {
        "vertical_datum": datum,
        "original_filename": filename,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _OPERATOR_BATHY_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        logger.warning(
            "Failed to write bathymetry metadata to %s — continuing",
            _OPERATOR_BATHY_META,
            exc_info=True,
        )

    logger.info(
        "Operator bathymetry uploaded: datum=%s, resolution=%.1fm, bbox=(%s)",
        datum,
        res_m,
        ", ".join(f"{v:.4f}" for v in [bounds.left, bounds.bottom, bounds.right, bounds.top]),
    )

    # --- Compute per-level coverage from file resolution ---
    levels: list[_BathyUploadLevelCoverage] = []
    for level_num, threshold_m in _BATHY_LEVEL_THRESHOLDS.items():
        coverage = _bathy_coverage_for_resolution(res_m, threshold_m)
        levels.append(
            _BathyUploadLevelCoverage(
                level=level_num,
                coverage=coverage,
                coverage_label=i18n.t(f"marine.bathymetry.upload.coverage.{coverage}"),
            )
        )

    # --- Build response ---
    # No datum conversion is applied (ADR-098: match-at-source strategy).
    # datum_applied_label is omitted — the SWAN pipeline fetches CO-OPS predictions
    # in the operator-specified datum at run time; no local conversion happens here.
    return BathymetryUploadResponse(
        accepted=True,
        status_label=i18n.t("marine.bathymetry.upload.accepted"),
        bbox=[bounds.left, bounds.bottom, bounds.right, bounds.top],
        resolution_m=round(res_m, 2),
        crs=crs_str,
        datum=datum,
        levels=levels,
    )


# ---------------------------------------------------------------------------
# Test marine service connectivity (T7.3, C-49, LC-P7-4)
# ---------------------------------------------------------------------------


class TestMarineRequest(BaseModel):
    """Request body for POST /setup/providers/test-marine (LC-P7-4)."""

    model_config = ConfigDict(extra="forbid")

    #: URL of the marine service (e.g. ``https://localhost:8780``).
    url: str
    #: Bearer token (the value of MARINE_SERVICE_SECRET on the marine
    #: service host). Optional — a URL-only test still proves reachability
    #: via the auth-exempt /health endpoint; the secret probe below only
    #: runs when this is supplied.
    secret: str | None = None
    #: Verify TLS certificate on the marine service. Mirrors the
    #: `marine_verify_tls` apply field (API-MANUAL §19.2) so Test
    #: Connection exercises the same trust setting the operator is saving.
    verify_tls: bool = True


class TestMarineResponse(BaseModel):
    """Response from POST /setup/providers/test-marine (LC-P7-4 pinned shape)."""

    #: True when /health responded 2xx and (if a secret was supplied) the
    #: secret probe did not come back 401.
    ok: bool
    #: Marine service package version from the health response, or None.
    version: str | None = None
    #: Number of configured surf spots with valid SWAN output, or None.
    spots: int | None = None
    #: ISO-8601 UTC timestamp of the last successful SWAN run, or None.
    last_run: str | None = None
    #: "ok" | "degraded" from the health response, or None.
    status: str | None = None
    #: Human-readable error description when ok is False.
    error: str | None = None


@router.post("/providers/test-marine", response_model=TestMarineResponse)
async def providers_test_marine(
    body: TestMarineRequest,
    request: Request,
) -> TestMarineResponse:
    """Test connectivity to the marine service (T7.3, LC-P7-4).

    Two probes:

    1. **Primary — GET {url}/health** (no auth; API-MANUAL §19.7 documents
       this endpoint as auth-exempt). Proves reachability and returns
       ``version``/``spots``/``last_run``/``status``. Because /health takes
       no credential, a 200 here proves nothing about whether the supplied
       secret is correct.
    2. **Secondary — GET {url}/discovery/grib-availability, only when a
       secret was supplied.** An existing, cheap, bearer-authenticated
       endpoint (an ``importlib`` spec check on the marine service side;
       C-42). A 401 here is reported as "Secret rejected" rather than
       silently ignored. Without this second probe, a wrong secret would
       pass Test Connection and only fail at the first real config push —
       the "valid response, wrong answer" failure mode this plan exists to
       remove.

    TLS verification follows the operator-supplied ``verify_tls`` (mirrors
    the ``marine_verify_tls`` apply field being configured in the same
    wizard step), unlike the old test-compute endpoint's hardcoded
    ``verify=False``.

    Timeout: 10 s per probe. Auth: requires setup session.
    """
    await require_setup_session(request)

    import httpx  # noqa: PLC0415 — lazy import; httpx is in the project deps

    base_url = body.url.rstrip("/")
    health_url = base_url + "/health"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=body.verify_tls) as client:
            resp = await client.get(health_url)
    except httpx.ConnectError as exc:
        logger.debug("test-marine ConnectError for %s: %s", health_url, exc)
        return TestMarineResponse(ok=False, error="Connection refused")
    except httpx.TimeoutException:
        return TestMarineResponse(ok=False, error="Connection timed out")
    except Exception as exc:  # noqa: BLE001
        logger.warning("test-marine unexpected error for %s: %s", health_url, type(exc).__name__)
        return TestMarineResponse(ok=False, error=f"Connection failed: {type(exc).__name__}")

    if resp.status_code >= 500:
        return TestMarineResponse(
            ok=False, error=f"Marine service error (HTTP {resp.status_code})"
        )
    if resp.status_code >= 400:
        return TestMarineResponse(
            ok=False, error=f"Unexpected response (HTTP {resp.status_code})"
        )

    # Parse informational fields from the health body (API-MANUAL §19.7 shape).
    version: str | None = None
    spots: int | None = None
    last_run: str | None = None
    status: str | None = None
    try:
        data = resp.json()
        if isinstance(data, dict):
            if data.get("version"):
                version = str(data["version"])
            if data.get("spots") is not None:
                spots = int(data["spots"])
            if data.get("last_run"):
                last_run = str(data["last_run"])
            if data.get("status"):
                status = str(data["status"])
    except Exception:  # noqa: BLE001
        pass  # informational only — a non-JSON health body is still a reachable pass

    if not body.secret:
        return TestMarineResponse(
            ok=True, version=version, spots=spots, last_run=last_run, status=status
        )

    # Secondary probe: proves the secret, since /health took none.
    grib_url = base_url + "/discovery/grib-availability"
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=body.verify_tls) as client:
            grib_resp = await client.get(
                grib_url, headers={"Authorization": f"Bearer {body.secret}"}
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "test-marine secret probe failed for %s: %s", grib_url, type(exc).__name__
        )
        return TestMarineResponse(
            ok=False, version=version, spots=spots, last_run=last_run, status=status,
            error="Reachable, but the secret probe request failed",
        )

    if grib_resp.status_code == 401:
        return TestMarineResponse(
            ok=False, version=version, spots=spots, last_run=last_run, status=status,
            error="Secret rejected",
        )
    if grib_resp.status_code >= 400:
        return TestMarineResponse(
            ok=False, version=version, spots=spots, last_run=last_run, status=status,
            error=f"Secret verification failed (HTTP {grib_resp.status_code})",
        )

    return TestMarineResponse(
        ok=True, version=version, spots=spots, last_run=last_run, status=status
    )
