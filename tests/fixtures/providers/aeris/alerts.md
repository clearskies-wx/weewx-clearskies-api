# Aeris Alerts Provider Fixture — alerts.json

Sidecar documentation per 3b-1 fixture-capture discipline.

## alerts.json

- **Capture date:** 2026-05-09
- **Endpoint:** `GET /alerts/41.6022,-98.9178?client_id=[REDACTED]&client_secret=[REDACTED]`
- **Lat/Lon:** 41.6022 N, 98.9178 W (Ord, NE — Valley County, Nebraska)
  - Chosen because NWS had an active Fire Weather Watch for Valley County (NEZ039) at
    capture time.
- **Aeris account tier:** Free (1000 calls/day; registered to weather.shaneburkhardt.com)
- **Alert count:** 1 (Fire Weather Watch, NOAA/NWS Hastings NE office)
- **Capture note:** Real live capture 2026-05-09. No trimming needed (single alert record).
- **Redaction:** `client_id` and `client_secret` were query parameters in the request URL —
  they do NOT appear in the response body. No redaction needed in the fixture file itself.
  Tests use placeholder values (`INTEGRATION_TEST_CLIENT_ID` / `INTEGRATION_TEST_CLIENT_SECRET`).

---

## Wire-shape findings (critical for brief call 16 PARTIAL-DOMAIN decision + severity map)

### Canonical-mapped fields — which appeared in the real response

| Canonical field | Wire path | Present in fixture? | Value in fixture |
|---|---|---|---|
| `id` | `id` (top-level) | YES | `"6a000faac27d070dad868226"` |
| `headline` | `details.name` | YES | `"FIRE WEATHER WATCH"` |
| `description` | `details.body` | YES | Long text body (see fixture) |
| `severity` | `details.priority` (mapped) | YES (but see priority note) | `96` (see note below) |
| `urgency` | `details.urgency` | **ABSENT** — not in wire | `null` (field absent from `details`) |
| `certainty` | `details.certainty` | **ABSENT** — not in wire | `null` (field absent from `details`) |
| `event` | `details.type` | YES | `"FW.A"` (short code) |
| `effective` | `timestamps.issuedISO` | YES | `"2026-05-09T23:54:00-05:00"` |
| `expires` | `timestamps.expiresISO` | YES | `"2026-05-11T22:00:00-05:00"` |
| `senderName` | `details.emergency` or `place.name` | PARTIAL (see emergency note) | `details.emergency = false` (boolean); `place.name = "valley"` |
| `areaDesc` | `place.name` | YES | `"valley"` |
| `category` | `details.category` | **ABSENT** | `null` (field not present in `details`); Aeris uses `details.cat` = `"fire"` instead |

---

## Critical wire-shape divergences from brief (surfaced to lead at fixture-capture time)

### 1. details.priority = 96, not 1-5

The brief's severity map (brief call 12, canonical-data-model §4.3) maps priority
integers 1-5 to canonical enum. The real Aeris wire uses a different scale:
- `priority=96` for Fire Weather Watch (this fixture)
- `priority=60` for Wind Advisory (from api-docs §Alerts example)

The implementation's `_AERIS_SEVERITY_MAP = {1: "warning", 2: "watch", 3: "advisory",
4: "advisory", 5: "advisory"}` will NOT match any real Aeris priority value and will
always fall through to the "unknown → advisory + WARNING log" path.

**Test impact:** Tests must assert that the real fixture produces `severity="advisory"` with
a WARNING log (unknown priority fallback), NOT "warning" or "watch". The severity map needs
an update from api-dev to cover real Aeris priority ranges.

**Lead direction needed:** Should the severity map be corrected to match real Aeris priority
values? Known data points: priority=60 → Wind Advisory, priority=96 → Fire Weather Watch.
Aeris priority scale is 1-100+ (higher number = higher priority per api-docs).

### 2. details.emergency = false (boolean), not string

The brief's `_AerisAlertDetails` model declares `emergency: str | None = None`. The real
wire returns `emergency: false` (JSON boolean). With `extras="ignore"` and Pydantic's
`str | None` type, a boolean `False` will be coerced or may fail validation.

**Test impact:** The model validation against the real fixture will coerce `false` to the
string `"False"` or fail. Tests must verify actual behavior.

The senderName disjunction in `_to_canonical` checks
`if record.details.emergency and record.details.emergency.strip()` — with `emergency=False`
(coerced to string `"False"`), this would incorrectly set senderName to `"False"`. If
Pydantic coerces `False` → `None`, the place.name fallback works correctly.

### 3. details.category field is named 'cat' in real wire, not 'category'

The real wire has `details.cat = "fire"`, not `details.category`. The canonical data model
§4.3 says `category = details.category`. The real wire does not have a `details.category`
field at all. With `extras="ignore"`, `details.cat` is silently dropped and
`details.category` is always `None`.

**PARTIAL-DOMAIN finding:** `urgency`, `certainty`, and `category` are all absent from real
free-tier Aeris alerts wire shape. `details.cat` contains a category-like value but under
a different field name.

---

## Error-shape fixtures (hand-crafted)

See `alerts_error_401.json`, `alerts_error_429.json`, `alerts_warn_invalid_location.json`.
These are hand-crafted following the same patterns as the existing Aeris forecast error
fixtures (same provider, same envelope shape).
