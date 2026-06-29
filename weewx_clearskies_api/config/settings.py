"""Settings model and config loader.

Config loading order per ADR-027:
  1. CLEARSKIES_CONFIG env var (if set, points directly to the .conf file)
  2. /etc/weewx-clearskies/api.conf
  3. ~/.config/weewx-clearskies/api.conf  (XDG fallback)

Secrets come from environment variables only (loaded from
/etc/weewx-clearskies/secrets.env by the process manager before startup).
The operator is responsible for mode 0600 on secrets.env per ADR-027 §3.

Section mapping:
  [api]      → ApiSettings
  [health]   → HealthSettings
  [logging]  → LoggingSettings
  [database] → DatabaseSettings  (stub — DB wired in Task 2)

Note: webcam is a UI concern (dashboard/stack), not an API concern.
There is no [webcam] section here; webcam config belongs in the dashboard.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import Any

import configobj

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised for operator configuration errors that prevent startup.

    Used by:
      - cache.py: unsupported CLEARSKIES_CACHE_URL scheme
      - __main__.py catches and exits non-zero (same pattern as write-probe)
    """


# ---------------------------------------------------------------------------
# Sentinel config paths (ADR-027 search order)
# ---------------------------------------------------------------------------
_CONFIG_SEARCH_PATH: list[Path] = [
    Path("/etc/weewx-clearskies/api.conf"),
    Path.home() / ".config" / "weewx-clearskies" / "api.conf",
]

# Pattern that flags a leaf key that looks like a secret pasted into
# the .conf file instead of secrets.env (ADR-027 §3 secret-leak guard).
# This is not exhaustive — it catches the common mistake only.
_SECRET_KEY_RE = re.compile(r"(?i)_(KEY|SECRET|TOKEN|PASSWORD)$")


# ---------------------------------------------------------------------------
# Settings dataclasses (hand-rolled — avoids pydantic-settings env-var
# coupling for the INI sections; env vars for *secrets* only)
# ---------------------------------------------------------------------------


class ApiSettings:
    """[api] section settings."""

    #: Bind host for the public API. Default loopback per ADR-037.
    bind_host: str
    #: Bind port for the public API.
    bind_port: int
    #: Maximum request body size in bytes (default 1 MiB, security baseline §3.1).
    max_request_bytes: int
    #: Extra CORS origins (comma-separated or INI list).
    cors_origins: list[str]

    def __init__(self, section: dict[str, Any]) -> None:
        self.bind_host = str(section.get("bind_host", "127.0.0.1"))
        self.bind_port = int(section.get("bind_port", 8765))
        self.max_request_bytes = int(section.get("max_request_bytes", 1 * 1024 * 1024))
        raw_origins = section.get("cors_origins", [])
        if isinstance(raw_origins, str):
            # Single-value INI line
            raw_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
        self.cors_origins = list(raw_origins)

    def validate(self) -> None:
        """Raise ValueError on bad values. Called at startup."""
        # Validate bind_host is a legal IP address or hostname.
        # ipaddress.ip_address accepts both IPv4 and IPv6 per coding.md §1.
        # Hostname strings are allowed too — they'll be resolved via getaddrinfo.
        if self.bind_host not in ("", "localhost", "*"):
            with contextlib.suppress(ValueError):
                ipaddress.ip_address(self.bind_host)
        if not (1 <= self.bind_port <= 65535):
            raise ValueError(f"[api] bind_port {self.bind_port!r} out of range 1–65535")
        if self.max_request_bytes < 1:
            raise ValueError("[api] max_request_bytes must be >= 1")


class HealthSettings:
    """[health] section settings."""

    #: Bind host for the health port. Default loopback per ADR-030.
    bind_host: str
    #: Bind port for /health/live and /health/ready (default 8081 per ADR-030).
    bind_port: int
    #: Expose Prometheus /metrics on the health port (ADR-031). Default False.
    #: Env var CLEARSKIES_METRICS_ENABLED wins over the INI value.
    metrics_enabled: bool

    def __init__(self, section: dict[str, Any]) -> None:
        self.bind_host = str(section.get("bind_host", "127.0.0.1"))
        self.bind_port = int(section.get("bind_port", 8081))
        # ADR-031: opt-in metrics endpoint. Env var wins over ini file.
        env_val = os.environ.get("CLEARSKIES_METRICS_ENABLED", "").strip().lower()
        if env_val:
            self.metrics_enabled = env_val in ("true", "1", "yes")
        else:
            ini_val = str(section.get("metrics_enabled", "false")).strip().lower()
            self.metrics_enabled = ini_val in ("true", "1", "yes")

    def validate(self) -> None:
        if not (1 <= self.bind_port <= 65535):
            raise ValueError(f"[health] bind_port {self.bind_port!r} out of range 1–65535")


class LoggingSettings:
    """[logging] section settings."""

    #: Log level. Overridden by CLEARSKIES_LOG_LEVEL env var at runtime.
    level: str

    def __init__(self, section: dict[str, Any]) -> None:
        env_level = os.environ.get("CLEARSKIES_LOG_LEVEL", "").upper()
        raw_level = env_level or str(section.get("level", "INFO")).upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if raw_level not in valid:
            raise ValueError(f"[logging] level {raw_level!r} not in {valid}")
        self.level = raw_level


class DatabaseSettings:
    """[database] section settings (ADR-012, ADR-027).

    Non-secret fields come from the INI config file.
    Credentials (user/password) are read from env vars at engine-build time
    by db/engine.py — they never touch this object:
      WEEWX_CLEARSKIES_DB_USER
      WEEWX_CLEARSKIES_DB_PASSWORD

    Pool settings are configurable per ADR-012 (defaults: pool_size=5,
    max_overflow=10).  SQLite ignores pool settings (NullPool is used).
    """

    #: Database type: "sqlite" or "mysql".
    kind: str
    #: For sqlite: path to the .sdb file.
    path: str
    #: For mysql: host — IP or hostname.  IPv4/IPv6 both accepted (coding.md §1).
    host: str
    #: For mysql: port.
    port: int
    #: For mysql: database name.
    name: str
    #: Connection pool size (mysql only, ignored for sqlite). ADR-012 default: 5.
    pool_size: int
    #: Max pool overflow (mysql only, ignored for sqlite). ADR-012 default: 10.
    max_overflow: int

    def __init__(self, section: dict[str, Any]) -> None:
        self.kind = str(section.get("kind", "sqlite"))
        self.path = str(section.get("path", "/var/lib/weewx/weewx.sdb"))
        self.host = str(section.get("host", "127.0.0.1"))
        self.port = int(section.get("port", 3306))
        self.name = str(section.get("name", "weewx"))
        self.pool_size = int(section.get("pool_size", 5))
        self.max_overflow = int(section.get("max_overflow", 10))

    def validate(self) -> None:
        """Raise ValueError on bad values. Called at startup."""
        valid_kinds = {"sqlite", "mysql"}
        if self.kind.lower() not in valid_kinds:
            raise ValueError(
                f"[database] kind {self.kind!r} not in {valid_kinds}. "
                "Supported values: 'sqlite', 'mysql'."
            )
        if self.kind.lower() == "mysql":
            if not (1 <= self.port <= 65535):
                raise ValueError(
                    f"[database] port {self.port!r} out of range 1–65535"
                )
            if not self.name:
                raise ValueError("[database] name must not be empty for mysql kind")
            if not self.host:
                raise ValueError("[database] host must not be empty for mysql kind")
        if self.kind.lower() == "sqlite" and not self.path:
            raise ValueError("[database] path must not be empty for sqlite kind")
        if self.pool_size < 1:
            raise ValueError("[database] pool_size must be >= 1")
        if self.max_overflow < 0:
            raise ValueError("[database] max_overflow must be >= 0")


class WeewxSettings:
    """[weewx] section settings.

    Holds the path to weewx.conf (read at startup by services/units.py) and
    the reports directory path (used by the /reports endpoints).

    Default weewx.conf path: /etc/weewx/weewx.conf (stock Debian deb install).
    Default reports directory: /var/www/html/weewx/NOAA (stock Debian deb install
    with SeasonsReport NOAA submodule).  Override to match your installation.
    """

    #: Path to weewx.conf.
    config_path: str
    #: Directory where weewx writes NOAA-*.txt report files.
    reports_directory: str
    #: Optional path to a directory containing the weewx package, prepended
    #: to sys.path at startup so ``import weewx.units`` succeeds (ADR-056).
    python_path: str | None

    def __init__(self, section: dict[str, Any]) -> None:
        self.config_path = str(section.get("config_path", "/etc/weewx/weewx.conf"))
        self.reports_directory = str(
            section.get("reports_directory", "/var/www/html/weewx/NOAA")
        )
        raw_python_path = section.get("python_path")
        self.python_path = str(raw_python_path) if raw_python_path is not None else None


