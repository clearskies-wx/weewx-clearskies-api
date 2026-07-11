"""WaveWatch III (NOAA GFS Wave) forecast provider module (PROVIDER-MANUAL §14.3).

Five responsibilities per PROVIDER-MANUAL §1:
  1. Outbound API call — NOAA ERDDAP griddap JSON access, single global
     wave-model grid, with model-cycle fallback.
  2. Response parsing — ERDDAP's generic `{"table": {"columnNames", "rows"}}`
     JSON table shape, wire-validated via a small Pydantic envelope.
  3. Canonical field translation — 9 ERDDAP wave variables mapped to
     `MarineForecastPoint` fields; NaN → None per ERDDAP's missing-value
     convention.
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — ERDDAP errors translated to the canonical taxonomy
     (PROVIDER-MANUAL §10); `ProviderHTTPClient` exceptions are allowed to
     propagate unwrapped except where this module explicitly catches a 404
     to drive model-cycle fallback (still re-raises any non-404 unchanged).

Resolved divergence — `wind_wave_direction` (recorded for history):
  T1.3's brief asked for CAPABILITY to include "wind_wave_direction" (ERDDAP
  `wvdir`) alongside wind_wave_height/period, but at the time this module was
  drafted, the canonical `MarineForecastPoint` model had no `windWaveDirection`
  field. Flagged to the coordinator before implementation; the coordinator
  added `windWaveDirection: float | None = None` to
  `weewx_clearskies_api/models/responses.py` (commit 351e516) so `wvdir` maps
  cleanly. This module requests `wvdir` from ERDDAP and maps it to
  `windWaveDirection` (Option C).

Resolved divergence — ERDDAP server unreachable, switched to PacIOOS
(recorded for history):
  The originally documented ERDDAP base (`erddap.aoml.noaa.gov`) and its
  7-grid regional/global dataset table were never live-verified and turned
  out to be completely unreachable (connection timeout). NOAA CoastWatch's
  ERDDAP (`coastwatch.pfeg.noaa.gov`) IS reachable and hosts a WaveWatch III
  dataset alias (`NWW3_Global_Best`, PacIOOS/Univ. Hawaii, GFS-forced), but
  it is a single GLOBAL grid — no regional US-coast/Alaska/Pacific subsets
  exist on this server. Flagged to the coordinator before implementation;
  the coordinator approved collapsing `_GRIDS` to one global entry and
  dropping the priority-based multi-grid selection.

  Further live-testing (2026-07-11) found that CoastWatch's `NWW3_Global_Best`
  is itself a mirror alias: griddap requests return HTTP 302 redirecting to
  the true origin, `pae-paha.pacioos.hawaii.edu/erddap/griddap/ww3_global`
  (PacIOOS's own ERDDAP). `ProviderHTTPClient` deliberately defaults
  `follow_redirects=False` (security-baseline: prevents token-leak via
  accidental 30x redirect) — following the CoastWatch alias would either
  require a per-module redirect opt-in or silently return an empty body
  (observed: `JSONDecodeError` on an empty 302 response). Querying the
  origin `pae-paha.pacioos.hawaii.edu` directly (dataset ID `ww3_global`,
  confirmed identical variables/dimensions/coverage via its own `/info/`
  endpoint) avoids the redirect entirely — no client security posture
  change needed. `ERDDAP_BASE` and `_GRIDS[0]["dataset"]` reflect the origin
  server, not the CoastWatch alias.

  This dataset also does not carry true 10m wind speed/direction as separate
  variables (the old `ws`/`wdir` from the undocumented aoml dataset). It
  offers exactly 9 wave-derived variables: `Thgt/Tper/Tdir` (primary wave),
  `whgt/wper/wdir` (wind-driven wave height/period/**direction** — note this
  server's `wdir` means wind-*wave* direction, not true wind direction),
  `shgt/sper/sdir` (swell). No suitable paired GFS wind forecast product
  exists on this ERDDAP server (only CCMP, a satellite NRT reanalysis product
  — wrong cadence/purpose). Coordinator approved dropping `windSpeed` and
  `windDirection` from `CAPABILITY.supplied_canonical_fields` and from
  `_VAR_TO_CANONICAL`; both fields remain declared as optional on
  `MarineForecastPoint` (unaffected — simply stay `None` for this provider).

  Additional technical differences from the old (unverified) contract:
    - Longitude convention is 0..360 on this server, not -180..180. Callers
      still pass -180..180; `_build_griddap_url()` converts internally.
    - The dataset has a `depth` dimension (`[time][depth][lat][lon]`), fixed
      at the single surface value `0.0`; every variable subset includes a
      literal `[(0.0)]` depth selector.
    - Native time resolution is hourly, not 3-hourly. A stride of 3 in the
      ERDDAP bracket syntax (`[(t0):3:(t1)]`) recovers 3-hour forecast steps.
    - Coverage bound is -77.5°S (dataset's actual `geospatial_lat_min`), not
      -78°S.

supplied_canonical_fields naming: listed in canonical camelCase
(`waveHeight`, `windWaveHeight`, ...) matching `MarineForecastPoint`'s actual
field names and the project convention set by providers/alerts/nws.py's
CAPABILITY declaration — not the brief's illustrative snake_case shorthand.

ruff: noqa: N815  (ERDDAP wire field names are camelCase: columnNames, columnTypes)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.errors import (
    GeographicallyUnsupported,
    ProviderProtocolError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "wavewatch"
DOMAIN = "marine"

ERDDAP_BASE = "https://pae-paha.pacioos.hawaii.edu/erddap/griddap"

# ERDDAP's `[time][depth][latitude][longitude]` dimension order requires a
# literal depth selector even though the dataset has only one depth value
# (0.0, surface). Fixed constant, not derived from lat/lon.
_DEPTH_SELECTOR = "[(0.0)]"

# Native time resolution on this dataset is hourly; a stride of 3 recovers
# the documented 3-hour forecast steps (PROVIDER-MANUAL §14.3).
_TIME_STRIDE = 3

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (WaveWatch III marine provider)"

_CACHE_TTL_SECONDS = 1800  # 30 min per PROVIDER-MANUAL §14.3

# 72h forecast at 3h steps → up to 25 timesteps per successful cycle fetch.
_FORECAST_HOURS = 72

# Fall back to up to 3 previous 6-hourly model cycles (4 attempts total)
# when the most recent cycle's data isn't published yet (PROVIDER-MANUAL §14.3).
_MAX_CYCLE_FALLBACKS = 3
_CYCLE_STEP_HOURS = 6

# Model-run data-availability lag before the "current" cycle is queried.
_CYCLE_AVAILABILITY_LAG_HOURS = 4.5

# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.3)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "waveHeight",
        "wavePeriod",
        "waveDirection",
        "windWaveHeight",
        "windWavePeriod",
        "windWaveDirection",
        "swellHeight",
        "swellPeriod",
        "swellDirection",
    ),
    geographic_coverage="global",  # excludes waters south of -77.5°S; see module docstring
    auth_required=(),
    default_poll_interval_seconds=_CACHE_TTL_SECONDS,
    operator_notes=(
        "NOAA WaveWatch III via ERDDAP. Global coverage (except south of "
        "-77.5°S). No API key required. Does not supply true 10m wind speed/"
        "direction — see the NDBC buoy provider for observed wind."
    ),
    refresh_interval=_CACHE_TTL_SECONDS,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="NOAA WaveWatch III",
        attribution_text="Data courtesy of NOAA Environmental Modeling Center",
        text_prefix="Data courtesy of",
        text_provider_name="NOAA Environmental Modeling Center",
        url="https://polar.ncep.noaa.gov/waves/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (PROVIDER-MANUAL §14.3 — "2 req/s, ERDDAP is a shared resource")
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="wavewatch-marine",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=2,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Grid selection table (PROVIDER-MANUAL §14.3)
# resolution_deg drives cache-key lat/lon rounding, not the ERDDAP query
# itself — ERDDAP snaps a single bracketed value to the nearest grid point
# server-side.
#
# Single global entry: CoastWatch's ERDDAP hosts only one WaveWatch III
# dataset (NWW3_Global_Best) — no regional US-coast/Alaska/Pacific subsets
# exist on this server, unlike the originally documented (unreachable) aoml
# ERDDAP's 7-grid table. See module docstring "Resolved divergence" entry.
# ---------------------------------------------------------------------------

_GRIDS: list[dict[str, Any]] = [
    {
        "dataset": "ww3_global",
        "name": "Global",
        "priority": 1,
        "lat_range": (-77.5, 77.5),
        "lon_range": (-180.0, 180.0),
        "resolution_deg": 0.5,
    },
]


def _select_grid(lat: float, lon: float) -> dict[str, Any] | None:
    """Return the highest-priority grid whose bounds contain (lat, lon), or None."""
    for grid in sorted(_GRIDS, key=lambda g: g["priority"]):
        lat_lo, lat_hi = grid["lat_range"]
        lon_lo, lon_hi = grid["lon_range"]
        if lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi:
            return grid
    return None


# ---------------------------------------------------------------------------
# ERDDAP variable → canonical field mapping (PROVIDER-MANUAL §14.3)
# ---------------------------------------------------------------------------

_VAR_TO_CANONICAL: dict[str, str] = {
    "Thgt": "waveHeight",
    "Tper": "wavePeriod",
    "Tdir": "waveDirection",
    "whgt": "windWaveHeight",
    "wper": "windWavePeriod",
    "wdir": "windWaveDirection",  # this server's `wdir` = wind-WAVE direction, not true wind
    "shgt": "swellHeight",
    "sper": "swellPeriod",
    "sdir": "swellDirection",
}

# ---------------------------------------------------------------------------
# Wire-shape Pydantic models (PROVIDER-MANUAL §11 fixture-first / security-
# baseline §3.5).  ERDDAP's griddap JSON envelope is generic — columnNames
# depend on the requested variable list, so the shape validated here is the
# table envelope only; per-column presence is checked in _parse_forecast().
# ---------------------------------------------------------------------------


class _ErddapTable(BaseModel):
    """ERDDAP griddap `.json` table body."""

    model_config = ConfigDict(extra="ignore")

    columnNames: list[str]
    columnTypes: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class _ErddapResponse(BaseModel):
    """ERDDAP griddap `.json` response envelope: `{"table": {...}}`."""

    model_config = ConfigDict(extra="ignore")

    table: _ErddapTable


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton — one per module, not per request)
# ---------------------------------------------------------------------------

_http_client = ProviderHTTPClient(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    user_agent=_USER_AGENT,
)

# ---------------------------------------------------------------------------
# Model-run cycle determination (PROVIDER-MANUAL §14.3)
# ---------------------------------------------------------------------------


def _compute_model_cycle(now: datetime) -> datetime:
    """Return the most recent WaveWatch III model-run cycle start time.

    Cycles run at 00/06/12/18 UTC. Data for a given cycle is not published
    immediately — apply a data-availability lag before flooring to the
    nearest cycle hour.
    """
    adjusted = now - timedelta(hours=_CYCLE_AVAILABILITY_LAG_HOURS)
    cycle_hour = (adjusted.hour // 6) * 6
    return adjusted.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Cache key construction (PROVIDER-MANUAL §14.3)
# ---------------------------------------------------------------------------


def _round_for_cache(lat: float, lon: float, resolution_deg: float) -> tuple[float, float]:
    """Round lat/lon to the selected grid's resolution for cache-key stability."""
    lat_rounded = round(lat / resolution_deg) * resolution_deg
    lon_rounded = round(lon / resolution_deg) * resolution_deg
    return round(lat_rounded, 4), round(lon_rounded, 4)


