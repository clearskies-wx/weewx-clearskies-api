# aeris_current.json — fixture sidecar

**Capture date (UTC):** 2026-05-10

**Source:** real-capture from live Aeris /airquality endpoint

**Endpoint URL:**
`https://data.api.xweather.com/airquality/47.6062,-122.3321?filter=airnow&client_id=<redacted>&client_secret=<redacted>`

**Coordinates:** Seattle, WA — 47.6062, -122.3321

**Response summary:**
- `success: true` with non-empty `response[0].periods`
- AQI: 33 (Good, EPA airnow methodology)
- Dominant pollutant: `o3` (Ozone)
- Six pollutants present: o3, pm2.5, pm10, co, no2, so2
- `place.name`: "seattle"

**sha256 of fixture body:**
`d0aec081fb3f0e27b517d91e9b4da6ce5361b705faf885bfd9fd0094e3cc8c7e`

**Capture notes:**
Captured 2026-05-10 using the free-tier Aeris credentials (registered namespace).
The /airquality endpoint was accessible on the free plan.
`filter=airnow` passed explicitly per LC21 (locks US EPA AQI methodology).
All six canonical pollutants populated in `pollutants[]` array.
Dominant pollutant is `o3` (ozone) — not `pm2.5` as in the docs example, reflecting
actual Seattle air quality on capture date. Both PPB and UGM3 fields populated for
gas pollutants; particulates have `valuePPB: null` as documented in wire-shape notes.
