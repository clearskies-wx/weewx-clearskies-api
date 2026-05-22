# Development Guide — weewx-clearskies-api

## Prerequisites

- Python 3.12 or later
- [`uv`](https://github.com/astral-sh/uv) (recommended) or pip

Install runtime and dev dependencies:

```bash
# With uv (recommended)
uv sync --extra dev

# With pip
pip install -e ".[dev]"
```

Run the full test suite (no live network, no DB required for unit tests):

```bash
pytest tests/ -m "not integration and not live_network"
```

Run integration tests (requires a running MariaDB or seeded SQLite):

```bash
pytest tests/ -m "integration"
```

Run with a specific database backend (MariaDB):

```bash
CLEARSKIES_DB_URL="mysql+pymysql://weewx_ro:password@127.0.0.1:3306/weewx" \
  pytest tests/ -m "integration"
```

---

## Project layout

```
weewx_clearskies_api/
├── providers/
│   ├── _common/          # Shared infrastructure: HTTP client, errors, capability registry,
│   │                     #   rate limiter, cache plumbing
│   ├── alerts/           # Severe-weather alert providers (ADR-016)
│   ├── aqi/              # Air-quality providers (ADR-013)
│   ├── earthquakes/      # Earthquake providers (ADR-040)
│   ├── forecast/         # Forecast providers (ADR-007)
│   └── radar/            # Radar tile/frame providers (ADR-015)
├── endpoints/            # FastAPI route handlers — consume provider fetch()
├── models/               # Pydantic canonical response models
├── services/             # Station, units, almanac helpers
└── ...
tests/
├── fixtures/
│   └── providers/
│       ├── openmeteo/    # Recorded JSON responses for Open-Meteo
│       ├── nws/          # Recorded JSON responses for NWS
│       └── ...           # One subdirectory per provider
├── providers/            # Nested unit test modules (domain/test_<provider>.py)
│   └── aqi/
│       ├── test_openmeteo.py
│       ├── test_aeris.py
│       └── ...
└── test_providers_<domain>_<provider>_integration.py  # Integration tests
```

---

## How to add a new provider module

The steps below use a hypothetical `pirateweather` forecast provider as the
running example. Substitute your domain and provider name throughout.

### Step 1: Choose a domain

Pick the domain that matches the data type the provider supplies:

| Domain | Canonical return type | Example providers |
|---|---|---|
| `forecast` | `ForecastBundle` | `openmeteo`, `nws`, `aeris` |
| `aqi` | `AQIReading` | `openmeteo`, `aeris`, `iqair` |
| `alerts` | `list[AlertRecord]` | `nws`, `aeris`, `openweathermap` |
| `earthquakes` | `list[EarthquakeRecord]` | `usgs`, `geonet`, `emsc` |
| `radar` | tile URL / WMS endpoint (no `fetch()`) | `rainviewer`, `iem_nexrad` |

A provider that supplies data across more than one domain gets one module per
domain — for example, Aeris has `forecast/aeris.py`, `aqi/aeris.py`, and
`alerts/aeris.py` as three independent modules.

### Step 2: Create the module file

Place the module at:

```
weewx_clearskies_api/providers/<domain>/<provider_id>.py
```

For example: `weewx_clearskies_api/providers/forecast/pirateweather.py`

Start with this skeleton (copy `openmeteo.py` as the closest reference):

```python
"""Pirate Weather forecast provider module (ADR-007, ADR-038).

Five responsibilities per ADR-038 §2:
  1. Outbound API call — Pirate Weather /forecast/<key>/<lat>,<lon>
  2. Response parsing — wire-shape Pydantic models for _PirateWeatherResponse
  3. Translation to canonical ForecastBundle (HourlyForecastPoint + DailyForecastPoint)
  4. Capability declaration — CAPABILITY symbol consumed at startup
  5. Error handling — provider errors translated to canonical taxonomy
"""

from __future__ import annotations

from weewx_clearskies_api.providers._common.capability import ProviderCapability
from weewx_clearskies_api.providers._common.errors import (
    GeographicallyUnsupported,
    KeyInvalid,
    ProviderProtocolError,
    QuotaExhausted,
    TransientNetworkError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

PROVIDER_ID = "pirateweather"
DOMAIN = "forecast"
```

### Step 3: Implement the 5 responsibilities

Every provider module is responsible for exactly these five things and nothing
else. Caching, logging format, and persistence are handled by other layers.

#### 1. Outbound call — use `ProviderHTTPClient`

Instantiate one `ProviderHTTPClient` at module level (not per request) and
call its `.get()` method:

```python
_http_client: ProviderHTTPClient | None = None

def _client_for() -> ProviderHTTPClient:
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent="(weewx-clearskies-api/0.1.0)",
        )
    return _http_client
```

Call it inside `fetch()`:

```python
response = _client_for().get(
    f"https://api.pirateweather.net/forecast/{api_key}/{lat},{lon}",
    log_url=f"https://api.pirateweather.net/forecast/[REDACTED]/{lat},{lon}",
)
```

Use the `log_url` parameter whenever the API key appears in the URL path (as in
`openmeteo.py`'s Aeris pattern). This prevents key leakage in structured log
output. For query-param keys (the more common case), `ProviderHTTPClient.get()`
accepts a `params=` dict and the URL stays clean.

`ProviderHTTPClient` handles retries with jittered exponential backoff (up to 3
total attempts by default) and translates `429` → `QuotaExhausted`, `401`/`403`
→ `KeyInvalid`, and network errors → `TransientNetworkError` automatically.

The rate limiter is per-module, not shared:

```python
_rate_limiter = RateLimiter(
    name="pirateweather-forecast",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=5,
    window_seconds=1,
)
```

Call `_rate_limiter.acquire()` before `_client_for().get(...)` inside `fetch()`.

#### 2. Response parsing — wire-shape Pydantic models

Define private Pydantic models that match the provider's exact wire shape.
Use `extra="ignore"` so future provider API additions don't break the module.
Missing required fields raise `ValidationError`, which you translate to
`ProviderProtocolError` (see responsibility 5):

```python
from pydantic import BaseModel, ConfigDict

class _PirateWeatherHourly(BaseModel):
    model_config = ConfigDict(extra="ignore")
    time: int           # Unix timestamp
    temperature: float | None = None
    humidity: float | None = None
    windSpeed: float | None = None   # noqa: N815

class _PirateWeatherResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    latitude: float
    longitude: float
    timezone: str
    hourly: _PirateWeatherHourly | None = None
```

Validate the raw response JSON through the wire model before touching any
fields:

```python
try:
    wire = _PirateWeatherResponse.model_validate(response.json())
except (ValidationError, ValueError) as exc:
    raise ProviderProtocolError(
        f"Pirate Weather response validation failed: {exc}",
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
    ) from exc
```

Record a real API response as a fixture file under
`tests/fixtures/providers/pirateweather/forecast.json` before writing the
parsing tests. See Step 5 for details.

#### 3. Canonical translation — map provider fields to canonical model fields

Write a `_to_canonical()` function that converts the wire model into the
canonical Pydantic response type. The canonical types are in
`weewx_clearskies_api/models/responses.py` (`ForecastBundle`,
`HourlyForecastPoint`, `DailyForecastPoint`, `AQIReading`, `AlertRecord`,
`EarthquakeRecord`).

Key conventions (all from `openmeteo.py`):

- **Timestamps**: all times must be ISO 8601 UTC with a `Z` suffix
  (`"2026-05-22T14:00:00Z"`). If the provider gives Unix timestamps, convert
  with `datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`.
  If it gives local ISO strings, apply the station's UTC offset to get UTC
  (see `_local_iso_to_utc_iso8601()` in `openmeteo.py`).
- **Units**: pass the operator's `target_unit` (`"US"` | `"METRIC"` |
  `"METRICWX"`) to the provider's unit query params wherever possible. If the
  provider has no unit param, convert after ingestion.
- **Identifiers**: normalize provider-specific field names to canonical names.
  For AQI, `PM2.5`/`pm25`/`pm2_5` all become the canonical `"PM2.5"`.
- **`source` field**: set to `PROVIDER_ID` on every canonical record emitted.

```python
def _to_canonical(wire: _PirateWeatherResponse) -> ForecastBundle:
    hourly_points = [
        HourlyForecastPoint(
            validTime=datetime.fromtimestamp(h.time, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            outTemp=h.temperature,
            outHumidity=h.humidity,
            windSpeed=h.windSpeed,
            source=PROVIDER_ID,
        )
        for h in (wire.hourly or [])
    ]
    return ForecastBundle(
        hourly=hourly_points,
        daily=[],
        discussion=None,
        source=PROVIDER_ID,
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
```

#### 4. Capability declaration — the `CAPABILITY` constant

Export a module-level `CAPABILITY` constant of type `ProviderCapability`. All 7
required fields must be set:

```python
CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,                   # stable string, matches the dispatch key
    domain=DOMAIN,                             # one of the five domain strings
    supplied_canonical_fields=(                # tuple of canonical field names
        "validTime", "outTemp", "outHumidity", "windSpeed",
        "validDate", "tempMax", "tempMin",
    ),
    geographic_coverage="global",              # "global" or region description
    auth_required=("PIRATEWEATHER_API_KEY",),  # () for keyless providers
    default_poll_interval_seconds=1800,        # recommended fetch cadence
    operator_notes=(
        "Pirate Weather free tier; 10 000 calls/month. "
        "Requires PIRATEWEATHER_API_KEY in api.conf."
    ),
)
```

`supplied_canonical_fields` is the source of truth for what this module can
supply. Only list fields the module actually populates — omit fields it cannot
supply (e.g., `openmeteo.py` omits `discussion` because Open-Meteo has no
discussion endpoint). The configuration UI surfaces `operator_notes` to help
operators make informed provider choices.

#### 5. Error handling — use the canonical error classes

All exceptions that cross the provider module boundary must be from
`weewx_clearskies_api.providers._common.errors`. Never let upstream exception
types (`httpx.HTTPError`, `pydantic.ValidationError`, `json.JSONDecodeError`,
etc.) propagate out of the module.

`ProviderHTTPClient.get()` already translates the most common HTTP error codes
(429, 401, 403, 5xx). Your module only needs to handle cases the HTTP client
cannot see — for example, provider-specific error envelopes inside a 200
response, or location-rejection signalled via a 404:

```python
# NWS pattern: 404 on /points for non-US lat/lon → GeographicallyUnsupported
except ProviderProtocolError as exc:
    if exc.status_code == 404:
        raise GeographicallyUnsupported(
            f"Pirate Weather does not cover lat={lat}, lon={lon}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc
    raise
```

The full error taxonomy and when to use each class is in the
[Error taxonomy](#error-taxonomy) section below.

### Step 4: Register the provider

`providers/_common/dispatch.py` is the only place that maps
`(domain, provider_id)` to a module. Add one import and one dict entry:

```python
# In dispatch.py — add import:
from weewx_clearskies_api.providers.forecast import pirateweather as forecast_pirateweather

# Add dict entry in PROVIDER_MODULES:
("forecast", "pirateweather"): forecast_pirateweather,
```

That is the entire registration step. No entry-point magic, no plugin
discovery. The module set is the bundled set; outside contributors open a PR
(ADR-038 §Internal contract).

If the new provider requires operator-supplied credentials (API keys), also add
a settings field in `weewx_clearskies_api/config/settings.py` under the
relevant settings class (e.g., `ForecastSettings.pirateweather_api_key`), and
wire the credential through to the module's `fetch()` signature inside the
domain endpoint handler (`endpoints/forecast.py`).

### Step 5: Write tests

All provider tests follow the same three-layer pattern. Use
`tests/providers/aqi/test_openmeteo.py` or
`tests/providers/aqi/test_aeris.py` as style references.

#### Capture a fixture

Record a real API response and save it as a JSON file:

```
tests/fixtures/providers/pirateweather/forecast.json
```

Capture with `curl` or a one-off Python script. The fixture must be a real
response, not synthetic JSON invented from the documentation. If the provider
requires auth and you have credentials, capture with those credentials. If you
cannot capture a real response, the fixture must be documented as synthetic
and the gap recorded in the test module docstring.

#### Parser unit tests (no network, no DB)

Place unit tests in:

```
tests/providers/forecast/test_pirateweather.py
```

Test the wire-shape models and `_to_canonical()` directly against the fixture:

```python
import json
from pathlib import Path

FIXTURE = json.loads(
    (Path(__file__).parent.parent.parent / "fixtures/providers/pirateweather/forecast.json")
    .read_text()
)

def test_wire_validation_happy_path():
    wire = _PirateWeatherResponse.model_validate(FIXTURE)
    assert wire.latitude == pytest.approx(47.6062)

def test_canonical_translation_timestamp_utc():
    wire = _PirateWeatherResponse.model_validate(FIXTURE)
    bundle = _to_canonical(wire)
    assert bundle.hourly[0].validTime.endswith("Z")

def test_capability_fields():
    assert CAPABILITY.provider_id == "pirateweather"
    assert CAPABILITY.domain == "forecast"
    assert "validTime" in CAPABILITY.supplied_canonical_fields
```

Cover the error paths the HTTP client does not handle: ValidationError →
ProviderProtocolError, provider-specific error envelopes, and
GeographicallyUnsupported if applicable.

#### Mock-network integration tests

Use `respx` to mock outbound HTTP calls and test the full `fetch()` path:

```python
import respx
import httpx

@respx.mock
def test_fetch_happy_path(tmp_path):
    respx.get("https://api.pirateweather.net/forecast/...").mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    bundle = fetch(lat=47.6062, lon=-122.3321, target_unit="US", api_key="test-key")
    assert len(bundle.hourly) > 0

@respx.mock
def test_fetch_429_raises_quota_exhausted():
    respx.get(...).mock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
    with pytest.raises(QuotaExhausted) as exc_info:
        fetch(lat=47.6062, lon=-122.3321, target_unit="US", api_key="test-key")
    assert exc_info.value.retry_after_seconds == 60

@respx.mock
def test_fetch_401_raises_key_invalid():
    respx.get(...).mock(return_value=httpx.Response(401))
    with pytest.raises(KeyInvalid):
        fetch(lat=47.6062, lon=-122.3321, target_unit="US", api_key="bad-key")
```

Place mock-network tests in a flat test file if they span the full HTTP stack:

```
tests/test_providers_forecast_pirateweather_unit.py
```

#### No live-network calls in CI

Mark any test that requires a real API call with `@pytest.mark.live_network`.
These tests are excluded from CI by default:

```bash
# CI runs:
pytest -m "not live_network"

# Developer local run with live network:
pytest -m "live_network"
```

No test that hits a live external API may run without this mark.

---

## Error taxonomy

All provider modules raise from this hierarchy. The HTTP handler in
`weewx_clearskies_api/errors.py` maps each class to the appropriate HTTP
status code automatically.

| Exception class | When to raise | HTTP status |
|---|---|---|
| `QuotaExhausted` | Rate-limit or daily cap exceeded; transient — retry after backoff. Set `retry_after_seconds` when the provider supplies a value. | 503 + `Retry-After` |
| `KeyInvalid` | Auth failure (401/403); permanent until the operator updates their API key in `api.conf`. | 502 |
| `GeographicallyUnsupported` | The provider explicitly rejects the operator's location (e.g., NWS `/points` 404 for non-US coordinates). | 503 |
| `FieldUnsupported` | The provider does not supply the requested data type (e.g., a free-tier endpoint that omits a field). | 502 |
| `TransientNetworkError` | DNS failure, TCP refused, TLS error, or 5xx after all retries are exhausted. Raised automatically by `ProviderHTTPClient` — only raise manually when you detect a network-level failure outside the HTTP client. | 502 |
| `ProviderProtocolError` | Unexpected response shape — the provider changed its API format. Logged at ERROR for operator triage. Raise on Pydantic `ValidationError` or when a 200 response carries an error envelope. | 502 |

All six classes inherit from `ProviderError` and carry `provider_id`, `domain`,
`retry_after_seconds`, and `status_code` attributes. Never let upstream
exception types cross the module boundary.

---

## Running tests

```bash
# Unit tests only (no DB, no network)
pytest tests/ -m "not integration and not live_network"

# Integration tests against MariaDB (requires running stack)
CLEARSKIES_DB_URL="mysql+pymysql://weewx_ro:password@127.0.0.1:3306/weewx" \
  pytest tests/ -m "integration and not live_network"

# Integration tests against SQLite (seeded fixture DB)
CLEARSKIES_DB_URL="sqlite:///tests/fixtures/weewx.sdb" \
  pytest tests/ -m "integration and not live_network"

# Redis cache integration tests (requires Redis)
pytest tests/ -m "integration and redis"

# Single provider domain in isolation
pytest tests/providers/aqi/ tests/test_providers_aqi_*.py -m "not live_network"

# Live-network tests (developer local only, requires real API credentials)
pytest tests/ -m "live_network"
```

---

## Code style

- **Formatter**: `ruff format` (line length 100, double quotes).
- **Linter**: `ruff check` with rule sets E, F, W, I, B, S, N, UP, SIM.
- **Type checking**: `mypy --strict`. All public functions require type
  annotations. Provider modules use `from __future__ import annotations` for
  forward-reference support.
- **Field naming**: canonical camelCase field names (e.g., `validTime`,
  `outTemp`) in Pydantic models and wire shapes. Suppress `ruff N815` in
  provider modules with `# ruff: noqa: N815` at the file top.
- **No bare `except Exception`**: catch specific exception types per
  `rules/coding.md §3`. The `ProviderHTTPClient` pattern is the reference.
- Run both tools before opening a PR:

```bash
ruff format weewx_clearskies_api/ tests/
ruff check weewx_clearskies_api/ tests/
mypy weewx_clearskies_api/
```