class StationSettings:
    """[station] section settings (3a-2; default_locale added ADR-021).

    Optional overrides for station identity.  Absent → clearskies-api derives
    from weewx.conf [Station].
    """

    #: Supported locale codes for the dashboard (ADR-021).
    SUPPORTED_LOCALES: frozenset[str] = frozenset({
        "en", "de", "es", "fil", "fr", "it", "ja",
        "nl", "pt-PT", "pt-BR", "ru", "zh-CN", "zh-TW",
    })

    #: Optional station_id override.  Absent → slug of weewx.conf location.
    station_id: str | None
    #: Optional IANA TZ override (api.conf is highest priority per ADR-020).
    timezone: str | None
    #: Default locale for the dashboard (ADR-021). Env var wins over INI.
    default_locale: str

    def __init__(self, section: dict[str, Any]) -> None:
        raw_id = str(section.get("station_id", "")).strip()
        self.station_id = raw_id if raw_id else None

        raw_tz = str(section.get("timezone", "")).strip()
        self.timezone = raw_tz if raw_tz else None

        # ADR-021: env var wins over INI value; default "en".
        env_locale = os.environ.get("CLEARSKIES_DEFAULT_LOCALE", "").strip()
        if env_locale:
            self.default_locale = env_locale
        else:
            self.default_locale = str(section.get("default_locale", "en"))

    def validate(self) -> None:
        """Raise ValueError if default_locale is not in the supported set (ADR-021)."""
        if self.default_locale not in self.SUPPORTED_LOCALES:
            raise ValueError(
                f"[station] default_locale {self.default_locale!r} not in "
                f"supported set: {sorted(self.SUPPORTED_LOCALES)}"
            )


class AlmanacSettings:
    """[almanac] section settings (3a-2).

    Ephemeris cache directory.  Default /var/cache/weewx-clearskies/skyfield/.
    """

    #: Directory where de421.bsp is cached (or pre-placed for offline installs).
    ephemeris_directory: str
    #: Path to a JSON meteor shower catalog.  Default ships with the package.
    #: Override to use a custom catalog (e.g. /etc/weewx-clearskies/meteor_showers.json).
    meteor_showers_catalog: str
    #: AstronomyAPI.com app_id from env var WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID.
    #: Optional: eclipse contact times work when configured, almanac works without.
    astronomyapi_app_id: str | None
    #: AstronomyAPI.com app_secret from env var WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET.
    astronomyapi_app_secret: str | None

    def __init__(self, section: dict[str, Any]) -> None:
        self.ephemeris_directory = str(
            section.get(
                "ephemeris_directory",
                "/var/cache/weewx-clearskies/skyfield/",
            )
        )
        self.meteor_showers_catalog = str(
            section.get(
                "meteor_showers_catalog",
                "/etc/weewx-clearskies/meteor_showers.json",
            )
        )

        # AstronomyAPI.com credentials — env vars only, per ADR-027.
        # Optional: eclipse contact times work when configured, almanac works without them.
        raw_astro_id = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_ID", "").strip()
        self.astronomyapi_app_id = raw_astro_id if raw_astro_id else None

        raw_astro_secret = os.environ.get("WEEWX_CLEARSKIES_ASTRONOMYAPI_APP_SECRET", "").strip()
        self.astronomyapi_app_secret = raw_astro_secret if raw_astro_secret else None


class ContentSettings:
    """[content] section settings (3a-2).

    Directory containing about.md and legal.md.  Default /etc/weewx-clearskies/content/.
    """

    #: Directory containing operator-authored markdown files.
    directory: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.directory = str(
            section.get("directory", "/etc/weewx-clearskies/content/")
        )


class AlertsSettings:
    """[alerts] section settings (3b-1, extended 3b-7 with Aeris credentials,
    extended 3b-8 with OWM appid).

    Provider id and NWS-specific knobs.  Aeris and OWM credentials are loaded
    from env vars at __init__ time per ADR-027 §3 (secrets never in INI; sourced
    from secrets.env loaded by the process manager).

    Naming deviation (brief Q1, user decision 2026-05-08):
      WEEWX_CLEARSKIES_AERIS_CLIENT_ID and WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET
      are provider-scoped (same env vars ForecastSettings reads).  Aeris
      credentials are provider-wide — one key works for /forecasts + /alerts.
      Domain-scoped names would force the operator to paste identical keys into
      two env vars.  Deviation documented here; no ADR amendment.

    OWM naming (3b-8, mirrors 3b-7 Aeris precedent):
      WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID is provider-scoped (same env var
      ForecastSettings reads).  One key works for forecast + alerts.
      Provider-scoped per 3b-5 brief Q2 user decision 2026-05-08.

    nws_user_agent_contact: operator's email or URL for NWS User-Agent.
    Per ADR-006, NO project-level default — operator responsibility.
    """

    #: Provider id: "nws", "aeris", "openweathermap", or absent.
    provider: str | None
    #: NWS User-Agent contact (email or URL).  Optional but recommended.
    nws_user_agent_contact: str | None
    #: Aeris client_id from env var WEEWX_CLEARSKIES_AERIS_CLIENT_ID (ADR-027 §3).
    #: Provider-scoped per 3b-4 brief Q1 user decision 2026-05-08.
    aeris_client_id: str | None
    #: Aeris client_secret from env var WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET.
    aeris_client_secret: str | None
    #: OWM appid from env var WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID (ADR-027 §3).
    #: Provider-scoped per 3b-5 brief Q2 user decision 2026-05-08; same key works
    #: for forecast + alerts (mirrors 3b-7 Aeris precedent).
    openweathermap_appid: str | None

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "")).strip()
        self.provider = raw_provider if raw_provider else None

        raw_contact = str(section.get("nws_user_agent_contact", "")).strip()
        self.nws_user_agent_contact = raw_contact if raw_contact else None

        # Aeris credentials — env vars only, never from the [alerts] INI section.
        # Per ADR-027 §3: secrets come from the process manager's secrets.env file.
        # Same env vars as ForecastSettings (provider-scoped, not domain-scoped).
        raw_aeris_id = os.environ.get("WEEWX_CLEARSKIES_AERIS_CLIENT_ID", "").strip()
        self.aeris_client_id = raw_aeris_id if raw_aeris_id else None

        raw_aeris_secret = os.environ.get("WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET", "").strip()
        self.aeris_client_secret = raw_aeris_secret if raw_aeris_secret else None

        # OWM appid — env var only, never from INI. Long-form provider-scoped name
        # per 3b-5 brief Q2 user decision 2026-05-08 (matches module filename +
        # dispatch key). Same env var as ForecastSettings.openweathermap_appid.
        raw_owm_appid = os.environ.get("WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID", "").strip()
        self.openweathermap_appid = raw_owm_appid if raw_owm_appid else None

    def validate(self) -> None:
        """Raise ValueError on invalid provider id."""
        valid_providers = {"nws", "aeris", "openweathermap"}
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[alerts] provider {self.provider!r} not in {valid_providers}. "
                "Supported values: 'nws', 'aeris', 'openweathermap'."
            )


