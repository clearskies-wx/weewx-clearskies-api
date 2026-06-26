# Configuration — weewx-clearskies-api

clearskies-api uses a ConfigObj/INI-format config file. Secrets (database credentials, API keys) are never stored in the config file — they come from environment variables, typically loaded from a mode-0600 `secrets.env` file by the process manager.

---

## Config file location

The service searches for `api.conf` in this order:

1. `CLEARSKIES_CONFIG` environment variable (if set, used directly)
2. `/etc/weewx-clearskies/api.conf`
3. `~/.config/weewx-clearskies/api.conf`

The service refuses to start if no config file is found at any of these paths.

An annotated example is at `etc/api.conf.example` in the repository. Copy it to `/etc/weewx-clearskies/api.conf` and edit.

---

## Secret-leak guard

Any INI key whose name ends in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD` (case-insensitive) causes the service to refuse to start with a FATAL log. Secrets belong in `secrets.env`, not `api.conf`.

---

## [api] — public API listener

| Key | Default | Description |
|---|---|---|
| `bind_host` | `127.0.0.1` | Bind address for the public API. Default loopback keeps the service behind the reverse proxy. For cross-host deploys set to a LAN IP (e.g. `192.0.2.5` or `2001:db8::5`). |
| `bind_port` | `8765` | TCP port for the public API. |
| `max_request_bytes` | `1048576` | Maximum request body size in bytes (1 MiB). Requests larger than this return 413. |
| `cors_origins` | _(empty)_ | Extra CORS origins beyond same-origin. Leave empty when the reverse proxy serves the SPA and API from the same domain. For cross-origin deploys, list origins one per line or comma-separated. |

**Example — cross-origin setup:**

```ini
[api]
bind_host = 127.0.0.1
bind_port = 8765
cors_origins = https://weather.example.com, https://weather.example.net
```

---

## [health] — health check port

The health check port is separate from the public API so monitoring probes don't pollute access logs and don't require authentication.

| Key | Default | Description |
|---|---|---|
| `bind_host` | `127.0.0.1` | Bind address for health probes. Loopback only — never expose this to the internet. |
| `bind_port` | `8081` | TCP port for `/health/live` and `/health/ready`. |

Endpoints:
- `GET /health/live` — returns `{"status": "ok"}` as long as the process is running.
- `GET /health/ready` — returns `{"status": "ok"}` when the database is reachable; `{"status": "degraded"}` otherwise.

---

## [logging] — log output

| Key | Default | Description |
|---|---|---|
| `level` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Overridden by the `CLEARSKIES_LOG_LEVEL` environment variable. |

Logs are structured JSON (one object per line). Auth headers and SQL parameter values are redacted before logging.

---

## [ratelimit] — request rate limiting

Rate limiting is applied per source IP on all unauthenticated paths.

| Key | Default | Description |
|---|---|---|
| `requests_per_minute` | `60` | Maximum requests per IP per window. |
| `window_seconds` | `60` | Window duration in seconds. |

**Note:** In the default configuration, the rate-limit counter is per-process. Multi-worker deployments need a shared Redis backend (`CLEARSKIES_CACHE_URL`) for the limit to be effective across workers.

---

## [database] — weewx archive database

| Key | Default | Description |
|---|---|---|
| `kind` | `sqlite` | Database type: `sqlite` or `mysql`. |
| `path` | `/var/lib/weewx/weewx.sdb` | SQLite: path to the weewx `.sdb` file. Ignored for `mysql`. |
| `host` | `127.0.0.1` | MySQL/MariaDB: hostname or IP. Accepts IPv4 (e.g. `192.0.2.10`) and IPv6 (e.g. `2001:db8::10`). Ignored for `sqlite`. |
| `port` | `3306` | MySQL/MariaDB: TCP port. Ignored for `sqlite`. |
| `name` | `weewx` | MySQL/MariaDB: database name. Ignored for `sqlite`. |
| `pool_size` | `5` | MySQL/MariaDB: SQLAlchemy connection pool size. Ignored for `sqlite`. |
| `max_overflow` | `10` | MySQL/MariaDB: maximum connections above `pool_size`. Ignored for `sqlite`. |

**Credentials (environment variables — never in api.conf):**

| Variable | Description |
|---|---|
| `WEEWX_CLEARSKIES_DB_USER` | Read-only database username (e.g. `clearskies_ro`) |
| `WEEWX_CLEARSKIES_DB_PASSWORD` | Database password |

```ini
[database]
kind = sqlite
path = /var/lib/weewx/weewx.sdb
```

```ini
[database]
kind = mysql
host = 127.0.0.1
port = 3306
name = weewx
pool_size = 5
max_overflow = 10
```

---

## [weewx] — paths to weewx files

| Key | Default | Description |
|---|---|---|
| `config_path` | `/etc/weewx/weewx.conf` | Path to the weewx configuration file. Used to read station metadata and unit preferences. |
| `reports_directory` | `/var/www/html/weewx/NOAA` | Directory where weewx writes NOAA-*.txt report files. |

---

## [station] — station identity overrides

These are optional overrides. When absent, values are derived from `weewx.conf`.

| Key | Default | Description |
|---|---|---|
| `station_id` | _(derived from weewx.conf location)_ | Station identifier slug. Used in API responses. |
| `timezone` | _(from weewx.conf, then OS)_ | IANA timezone name (e.g. `America/Chicago`). Overrides weewx.conf and OS TZ. |
| `hidden` | _(empty)_ | Comma-separated list of built-in page slugs to hide from `/pages`. Cannot hide `now`. |

---

## [almanac] — ephemeris data

| Key | Default | Description |
|---|---|---|
| `ephemeris_directory` | `/var/cache/weewx-clearskies/skyfield/` | Directory where Skyfield caches the `de421.bsp` ephemeris file (~17 MB). Writable by the service user. For air-gapped hosts, pre-download and place the file here. |

---

## [content] — operator-authored page content

| Key | Default | Description |
|---|---|---|
| `directory` | `/etc/weewx-clearskies/content/` | Directory containing `about.md` and `legal.md`. These are returned by `/content/about` and `/content/legal`. Missing files return 404. |

---

## [pages] — page visibility

| Key | Default | Description |
|---|---|---|
| `hidden` | _(empty)_ | Comma-separated list of built-in page slugs to remove from the `/pages` response. Cannot hide `now`. |

---

## [forecast] — forecast provider

One forecast provider is active per deployment. Select the provider that covers your station's location.

| Key | Default | Description |
|---|---|---|
| `provider` | _(none)_ | Provider id: `openmeteo`, `nws`, `aeris`, `openweathermap`, or `wunderground`. Absent → `/forecast` returns `source: "none"`. |
| `nws_user_agent_contact` | _(none)_ | Your email or URL for the NWS `User-Agent` header. Required by NWS terms of service when using the `nws` provider. |

**Credentials (environment variables):**

| Variable | Provider | Description |
|---|---|---|
| `WEEWX_CLEARSKIES_AERIS_CLIENT_ID` | `aeris` | Aeris client ID |
| `WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET` | `aeris` | Aeris client secret |
| `WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID` | `openweathermap` | OpenWeatherMap API key |
| `WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY` | `wunderground` | Weather Underground API key |
| `WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID` | `wunderground` | Your PWS station ID |

**Example — Open-Meteo (keyless, global coverage):**

```ini
[forecast]
provider = openmeteo
```

**Example — NWS (US only, keyless):**

```ini
[forecast]
provider = nws
nws_user_agent_contact = your-email@example.com
```

**Example — OpenWeatherMap (keyed, global):**

```ini
[forecast]
provider = openweathermap
```

With `WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID=<your-key>` in `secrets.env`.

---

## [alerts] — weather alerts provider

| Key | Default | Description |
|---|---|---|
| `provider` | _(none)_ | Provider id: `nws`, `aeris`, or `openweathermap`. Absent → `/alerts` returns `source: "none"`. |
| `nws_user_agent_contact` | _(none)_ | Email or URL for the NWS `User-Agent` header (required for `nws` provider). |

Aeris and OpenWeatherMap credentials are shared with the `[forecast]` section (same environment variables).

---

## [aqi] — air quality provider

| Key | Default | Description |
|---|---|---|
| `provider` | _(none)_ | Provider id: `openmeteo`, `aeris`, `openweathermap`, or `iqair`. Absent → `/aqi/current` returns no data. |

**Credentials (environment variables):**

| Variable | Provider | Description |
|---|---|---|
| `WEEWX_CLEARSKIES_AERIS_CLIENT_ID` | `aeris` | Shared with `[forecast]` |
| `WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET` | `aeris` | Shared with `[forecast]` |
| `WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID` | `openweathermap` | Shared with `[forecast]` |
| `WEEWX_CLEARSKIES_IQAIR_KEY` | `iqair` | IQAir API key (AQI-only) |

---

## [aqi.history] — AQI history column mapping

Maps canonical AQI field names to your weewx archive column names. Leave all empty if you do not store AQI data in the archive (the default; `/aqi/history` returns an empty list).

| Key | Default | Description |
|---|---|---|
| `column_aqi` | _(empty)_ | Archive column for the composite AQI value |
| `column_aqi_category` | _(empty)_ | Archive column for the AQI category label |
| `column_aqi_main_pollutant` | _(empty)_ | Archive column for the dominant pollutant label |
| `column_aqi_location` | _(empty)_ | Archive column for the AQI location label |
| `column_pm25` | _(empty)_ | Archive column for PM2.5 in µg/m³ |
| `column_pm10` | _(empty)_ | Archive column for PM10 in µg/m³ |
| `column_o3` | _(empty)_ | Archive column for O₃ in ppm |
| `column_no2` | _(empty)_ | Archive column for NO₂ in ppm |
| `column_so2` | _(empty)_ | Archive column for SO₂ in ppm |
| `column_co` | _(empty)_ | Archive column for CO in ppm |

---

## [earthquakes] — earthquake provider

| Key | Default | Description |
|---|---|---|
| `provider` | _(none)_ | Provider id: `usgs`, `geonet`, `emsc`, or `renass`. All are keyless. Choose the one covering your region. |
| `default_radius_km` | `100` | Search radius in km from the station's lat/lon. Overridable per-request with `?radius_km=`. |

Regional coverage:
- `usgs` — global, recommended for US/NA operators
- `geonet` — New Zealand and SW Pacific
- `emsc` — Europe and Mediterranean
- `renass` — France and surrounding region

---

## [radar] — radar provider

| Key | Default | Description |
|---|---|---|
| `provider` | _(none)_ | Provider id (see table below). |
| `iframe_url` | _(none)_ | Required when `provider = iframe`. The URL to embed in the dashboard radar tile. |
| `librewxr_endpoint` | `https://api.librewxr.net` | LibreWxR API base URL. Override to point at a self-hosted LibreWxR instance. |
| `librewxr_bounds` | _(none)_ | Optional bounding box for LibreWxR tiles: `"south,west,north,east"` (e.g. `"24.0,-125.0,50.0,-66.0"` for CONUS). Leave empty for global tiles. |
| `librewxr_refresh_interval` | `600` | Seconds between dashboard re-fetches of the LibreWxR frame index. |

