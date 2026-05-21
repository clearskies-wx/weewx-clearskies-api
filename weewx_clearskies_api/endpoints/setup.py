"""Setup endpoints (ADR-038 §secure channel).

Six endpoints that let the config UI wizard pair with the API over TLS during
initial setup.  All endpoints return 410 once setup_complete is True.

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

import ipaddress
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import configobj
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from weewx_clearskies_api.db.reflection import STOCK_COLUMN_MAP, SchemaReflector
from weewx_clearskies_api.services.station import _get_str_field, _parse_altitude
from weewx_clearskies_api.services.weewx_conf import WeewxConfLoadError, load_weewx_conf
from weewx_clearskies_api.trust import TrustManager, _read_secrets_env, _write_secrets_env

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


async def require_setup_active(request: Request) -> TrustManager:
    """Ensure setup is not yet complete. Returns TrustManager. Raises 410 if complete."""
    tm: TrustManager = request.app.state.trust_manager
    if tm.setup_complete:
        raise HTTPException(410, detail="Setup already complete")
    return tm


async def require_setup_session(request: Request) -> TrustManager:
    """Ensure valid setup session exists. Returns TrustManager. Raises 410 if complete, 401 if no/invalid session."""
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


class SchemaResponse(BaseModel):
    columns: list[ColumnEntry]
    stock_count: int
    unmapped_count: int


class StationResponse(BaseModel):
    station_name: str
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
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


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: DatabaseApplyConfig
    column_mapping: dict[str, str] = {}
    station: StationApplyConfig = StationApplyConfig()
    weewx_conf_path: str | None = None
    #: Provider configurations keyed by domain: "forecast", "aqi", "alerts",
    #: "radar", "earthquakes".  Each entry sets the provider id in api.conf and
    #: writes any credential to secrets.env using provider-scoped env var names.
    providers: dict[str, ProviderConfig] | None = None
    #: MQTT/realtime proxy shared secret.  Written to secrets.env as
    #: WEEWX_CLEARSKIES_PROXY_SECRET.
    proxy_secret: str | None = None


class ApplyResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    p = pc.provider.lower()

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

    # [column_mapping] — operator-supplied canonical → archive column pairs.
    if apply.column_mapping:
        if "column_mapping" not in cfg:
            cfg["column_mapping"] = {}
        for canonical, archive_col in apply.column_mapping.items():
            cfg["column_mapping"][canonical] = archive_col

    # [forecast] / [aqi] / [alerts] / [radar] / [earthquakes] — non-secret provider
    # fields only.  Credentials are written to secrets.env by the apply handler.
    if apply.providers:
        for domain, pc in apply.providers.items():
            section = domain.lower()
            if section not in cfg:
                cfg[section] = {}
            cfg[section]["provider"] = pc.provider

            # NWS contact email/URL (non-secret; stored in api.conf per settings.py).
            # Valid for forecast and alerts domains.
            if pc.nws_user_agent_contact and section in ("forecast", "alerts"):
                cfg[section]["nws_user_agent_contact"] = pc.nws_user_agent_contact

            # Radar iframe URL (non-secret; stored in api.conf per settings.py).
            if pc.iframe_url and section == "radar":
                cfg[section]["iframe_url"] = pc.iframe_url

    cfg.write()


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

        columns.append(ColumnEntry(
            name=col_info.db_name,
            db_type=db_type,
            stock=col_info.is_stock,
            canonical=col_info.canonical_name,
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
    raw_altitude_val = station_section.get("altitude", "")
    if isinstance(raw_altitude_val, list):
        raw_altitude = ", ".join(str(x) for x in raw_altitude_val)
    else:
        raw_altitude = str(raw_altitude_val).strip()
    if raw_altitude:
        try:
            altitude_meters = _parse_altitude(raw_altitude)
        except Exception:  # noqa: BLE001
            pass

    station_type = _get_str_field(station_section, "station_type") or None

    return StationResponse(
        station_name=station_name,
        latitude=latitude,
        longitude=longitude,
        altitude_meters=altitude_meters,
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

        _write_secrets_env(secrets_path, existing)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write secrets.env during setup apply: %s", type(exc).__name__)
        raise HTTPException(500, detail="Failed to write secrets file.") from exc

    # 3. Mark setup complete — invalidates session and trust token.
    tm.mark_setup_complete()

    return ApplyResponse(
        success=True,
        message="Configuration saved. Restart the API to apply.",
    )
