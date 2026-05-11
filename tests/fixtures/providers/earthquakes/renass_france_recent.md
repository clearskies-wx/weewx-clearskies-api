# ReNaSS Fixture: renass_france_recent.json

mode: real-capture

source URL: https://api.franceseisme.fr/fdsnws/event/1/query?format=json&limit=3&orderby=time&latitude=48.5&longitude=7.7&maxradiuskm=500
captured: 2026-05-11 (live from weather-dev)

## Scenario

3 most-recent earthquakes from ReNaSS (BCSF-Rénass / EPOS-France) via the new
api.franceseisme.fr endpoint (the legacy renass.unistra.fr endpoint returns 404 since
the EPOS-France migration — verified 2026-05-11). Events near Strasbourg (lat=48.5,
lon=7.7, radius=500 km) to demonstrate real French network wire shape.

## Key facts for test assertions

Feature 0 (fr2026trycyd):
- id: top-level Feature.id = "fr2026trycyd" (ONLY id source — no properties.publicID/unid)
- properties.time: "2026-05-11T16:36:59.105052Z" (ISO 8601 Z, microsecond precision)
- properties.mag: 1.716991822
- properties.magType: "MLv" (camelCase; differs from EMSC lowercase magtype)
- properties.depth: 14.12690163 (POSITIVE km — canonical source)
- geometry.coordinates[2]: -14.12690163 (NEGATIVE — do NOT use for canonical depth)
- properties.description.en: "Event of magnitude 1.7, near Pau" → canonical place
- properties.description.fr: "Évènement de magnitude 1.7, proche de Pau" → extras["description_fr"]
- properties.url.en: "https://renass.unistra.fr/en/events/fr2026trycyd" → canonical url
- properties.url.fr: "https://renass.unistra.fr/fr/evenements/fr2026trycyd" → extras["url_fr"]
- properties.automatic: true → canonical status = "automatic"
- properties.type: null → extras["type"]
- latitude: 43.00603485, longitude: 0.2690240741

Feature 1 (fr2026trxulp):
- properties.automatic: true → status = "automatic"
- properties.type: null

Feature 2 (fr2026trxhpm):
- properties.type: "quarry blast" → extras["type"] (not filtered; passthrough per brief)
- properties.automatic: false → status = "reviewed"
- depth: 0.0 (surface event)

## Wire-shape notes

- description and url are BILINGUAL OBJECTS {fr: str, en: str}; canonical takes .en
- .fr halves route to extras["description_fr"] and extras["url_fr"] (flat string keys)
- automatic boolean → status derivation: true="automatic", false="reviewed"
- magType is camelCase (MLv, ML) — differs from EMSC lowercase magtype
- geometry.coordinates[2] is NEGATIVE (GeoJSON Z-up); always use properties.depth
- Detail page URLs still at renass.unistra.fr (website stayed on old host despite API migration)
- id is top-level Feature.id only; no properties.publicID or properties.unid equivalent