class AQISettings:
    """[aqi] section settings (3b-9, extended 3b-10 with Aeris, 3b-12 with IQAir,
    extended 4AB-1 with regional config per ADR-059).

    Provider id for the AQI data source.  Open-Meteo is keyless — no env vars
    needed.  Aeris (3b-10) is keyed — credentials come from the shared [aeris]
    section (provider-scoped per 3b-4 Q1 user decision; same env vars as
    forecast/alerts Aeris).  OWM (3b-11) is keyed — provider-scoped per 3b-5 Q2
    decision; same env var as forecast/alerts OWM.  IQAir (3b-12) is keyed —
    domain-scoped per Q1 user decision 2026-05-11 (IQAir is AQI-only; distinct
    from multi-domain Aeris/OWM).

    Regional config (ADR-059):
      aeris_aqi_filter: one of airnow|china|india|eaqi|caqi|uk|de|cai (default airnow).
      openmeteo_aqi_index: one of us_aqi|european_aqi (default us_aqi).
      iqair_aqi_scale: one of us|cn (default us).
      OWM has no regional config — always returns OWM 1-5 ordinal scale.

    Per ADR-013: single AQI provider per deploy.  No multi-provider fallback.
    """

    #: Provider id: "openmeteo", "aeris", "openweathermap", "iqair".
    provider: str | None
    #: IQAir API key (domain-scoped per Q1 user decision 2026-05-11; AQI-only provider).
    iqair_key: str | None
    #: Aeris regional AQI filter (ADR-059). Passed as filter= query param.
    #: One of: airnow|china|india|eaqi|caqi|uk|de|cai. Default: airnow.
    aeris_aqi_filter: str
    #: OpenMeteo AQI index variable (ADR-059). Determines which AQI variable to request.
    #: One of: us_aqi|european_aqi. Default: us_aqi.
    openmeteo_aqi_index: str
    #: IQAir AQI scale (ADR-059). Determines whether to read aqius (US EPA) or aqicn (China MEP).
    #: One of: us|cn. Default: us.
    iqair_aqi_scale: str

    _VALID_AERIS_FILTERS: frozenset[str] = frozenset({
        "airnow", "china", "india", "eaqi", "caqi", "uk", "de", "cai",
    })
    _VALID_OPENMETEO_INDICES: frozenset[str] = frozenset({"us_aqi", "european_aqi"})
    _VALID_IQAIR_SCALES: frozenset[str] = frozenset({"us", "cn"})

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "")).strip()
        self.provider = raw_provider if raw_provider else None

        # IQAir API key — env var only, never from INI.  Long-form provider-scoped
        # naming per LC11 / OWM precedent.  Domain-scoped because IQAir serves only
        # the AQI domain (not forecast/alerts — distinct from Aeris/OWM which are
        # provider-scoped across multiple domains).  Q1 user decision 2026-05-11.
        raw_iqair_key = os.environ.get("WEEWX_CLEARSKIES_IQAIR_KEY", "").strip()
        self.iqair_key = raw_iqair_key if raw_iqair_key else None

        # Regional AQI config (ADR-059) — from INI section, not env vars (not secrets).
        self.aeris_aqi_filter = str(section.get("aeris_aqi_filter", "airnow")).strip()
        self.openmeteo_aqi_index = str(section.get("openmeteo_aqi_index", "us_aqi")).strip()
        self.iqair_aqi_scale = str(section.get("iqair_aqi_scale", "us")).strip()

    def validate(self) -> None:
        """Raise ValueError on invalid provider id or regional config."""
        valid_providers = {"openmeteo", "aeris", "openweathermap", "iqair"}
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[aqi] provider {self.provider!r} not in {valid_providers}. "
                "Supported values: 'openmeteo', 'aeris', 'openweathermap', 'iqair'."
            )
        if self.aeris_aqi_filter not in self._VALID_AERIS_FILTERS:
            raise ValueError(
                f"[aqi] aeris_aqi_filter {self.aeris_aqi_filter!r} not in "
                f"{sorted(self._VALID_AERIS_FILTERS)}. "
                "Supported values per ADR-059: 'airnow', 'china', 'india', "
                "'eaqi', 'caqi', 'uk', 'de', 'cai'."
            )
        if self.openmeteo_aqi_index not in self._VALID_OPENMETEO_INDICES:
            raise ValueError(
                f"[aqi] openmeteo_aqi_index {self.openmeteo_aqi_index!r} not in "
                f"{sorted(self._VALID_OPENMETEO_INDICES)}. "
                "Supported values per ADR-059: 'us_aqi', 'european_aqi'."
            )
        if self.iqair_aqi_scale not in self._VALID_IQAIR_SCALES:
            raise ValueError(
                f"[aqi] iqair_aqi_scale {self.iqair_aqi_scale!r} not in "
                f"{sorted(self._VALID_IQAIR_SCALES)}. "
                "Supported values per ADR-059: 'us', 'cn'."
            )


class AQIHistorySettings:
    """[aqi.history] section settings (P4-T3, ADR-013 corrected).

    Maps canonical AQI field names to weewx archive column names.
    Empty string = field not available in this operator's archive.

    Path A operators: populate columns matching their archive schema.
    Path B operators: leave all fields empty (all-empty defaults); /aqi/history
      returns an empty data list (not an error).

    Column names come exclusively from this config object (trusted constants),
    never from user input.  Bound into the service via wire_aqi_settings().
    """

    #: Archive column name for the composite AQI value.  Empty = not present.
    column_aqi: str
    #: Archive column name for the AQI category label.  Empty = not present.
    column_aqi_category: str
    #: Archive column name for the main pollutant label.  Empty = not present.
    column_aqi_main_pollutant: str
    #: Archive column name for the AQI location label.  Empty = not present.
    column_aqi_location: str
    #: Archive column name for PM2.5 concentration (µg/m³).  Empty = not present.
    column_pm25: str
    #: Archive column name for PM10 concentration (µg/m³).  Empty = not present.
    column_pm10: str
    #: Archive column name for O3 concentration (ppm).  Empty = not present.
    column_o3: str
    #: Archive column name for NO2 concentration (ppm).  Empty = not present.
    column_no2: str
    #: Archive column name for SO2 concentration (ppm).  Empty = not present.
    column_so2: str
    #: Archive column name for CO concentration (ppm).  Empty = not present.
    column_co: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.column_aqi = str(section.get("column_aqi", "")).strip()
        self.column_aqi_category = str(section.get("column_aqi_category", "")).strip()
        self.column_aqi_main_pollutant = str(section.get("column_aqi_main_pollutant", "")).strip()
        self.column_aqi_location = str(section.get("column_aqi_location", "")).strip()
        self.column_pm25 = str(section.get("column_pm25", "")).strip()
        self.column_pm10 = str(section.get("column_pm10", "")).strip()
        self.column_o3 = str(section.get("column_o3", "")).strip()
        self.column_no2 = str(section.get("column_no2", "")).strip()
        self.column_so2 = str(section.get("column_so2", "")).strip()
        self.column_co = str(section.get("column_co", "")).strip()


class CacheWarmerSettings:
    """[cache_warmer] section settings (ADR-045).

    Background pre-computation of slow endpoints.  The warmer daemon thread
    runs compute_sun_times_year, compute_moon_phases, and get_records on
    configurable intervals, storing results in the ADR-017 CacheBackend so
    that request-time endpoint handlers return in <10ms on cache hit.
    """

    #: Enable the background warmer.  Default True.
    enabled: bool
    #: How often to re-warm the records endpoints (seconds).  Default 1800 (30 min).
    records_interval_seconds: int
    #: How often to re-warm the almanac endpoints (seconds).  Default 21600 (6 h).
    almanac_interval_seconds: int
    #: How often to re-warm the AQI history endpoint (seconds).  Default 1800 (30 min).
    aqi_history_interval_seconds: int
    #: How often to re-warm the planets endpoint (seconds).  Default 21600 (6 h).
    planets_interval_seconds: int
    #: How often to re-warm the eclipses endpoint (seconds).  Default 86400 (24 h).
    eclipses_interval_seconds: int
    #: How often to re-warm the meteor showers endpoint (seconds).  Default 86400 (24 h).
    meteor_showers_interval_seconds: int
    #: How often to re-warm the earthquake faults endpoint (seconds).  Default 21600 (6 h).
    faults_interval_seconds: int
    #: How often to re-warm the seeing forecast endpoint (seconds).  Default 10800 (3 h).
    seeing_interval_seconds: int

    def __init__(self, section: dict[str, Any]) -> None:
        raw_enabled = str(section.get("enabled", "true")).strip().lower()
        self.enabled = raw_enabled in ("true", "1", "yes")
        self.records_interval_seconds = int(section.get("records_interval_seconds", 1800))
        self.almanac_interval_seconds = int(section.get("almanac_interval_seconds", 21600))
        self.aqi_history_interval_seconds = int(section.get("aqi_history_interval_seconds", 1800))
        self.planets_interval_seconds = int(section.get("planets_interval_seconds", 21600))
        self.eclipses_interval_seconds = int(section.get("eclipses_interval_seconds", 86400))
        self.meteor_showers_interval_seconds = int(section.get("meteor_showers_interval_seconds", 86400))
        self.faults_interval_seconds = int(section.get("faults_interval_seconds", 21600))
        self.seeing_interval_seconds = int(section.get("seeing_interval_seconds", 10800))

    def validate(self) -> None:
        """Raise ValueError on non-positive intervals."""
        if self.records_interval_seconds < 1:
            raise ValueError("[cache_warmer] records_interval_seconds must be >= 1")
        if self.almanac_interval_seconds < 1:
            raise ValueError("[cache_warmer] almanac_interval_seconds must be >= 1")
        if self.aqi_history_interval_seconds < 1:
            raise ValueError("[cache_warmer] aqi_history_interval_seconds must be >= 1")
        if self.planets_interval_seconds < 1:
            raise ValueError("[cache_warmer] planets_interval_seconds must be >= 1")
        if self.eclipses_interval_seconds < 1:
            raise ValueError("[cache_warmer] eclipses_interval_seconds must be >= 1")
        if self.meteor_showers_interval_seconds < 1:
            raise ValueError("[cache_warmer] meteor_showers_interval_seconds must be >= 1")
        if self.faults_interval_seconds < 1:
            raise ValueError("[cache_warmer] faults_interval_seconds must be >= 1")
        if self.seeing_interval_seconds < 1:
            raise ValueError("[cache_warmer] seeing_interval_seconds must be >= 1")


