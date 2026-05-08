# OpenWeatherMap Forecast Provider Fixtures

Sidecar documentation per 3b-1 fixture-capture discipline.

## onecall.json

- **Type:** Synthetic — constructed from `docs/reference/api-docs/openweathermap.md` L161-213
  example response. Fields mirrored from the example; NOT captured live.
- **Created:** 2026-05-08
- **Lat/Lon:** 47.6062 N, 122.3321 W (Seattle, WA — same coordinates as NWS/Aeris fixtures)
- **Tier simulated:** Paid "One Call by Call" subscription (`/data/3.0/onecall`)
- **Hourly count:** 48 (OWM One Call 3.0 maximum per api-docs §"One Call API 3.0")
- **Daily count:** 8 (OWM One Call 3.0 maximum)
- **Reason for synthetic:** No paid OWM One Call 3.0 subscription available at fixture-capture
  time. A free-tier OWM key returns 401 from `/data/3.0/onecall` per the documented behavior
  (api-docs §"Known issues / gotchas"). Synthetic-from-api-docs-example pattern applied per
  brief L3 rule + 3b-4 process lesson.
- **Injected fields (all synthetic, not captured):** full `hourly[]` (48 entries) and
  `daily[]` (8 entries) with realistic value variation across the arrays. Base shape mirrored
  from L161-213 example.

### Wire-shape notes

- `timezone_offset`: -25200 (PDT, -7h). Used for `validDate` derivation per lead-call 25.
- `hourly[].dt`: epoch UTC seconds, spaced 1h apart starting 2026-05-08T20:00:00Z.
- `hourly[].rain`: `{1h: <mm>}` sub-object present on rain periods (id 500, 501). Absent on
  non-rain periods (per api-docs §"Rain/snow keys may be absent").
- `hourly[].pop`: 0–1 float (not percent). Must multiply × 100 per lead-call 22.
- `daily[].dt`: epoch UTC seconds, 1 day apart starting 2026-05-09T07:00:00Z (local midnight PDT).
- `daily[].rain`: scalar mm (not sub-object — unlike hourly shape). Present only for rain days.
- `daily[].summary`: human-readable text present for all 8 days. Both `weatherText` (preferred)
  and `narrative` map to this field per lead-calls 20+21.
- `daily[].temp.max` / `daily[].temp.min`: canonical `tempMax` / `tempMin`.
- Weather code range coverage in this fixture:
  - 800 (Clear): hourly entries 10-11, 22-23; daily entries 4-5.
  - 801 (Few clouds): hourly entries 9, 21, 33.
  - 802 (Scattered clouds): hourly entries 8, 20, 32; daily entry 3.
  - 803 (Broken clouds): hourly entries 0, 1, 12, 13, 24, 25, 36, 37; daily entries 0, 6.
  - 804 (Overcast): hourly entries 2, 3, 14, 15, 26, 27, 38, 39; daily entry 7.
  - 500 (Light rain): hourly entries 5, 6, 7, 17, 18, 19, 29, 30, 31, 41, 42, 43, 45, 46, 47;
    daily entry 2.
  - 501 (Moderate rain): hourly entries 4, 16, 28, 40; daily entry 1.
- **precipType coverage:** rain (500, 501), None (800-804). No snow/sleet/freezing-rain in
  this fixture. Snow/sleet/freezing-rain code paths tested with synthetic period dicts in
  the unit suite (no full-fixture needed for code-level coverage).

## error_401_basic_tier.json

- **Type:** Synthetic — based on `docs/reference/api-docs/openweathermap.md` §"Known issues
  / gotchas": "A free-tier API key alone returns 401 from `/data/3.0/onecall`."
- **Created:** 2026-05-08
- **Shape:** `{cod: 401, message: "..."}`
- **HTTP status:** 401
- **Used to test:** Q1 graceful-empty-bundle path — `KeyInvalid` from One Call 3.0 returns
  empty `ForecastBundle` (NOT 502 error), per brief §Q1 user decision 2026-05-08.

## error_429_quota.json

- **Type:** Synthetic — based on OWM standard rate-limit error envelope shape per
  `docs/reference/api-docs/openweathermap.md` §"Rate limits".
- **Created:** 2026-05-08
- **Shape:** `{cod: 429, message: "..."}`
- **HTTP status:** 429
- **Used to test:** `QuotaExhausted` exception raised on 429, propagated as 503
  ProviderProblem per canonical error taxonomy.