| Provider id | Type | Coverage | Notes |
|---|---|---|---|
| `rainviewer` | keyless | Global | Default fallback; attribution required |
| `iem_nexrad` | keyless | US (NEXRAD WSR-88D) | **Deprecated** — migrate to `librewxr` |
| `noaa_mrms` | keyless | US (Multi-Radar/Multi-Sensor) | **Deprecated** — migrate to `librewxr` |
| `msc_geomet` | keyless | Canada (MSC GeoMet WMS) | |
| `dwd_radolan` | keyless | Germany (DWD RADOLAN) | |
| `librewxr` | keyless + Caddy tile proxy | Global | Zoom 12, 13 color schemes, WebP, 60-min nowcast, alerts; self-host recommended |
| `openweathermap` | keyed (OWM appid) | Global | |
| `iframe` | operator URL | Any external radar embed | |

**Removed:** `aeris` is no longer supported as a radar provider. If you previously used `aeris` for radar, switch to `rainviewer` or `librewxr`.

**Deprecated:** `iem_nexrad` and `noaa_mrms` still work but emit a startup warning. Migrate to `librewxr` for better quality (zoom 12, nowcast, color scheme selection, WebP tiles).

OpenWeatherMap credentials are shared with the `[forecast]` section.

Tiles for `librewxr` are proxied by Caddy at the `/librewxr` prefix — the API itself does not proxy tile bytes for this provider.