class EarthquakesSettings:
    """[earthquakes] section settings (3b-13).

    Provider id for the earthquake data source.  All four day-1 providers (usgs,
    geonet, emsc, renass) are keyless — no env vars needed.

    Per ADR-040: single earthquake provider per deploy.  No multi-provider
    fallback or aggregation.
    """

    #: Provider id: "usgs", "geonet", "emsc", "renass", or absent.
    provider: str | None
    #: Default radius in km from station lat/lon.  Override per-request via ?radius_km.
    default_radius_km: float
    #: Default minimum magnitude filter.  Used when ?minmagnitude not supplied.
    min_magnitude: float
    #: Default lookback window in days.  Used to compute starttime when ?from not supplied.
    default_days: int

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "")).strip()
        self.provider = raw_provider if raw_provider else None

        raw_radius = section.get("default_radius_km", 100)
        try:
            self.default_radius_km = float(raw_radius)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"[earthquakes] default_radius_km {raw_radius!r} must be a number."
            ) from exc
        if self.default_radius_km < 0:
            raise ValueError(
                f"[earthquakes] default_radius_km {self.default_radius_km!r} must be >= 0."
            )

        raw_min_mag = section.get("min_magnitude", 2.0)
        try:
            self.min_magnitude = float(raw_min_mag)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"[earthquakes] min_magnitude {raw_min_mag!r} must be a number."
            ) from exc
        if self.min_magnitude < 0:
            raise ValueError(
                f"[earthquakes] min_magnitude {self.min_magnitude!r} must be >= 0."
            )

        raw_days = section.get("default_days", 7)
        try:
            self.default_days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"[earthquakes] default_days {raw_days!r} must be an integer."
            ) from exc
        if self.default_days < 1:
            raise ValueError(
                f"[earthquakes] default_days {self.default_days!r} must be >= 1."
            )

    def validate(self) -> None:
        """Raise ValueError on invalid provider id."""
        valid_providers = {"usgs", "geonet", "emsc", "renass"}
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[earthquakes] provider {self.provider!r} not in {valid_providers}. "
                "Supported values: 'usgs', 'geonet', 'emsc', 'renass'."
            )


class SeeingSettings:
    """[seeing] section settings.

    Provider id for astronomical seeing forecasts.  7Timer is keyless —
    no API key or env vars needed.  Set provider to None (or omit [seeing])
    to disable the seeing endpoint entirely.
    """

    #: Provider id: "7timer" (default) or None to disable.
    provider: str | None
    #: 7Timer ASTRO API base URL.
    base_url: str
    #: HTTP request timeout in seconds.
    timeout_seconds: int

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "7timer")).strip()
        self.provider = raw_provider if raw_provider else None
        self.base_url = str(
            section.get("base_url", "https://www.7timer.info/bin/astro.php")
        ).strip()
        self.timeout_seconds = int(section.get("timeout_seconds", "10"))

    def validate(self) -> None:
        """Raise ValueError on invalid provider id."""
        valid_providers = {"7timer"}
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[seeing] provider {self.provider!r} not in {valid_providers}. "
                "Valid: '7timer'."
            )


class RadarSettings:
    """[radar] section settings (3b-14; extended 3b-15 with 2 keyed providers;
    extended 3b-16 with iframe provider; extended T1.2 with librewxr provider).

    Provider id for the radar data source.  Keyless providers: rainviewer,
    iem_nexrad (deprecated), noaa_mrms (deprecated), msc_geomet, dwd_radolan,
    librewxr.  Keyed provider: openweathermap.  Embed provider: iframe.

    Note: aeris was removed from the valid radar provider set (T1.2 — aeris
    radar is no longer supported; aeris remains valid for forecast/alerts/AQI).
    Note: iem_nexrad and noaa_mrms are deprecated — they continue to work but
    a deprecation warning is emitted at startup directing operators to librewxr.
    Note: mapbox_jma is NOT included — deferred per ADR-015 2026-05-11 amendment
    (Mapbox JMA tilesets are raster-array shape, GL-JS-only; incompatible with
    Leaflet).

    Per ADR-015: single radar provider per deploy (operator picks one per
    their lat/lon).  Per-region auto-pick is a setup-wizard concern (out of scope).

    Keyed provider credentials (openweathermap) are NOT stored here —
    they are wired at startup via wire_radar_settings() in endpoints/radar.py,
    which reads them from settings.forecast (provider-scoped per 3b-5 Q2).

    iframe provider: operator supplies iframe_url in [radar] section; the
    dashboard embeds the URL directly.  No frames index, no tile proxy.

    librewxr provider: configurable via librewxr_endpoint, librewxr_bounds,
    librewxr_refresh_interval.  Tiles are proxied by Caddy (no tile proxy in
    the API).  configure() is called at startup to pass these to the module.
    """

    #: Provider id: "rainviewer", "iem_nexrad", "noaa_mrms", "msc_geomet",
    #: "dwd_radolan", "openweathermap", "iframe", "librewxr", or absent.
    #: Note: "aeris" removed from radar valid set (T1.2).
    provider: str | None
    #: iframe embed URL.  Required when provider == "iframe"; None otherwise.
    iframe_url: str | None
    #: LibreWxR API base URL. Default: "https://api.librewxr.net".
    #: Override to point at a self-hosted LibreWxR instance.
    librewxr_endpoint: str
    #: LibreWxR optional bounding box as "south,west,north,east" CSV.
    #: None = no bounds constraint (global tiles).
    librewxr_bounds: str | None
    #: Seconds between dashboard re-fetches of the LibreWxR frame index.
    librewxr_refresh_interval: int

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "")).strip()
        self.provider = raw_provider if raw_provider else None
        raw_iframe_url = str(section.get("iframe_url", "")).strip()
        self.iframe_url = raw_iframe_url if raw_iframe_url else None
        self.librewxr_endpoint = str(
            section.get("librewxr_endpoint", "https://api.librewxr.net")
        ).strip() or "https://api.librewxr.net"
        raw_librewxr_bounds = str(section.get("librewxr_bounds", "")).strip()
        self.librewxr_bounds = raw_librewxr_bounds if raw_librewxr_bounds else None
        self.librewxr_refresh_interval = int(
            section.get("librewxr_refresh_interval", 600)
        )

    def validate(self) -> None:
        """Raise ValueError on invalid provider id."""
        valid_providers = {
            "rainviewer",
            "iem_nexrad",      # deprecated — prefer librewxr; deprecation warning at startup
            "noaa_mrms",       # deprecated — prefer librewxr; deprecation warning at startup
            "msc_geomet",
            "dwd_radolan",
            "openweathermap",  # keyed — added 3b-15; credentials in settings.forecast
            "iframe",          # iframe embed — added 3b-16; requires iframe_url in [radar]
            "librewxr",        # added T1.2; configurable endpoint, Caddy tile proxy
        }
        # aeris removed from radar valid set (T1.2): aeris radar no longer supported.
        # mapbox_jma is NOT valid — deferred per ADR-015 2026-05-11 amendment.
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[radar] provider {self.provider!r} not in {valid_providers}. "
                "Supported values: 'rainviewer', 'msc_geomet', 'dwd_radolan', "
                "'librewxr' (keyless); "
                "'openweathermap' (keyed; credentials in [forecast] section); "
                "'iframe' (embed; requires iframe_url in [radar] section). "
                "Deprecated (still work but use librewxr instead): "
                "'iem_nexrad', 'noaa_mrms'. "
                "Removed: 'aeris' (no longer supported for radar)."
            )
        if self.provider == "iframe" and not self.iframe_url:
            raise ValueError(
                "[radar] provider='iframe' requires iframe_url to be set in api.conf"
            )


