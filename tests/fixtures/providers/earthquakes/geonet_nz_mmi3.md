# GeoNet Fixture: geonet_nz_mmi3.json

mode: real-capture

source URL: https://api.geonet.org.nz/quake?MMI=3
captured: 2026-05-11 (live from weather-dev); sliced to first 3 features for fixture size

## Scenario

Recent New Zealand earthquakes at Modified Mercalli Intensity (MMI) >= 3.
Captured using MMI=3 filter to get events with noticeable shaking.
Response truncated to 3 features (original had 100; 3 representative events retained).

## Key facts for test assertions

Feature 0 (2026p353000):
- id: properties.publicID = "2026p353000" (NO top-level Feature.id in GeoNet)
- properties.time: "2026-05-11T14:38:39.296Z" (ISO 8601 with Z, no conversion needed)
- properties.magnitude: 2.4993398757078116
- properties.magnitudeType: NOT PROVIDED → None (GeoNet does not expose magnitudeType)
- properties.depth: 20.33428955078125 (positive km, from properties not geometry)
- properties.mmi: 3 (lowercase; NOT MMI — GeoNet api-docs confirmed 2026-05-11)
- properties.locality: "10 km south of Taumarunui" → canonical place
- properties.quality: "best" → canonical status
- geometry.coordinates: [175.256698608, -38.977085114] → longitude, latitude (NO depth in coords)
- url: NOT in response → constructed as "https://www.geonet.org.nz/earthquake/2026p353000"
- tsunami: NOT provided → None
- felt: NOT provided → None
- alert: NOT provided → None

Feature 2 (2026p351278):
- quality: "deleted" — tests status passthrough

## Wire-shape notes

- GeoNet is NOT strict FDSN-Event — different field names and structure
- id comes from properties.publicID (no top-level Feature.id)
- geometry.coordinates is [lon, lat] ONLY (2 elements, no depth Z component)
- mmi is LOWERCASE (not MMI like the query param)
- magnitudeType is not exposed — canonical magnitudeType = None
- url is not in response — must be constructed from publicID
- quality field maps to canonical status
- operator radius filter must be applied post-fetch (GeoNet has no lat/lon/radius params)