**Example — RainViewer (keyless, global):**

```ini
[radar]
provider = rainviewer
```

**Example — LibreWxR (recommended, global coverage):**

```ini
[radar]
provider = librewxr
# Optional: self-hosted endpoint
# librewxr_endpoint = https://radar.example.com
# Optional: bounding box (CONUS)
# librewxr_bounds = 24.0,-125.0,50.0,-66.0
# librewxr_refresh_interval = 600
```

**Example — LibreWxR self-hosted with bounding box:**

```ini
[radar]
provider = librewxr
librewxr_endpoint = https://radar.example.com
librewxr_bounds = 24.0,-125.0,50.0,-66.0
librewxr_refresh_interval = 300
```

**Example — operator iframe embed:**

```ini
[radar]
provider = iframe
iframe_url = https://radar.weather.gov/station/KLOT/standard
```

---

## [conditions] — conditions text engine and haze detection

| Key | Default | Description |
|---|---|---|
| `engine` | `auto` | Conditions text blending mode: `auto` (blend local sensor data with provider), `provider` (verbatim provider text), or `off` (weatherText is always absent). |
| `haze_detection` | `true` | Enable or disable haze detection (ADR-067). When `false`, the haze channel is inactive and no "Hazy" label is ever emitted. |
| `haze_aqi_provider` | _(inherits from `[aqi]`)_ | Override the AQI provider used for haze PM confirmation. If absent or empty, the provider configured in `[aqi]` is used. |
| `calibration_percentile` | `0.92` | Percentile target for the auto-calibration clean-sky Kcs baseline (ADR-068). A ±2.5 pp band is derived from this value. Range: 0.90–0.95. |
| `calibration_window_days` | `90` | Primary rolling window in days for clean-sky sample collection. The 180-day fallback window is fixed. Range: 30–365. |
| `calibration_min_samples` | `22` | Minimum qualifying clean-sky samples before haze detection activates. Range: 10–100. |
| `gamma` | `0.45` | Hygroscopic correction exponent γ (Hanel 1976). Advanced override. Range: 0.1–1.0. |

