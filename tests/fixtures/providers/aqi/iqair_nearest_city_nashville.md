# IQAir Fixture: iqair_nearest_city_nashville.json

mode: synthetic-from-published-example

injected fields: none — this IS the published example verbatim

source: docs/reference/api-docs/iqair.md §"Example response (Community / free tier)"
source URL: https://api-docs.iqair.com/ (reproduced in api-docs file)
captured: 2026-05-11 (api-docs last verified date)

## Notes

This fixture reproduces the Nashville Community/free-tier example response published in
the IQAir API docs and transcribed verbatim into docs/reference/api-docs/iqair.md.

No real-capture was performed because no IQAir Community API key is available on
weather-dev at round 3b-12 time. Per the L3 synthetic-from-published-example pattern
(clearskies-test-author.md), the published API docs example serves as the fixture when
real-capture is unavailable.

## Key facts for test assertions

- status: "success"
- data.city: "Nashville"
- data.state: "Tennessee"
- data.country: "USA"
- data.current.pollution.ts: "2019-04-08T18:00:00.000Z"
- data.current.pollution.aqius: 10  → aqi=10, aqiCategory="Good" (0–50 EPA band)
- data.current.pollution.mainus: "p2"  → aqiMainPollutant="PM2.5"
- data.current.pollution.aqicn: 3  (China AQI — not used in canonical)
- data.current.pollution.maincn: "p2"  (China dominant pollutant — not used)
- observedAt (expected): "2019-04-08T18:00:00Z"  (millis dropped, Z suffix preserved)
- aqiLocation (expected): "Nashville, Tennessee"  (city + ", " + state per LC4)
- pollutant* fields: all None (PARTIAL-DOMAIN — free tier has no concentrations)

## When to replace

Replace with a real-capture fixture when an IQAir Community API key becomes available
on weather-dev. The sidecar should then read "mode: real-capture" with timestamp +
station coordinates. Test code should not need to change.
