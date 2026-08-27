# Changelog

All notable changes to weewx-clearskies-api are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0: minor version bumps may include breaking changes. Read this file before upgrading.

The cross-repo compatibility matrix (which api/dashboard/realtime versions work together) is in [`clearskies-stack/README.md`](https://github.com/inguy24/weewx-clearskies-stack/blob/master/README.md).

---

## [Unreleased]

### Removed

**ADR-078 geographic-features single-file overlay (M5 — ADR-078 Amendment 2, Accepted 2026-08-27)**
- `endpoints/geographic_features.py` and `services/geographic_features.py` — deleted. The three
  routes (`GET /api/v1/geographic-features/tiles`, `GET /api/v1/geographic-features/status`,
  `POST /setup/geographic-features/update`) no longer exist and 404 generically.
- `config/settings.py` — `GeographicFeaturesSettings` class deleted; `Settings.geographic_features`
  no longer exists. A legacy `[geographic_features]` section in `api.conf` is now silently ignored
  (nothing reads `cfg["geographic_features"]` any more; no per-section validation error is raised),
  same treatment as `[imagery]` after M4-B. Operators may delete the section or the on-disk
  `/etc/weewx-clearskies/geographic-features.pmtiles` file; neither has any effect any more.
- `app.py` / `__main__.py` — router imports, `include_router()` calls, and
  `wire_geographic_features_settings()` import/call all removed.
- Superseded entirely by the `[basemap]` family (`GET /api/v1/basemap/{tier}/tiles`, `GET
  /api/v1/basemap/status`, `POST /setup/basemap/update`), which shipped additively under M1 and now
  stands alone — see the "Basemap (M1 — CS-BASEMAP)" entry below.

### Changed

**Surf height map background (M4 — SURF-MAP-BASEMAP, PA9, Q5)**
- `GET /api/v1/imagery/config` now always answers with the product basemap
  (`provider: "basemap"`), regardless of `[imagery] provider` — NAIP/Esri/Esri-Topo ("map") are no
  longer reachable from any user-facing surface. The endpoint no longer 404s when `[imagery]` is
  absent. Response gains `light: {tileUrl, attribution}` (OSM raster), `dark: {pmtilesUrl:
  "/api/v1/basemap/local/tiles", maxDataZoom: 15, attribution}` (the local Protomaps tier),
  `zoomMin: 0`, `zoomMax: 19`. The legacy top-level `tileUrl`/`attribution` fields carry the light
  values for old-client compatibility.
- `models/responses.py`: new `ImageryLightSource`, `ImageryDarkSource`; `ImageryConfigResponse`
  gains optional `light`/`dark`/`zoomMin`/`zoomMax` (additive).

### Removed

**Imagery provider machinery (M4-B — Q10-6, "if we dont need it then get rid of it")**
- `providers/imagery/{esri,esri_topo,naip}.py` and their package `__init__.py` — deleted. Nothing
  user-facing read the `[imagery]` provider any more after M4's `/imagery/config` change above.
- `providers/_common/dispatch.py` — the three `("imagery", ...)` rows removed.
- `config/settings.py` — `ImagerySettings` class deleted; `Settings.imagery` no longer exists. A
  legacy `[imagery]` section in `api.conf` is now silently ignored (nothing reads `cfg["imagery"]`
  any more; no per-section validation error is raised). Operators may delete the section from
  `api.conf` — it has no effect.
- `__main__.py` — startup imagery-module dispatch-table checks and the `wire_imagery_settings()`
  call/import removed.
- `endpoints/imagery.py` — `wire_imagery_settings()`, `reset_imagery_settings_for_tests()`,
  `_select_provider()`, the `_imagery_provider`/tile-cache-TTL module globals, the startup
  WARNING, and `GET /api/v1/imagery/tiles/{z}/{x}/{y}` (the NAIP tile proxy: `get_imagery_tile`,
  `_validate_tile_coords`, `_get_imagery_tile_params`) are all gone — that route now 404s.
  `GET /api/v1/imagery/config` is unchanged (byte-identical response; `lat`/`lon` still required
  and validated, still unused for provider selection).
- `models/params.py` — `ImageryTileQueryParams` deleted. `ImageryConfigQueryParams` unchanged.
- `endpoints/setup.py` — `"imagery"` dropped from `_PROVIDER_DOMAINS` (wizard-apply prefill).

### Added

**Basemap (M1 — CS-BASEMAP)**
- `GET /api/v1/basemap/{tier}/tiles` (`world`/`local`/`radar`) — tiered PMTiles serving with HTTP
  Range request support, generalising ADR-078's single-file geographic-features overlay into
  three zoom-tiered files covering every map surface (marine, seismic, radar/satellite)
- `GET /api/v1/basemap/status` — per-tier availability, size, extract bounds/zoom range, plus
  `updating`/`last_error`/`last_started_at`/`last_finished_at`
- `POST /setup/basemap/update` — background extraction of all three tiers (world → local → radar)
  in one daemon thread; the extraction box is always derived from station + configured marine
  locations (local tier) or the radar provider's declared coverage (radar tier) — never
  operator-typed
- `[basemap]` config section: `enabled` (bool, default `true`) — the only key
- ADR-078's `[geographic_features]` endpoints/service remained in place this round (additive build);
  removed in M5 — see "Removed" above
- Gate M1-API F1 fix: a marine service that is installed but has no locations yet answers HTTP 404
  on `/marine`; the local tier now treats that as "no marine box" (seismic box alone) instead of
  refusing forever. `MarineDiscoveryUnavailableError` gains a `status_code` attribute so callers
  dispatch on state, not on the message string

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
- `GET /api/v1/pages` — all 9 built-in pages unconditionally (page visibility filtering is the dashboard's responsibility via `pages.json`)
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
- Docker image published to `ghcr.io/clearskies-wx/weewx-clearskies-api`

### Known limitations

- `/aqi/history` returns data only when `[aqi.history]` column mappings are configured. No built-in weewx AQI extension writes these columns; requires a third-party extension or custom weewx configuration.
- Skyfield downloads the de421 ephemeris (~17 MB) on first run. Air-gapped hosts must pre-populate `[almanac] ephemeris_directory`.
- Rate limiting is per-process; multi-worker deployments need Redis for effective limiting.
- The `/branding` endpoint is not implemented; dashboard uses built-in defaults.

[0.1.0]: https://github.com/inguy24/weewx-clearskies-api/releases/tag/v0.1.0
