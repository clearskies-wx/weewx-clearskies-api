# weewx-clearskies-api

HTTP/JSON REST API for the [Clear Skies](https://github.com/inguy24/weewx-clearskies-stack) weather dashboard. Reads the [weewx](https://github.com/weewx/weewx) archive database and calls external data providers (forecast, AQI, alerts, earthquakes, radar) through internal plugin modules.

Part of Clear Skies — a modular, modern weather UI stack for weewx.

Distributed AS-IS under [GPL v3](LICENSE).

---

## What it does

clearskies-api is a FastAPI service that exposes a versioned JSON API (`/api/v1/...`) consumed by the dashboard SPA. It has two responsibilities:

1. **Archive access** — read-only queries against the weewx archive database (SQLite or MariaDB). Current conditions, historical records, NOAA reports, almanac data, station metadata.
2. **External providers** — forecast, AQI, severe-weather alerts, earthquakes, and radar data, fetched from external services through per-provider plugin modules that live inside this repo. No weewx extensions required.

The service binds to loopback by default and sits behind a reverse proxy (Apache, Nginx, or Caddy). The database user is enforced to be read-only at startup.

---

## Architecture

```
weewx archive DB (SQLite or MariaDB)
        |
        | SELECT only (clearskies_ro user)
        |
        v
weewx-clearskies-api  (FastAPI / Python 3.12 / uvicorn)
  /api/v1/current        current conditions
  /api/v1/archive        historical records
  /api/v1/records        all-time records
  /api/v1/reports        NOAA text reports
  /api/v1/almanac        sun/moon data (Skyfield)
  /api/v1/station        station metadata
  /api/v1/capabilities   provider capability declarations
  /api/v1/forecast       forecast from configured provider
  /api/v1/alerts         weather alerts from configured provider
  /api/v1/aqi            air quality from configured provider
  /api/v1/earthquakes    seismic events from configured provider
  /api/v1/radar/...      radar tile/frame metadata
  /api/v1/pages          page visibility config
  /api/v1/charts/groups  chart group config
  /api/v1/content/about  operator about-page content
  /api/v1/content/legal  operator legal-page content
        |
        | JSON over HTTPS (via reverse proxy)
        v
weewx-clearskies-dashboard  (React SPA)
```

External data providers are internal plugin modules under `weewx_clearskies_api/providers/`. One provider is active per domain per deployment. No weewx extensions are shipped.

---

## Day-1 providers

| Domain | Keyless | Keyed |
|---|---|---|
| Forecast | Open-Meteo, NWS (US only) | Aeris, OpenWeatherMap, Weather Underground |
| Alerts | NWS (US only) | Aeris, OpenWeatherMap |
| AQI | Open-Meteo | Aeris, OpenWeatherMap, IQAir |
| Earthquakes | USGS, GeoNet (NZ), EMSC (Europe), RéNaSS (France) | — |
| Radar | RainViewer, IEM NEXRAD (US), NOAA MRMS (US), MSC GeoMet (CA), DWD RADOLAN (DE) | Aeris, OpenWeatherMap, operator iframe embed |

---

## Quick start

```bash
pip install weewx-clearskies-api

# Copy the example config and edit it
sudo cp /usr/local/lib/python3.12/dist-packages/weewx_clearskies_api/etc/api.conf.example \
     /etc/weewx-clearskies/api.conf

# Set secrets in a mode-0600 file (never in api.conf)
sudo tee /etc/weewx-clearskies/secrets.env <<'EOF'
WEEWX_CLEARSKIES_DB_USER=clearskies_ro
WEEWX_CLEARSKIES_DB_PASSWORD=<password>
EOF
sudo chmod 0600 /etc/weewx-clearskies/secrets.env

# Start (loads secrets.env first, then the service)
source /etc/weewx-clearskies/secrets.env
weewx-clearskies-api

# Verify
curl http://127.0.0.1:8765/api/v1/station
```

For a complete deployment — API + realtime service + dashboard + reverse proxy — use [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack).

---

## API documentation

The auto-generated OpenAPI spec and Swagger UI are available at:

- `/api/v1/docs` — Swagger UI (interactive)
- `/api/v1/openapi.json` — raw OpenAPI 3.1 spec

The canonical contract that governs all response shapes is at [`docs/contracts/openapi-v1.yaml`](https://github.com/inguy24/weewx-clearskies-stack/blob/master/docs/contracts/openapi-v1.yaml) in the stack repo.

---

## Documentation

| Doc | Contents |
|---|---|
| [INSTALL.md](INSTALL.md) | Step-by-step install for native (pip + systemd) and Docker |
| [CONFIG.md](CONFIG.md) | Every config option with defaults and examples |
| [SECURITY.md](SECURITY.md) | Auth model, trust boundaries, vulnerability reporting |
| [CHANGELOG.md](CHANGELOG.md) | Release notes and upgrade guidance |

---

## Sibling repositories

| Repo | Role |
|---|---|
| [weewx-clearskies-dashboard](https://github.com/inguy24/weewx-clearskies-dashboard) | React SPA — the browser UI |
| [weewx-clearskies-realtime](https://github.com/inguy24/weewx-clearskies-realtime) | SSE bridge — publishes weewx loop packets as Server-Sent Events |
| [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack) | Docker Compose deployment, setup wizard, architecture diagrams |

---

## License

[GNU General Public License v3.0](LICENSE)

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

Distributed AS-IS. See LICENSE for full terms.