def _build_cache_key(dataset: str, lat: float, lon: float) -> str:
    """SHA-256 of (provider_id, grid, rounded lat/lon) per PROVIDER-MANUAL §14.3."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "grid": dataset, "lat": lat, "lon": lon},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ERDDAP griddap URL construction (PROVIDER-MANUAL §14.3)
# ---------------------------------------------------------------------------


def _to_erddap_longitude(lon: float) -> float:
    """Convert a -180..180 longitude to this server's 0..360 convention.

    CoastWatch's `NWW3_Global_Best` dataset uses 0..360 (`geospatial_lon_min
    = 0.0`, `geospatial_lon_max = 359.5`); callers of this module pass
    standard -180..180 (API-MANUAL convention). Live-verified 2026-07-11.
    """
    return lon if lon >= 0 else lon + 360.0


def _build_griddap_url(
    dataset: str,
    time_start: datetime,
    time_end: datetime,
    lat: float,
    lon: float,
) -> str:
    """Build the ERDDAP griddap JSON access URL for one model-cycle attempt.

    ERDDAP's bracketed dimension-subsetting syntax
    (`[(t0):stride:(t1)][(depth)][(lat)][(lon)]`) is passed as a literal
    query string rather than via ProviderHTTPClient's `params=` dict — httpx
    would otherwise percent-encode the brackets/parens/colons that ERDDAP
    requires literally, per the convention used by every ERDDAP client
    (erddapy, rerddapXtracto).

    Each requested variable gets its own dimension-subset suffix (ERDDAP
    griddap multi-variable syntax: `var1[dims],var2[dims],...` — a single
    subset shared across a bare comma-separated variable list is NOT valid
    and returns an ERDDAP 500 "wasn't found" error; live-verified 2026-07-11).

    `NWW3_Global_Best` has a `[time][depth][latitude][longitude]` dimension
    order — `_DEPTH_SELECTOR` supplies the fixed surface (0.0) depth value.
    """
    t0 = time_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = time_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    erddap_lon = _to_erddap_longitude(lon)
    subset = f"[({t0}):{_TIME_STRIDE}:({t1})]{_DEPTH_SELECTOR}[({lat})][({erddap_lon})]"
    variables = ",".join(f"{var}{subset}" for var in _VAR_TO_CANONICAL)
    return f"{ERDDAP_BASE}/{dataset}.json?{variables}"


# ---------------------------------------------------------------------------
# NaN handling (PROVIDER-MANUAL §14.3 — "NaN values → map to None")
# ---------------------------------------------------------------------------


def _nan_to_none(value: Any) -> float | None:
    """Map ERDDAP NaN/missing markers to None; pass numeric values through as float."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# Time normalization (ADR-020)
# ---------------------------------------------------------------------------


def _normalize_erddap_time(raw: str) -> str:
    """Convert an ERDDAP time-column value to UTC ISO-8601 with Z suffix."""
    try:
        cleaned = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError) as exc:
        raise ProviderProtocolError(
            f"WaveWatch time parse failed for {raw!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


# ---------------------------------------------------------------------------
# Wire → canonical normalization (PROVIDER-MANUAL §14.3 / API-MANUAL §16)
# ---------------------------------------------------------------------------


def _parse_forecast(raw: dict[str, Any]) -> list[MarineForecastPoint]:
    """Parse one ERDDAP griddap JSON response body into canonical forecast points.

    Raises:
        ProviderProtocolError: envelope validation fails, the "time" column
            or an expected variable column is missing, or a row's length
            doesn't match the column count.
    """
    try:
        wire = _ErddapResponse.model_validate(raw)
    except ValidationError as exc:
        raise ProviderProtocolError(
            f"WaveWatch ERDDAP response validation failed: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc

    column_names = wire.table.columnNames

    if "time" not in column_names:
        raise ProviderProtocolError(
            f"WaveWatch ERDDAP response missing 'time' column; got columns: {column_names}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    time_idx = column_names.index("time")

    var_indices: dict[str, int] = {}
    for wire_var in _VAR_TO_CANONICAL:
        if wire_var not in column_names:
            raise ProviderProtocolError(
                f"WaveWatch ERDDAP response missing expected variable column "
                f"{wire_var!r}; got columns: {column_names}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            )
        var_indices[wire_var] = column_names.index(wire_var)

    points: list[MarineForecastPoint] = []
    for row in wire.table.rows:
        if len(row) != len(column_names):
            raise ProviderProtocolError(
                f"WaveWatch ERDDAP row length {len(row)} does not match "
                f"column count {len(column_names)}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            )
        time_value = _normalize_erddap_time(str(row[time_idx]))
        field_values: dict[str, float | None] = {
            _VAR_TO_CANONICAL[wire_var]: _nan_to_none(row[idx])
            for wire_var, idx in var_indices.items()
        }
        points.append(MarineForecastPoint(time=time_value, **field_values))

    return points


# ---------------------------------------------------------------------------
# Public fetch entrypoint (PROVIDER-MANUAL §1, §14.3)
# ---------------------------------------------------------------------------


def fetch(
    *,
    lat: float,
    lon: float,
) -> dict[str, Any]:
    """Fetch WaveWatch III wave forecast for a location.

    Returns:
        dict with:
          - "forecast": list[MarineForecastPoint] — up to 25 timesteps,
            72h at 3h intervals (fewer if ERDDAP returns a sparser series).
            Empty list when no model cycle had data after all fallback
            attempts.
          - "grid": str — display name of the selected grid.
          - "model_run": str — ISO-8601 UTC of the model-run cycle actually
            used, or the most recently attempted cycle if none had data.

    Raises:
        GeographicallyUnsupported: (lat, lon) falls outside the global grid
            (currently: south of -77.5°S).
        QuotaExhausted: ERDDAP rate-limited the request.
        KeyInvalid: unexpected auth failure (ERDDAP is keyless; exotic).
        TransientNetworkError: network/DNS failure or 5xx after retries.
        ProviderProtocolError: non-404 4xx, or response validation failure.
    """
    grid = _select_grid(lat, lon)
    if grid is None:
        raise GeographicallyUnsupported(
            f"No WaveWatch III grid covers lat={lat}, lon={lon} "
            "(coverage is global except waters south of -77.5°S)",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    cache_lat, cache_lon = _round_for_cache(lat, lon, grid["resolution_deg"])
    cache_key = _build_cache_key(grid["dataset"], cache_lat, cache_lon)

    cached = get_cache().get(cache_key)
    if cached is not None:
        return {
            "forecast": [MarineForecastPoint.model_validate(p) for p in cached["forecast"]],
            "grid": cached["grid"],
            "model_run": cached["model_run"],
        }

    base_cycle = _compute_model_cycle(datetime.now(UTC))

    forecast: list[MarineForecastPoint] = []
    model_run_used: datetime | None = None

    for attempt in range(_MAX_CYCLE_FALLBACKS + 1):
        cycle_time = base_cycle - timedelta(hours=_CYCLE_STEP_HOURS * attempt)
        time_end = cycle_time + timedelta(hours=_FORECAST_HOURS)
        url = _build_griddap_url(grid["dataset"], cycle_time, time_end, lat, lon)

        _rate_limiter.acquire()
        try:
            response = _http_client.get(url)
        except ProviderProtocolError as exc:
            if exc.status_code == 404:
                logger.warning(
                    "WaveWatch cycle %s unavailable (404) for grid=%s; trying earlier cycle",
                    cycle_time.isoformat(),
                    grid["dataset"],
                )
                continue
            raise

        points = _parse_forecast(response.json())
        if points:
            forecast = points
            model_run_used = cycle_time
            break

        logger.warning(
            "WaveWatch cycle %s returned no data for grid=%s; trying earlier cycle",
            cycle_time.isoformat(),
            grid["dataset"],
        )

    if not forecast:
        logger.warning(
            "WaveWatch III: no forecast data available for lat=%s lon=%s grid=%s "
            "after %d model-cycle attempts",
            lat,
            lon,
            grid["dataset"],
            _MAX_CYCLE_FALLBACKS + 1,
        )
        model_run_iso = base_cycle.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        model_run_iso = model_run_used.strftime("%Y-%m-%dT%H:%M:%SZ")

    result_for_cache = {
        "forecast": [p.model_dump() for p in forecast],
        "grid": grid["name"],
        "model_run": model_run_iso,
    }
    get_cache().set(cache_key, result_for_cache, ttl_seconds=_CACHE_TTL_SECONDS)

    logger.info(
        "WaveWatch III forecast fetched: %d timestep(s) for grid=%s lat=%s lon=%s",
        len(forecast),
        grid["dataset"],
        lat,
        lon,
    )

    return {"forecast": forecast, "grid": grid["name"], "model_run": model_run_iso}
