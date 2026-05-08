# Fixture: forecast.json

**Source location:** Seattle, WA — latitude=47.6062, longitude=-122.3321

**Capture date:** 2026-05-07

**Captured on:** weather-dev (192.168.2.113) via the LXC container path
`ssh ratbert "lxc exec weather-dev -- sudo -u ubuntu bash -lc '...'"`

**Capture command:**
```bash
curl "https://api.open-meteo.com/v1/forecast?latitude=47.6062&longitude=-122.3321&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability,precipitation,weather_code,cloud_cover&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,uv_index_max,weather_code&timezone=America%2FLos_Angeles&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&timeformat=iso8601" \
  | python3 -m json.tool > tests/fixtures/providers/openmeteo/forecast.json
```

**Response shape:**
- `latitude`: 47.595562 (Open-Meteo snaps to nearest grid point)
- `longitude`: -122.32443
- `utc_offset_seconds`: -25200 (America/Los_Angeles PDT = UTC-7)
- `timezone`: America/Los_Angeles

**Forecast point counts:**
- 168 hourly points (7 days × 24 hours — Open-Meteo default `forecast_days=7`)
- 7 daily points

**Spot-check values (index 0, for test assertions):**
- `hourly.time[0]`: `"2026-05-07T00:00"` (station-local ISO, no offset)
- `hourly.temperature_2m[0]`: 52.9 (°F, `temperature_unit=fahrenheit`)
- `hourly.weather_code[0]`: 3 (Overcast)
- `daily.time[0]`: `"2026-05-07"` (station-local date)
- `daily.temperature_2m_max[0]`: 61.9 (°F)
- `daily.sunrise[0]`: `"2026-05-07T05:42"` (station-local ISO)

**UTC conversion check (hourly index 0):**
`"2026-05-07T00:00"` + `utc_offset_seconds=-25200` (−7 h) → `"2026-05-07T07:00:00Z"`

**UTC conversion check (daily sunrise index 0):**
`"2026-05-07T05:42"` + `utc_offset_seconds=-25200` (−7 h) → `"2026-05-07T12:42:00Z"`

**Schema shape rule:** This fixture is the source of truth for the
`_OpenMeteoForecastResponse` wire-shape Pydantic model field coverage.
Do not reduce to a synthetic subset — that hides protocol-evolution bugs
per `rules/clearskies-process.md` "Real schemas in unit tests where shape matters."

**Re-capture instructions:**
```bash
# On weather-dev (via ratbert):
curl "https://api.open-meteo.com/v1/forecast?latitude=47.6062&longitude=-122.3321&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability,precipitation,weather_code,cloud_cover&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,uv_index_max,weather_code&timezone=America%2FLos_Angeles&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&timeformat=iso8601" \
  | python3 -m json.tool > tests/fixtures/providers/openmeteo/forecast.json
# Then update this .md with the capture date and spot-check values.
```
