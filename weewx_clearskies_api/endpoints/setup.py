"""Setup endpoints (ADR-038 §secure channel).

Six endpoints that let the config UI wizard pair with the API over TLS during
initial setup.  After initial setup, endpoints accept re-runs authenticated via
X-Clearskies-Proxy-Auth (the same shared secret used for normal API requests).

Endpoints:
  POST /setup/handshake    — exchange trust token for a session_id
  GET  /setup/db-defaults  — return weewx.conf DB connection defaults
  POST /setup/db-test      — test a DB connection with supplied credentials
  GET  /setup/schema       — reflect DB schema using stored db_params
  GET  /setup/station      — return weewx.conf station identity
  POST /setup/apply        — write api.conf + secrets.env, mark setup complete

No /api/v1 prefix — setup is a separate surface registered without a prefix
in app.py.  All endpoints live directly under /setup/...
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
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
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, SchemaReflector
from weewx_clearskies_api.services.station import _get_str_field, _parse_altitude
from weewx_clearskies_api.services.weewx_conf import WeewxConfLoadError, get_weewx_conf, load_weewx_conf
from weewx_clearskies_api.services.weewx_metadata import get_unit_for_group
from weewx_clearskies_api.trust import TrustManager, _read_secrets_env, _write_secrets_env

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])


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
    host: str
    port: int
    user: str
    name: str
    conf_path: str


class DbTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    user: str
    password: str
    name: str


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

    host: str
    port: int
    user: str
    password: str
    name: str


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
      wunderground   → WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY / WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID
      iqair      → WEEWX_CLEARSKIES_IQAIR_KEY
      nws        → no credentials; nws_user_agent_contact is non-secret (api.conf)
    """

    model_config = ConfigDict(extra="forbid")

    #: Provider id (e.g. "nws", "aeris", "openweathermap", "iqair", "wunderground",
    #: "openmeteo", "rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet",
    #: "dwd_radolan", "usgs", "geonet", "emsc", "renass", "iframe").
    provider: str
    #: Primary credential. Maps to provider-specific env var (see class docstring).
    api_key: str | None = None
    #: Secondary credential (Aeris only: client_secret).
    api_secret: str | None = None
    #: Wunderground PWS station ID (required alongside api_key for wunderground).
    pws_station_id: str | None = None
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


class ApplyResponse(BaseModel):
    success: bool
    message: str
    #: One-time token valid for a single POST /setup/restart call within 60 s.
    #: Issued so the wizard can trigger a restart immediately after apply even
    #: before WEEWX_CLEARSKIES_PROXY_SECRET is loaded into the running process's
    #: environment (the secret was just written to secrets.env by this call).
    restart_token: str | None = None


class RestartResponse(BaseModel):
    status: str


class CurrentConfigDatabaseSection(BaseModel):
    host: str
    port: int
    user: str
    password: str
    name: str


class CurrentConfigProviderCredentials(BaseModel):
    """Credential fields for a single provider (all fields optional)."""

    client_id: str | None = None       # Aeris: AERIS_CLIENT_ID
    client_secret: str | None = None   # Aeris: AERIS_CLIENT_SECRET
    appid: str | None = None           # OpenWeatherMap: OPENWEATHERMAP_APPID
    api_key: str | None = None         # Wunderground: WUNDERGROUND_API_KEY
    pws_station_id: str | None = None  # Wunderground: WUNDERGROUND_PWS_STATION_ID
    key: str | None = None             # IQAir: IQAIR_KEY


class CurrentConfigProviderSection(BaseModel):
    provider: str
    credentials: CurrentConfigProviderCredentials


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

    elif p == "wunderground":
        # Provider-scoped; two credentials required for ADR-007 gate.
        if pc.api_key:
            secrets["WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY"] = pc.api_key
        if pc.pws_station_id:
            secrets["WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID"] = pc.pws_station_id

    elif p == "iqair":
        # Domain-scoped (AQI-only provider; Q1 user decision 2026-05-11).
        if pc.api_key:
            secrets["WEEWX_CLEARSKIES_IQAIR_KEY"] = pc.api_key

    # nws, openmeteo, rainviewer, iem_nexrad, noaa_mrms, msc_geomet, dwd_radolan,
    # usgs, geonet, emsc, renass, iframe — all keyless; no env vars to write.
    # (nws_user_agent_contact and iframe_url are non-secret; written to api.conf.)

    return secrets


def _write_api_conf(config_dir: Path, apply: ApplyRequest) -> None:
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
    cfg["database"]["kind"] = "mysql"
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
    """Return DB connection defaults from weewx.conf. Password is never included."""
    tm = await require_setup_session(request)  # noqa: F841 — side-effect: auth check
    settings = request.app.state.settings
    weewx_conf_path: str = settings.weewx.config_path

    cfg = _load_weewx_conf_for_setup(weewx_conf_path)

    host = "localhost"
    port = 3306
    user = "weewx"
    db_name = "weewx"

    if cfg is not None:
        db_section = cfg.get("DatabaseTypes", {})
        # weewx.conf stores MySQL settings under [DatabaseTypes] [[MySQL]]
        mysql_section: dict[str, Any] = {}
        if isinstance(db_section, dict):
            mysql_section = dict(db_section.get("MySQL", {}))

        if mysql_section:
            host = str(mysql_section.get("host", host))
            try:
                port = int(mysql_section.get("port", port))
            except (ValueError, TypeError):
                pass
            user = str(mysql_section.get("user", user))

        # Database name is under [Databases] [[weewx_mysql]] database_name
        databases_section = cfg.get("Databases", {})
        if isinstance(databases_section, dict):
            for _db_key, db_val in databases_section.items():
                if isinstance(db_val, dict):
                    db_type = str(db_val.get("database_type", "")).lower()
                    if db_type == "mysql":
                        db_name = str(db_val.get("database_name", db_name))
                        break

    return DbDefaultsResponse(
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
    db_host = "localhost"
    db_port = 3306
    db_user = ""
    db_name = "weewx"

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
            if db_section.get("host"):
                db_host = str(db_section["host"])
            if db_section.get("port"):
                try:
                    db_port = int(db_section["port"])
                except (ValueError, TypeError):
                    pass
            if db_section.get("name"):
                db_name = str(db_section["name"])

    # DB user and password come from secrets.env (the authoritative source for
    # credentials; api.conf only stores the non-secret DB fields).
    db_user = secrets.get("WEEWX_CLEARSKIES_DB_USER", db_user)
    db_password = secrets.get("WEEWX_CLEARSKIES_DB_PASSWORD", "")

    database = CurrentConfigDatabaseSection(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        name=db_name,
    )

    # --- Providers ---
    # Non-secret fields (provider id) come from api.conf.
    # Credentials come from secrets.env using the provider-scoped naming that
    # _provider_secrets() wrote at apply time.
    _PROVIDER_DOMAINS = ("forecast", "aqi", "alerts", "radar", "earthquakes")
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
        elif p == "wunderground":
            creds.api_key = secrets.get("WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY") or None
            creds.pws_station_id = secrets.get("WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID") or None
        elif p == "iqair":
            creds.key = secrets.get("WEEWX_CLEARSKIES_IQAIR_KEY") or None
        # Keyless providers (nws, openmeteo, rainviewer, etc.) have no credential fields.
        providers[domain] = CurrentConfigProviderSection(
            provider=provider_id,
            credentials=creds,
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