**Example — disable haze detection:**

```ini
[conditions]
engine = auto
haze_detection = false
```

**Example — advanced calibration tuning:**

```ini
[conditions]
engine = auto
calibration_percentile = 0.93
calibration_window_days = 60
calibration_min_samples = 15
gamma = 0.5
```

---

## Environment variables — all secrets

Summary of all environment variables. Place these in `/etc/weewx-clearskies/secrets.env` (mode 0600):

| Variable | Required when | Description |
|---|---|---|
| `WEEWX_CLEARSKIES_DB_USER` | Always | Read-only DB username |
| `WEEWX_CLEARSKIES_DB_PASSWORD` | Always (MySQL) | DB password |
| `WEEWX_CLEARSKIES_PROXY_SECRET` | Cross-host deploys | Shared secret for `X-Clearskies-Proxy-Auth` header |
| `WEEWX_CLEARSKIES_AERIS_CLIENT_ID` | `aeris` provider | Aeris client ID (shared: forecast, alerts, AQI, radar) |
| `WEEWX_CLEARSKIES_AERIS_CLIENT_SECRET` | `aeris` provider | Aeris client secret |
| `WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID` | `openweathermap` provider | OWM API key (shared: forecast, alerts, AQI, radar) |
| `WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY` | `wunderground` forecast | Weather Underground API key |
| `WEEWX_CLEARSKIES_WUNDERGROUND_PWS_STATION_ID` | `wunderground` forecast | Your PWS station ID |
| `WEEWX_CLEARSKIES_IQAIR_KEY` | `iqair` AQI | IQAir API key |
| `CLEARSKIES_CACHE_URL` | Redis cache backend | `redis://127.0.0.1:6379/0` or `redis://[::1]:6379/0` |
| `CLEARSKIES_LOG_LEVEL` | Optional | Overrides `[logging] level` in api.conf |

---

## Provider response caching

External provider responses are cached to reduce outbound API calls. The default cache is in-process memory and does not survive restarts.

For persistence and multi-worker support, set `CLEARSKIES_CACHE_URL` to a Redis instance:

```bash
# IPv4
CLEARSKIES_CACHE_URL=redis://127.0.0.1:6379/0

# IPv6
CLEARSKIES_CACHE_URL=redis://[::1]:6379/0
```

The cache backend is detected at startup; an unreachable Redis instance causes the service to refuse to start.
