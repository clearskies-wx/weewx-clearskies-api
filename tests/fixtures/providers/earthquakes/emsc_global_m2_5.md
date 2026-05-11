# EMSC Fixture: emsc_global_m2_5.json

mode: real-capture

source URL: https://www.seismicportal.eu/fdsnws/event/1/query?format=json&minmag=2.5&limit=3&orderby=time
captured: 2026-05-11 (live from weather-dev)

## Scenario

3 most-recent M2.5+ global earthquakes from EMSC SeismicPortal.
Captured to represent real wire shape for the EMSC provider unit and integration tests.
Global query (no lat/lon/radius) to demonstrate the EMSC response structure.

## Key facts for test assertions

Feature 0 (20260511_0000281):
- id: top-level Feature.id = "20260511_0000281" (same as properties.unid)
- properties.time: "2026-05-11T17:00:26.0Z" (ISO 8601 Z, one decimal)
- properties.mag: 2.5
- properties.magtype: "m" (lowercase; can be plain "m" when scale unknown)
- properties.depth: 5.0 (POSITIVE km — canonical source; NOT geometry.coordinates[2]=-5.0)
- geometry.coordinates[2]: -5.0 (NEGATIVE; GeoJSON Z-up; do NOT use for canonical depth)
- properties.flynn_region: "MOLUCCA SEA" → canonical place
- properties.lat: 1.39, properties.lon: 126.66 → canonical latitude/longitude
- properties.auth: "BMKG" → extras
- properties.evtype: "ke" → extras
- properties.unid: "20260511_0000281" → extras (and matches top-level id)
- url: NOT in response → constructed as "https://www.seismicportal.eu/eventdetails.html?unid=20260511_0000281"
- status: NOT in JSON flavor (only in XML/QuakeML) → NOT in canonical; goes to extras if needed
- tsunami, felt, mmi, alert: NOT provided → None

Feature 1 (20260511_0000279):
- magtype: "ml" (named type)
- properties.depth: 37.8 (positive), geometry.coordinates[2]: -37.8 (negative)

## Wire-shape notes

- EMSC depth sign: geometry.coordinates[2] is NEGATIVE (GeoJSON Z-up); properties.depth is POSITIVE
  Always use properties.depth for canonical depth
- magtype is LOWERCASE (unlike USGS magType camelCase and ReNaSS magType camelCase)
- status is absent in JSON flavor; route via extras if needed (out of v0.1 scope)
- id from top-level Feature.id; also available as properties.unid (identical values)
- url must be constructed from unid
- Time precision varies: "17:00:26.0Z" (1 decimal) vs "17:07:33.440295Z" (6 decimals) both valid
