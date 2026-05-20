# Changelog

All notable changes to weewx-clearskies-api are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0: minor version bumps may include breaking changes. Read this file before upgrading.

The cross-repo compatibility matrix (which api/dashboard/realtime versions work together) is in [`clearskies-stack/README.md`](https://github.com/inguy24/weewx-clearskies-stack/blob/master/README.md).

---

## [0.1.0] — 2026-05-19

First public release.

### Added

**Core API (FastAPI / Python 3.12 / SQLAlchemy 2.x)**
- Versioned JSON API at `/api/v1/...`
- Auto-generated OpenAPI 3.1 spec and Swagger UI at `/api/v1/docs`
- RFC 9457 Problem Details (`application/problem+json`) on all error responses
- IPv4/IPv6 dual-stack listener via `socket.getaddrinfo`

**Database layer**
- SQLite and MariaDB/MySQL backends behind one config knob (`[database] kind`)
- Read-only database user enforced at startup via write-probe; service exits if user has write access
- Schema reflection at startup; unmapped weewx archive columns logged as warnings, not fatal

**Observation endpoints**
- `GET /api/v1/current` — latest archive record (69 fields)
- `GET /api/v1/archive` — historical records with `from`/`to`/`limit`/`cursor` pagination
- `GET /api/v1/records` — all-time station records
- `GET /api/v1/reports` — NOAA monthly and annual text report index
- `GET /api/v1/reports/{year}` — annual NOAA summary
- `GET /api/v1/reports/{year}/{month}` — monthly NOAA report

**Station and almanac**
- `GET /api/v1/station` — station name, lat/lon, elevation, timezone, unit system
- `GET /api/v1/almanac` — sun/moon data for today and the next 7 days (Skyfield de421 ephemeris)
- `GET /api/v1/almanac/sun-times` — sunrise/sunset for a configurable date range
- `GET /api/v1/almanac/moon-phases` — moon phase calendar

**Provider data**
- `GET /api/v1/forecast` — forecast from configured provider (hours/days slice params)
- `GET /api/v1/alerts` — active weather alerts from configured provider (severity filter)
- `GET /api/v1/aqi/current` — current air quality from configured provider
- `GET /api/v1/aqi/history` — historical AQI from archive (requires column mapping; returns empty list when not configured)
- `GET /api/v1/earthquakes` — recent seismic events (radius_km, min_magnitude, limit params)
- `GET /api/v1/radar/{provider}/frames` — radar frame metadata
- `GET /api/v1/radar/{provider}/tiles/{z}/{x}/{y}` — tile proxy for keyed providers

**Config and capabilities**
- `GET /api/v1/capabilities` — provider capability declarations for configured providers
- `GET /api/v1/pages` — visible page list (respects `[pages] hidden`)
- `GET /api/v1/charts/groups` — chart group config
- `GET /api/v1/content/about` — operator about-page markdown
- `GET /api/v1/content/legal` — operator legal-page markdown

**Day-1 providers**

| Domain | Provider | Auth |
|---|---|---|
| Forecast | Open-Meteo | keyless |
| Forecast | NWS | keyless (US only) |
| Forecast | Aeris | `WEEWX_CLEARSKIES_AERIS_CLIENT_ID/SECRET` |
| Forecast | OpenWeatherMap | `WEEWX_CLEARSKIES_OPENWEATHERMAP_APPID` |
| Forecast | Weather Underground | `WEEWX_CLEARSKIES_WUNDERGROUND_API_KEY/PWS_STATION_ID` |
| Alerts | NWS | keyless (US only) |
| Alerts | Aeris | shared with forecast |
| Alerts | OpenWeatherMap | shared with forecast |
| AQI | Open-Meteo | keyless |
| AQI | Aeris | shared with forecast |
| AQI | OpenWeatherMap | shared with forecast |
| AQI | IQAir | `WEEWX_CLEARSKIES_IQAIR_KEY` |
| Earthquakes | USGS | keyless |
| Earthquakes | GeoNet | keyless (NZ) |
| Earthquakes | EMSC | keyless (Europe) |
| Earthquakes | RéNaSS | keyless (France) |
| Radar | RainViewer | keyless |
| Radar | IEM NEXRAD | keyless (US) |
| Radar | NOAA MRMS | keyless (US) |
| Radar | MSC GeoMet | keyless (Canada) |
| Radar | DWD RADOLAN | keyless (Germany) |
| Radar | Aeris | shared with forecast |
| Radar | OpenWeatherMap | shared with forecast |
| Radar | iframe | operator-supplied URL |

**Security**
- Read-only database user write-probe at startup
- Optional `X-Clearskies-Proxy-Auth` shared secret for cross-host deploys
- JSON structured logging with auth header and SQL parameter value redaction
- Request size limit (1 MiB default)
- Per-IP rate limiting (60 req/min default)
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`)
- Separate loopback-only health port (8081)
- Pydantic `extra="forbid"` input validation via `Depends()` on all routes
- Secret-leak guard in config loader
- `pip-audit` and `gitleaks` CI gates

**Infrastructure**
- ConfigObj/INI config file with search-path and secret-leak guard
- Secrets loaded from environment variables (mode-0600 `secrets.env`)
- Pluggable provider response cache: in-process memory (default) or Redis (`CLEARSKIES_CACHE_URL`)
- `systemd` unit example (see INSTALL.md)
- Docker image published to `ghcr.io/inguy24/weewx-clearskies-api`

### Known limitations

- `/aqi/history` returns data only when `[aqi.history]` column mappings are configured. No built-in weewx AQI extension writes these columns; requires a third-party extension or custom weewx configuration.
- Skyfield downloads the de421 ephemeris (~17 MB) on first run. Air-gapped hosts must pre-populate `[almanac] ephemeris_directory`.
- Rate limiting is per-process; multi-worker deployments need Redis for effective limiting.
- The `/branding` endpoint is not implemented; dashboard uses built-in defaults.

[0.1.0]: https://github.com/inguy24/weewx-clearskies-api/releases/tag/v0.1.0
