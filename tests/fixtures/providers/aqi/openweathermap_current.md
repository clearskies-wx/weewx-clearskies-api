# openweathermap_current.json — fixture sidecar

## Capture metadata

- **Provider:** OpenWeatherMap Air Pollution API (free tier)
- **Endpoint:** `GET https://api.openweathermap.org/data/2.5/air_pollution`
- **Capture date (UTC):** 2026-05-11T03:57:08Z
- **Coordinates:** `lat=47.6062 lon=-122.3321` (Seattle, WA — same as 3b-9/3b-10 AQI fixtures)
- **Curl invocation (appid redacted):**
  ```
  curl "https://api.openweathermap.org/data/2.5/air_pollution?lat=47.6062&lon=-122.3321&appid=REDACTED"
  ```
- **sha256 of raw JSON body:**
  `b25bccd70804302974e365f33122f486c5294a267a84f5b06180147979fc101d`
- **Fixture origin:** Real capture (free-tier; no L3 synthetic fallback needed)

## Wire values

| Field | Value | Notes |
|---|---|---|
| `list[0].main.aqi` | 2 | OWM 1–5 ordinal (2 = Fair). IGNORED per LC4 — canonical aqi derived from concentrations. |
| `list[0].components.co` | 139.79 µg/m³ | Converted to ppm for canonical + sub-AQI computation |
| `list[0].components.no` | 0 µg/m³ | Dropped (no EPA AQI band for NO) |
| `list[0].components.no2` | 2.05 µg/m³ | Converted to ppm |
| `list[0].components.o3` | 66.23 µg/m³ | Converted to ppm |
| `list[0].components.so2` | 0.34 µg/m³ | Converted to ppm |
| `list[0].components.pm2_5` | 0.5 µg/m³ | Passthrough (group_concentration) |
| `list[0].components.pm10` | 0.81 µg/m³ | Passthrough (group_concentration) |
| `list[0].components.nh3` | 0.37 µg/m³ | Dropped (no EPA AQI band for NH3) |
| `list[0].dt` | 1778471818 | Unix UTC seconds → `2026-05-10T23:36:58Z` |

## Expected canonical output

With the ugm3_to_ppm formula (`ppm = µg/m³ × 24.45 / MW`) and EPA breakpoint tables:

| Pollutant | µg/m³ | ppm (formula) | sub-AQI |
|---|---|---|---|
| O3 | 66.23 | 33.736 | 300 (cap — above 0.200 ppm, 8-hr table max per Q1 Option A) |
| NO2 | 2.05 | 1.089 | 274 (in [0.650, 1.249, 201, 300] band) |
| SO2 | 0.34 | 0.1297 | 125 (in [0.076, 0.185, 101, 150] band) |
| CO | 139.79 | 122.02 | 500 (cap — above 50.4 ppm) |
| PM2.5 | 0.5 | — | 3 (in [0.0, 9.0, 0, 50] band) |
| PM10 | 0.81 | — | 1 (in [0, 54, 0, 50] band) |

- **aqi** = max(300, 274, 125, 500, 3, 1) = 500
- **aqiCategory** = "Hazardous" (AQI 500 → 301–500 band)
- **aqiMainPollutant** = "CO" (argmax; CO=500 wins; PM2.5 wins ties by table order but CO=500 is unambiguous)
- **aqiLocation** = null (PARTIAL-DOMAIN — no location field on OWM Air Pollution wire)
- **observedAt** = "2026-05-11T03:56:58Z" (epoch 1778471818 → UTC ISO-8601)
- **source** = "openweathermap"
