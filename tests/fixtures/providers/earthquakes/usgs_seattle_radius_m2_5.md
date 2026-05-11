# USGS Fixture: usgs_seattle_radius_m2_5.json

mode: real-capture

source URL: https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&latitude=47.6&longitude=-122.3&maxradiuskm=500&minmagnitude=2.5&limit=3&orderby=time
captured: 2026-05-11 (live from weather-dev)

## Scenario

3 most-recent M2.5+ earthquakes within 500 km of Seattle, WA (lat=47.6, lon=-122.3).
Captured to represent real wire shape for the USGS provider unit and integration tests.

## Key facts for test assertions

Feature 0 (uw62242697):
- id: "uw62242697"
- properties.place: "1 km W of Concrete, Washington"
- properties.time: 1778131207650 (epoch ms → "2026-05-07T02:40:07Z")
- properties.mag: 2.8379271030426025
- properties.magType: "ml"
- properties.status: "reviewed"
- properties.tsunami: 0 → bool False
- properties.felt: 160
- properties.mmi: null → None
- properties.alert: null → None
- geometry.coordinates: [-121.77283477783203, 48.53633499145508, 0.189999997615814]
  → longitude=-121.7728, latitude=48.5363, depth=0.19 km
- extras: net="uw", code="62242697", sig=193, gap=100, type="earthquake"

Feature 1 (uw62240147):
- id: "uw62240147"
- properties.tsunami: 0 → bool False
- properties.felt: 26
- properties.status: "reviewed"

Feature 2 (us6000sphj):
- id: "us6000sphj"
- properties.felt: null → None (not all events have felt reports)
- properties.tsunami: 0 → bool False

## Wire-shape notes

- properties.time is epoch milliseconds (NOT ISO 8601) — requires epoch_ms_to_utc_iso8601()
- properties.tsunami is 0 or 1 integer (NOT boolean) — requires bool() cast
- geometry.coordinates[2] is depth in km, positive below surface (no sign flip needed for USGS)
- id is top-level Feature.id field
- USGS-specific extras: net, code, ids, sources, types, sig, nst, dmin, rms, gap
