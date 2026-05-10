# Fixture: openmeteo_current.json

## Capture metadata

- **Capture date (UTC):** 2026-05-10T22:xx (approximately 22:00 GMT, matching the `current.time` field)
- **Coordinates used:** Seattle, WA — latitude=47.6062, longitude=-122.3321
- **Open-Meteo snapped to:** latitude=47.600006, longitude=-122.3 (provider grid snap)
- **Full URL:**
  ```
  https://air-quality-api.open-meteo.com/v1/air-quality?latitude=47.6062&longitude=-122.3321&current=us_aqi,us_aqi_pm2_5,us_aqi_pm10,us_aqi_nitrogen_dioxide,us_aqi_ozone,us_aqi_sulphur_dioxide,us_aqi_carbon_monoxide,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone&timezone=GMT
  ```
- **sha256 of raw response body (before pretty-printing):**
  `6abbe70aae2285243109f26331f08ad6dce64adc19cb3535fa4d738b74e52e78`
- **Provider:** Open-Meteo air-quality endpoint (keyless, no rate-limit gate)
- **Captured via:** WebFetch tool (Claude Code session)

## Field population notes

All per-pollutant fields were populated in this capture:

| Field | Value | Notes |
|---|---|---|
| `current.us_aqi` | 73 | Main AQI → Moderate category |
| `current.us_aqi_pm2_5` | 73 | Dominant sub-AQI (highest = main pollutant PM2.5) |
| `current.us_aqi_pm10` | 24 | Populated |
| `current.us_aqi_nitrogen_dioxide` | 0 | Populated (zero) |
| `current.us_aqi_ozone` | 23 | Populated |
| `current.us_aqi_sulphur_dioxide` | 0 | Populated (zero) |
| `current.us_aqi_carbon_monoxide` | 2 | Populated |
| `current.pm10` | 4.5 μg/m³ | Populated |
| `current.pm2_5` | 3.1 μg/m³ | Populated |
| `current.carbon_monoxide` | 155.0 μg/m³ | Populated |
| `current.nitrogen_dioxide` | 0.2 μg/m³ | Populated |
| `current.sulphur_dioxide` | 0.1 μg/m³ | Populated |
| `current.ozone` | 87.0 μg/m³ | Populated |

## Canonical derivations from this fixture

- `aqi` = 73 → int(73) = 73
- `aqiCategory` = "Moderate" (51–100 band)
- `aqiMainPollutant` = "PM2.5" (us_aqi_pm2_5=73 is the argmax; no tie)
- `aqiLocation` = None (PARTIAL-DOMAIN — Open-Meteo does not supply)
- `observedAt` = "2026-05-10T22:00:00Z" (current.time="2026-05-10T22:00" + Z suffix per LC4)
- `source` = "openmeteo"
- `pollutantPM25` = 3.1 (μg/m³, passthrough — no conversion)
- `pollutantPM10` = 4.5 (μg/m³, passthrough — no conversion)
- `pollutantO3` = 87.0 × 24.45 / 48.00 ≈ 44.335 ppm (converted)
- `pollutantNO2` = 0.2 × 24.45 / 46.01 ≈ 0.10626 ppm (converted)
- `pollutantSO2` = 0.1 × 24.45 / 64.07 ≈ 0.038164 ppm (converted)
- `pollutantCO` = 155.0 × 24.45 / 28.01 ≈ 135.306 ppm (converted)

## Synthetic fixtures needed

Two additional synthetic fixtures are hand-crafted from this real capture to cover
edge cases that cannot occur simultaneously with a real populated response:

1. **openmeteo_current_all_null.json** — all `current.*` fields null except `time`.
   Documents "no reading available" code path where `fetch()` returns None and
   caches the `{"_no_reading": True}` sentinel.
   Origin: synthetic from openmeteo_current.json — nulled all value fields.

2. **openmeteo_current_us_aqi_only.json** — `current.us_aqi` populated (73) but all
   six `us_aqi_*` sub-AQI fields are null. Tests that `aqiMainPollutant = None`
   when no sub-AQI breakdown is available, but `aqi` and `aqiCategory` populate.
   Origin: synthetic from openmeteo_current.json — nulled sub-AQI fields only.
