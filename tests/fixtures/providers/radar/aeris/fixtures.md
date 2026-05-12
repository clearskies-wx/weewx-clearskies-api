# Aeris Xweather Raster Maps — Radar Tile Fixture

## tile_4_4_6.png

**Origin:** live capture — 2026-05-11T17:xx UTC (Windows DILBERT local run).
Captured via `urllib.request` with the project Aeris credentials from `reference/CREDENTIALS.md`
(PWSWeather Contributor Plan path — confirmed accessible at capture time).

**Live capture URL:**
`https://maps.api.xweather.com/<redacted>/radar/4/4/6/current.png`
(client_id + client_secret redacted in sidecar; used real credentials at capture time)

**Tile coordinates:** z=4, x=4, y=6 (Pacific/Northwest North America region at zoom 4).
Layer: `radar` (global radar mosaic, ADR-015). Offset: `current` (hardcoded at v0.1).

**Live response:**
- HTTP status: 200
- Content-Type: `image/png;x-cost-v1=tile|1|1|1`
- Size: 3682 bytes
- First 8 bytes: `89504e470d0a1a0a` (valid PNG signature)

**Cross-check against api-docs claims (per brief §live-capture coordination):**
- api-docs claim: `image/png` → **MILD DIVERGENCE**: live response is
  `image/png;x-cost-v1=tile|1|1|1` (vendor cost-tracking parameter appended).
  The base media type is still `image/png`; the parameter is for Aeris's internal
  billing/cost tracking, not a content format change.

**Divergence impact:**
The impl uses `response.headers.get("Content-Type", "image/png")` which propagates
the full header value including the cost parameter. The endpoint then passes it to
`fastapi.Response(content=bytes, media_type=ct)`. FastAPI will send the full
`image/png;x-cost-v1=tile|1|1|1` as the response Content-Type, which is still
a valid `image/png` media type from the browser's perspective.

The test mocks return pure `image/png` which is fine for testing (the vendor
parameter doesn't affect the format, just Aeris's billing). A future round could
add a test that verifies the Content-Type parameter is preserved end-to-end if
the team decides that's important.

**PWSWeather Contributor Plan access:** confirmed accessible at capture time
(2026-05-11). No access restriction detected.

**Test use:** used as mock response content for httpx respx mock of the Aeris tile URL.