class GeographicFeaturesSettings:
    """[geographic_features] section settings (ADR-078).

    Controls the GET /api/v1/geographic-features endpoint which queries the
    Overpass API for OSM administrative boundaries, roads, and water features
    within a configurable bounding box.

    Bounds cascade (resolved in the service, not here):
      1. bounds (explicit CSV "south,west,north,east") — highest priority
      2. radar.librewxr_bounds — reuse the operator's configured LibreWxR bbox
      3. Computed from station lat/lon ± radius_km — lowest priority / fallback
    """

    #: Whether the geographic features endpoint is active.  False → endpoint
    #: returns an empty FeatureCollection with HTTP 200 rather than 503.
    enabled: bool
    #: Explicit bounding box as "south,west,north,east" CSV.  None = use cascade.
    bounds: str | None
    #: Radius in km used when no explicit bounds and no librewxr_bounds are set.
    radius_km: float
    #: How long in days to keep Overpass results in the cache.
    refresh_days: int
    #: Overpass API endpoint URL.
    overpass_endpoint: str

    def __init__(self, section: dict[str, Any]) -> None:
        raw_enabled = str(section.get("enabled", "true")).strip().lower()
        self.enabled = raw_enabled not in ("false", "0", "no", "off")
        raw_bounds = str(section.get("bounds", "")).strip()
        self.bounds = raw_bounds if raw_bounds else None
        self.radius_km = float(section.get("radius_km", 200.0))
        self.refresh_days = int(section.get("refresh_days", 90))
        self.overpass_endpoint = str(
            section.get("overpass_endpoint", "https://overpass-api.de/api/interpreter")
        ).strip() or "https://overpass-api.de/api/interpreter"

    def validate(self) -> None:
        """Raise ValueError on invalid values."""
        if self.radius_km <= 0:
            raise ValueError(
                f"[geographic_features] radius_km {self.radius_km!r} must be > 0."
            )
        if self.refresh_days <= 0:
            raise ValueError(
                f"[geographic_features] refresh_days {self.refresh_days!r} must be > 0."
            )
        if self.bounds is not None:
            parts = self.bounds.split(",")
            if len(parts) != 4:
                raise ValueError(
                    f"[geographic_features] bounds {self.bounds!r} must be "
                    "'south,west,north,east' (4 comma-separated floats)."
                )
            try:
                [float(p) for p in parts]
            except ValueError as exc:
                raise ValueError(
                    f"[geographic_features] bounds {self.bounds!r} contains "
                    "non-numeric values — expected 4 floats: south,west,north,east."
                ) from exc


class ForecastSettings:
    """[forecast] section settings (3b-2, extended 3b-3 with NWS UA contact,
    extended 3b-4 with Aeris credentials, extended 3b-5 with OWM appid).

    Provider id and NWS-specific knobs. Open-Meteo is keyless (no knobs).
    Aeris and OWM credentials are loaded from env vars at __init__ time per
    ADR-027 §3 (secrets never in INI; sourced from secrets.env loaded by the
    process manager).

    Naming deviation (brief Q1, user decision 2026-05-08):
      WEEWX_CLEARSKIES_AERIS_CLIENT_ID and WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET
      are provider-scoped (not domain-scoped as ADR-027 §3's literal schema
      prescribes).  Rationale: Aeris credentials are provider-wide — the same
      key works for /forecasts, /alerts, and /observations.  Domain-scoped names
      would force the operator to paste identical keys into two env vars.
      Deviation documented here and in providers/forecast/aeris.py; no ADR amendment.

    OWM naming (brief Q2, user decision 2026-05-08):
      WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID is provider-scoped, long-form.
      Matches the module filename (openweathermap.py) and dispatch key
      ("openweathermap").  Consistent with the 3b-4 Aeris precedent.
      No ADR amendment needed — same deviation class as Aeris.

    nws_user_agent_contact: operator's email or URL for NWS User-Agent.
    Per ADR-006, NO project-level default — operator responsibility.

    Accepts all five ADR-007 day-1 forecast providers even though only
    "openmeteo", "nws", "aeris", and "openweathermap" are in dispatch this
    round. Providers not yet in dispatch raise KeyError at startup
    (fail-closed, same pattern as AlertsSettings).
    """

    #: Provider id: "openmeteo", "nws", "aeris", "openweathermap", "wunderground", or absent.
    provider: str | None
    #: NWS User-Agent contact (email or URL).  Optional but recommended (ADR-006).
    nws_user_agent_contact: str | None
    #: Aeris client_id from env var WEEWX_CLEARSKIES_AERIS_CLIENT_ID (ADR-027 §3).
    aeris_client_id: str | None
    #: Aeris client_secret from env var WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET (ADR-027 §3).
    aeris_client_secret: str | None
    #: OWM appid from env var WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID (ADR-027 §3).
    #: Long-form provider-scoped naming per brief Q2 user decision 2026-05-08.
    openweathermap_appid: str | None
    #: Wunderground apiKey from env var WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY.
    #: Long-form provider-scoped naming per 3b-4/3b-5 precedent (same deviation
    #: from ADR-027 §3 literal schema as Aeris + OWM; no ADR amendment).
    wunderground_api_key: str | None
    #: Wunderground PWS station ID from env var WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID.
    #: Required alongside the apiKey per ADR-007 line 79 defense-in-depth gate:
    #: apiKeys are issued only to active PWS contributors, so requiring both env
    #: vars ensures operator's mental model matches the gating reality.
    #: PWS station ID isn't strictly a secret but is co-located with the apiKey
    #: for operational simplicity (all Wunderground config in env vars together).
    wunderground_pws_station_id: str | None
    #: Aeris forecast model: "standard" or "xcast" (ML-enhanced).
    #: Read from [forecast] section of api.conf. Default "xcast" (ADR-063).
    aeris_forecast_model: str

    def __init__(self, section: dict[str, Any]) -> None:
        raw_provider = str(section.get("provider", "")).strip()
        self.provider = raw_provider if raw_provider else None

        raw_contact = str(section.get("nws_user_agent_contact", "")).strip()
        self.nws_user_agent_contact = raw_contact if raw_contact else None

        # Aeris credentials — env vars only, never from the [forecast] INI section.
        # Per ADR-027 §3: secrets come from the process manager's secrets.env file.
        raw_aeris_id = os.environ.get("WEEWX_CLEARSKIES_AERIS_CLIENT_ID", "").strip()
        self.aeris_client_id = raw_aeris_id if raw_aeris_id else None

        raw_aeris_secret = os.environ.get("WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET", "").strip()
        self.aeris_client_secret = raw_aeris_secret if raw_aeris_secret else None

        # OWM appid — env var only, never from INI. Long-form provider-scoped name
        # per brief Q2 user decision 2026-05-08 (matches module filename + dispatch key).
        raw_owm_appid = os.environ.get("WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID", "").strip()
        self.openweathermap_appid = raw_owm_appid if raw_owm_appid else None

        # Wunderground credentials — env vars only, never from INI.
        # Long-form provider-scoped naming per 3b-4/3b-5 precedent.
        # ADR-007 line 79 "config time" gate operationalized as fetch-time KeyInvalid
        # (same precedent as Aeris/OWM; documented in wunderground.py module docstring).
        raw_wu_key = os.environ.get("WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY", "").strip()
        self.wunderground_api_key = raw_wu_key if raw_wu_key else None

        raw_wu_pws = os.environ.get("WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID", "").strip()
        self.wunderground_pws_station_id = raw_wu_pws if raw_wu_pws else None

        # Aeris forecast model — from INI (not a secret; config-time operator choice).
        # Default "xcast" per ADR-063. Normalise to lowercase; unknown values fall back to "xcast".
        raw_model = str(section.get("aeris_forecast_model", "xcast")).strip().lower()
        self.aeris_forecast_model = raw_model if raw_model in ("standard", "xcast") else "xcast"

    def validate(self) -> None:
        """Raise ValueError on invalid provider id or forecast model."""
        valid_providers = {"openmeteo", "nws", "aeris", "openweathermap", "wunderground"}
        if self.provider is not None and self.provider not in valid_providers:
            raise ValueError(
                f"[forecast] provider {self.provider!r} not in {valid_providers}. "
                "Supported values: 'openmeteo', 'nws', 'aeris', 'openweathermap', 'wunderground'."
            )
        if self.aeris_forecast_model not in ("standard", "xcast"):
            raise ValueError(
                f"[forecast] aeris_forecast_model {self.aeris_forecast_model!r} not valid. "
                "Supported values: 'standard', 'xcast'."
            )


