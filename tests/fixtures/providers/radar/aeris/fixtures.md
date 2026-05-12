# Aeris Xweather Raster Maps — Radar Tile Fixture

## tile_4_4_6.png

**Origin:** synthetic-from-spec — no Aeris client_id/client_secret was available for
live capture at 3b-15 fixture-capture time (2026-05-11). Fixture hand-crafted per the
synthetic-from-real pattern (precedent: 3b-4 Aeris paid-tier discussion, 3b-14 minimal
PNG pattern).

**Synthetic origin:** minimal 1×1 transparent RGBA PNG (70 bytes) created via Python
`struct`/`zlib`/`png` encoding. Content-Type matches what the Aeris api-docs specify:
`image/png`.

**Target tile URL (upstream docs, not live-verified):**
`https://maps.api.xweather.com/{client_id}_{client_secret}/radar/4/4/6/current.png`

**Per upstream docs:** Aeris Xweather Raster Maps returns `image/png` tiles.
Layer: `radar` (global radar mosaic). Offset `current` hardcoded at v0.1 per brief §LC-7.
ADR-015 note: PWSWeather Contributor Plan purportedly bundles Maps API access — confirm
at live-capture time when credentials become available.

**Injected fields:** none — entirely synthetic binary PNG fixture. A future round with
real Aeris credentials (PWSWeather Contributor Plan) can capture the live tile and swap
this out.

**Live capture cross-check:** NOT performed — Aeris credentials unavailable at capture time.
Test-author surfaced this to lead via SendMessage per brief gate. Lead direction:
proceed with synthetic, sidecar marks clearly.

**URL credential redaction (security):** Aeris embeds client_id_client_secret in the URL
PATH. The impl's `_redact_url()` helper replaces this segment with `<redacted>` before
any logging (LC-E). Tests verify the redaction helper directly.

**Size:** 70 bytes (1×1 transparent PNG; real tiles are 256×256 ≈ 10–50 KB).

**Test use:** used as mock response content for httpx respx mock of the Aeris tile URL
in unit and integration tests. Content-Type header set to `image/png` in all mocks.
