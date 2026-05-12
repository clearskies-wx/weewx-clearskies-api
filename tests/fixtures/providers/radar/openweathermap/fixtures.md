# OWM Weather Maps 1.0 — Radar Tile Fixture

## tile_4_4_6.png

**Origin:** live capture — 2026-05-11T17:xx UTC (Windows DILBERT local run).
Captured via `urllib.request` with the project OWM appid from `reference/CREDENTIALS.md`.

**Live capture URL:**
`https://tile.openweathermap.org/map/precipitation_new/4/4/6.png?appid=<appid>`
(appid redacted in sidecar; used real key at capture time)

**Tile coordinates:** z=4, x=4, y=6 (Pacific/Northwest North America region at zoom 4).

**Live response:**
- HTTP status: 200
- Content-Type: `image/png`
- Size: 58805 bytes
- First 8 bytes: `89504e470d0a1a0a` (valid PNG signature)

**Cross-check against api-docs claims (per brief §live-capture coordination):**
- api-docs claim: `image/png` → **CONFIRMED** (exact match)
- No divergence from api-docs

**Test use:** used as mock response content for httpx respx mock of the OWM tile URL
in unit and integration tests. Content-Type header set to `image/png` in all mocks
(matches live response exactly).