class BrandingSettings:
    """[branding] section settings (ADR-022, Gap #10).

    Operator-controlled branding values served by GET /api/v1/branding.
    All fields are optional with safe defaults so the endpoint works even if
    no [branding] section is present in api.conf.

    v0.1 scope: read-only; logo upload pipeline is Phase 2.
    """

    #: Accent color name — one of the curated palette entries per ADR-022.
    accent: str
    #: Default theme mode for new visitors per ADR-023.
    default_theme_mode: str
    #: URL to operator's custom stylesheet. None = no custom CSS.
    custom_css_url: str | None
    #: Human-readable station name shown in the dashboard title bar.
    site_title: str
    #: Name shown in the footer copyright line. Empty = no custom copyright entity.
    copyright_entity: str
    #: URL to the logo image used on light backgrounds. Empty = no custom logo.
    logo_light_url: str
    #: URL to the logo image used on dark backgrounds. Empty = falls back to logo_light_url.
    logo_dark_url: str
    #: Alt text for the logo image (WCAG 2.1 AA, ADR-022 §5.5).
    #: Empty = fallback to site_title or station name + " logo" at render time.
    logo_alt: str
    #: URL to the favicon. Empty = default Clear Skies favicon.
    favicon_url: str

    #: Valid accent values from ADR-022 (curated palette, AA-tested).
    VALID_ACCENTS: frozenset[str] = frozenset({
        "blue", "teal", "indigo", "purple", "green", "amber"
    })
    #: Valid theme modes from ADR-023.
    VALID_THEME_MODES: frozenset[str] = frozenset({
        "light", "dark", "auto-os", "auto-sunrise-sunset"
    })

    def __init__(self, section: dict[str, Any]) -> None:
        self.accent = str(section.get("accent", "blue")).strip()
        self.default_theme_mode = str(
            section.get("default_theme_mode", "auto-os")
        ).strip()
        raw_css_url = str(section.get("custom_css_url", "")).strip()
        self.custom_css_url = raw_css_url if raw_css_url else None
        self.site_title = str(section.get("site_title", "")).strip()
        self.copyright_entity = str(section.get("copyright_entity", "")).strip()
        self.logo_light_url = str(section.get("logo_light_url", "")).strip()
        self.logo_dark_url = str(section.get("logo_dark_url", "")).strip()
        self.logo_alt = str(section.get("logo_alt", "")).strip()
        self.favicon_url = str(section.get("favicon_url", "")).strip()

    def validate(self) -> None:
        """Raise ValueError on invalid accent or theme mode."""
        if self.accent not in self.VALID_ACCENTS:
            raise ValueError(
                f"[branding] accent {self.accent!r} not in "
                f"{sorted(self.VALID_ACCENTS)}. "
                "Supported values per ADR-022: "
                "'blue', 'teal', 'indigo', 'purple', 'green', 'amber'."
            )
        if self.default_theme_mode not in self.VALID_THEME_MODES:
            raise ValueError(
                f"[branding] default_theme_mode {self.default_theme_mode!r} not in "
                f"{sorted(self.VALID_THEME_MODES)}. "
                "Supported values per ADR-023: "
                "'light', 'dark', 'auto-os', 'auto-sunrise-sunset'."
            )


class SocialSettings:
    """[social] section settings.

    Social media profile URLs shown in the dashboard footer.
    All fields default to empty string (absent/unconfigured).
    Dashboard checks for non-empty before rendering social links.
    """

    #: Facebook profile or page URL.
    facebook_url: str
    #: Twitter/X profile URL.
    twitter_url: str
    #: Instagram profile URL.
    instagram_url: str
    #: YouTube channel URL.
    youtube_url: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.facebook_url = str(section.get("facebook_url", "")).strip()
        self.twitter_url = str(section.get("twitter_url", "")).strip()
        self.instagram_url = str(section.get("instagram_url", "")).strip()
        self.youtube_url = str(section.get("youtube_url", "")).strip()


class TlsSettings:
    """[tls] section settings (ADR-038).

    Paths to operator-supplied TLS cert and key PEM files.  Both must be set
    together or not at all (validated in validate()).  When absent, the API
    auto-generates a self-signed Ed25519 cert in config_dir at startup.

    Paths may also be overridden at startup via --tls-cert / --tls-key CLI flags;
    those override these settings values in main() before any file I/O happens.
    """

    #: Path to operator-supplied TLS certificate (PEM).  Empty = auto-generate.
    cert_path: str
    #: Path to operator-supplied TLS private key (PEM).  Empty = auto-generate.
    key_path: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.cert_path = str(section.get("cert_path", "")).strip()
        self.key_path = str(section.get("key_path", "")).strip()

    def validate(self) -> None:
        """Raise ValueError if exactly one of cert_path / key_path is set."""
        has_cert = bool(self.cert_path)
        has_key = bool(self.key_path)
        if has_cert != has_key:
            raise ValueError(
                "[tls] cert_path and key_path must both be set or both be absent. "
                "Supply both paths for operator-managed TLS, or omit both to "
                "use auto-generated self-signed certificates."
            )


class ConditionsSettings:
    """[conditions] section settings (Phase 0B / ADR-067 / ADR-068).

    Controls the current conditions text blending engine that populates
    the weatherText field on Observation responses, and haze detection.

    engine values:
      "auto"     — blends local sensor data with provider conditions (default).
      "provider" — uses provider conditions text verbatim (no local blending).
      "off"      — weatherText is always None on observations.

    Calibration parameters (calibration_percentile, calibration_window_days,
    calibration_min_samples) are fixed by the auto-calibration algorithm
    (ADR-068) and are not operator-configurable.  Old api.conf files that
    still contain these keys are silently ignored.
    """

    #: Blending engine mode: "auto" | "provider" | "off".
    engine: str

    #: Enable or disable haze detection (ADR-067).
    haze_detection: bool

    #: AQI provider override for haze PM data (inherits from [aqi] if not set).
    haze_aqi_provider: str | None

    #: Hygroscopic correction exponent γ (Hanel 1976 / Tang 1996).
    #: Advanced operator override.  Range: [0.1, 1.0].
    gamma: float

    #: Operator-specified OpenAQ sensor ID override (Phase 9).
    #: When set, the bootstrap loop skips candidate search and uses this sensor
    #: directly.  None means automatic sensor selection.
    openaq_sensor_id: int | None

    #: Sky classification dynamic threshold parameters (ADR-073).
    #: All optional — None means use module defaults.
    sky_decay_rate: float | None
    sky_clear_threshold: float | None
    sky_threshold_floor: float | None
    sky_min_elevation: float | None

    _VALID_ENGINES: frozenset[str] = frozenset({"auto", "provider", "off"})

    def __init__(self, section: dict[str, Any]) -> None:
        self.engine = str(section.get("engine", "auto")).strip()
        # Haze detection toggle (ADR-067)
        self.haze_detection = _bool(section.get("haze_detection", True))
        # AQI provider override for haze PM data
        self.haze_aqi_provider = section.get("haze_aqi_provider") or None
        # Hygroscopic correction exponent (advanced — operator override of Hanel default)
        self.gamma = float(section.get("gamma", 0.45))
        # Operator-specified OpenAQ sensor ID (Phase 9 smart sensor selection).
        # Old api.conf files without this key load cleanly (section.get returns None).
        raw_sensor_id = section.get("openaq_sensor_id")
        if raw_sensor_id is not None and str(raw_sensor_id).strip():
            try:
                self.openaq_sensor_id = int(raw_sensor_id)
            except (TypeError, ValueError):
                self.openaq_sensor_id = None
        else:
            self.openaq_sensor_id = None
        # Sky classification threshold overrides (ADR-073).
        self.sky_decay_rate = _opt_float(section, "sky_decay_rate")
        self.sky_clear_threshold = _opt_float(section, "sky_clear_threshold")
        self.sky_threshold_floor = _opt_float(section, "sky_threshold_floor")
        self.sky_min_elevation = _opt_float(section, "sky_min_elevation")

    def validate(self) -> None:
        """Raise ValueError on invalid engine or gamma value."""
        if self.engine not in self._VALID_ENGINES:
            raise ValueError(
                f"[conditions] engine {self.engine!r} not in "
                f"{sorted(self._VALID_ENGINES)}. "
                "Supported values: 'auto', 'provider', 'off'."
            )
        if not (0.1 <= self.gamma <= 1.0):
            raise ValueError(
                f"[conditions] gamma {self.gamma!r} must be in [0.1, 1.0]."
            )
        if self.sky_decay_rate is not None and not (0.01 <= self.sky_decay_rate <= 0.20):
            raise ValueError(
                f"[conditions] sky_decay_rate {self.sky_decay_rate!r} must be in [0.01, 0.20]."
            )
        if self.sky_clear_threshold is not None and not (0.5 <= self.sky_clear_threshold <= 1.0):
            raise ValueError(
                f"[conditions] sky_clear_threshold {self.sky_clear_threshold!r} must be in [0.5, 1.0]."
            )
        if self.sky_threshold_floor is not None and not (0.1 <= self.sky_threshold_floor <= 0.5):
            raise ValueError(
                f"[conditions] sky_threshold_floor {self.sky_threshold_floor!r} must be in [0.1, 0.5]."
            )
        if self.sky_min_elevation is not None and not (5.0 <= self.sky_min_elevation <= 30.0):
            raise ValueError(
                f"[conditions] sky_min_elevation {self.sky_min_elevation!r} must be in [5.0, 30.0]."
            )


