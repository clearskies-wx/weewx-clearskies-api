# Aeris Forecast Provider Fixtures

Sidecar documentation per 3b-1 fixture-capture discipline.

## forecasts_hourly.json

- **Capture date:** 2026-05-08
- **Endpoint:** `GET /forecasts/47.6062,-122.3321?filter=1hr&limit=240`
- **Lat/Lon:** 47.6062 N, 122.3321 W (Seattle, WA — same coordinates as NWS fixtures)
- **Aeris account tier:** Free (1000 calls/day; registered to weather.shaneburkhardt.com)
- **Period count:** 24 (trimmed from 240 to keep fixture manageable; covers 1 day of hourly periods)
- **Capture note:** Real live capture. Periods trimmed to first 24 (1 day) after capture.
- **Redaction:** `client_id` and `client_secret` were query parameters in the request URL —
  they do NOT appear in the response body. No redaction needed in the fixture file itself.
  Tests use placeholder values (`TEST_CLIENT_ID` / `TEST_CLIENT_SECRET`).

### Wire-shape notes (from real captured response)

- `windSpeedMaxMPS` IS present in hourly periods (brief lead-call 13 assumption confirmed).
- `windGustMaxMPS` is NOT present in hourly periods (field name is `windGustMPS` without Max suffix).
- `weatherPrimaryCoded` present: e.g., `"::SC"` (scatter cloud — no precip descriptor).
- `pop`, `sky`, `precipMM`, `precipIN`, `windDirDEG` all present.
- `humidity` present as `humidity` (not `outHumidity`).

## forecasts_daynight.json

- **Capture date:** 2026-05-08
- **Endpoint:** `GET /forecasts/47.6062,-122.3321?filter=daynight&limit=14`
- **Lat/Lon:** 47.6062 N, 122.3321 W (Seattle, WA)
- **Aeris account tier:** Free
- **Period count:** 14 (7 days × 2 periods: daytime + nighttime)
- **Redaction:** No credentials in response body.

### Wire-shape notes (from real captured response)

- **`summary` field:** `null` at both `response[0].summary` and `response[0].periods[0].summary`.
  Confirms free-tier has no discussion/summary text (expected per brief Q2 + lead-call 14).
- **`sunriseISO` / `sunsetISO`:** `null` in all periods from this free-tier account
  (appears in aeris.md observations example but NOT returned in free-tier forecast filter=daynight).
  Implementation must handle `null` for these fields.
- **`windGustMaxMPS`:** `null` in response but `windGustMPS` IS present (non-null, e.g., 7 m/s).
  Brief's `_AerisDayNightPeriod` model declares `windGustMaxMPS` but the real field name
  for daynight is `windGustMPS` (no "Max" suffix). This is a wire-shape gap — surfaced to lead.
- **`windSpeedMaxMPS`:** Present and non-null (e.g., 5 m/s). Confirmed field name is correct.
- **`uvi`:** Present (e.g., 5).
- **`maxTempF` / `minTempF`:** Present in day periods; `null` in night periods.

## forecasts_daynight_with_summary.json

- **Type:** Synthetic fixture (hand-crafted from `forecasts_daynight.json`)
- **Created:** 2026-05-08
- **Purpose:** Exercise the `_extract_aeris_discussion` runtime-detection path (Q2 user decision,
  lead-call 14). Free-tier Aeris does not return a `summary` field; this fixture injects one
  to test the paid-tier detection path without requiring a real paid-tier account.
- **Changes from base:**
  - Added `response[0].summary = "Partly cloudy skies expected through the period..."` (response-level)
  - Added `response[0].periods[0].summary = "Partly cloudy with a high near 65F..."` (period-level)
  - Added `response[0].periods[0].weatherPrimary` (present in base fixture as `weather` field)
- **Fixture strategy used:** synthetic-from-free (no paid-tier account available).
  Lead note: the exact paid-tier field name is unconfirmed from real Aeris paid-tier responses.
  Both `response[0].summary` and `response[0].periods[0].summary` are tested; impl team should
  detect both paths per lead-call 14.

## xcast_forecasts_hourly.json

- **Capture date:** 2026-06-20
- **Endpoint:** `GET /xcast/forecasts/33.6568,-117.9827?filter=1hr&limit=24`
- **Lat/Lon:** 33.6568 N, 117.9827 W (Huntington Beach, CA)
- **Aeris account tier:** PWS contributor (xcast included)
- **Period count:** 24 (1 day of hourly periods)
- **Capture note:** Real live capture. Response is wire-compatible with standard `/forecasts`
  hourly periods. Two additional fields per period: `tempConfidenceLimit` and
  `windConfidenceLimit`. Both are `null` in this fixture because there are no Xcast
  sensors near this location. When Xcast sensors are deployed nearby, these fields
  contain `{"upper": <float>, "lower": <float>}` confidence bounds.
- **Redaction:** No credentials in response body.

### Wire-shape notes

- All fields from standard `/forecasts?filter=1hr` are present with identical names.
- `tempConfidenceLimit` and `windConfidenceLimit` appended at end of each period object.
- `loc` snaps to a slightly different grid point (33.691, -118.010 vs 33.657, -117.983
  for standard) — xcast uses its own spatial grid.
- `interval` field is `"1hr"` (same as standard hourly).
- Temperature values differ from standard (e.g., tempC: 20.36 vs 19.8 at same hour) —
  ML enhancement is active even without local sensors.

### Xcast daynight limitation (verified 2026-06-20)

The `/xcast/forecasts` endpoint ignores `filter=daynight`. When `filter=daynight` is
requested, xcast returns hourly periods anyway (`interval: "1hr"`). Consequence: the
module uses xcast for hourly calls only; daynight calls always use the standard
`/forecasts` endpoint. No `xcast_forecasts_daynight.json` fixture is needed.

## error_401_invalid_credentials.json

- **Type:** Synthetic (based on Aeris API documentation §"HTTP status codes" + §"Common error codes")
- **Shape:** `success=false, error={code, description}, response=[]`
- **HTTP status:** 401 (invalid credentials)
- **Used to test:** `KeyInvalid` exception raised on 401 per lead-call 9/12.

## error_429_rate_limit.json

- **Type:** Synthetic (based on Aeris API documentation §"Rate limits" + error code `maxhits_min`)
- **Shape:** `success=false, error={code, description}, response=[]`
- **HTTP status:** 429 (rate limit exceeded)
- **Used to test:** `QuotaExhausted` exception raised on 429 per lead-call 9/18.

## error_warn_invalid_location.json

- **Type:** Synthetic (based on Aeris API documentation §"Common error / warning codes")
- **Shape:** `success=true, error={code:"warn_location", description}, response=[]`
- **HTTP status:** 200 (warning alongside success=true)
- **Used to test:** Empty bundle returned when Aeris cannot resolve location (lead-call 17).
  NOT a hard error — returns `ForecastBundle(hourly=[], daily=[], discussion=None, source="aeris")`.
