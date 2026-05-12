# OWM Weather Maps 1.0 — Radar Tile Fixture

## tile_4_4_6.png

**Origin:** synthetic-from-spec — no OWM appid was available for live capture at
3b-15 fixture-capture time (2026-05-11). Fixture hand-crafted per the synthetic-from-real
pattern (precedent: 3b-4 Aeris paid-tier discussion, 3b-14 minimal PNG pattern).

**Synthetic origin:** minimal 1×1 transparent RGBA PNG (70 bytes) created via Python
`struct`/`zlib`/`png` encoding. Content-Type matches what the OWM API docs specify:
`image/png`.

**Target tile URL (upstream docs, not live-verified):**
`https://tile.openweathermap.org/map/precipitation_new/4/4/6.png?appid=<key>`

**Per upstream docs:** OWM Weather Maps 1.0 returns `image/png` tiles.
Layer: `precipitation_new` (NWP model precipitation, NOT radar reflectivity).
ADR-015 operator note: UI label should be "Model precipitation".

**Injected fields:** none — this is an entirely synthetic fixture since the real-tier
response is binary image bytes; the binary format (PNG) is verifiable from the Content-Type
alone. A future round with a real OWM appid can capture the live tile and swap this out.

**Live capture cross-check:** NOT performed — OWM appid unavailable at capture time.
Test-author surfaced this to lead via SendMessage per brief gate. Lead direction:
proceed with synthetic, sidecar marks clearly.

**Size:** 70 bytes (1×1 transparent PNG; real tiles are 256×256 ≈ 10–50 KB).

**Test use:** used as mock response content for httpx respx mock of the OWM tile URL
in unit and integration tests. Content-Type header set to `image/png` in all mocks.