class ChartsSettings:
    """[charts] section settings — controls chart configuration loading."""

    config_path: str | None

    def __init__(self, section: dict[str, Any]) -> None:
        raw = section.get("config_path", "").strip()
        self.config_path = raw if raw else None


def _bool(value: object) -> bool:
    """Parse a boolean from a string or bool value (INI-safe)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _opt_float(section: dict[str, Any], key: str) -> float | None:
    """Parse an optional float from a config section. Returns None if absent or empty."""
    raw = section.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    return float(raw)


class FreshnessSettings:
    """[freshness] section settings (ADR-075 §4).

    Per-domain refresh intervals for the freshness response envelope.
    Defaults derive from weewx archive_interval for current_observation
    and records; static defaults for other domains.
    """

    current_observation: int  # seconds; default from archive_interval
    forecast: int
    alerts: int
    aqi: int
    almanac_daily: int
    almanac_positions: int
    radar: int
    earthquakes: int
    records: int  # seconds; default from archive_interval
    charts_config: int
    station_metadata: int
    seeing: int
    idle_timeout: int  # minutes; 0 = disabled
    idle_refresh_factor: int

    def __init__(self, section: dict[str, Any], archive_interval: int = 300) -> None:
        self.current_observation = int(section.get("current_observation", archive_interval))
        self.forecast = int(section.get("forecast", 1800))
        self.alerts = int(section.get("alerts", 300))
        self.aqi = int(section.get("aqi", 900))
        self.almanac_daily = int(section.get("almanac_daily", 86400))
        self.almanac_positions = int(section.get("almanac_positions", 60))
        self.radar = int(section.get("radar", 300))
        self.earthquakes = int(section.get("earthquakes", 300))
        self.records = int(section.get("records", archive_interval))
        self.charts_config = int(section.get("charts_config", 86400))
        self.station_metadata = int(section.get("station_metadata", 86400))
        self.seeing = int(section.get("seeing", 10800))
        self.idle_timeout = int(section.get("idle_timeout", 30))
        self.idle_refresh_factor = int(section.get("idle_refresh_factor", 10))

    def validate(self) -> None:
        for attr in ("current_observation", "forecast", "alerts", "aqi",
                     "almanac_daily", "almanac_positions", "radar",
                     "earthquakes", "records", "charts_config",
                     "station_metadata", "seeing"):
            if getattr(self, attr) < 1:
                raise ValueError(f"[freshness] {attr} must be >= 1")
        if self.idle_timeout < 0:
            raise ValueError("[freshness] idle_timeout must be >= 0")
        if self.idle_refresh_factor < 1:
            raise ValueError("[freshness] idle_refresh_factor must be >= 1")


class InputSettings:
    """[input] section settings (ADR-058).

    Controls the direct Unix-socket adapter that receives loop packets from
    the ClearSkiesLoopRelay weewx extension.

    When enabled is False, the adapter and SSE emitter are not started and
    the API operates in REST-only mode (no /sse stream).
    """

    #: Path to the Unix domain socket served by ClearSkiesLoopRelay.
    socket_path: str
    #: When False, the direct adapter and SSE emitter are not started.
    enabled: bool

    def __init__(self, section: dict[str, Any]) -> None:
        self.socket_path = str(
            section.get("socket_path", "/var/run/weewx-clearskies/loop.sock")
        )
        self.enabled = _bool(section.get("enabled", "true"))

    def validate(self) -> None:
        # socket_path is validated at connection time, not startup.
        pass


class UnitsSettings:
    """[units] section — operator display unit preferences (ADR-042).

    Mirrors weewx skin.conf [Units] subsection names for operator familiarity.
    ConfigObj returns nested [[subsections]] as nested dicts.
    """

    #: [[groups]] — display unit per group (e.g. group_temperature = degree_C).
    groups: dict[str, str]
    #: [[string_formats]] — decimal places per unit (e.g. degree_C = %.1f).
    string_formats: dict[str, str]
    #: [[labels]] — display symbols per unit (e.g. degree_C = °C).
    labels: dict[str, str]
    #: [[ordinates]] directions — comma-separated 16-point compass labels.
    directions: list[str]
    #: [[trend]] time_delta — barometer trend window in seconds (default 3 hours).
    trend_time_delta: int
    #: [[trend]] time_grace — grace period for trend computation (default 5 min).
    trend_time_grace: int

    def __init__(self, section: dict[str, Any]) -> None:
        # [[groups]] — display unit per group
        self.groups: dict[str, str] = dict(section.get("groups", {}))
        # [[string_formats]] — decimal places per unit
        self.string_formats: dict[str, str] = dict(section.get("string_formats", {}))
        # [[labels]] — display symbols per unit
        self.labels: dict[str, str] = dict(section.get("labels", {}))
        # [[ordinates]] — compass direction labels
        ordinates = section.get("ordinates", {})
        directions_raw = ordinates.get("directions", "") if isinstance(ordinates, dict) else ""
        self.directions: list[str] = [
            d.strip() for d in str(directions_raw).split(",") if d.strip()
        ] if directions_raw else []
        # [[trend]] — barometer trend window config
        trend = section.get("trend", {}) if isinstance(section.get("trend"), dict) else {}
        self.trend_time_delta: int = int(trend.get("time_delta", 10800))
        self.trend_time_grace: int = int(trend.get("time_grace", 300))

    def validate(self) -> None:
        pass


class Settings:
    """Top-level runtime settings, assembled from INI file + env vars."""

    configured: bool
    api: ApiSettings
    health: HealthSettings
    logging: LoggingSettings
    database: DatabaseSettings
    weewx: WeewxSettings
    station: StationSettings
    almanac: AlmanacSettings
    content: ContentSettings
    alerts: AlertsSettings
    aqi: AQISettings
    aqi_history: AQIHistorySettings
    earthquakes: EarthquakesSettings
    seeing: SeeingSettings
    radar: RadarSettings
    geographic_features: GeographicFeaturesSettings
    forecast: ForecastSettings
    tls: TlsSettings
    branding: BrandingSettings
    social: SocialSettings
    conditions: ConditionsSettings
    cache_warmer: CacheWarmerSettings
    charts: ChartsSettings
    input: InputSettings
    units: UnitsSettings
    freshness: FreshnessSettings
    column_mapping: dict[str, str]
    #: Operator-confirmed unit for each mapped column, parsed from the
    #: ``[column_units]`` section of api.conf.  Written by ``/setup/apply``
    #: after the wizard's column-mapping step.  Empty dict when the section
    #: is absent (fresh install or pre-T2.6 config).
    column_units: dict[str, str]

    def __init__(
        self,
        api: ApiSettings,
        health: HealthSettings,
        logging_settings: LoggingSettings,
        database: DatabaseSettings,
        weewx: WeewxSettings | None = None,
        station: StationSettings | None = None,
        almanac: AlmanacSettings | None = None,
        content: ContentSettings | None = None,
        alerts: AlertsSettings | None = None,
        aqi: AQISettings | None = None,
        aqi_history: AQIHistorySettings | None = None,
        earthquakes: EarthquakesSettings | None = None,
        seeing: SeeingSettings | None = None,
        radar: RadarSettings | None = None,
        geographic_features: GeographicFeaturesSettings | None = None,
        forecast: ForecastSettings | None = None,
        tls: TlsSettings | None = None,
        branding: BrandingSettings | None = None,
        social: SocialSettings | None = None,
        conditions: ConditionsSettings | None = None,
        cache_warmer: CacheWarmerSettings | None = None,
        charts: ChartsSettings | None = None,
        input: InputSettings | None = None,
        units: UnitsSettings | None = None,
        freshness: FreshnessSettings | None = None,
        column_mapping: dict[str, str] | None = None,
        column_units: dict[str, str] | None = None,
        configured: bool = True,
    ) -> None:
        self.configured = configured
        self.api = api
        self.health = health
        self.logging = logging_settings
        self.database = database
        self.weewx = weewx if weewx is not None else WeewxSettings({})
        self.station = station if station is not None else StationSettings({})
        self.almanac = almanac if almanac is not None else AlmanacSettings({})
        self.content = content if content is not None else ContentSettings({})
        self.alerts = alerts if alerts is not None else AlertsSettings({})
        self.aqi = aqi if aqi is not None else AQISettings({})
        self.aqi_history = aqi_history if aqi_history is not None else AQIHistorySettings({})
        self.earthquakes = earthquakes if earthquakes is not None else EarthquakesSettings({})
        self.seeing = seeing if seeing is not None else SeeingSettings({})
        self.radar = radar if radar is not None else RadarSettings({})
        self.geographic_features = (
            geographic_features if geographic_features is not None
            else GeographicFeaturesSettings({})
        )
        self.forecast = forecast if forecast is not None else ForecastSettings({})
        self.tls = tls if tls is not None else TlsSettings({})
        self.branding = branding if branding is not None else BrandingSettings({})
        self.social = social if social is not None else SocialSettings({})
        self.conditions = conditions if conditions is not None else ConditionsSettings({})
        self.cache_warmer = cache_warmer if cache_warmer is not None else CacheWarmerSettings({})
        self.charts = charts if charts is not None else ChartsSettings({})
        self.input = input if input is not None else InputSettings({})
        self.units = units if units is not None else UnitsSettings({})
        self.freshness = freshness if freshness is not None else FreshnessSettings({})
        self.column_mapping = column_mapping if column_mapping is not None else {}
        self.column_units = column_units if column_units is not None else {}

    def validate(self) -> None:
        """Validate all sections. Raises ValueError on the first failure."""
        self.api.validate()
        self.health.validate()
        self.database.validate()
        self.station.validate()
        self.alerts.validate()
        self.aqi.validate()
        self.earthquakes.validate()
        self.seeing.validate()
        self.radar.validate()
        self.geographic_features.validate()
        self.forecast.validate()
        self.tls.validate()
        self.branding.validate()
        self.conditions.validate()
        self.cache_warmer.validate()
        self.input.validate()
        self.freshness.validate()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def find_config_file() -> Path | None:
    """Return the first config file that exists, following ADR-027 search order."""
    env_path = os.environ.get("CLEARSKIES_CONFIG", "").strip()
    if env_path:
        return Path(env_path)
    for candidate in _CONFIG_SEARCH_PATH:
        if candidate.exists():
            return candidate
    return None


# Keep the private alias for backwards-compat with any internal callers.
_find_config_file = find_config_file


def _check_for_secrets_in_conf(cfg: configobj.ConfigObj) -> None:
    """Raise RuntimeError if any leaf key looks like a secret pasted into .conf.

    ADR-027 §3: secrets belong in secrets.env (mode 0600), never in .conf.
    This guard catches the common mistake. It is not adversarially exhaustive.
    Fires when the key name (not the value) matches the secret-name pattern.
    """
    def _walk(obj: Any, path: str, key: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}.{k}", k)
        else:
            # Leaf node — check if the key name looks like a secret.
            if _SECRET_KEY_RE.search(key):
                raise RuntimeError(
                    f"FATAL: config key {path!r} looks like a secret. "
                    "Secrets belong in secrets.env (mode 0600), not in api.conf. "
                    "See ADR-027 for details."
                )

    for top_key, top_val in dict(cfg).items():
        _walk(top_val, top_key, top_key)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load and validate settings from the INI config file.

    Args:
        config_path: Override path for tests. When None, uses ADR-027 search order.

    Returns:
        Validated Settings instance. When no config file is found, returns a minimal
        Settings with configured=False so the API can start in setup mode.

    Raises:
        RuntimeError: Secret detected in .conf file (ADR-027 leak guard).
        ValueError: A config value failed validation.
    """
    path = config_path or _find_config_file()

    if path is None:
        # No config found — start in setup mode. The API binds on defaults and
        # serves only /setup/* and GET /api/v1/status so the wizard can connect.
        logger.info("No configuration file found — starting in setup mode")
        return Settings(
            api=ApiSettings({"bind_host": "*", "bind_port": 8765}),
            health=HealthSettings({"bind_host": "127.0.0.1", "bind_port": 8081}),
            logging_settings=LoggingSettings({}),
            database=DatabaseSettings({}),
            tls=TlsSettings({}),
            configured=False,
        )

    if not path.exists():
        # Explicit config_path was passed but doesn't exist. Don't silently accept
        # a typo'd path — configobj would create an empty config from a missing
        # file and the service would start with all defaults, which is a footgun.
        raise FileNotFoundError(f"Configuration file not found: {path}")

    cfg = configobj.ConfigObj(str(path), interpolation=False)
    _check_for_secrets_in_conf(cfg)

    api_cfg = ApiSettings(dict(cfg.get("api", {})))
    health_cfg = HealthSettings(dict(cfg.get("health", {})))
    log_cfg = LoggingSettings(dict(cfg.get("logging", {})))
    db_cfg = DatabaseSettings(dict(cfg.get("database", {})))
    weewx_cfg = WeewxSettings(dict(cfg.get("weewx", {})))
    station_cfg = StationSettings(dict(cfg.get("station", {})))
    almanac_cfg = AlmanacSettings(dict(cfg.get("almanac", {})))
    content_cfg = ContentSettings(dict(cfg.get("content", {})))
    alerts_cfg = AlertsSettings(dict(cfg.get("alerts", {})))
    aqi_cfg = AQISettings(dict(cfg.get("aqi", {})))
    aqi_history_cfg = AQIHistorySettings(dict(cfg.get("aqi.history", {})))
    earthquakes_cfg = EarthquakesSettings(dict(cfg.get("earthquakes", {})))
    seeing_cfg = SeeingSettings(dict(cfg.get("seeing", {})))
    radar_cfg = RadarSettings(dict(cfg.get("radar", {})))
    geographic_features_cfg = GeographicFeaturesSettings(dict(cfg.get("geographic_features", {})))
    forecast_cfg = ForecastSettings(dict(cfg.get("forecast", {})))
    tls_cfg = TlsSettings(dict(cfg.get("tls", {})))
    branding_cfg = BrandingSettings(dict(cfg.get("branding", {})))
    social_cfg = SocialSettings(dict(cfg.get("social", {})))
    conditions_cfg = ConditionsSettings(dict(cfg.get("conditions", {})))
    cache_warmer_cfg = CacheWarmerSettings(dict(cfg.get("cache_warmer", {})))
    charts_cfg = ChartsSettings(dict(cfg.get("charts", {})))
    input_cfg = InputSettings(dict(cfg.get("input", {})))
    units_cfg = UnitsSettings(dict(cfg.get("units", {})))
    column_mapping_cfg = dict(cfg.get("column_mapping", {}))
    column_units_cfg = dict(cfg.get("column_units", {}))
    freshness_cfg = FreshnessSettings(dict(cfg.get("freshness", {})))

    settings = Settings(
        api=api_cfg,
        health=health_cfg,
        logging_settings=log_cfg,
        database=db_cfg,
        weewx=weewx_cfg,
        station=station_cfg,
        almanac=almanac_cfg,
        content=content_cfg,
        alerts=alerts_cfg,
        aqi=aqi_cfg,
        aqi_history=aqi_history_cfg,
        earthquakes=earthquakes_cfg,
        seeing=seeing_cfg,
        radar=radar_cfg,
        geographic_features=geographic_features_cfg,
        forecast=forecast_cfg,
        tls=tls_cfg,
        branding=branding_cfg,
        social=social_cfg,
        conditions=conditions_cfg,
        cache_warmer=cache_warmer_cfg,
        charts=charts_cfg,
        input=input_cfg,
        units=units_cfg,
        freshness=freshness_cfg,
        column_mapping=column_mapping_cfg,
        column_units=column_units_cfg,
    )
    settings.validate()

    logger.debug("Configuration loaded from %s", path)
    return settings
