# NWS Forecast Fixtures — Capture Metadata

## Capture details

| Field | Value |
|---|---|
| Capture date | 2026-05-07 |
| Capture source | Live NWS API via weather-dev (192.168.2.113) |
| Location | Seattle, WA |
| Latitude | 47.6062 |
| Longitude | -122.3321 |
| CWA (office) | SEW |
| GridX / GridY | 125 / 68 |
| IANA timezone | America/Los_Angeles |
| User-Agent | (weewx-clearskies-api-test, capture@example.com) |

## Fixture files

| File | Source URL | Description |
|---|---|---|
| `forecast_points.json` | `GET /points/47.6062,-122.3321` | Points metadata: cwa, gridId, gridX, gridY, timeZone, forecastHourly URL, forecast URL |
| `forecast_hourly.json` | `GET /gridpoints/SEW/125,68/forecast/hourly` | 156 hourly periods (~7 days); windSpeed single-value strings |
| `forecast.json` | `GET /gridpoints/SEW/125,68/forecast` | 14 day/night periods (7 days × 2); windSpeed range strings; **first period is isDaytime=false** (real capture edge case from brief call 18) |
| `products_afd_list.json` | `GET /products?type=AFD&location=SEW` | List of recent AFD products (first product ID used for body fetch) |
| `products_afd_body.json` | `GET /products/44453767-e473-4c16-835d-96495e091585` | AFD body: productText, issuanceTime, wmoCollectiveId=FXUS66, issuingOffice=KSEW |

## Notes

- `forecast.json` first period has `isDaytime: false` — this is the real-data edge case described in
  brief call 18 ("if the first response period may be a night period"). Tests use this fixture
  directly to verify `_pair_day_night` skips the leading night. No separate fixture needed for
  this edge case.
- `forecast_hourly.json` has 156 periods — sufficient for slice tests (brief requirement: AT LEAST 24).
- `products_afd_body.json` contains a real AFD text for 2026-05-07 evening (PDT = UTC-7).
  `issuanceTime` is `2026-05-08T03:40:00+00:00` (UTC; 8:40 PM PDT).

## Replay notes

To re-capture, run the curl sequence in the brief (phase-2-task-3b-3-forecast-brief.md §Recorded
fixture capture) with Seattle lat/lon. The grid coordinates (125, 68) are subject to NWS schema
versioning; if `/points` returns different gridX/gridY on re-capture, update this sidecar and the
capture commands.
