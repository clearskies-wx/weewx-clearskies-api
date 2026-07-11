"""NDBC buoy observations provider module (ADR-038, ADR-083, ADR-087).

Five responsibilities per ADR-038 §2:
  1. Outbound call — flat-file HTTP GETs against NDBC's realtime2 host
     (NOT a REST/JSON API; see "Wire format" below).
  2. Response parsing — whitespace-tokenized fixed-column text for the
     standard met (.txt) file; ``VALUE (FREQ)`` token-pair text for the
     spectral density (.data_spec) and spectral direction (.swdir) files.
  3. Translation to canonical MarineObservation / SpectralWaveComponent
     (models/responses.py, API-MANUAL §16).
  4. Capability declaration — CAPABILITY symbol consumed at startup.
  5. Error handling — provider errors translated to canonical taxonomy.

Wire format — REALITY, NOT THE ORIGINAL BRIEF (T1.1 lead call, 2026-07-09):
  The original brief/PROVIDER-MANUAL §14.1 draft described the spectral
  density file as ``.swden`` with a header row of frequency values that
  data rows are index-aligned against.  Live verification against
  www.ndbc.noaa.gov during T1.1 showed both of those are wrong:
    - ``.swden`` does not exist on NDBC's server (404 on every station
      tried).  The real raw spectral energy-density extension is
      ``.data_spec``.
    - Neither ``.data_spec`` nor ``.swdir`` index data against a
      frequency-value header row.  Every data row instead embeds each
      band as an inline ``VALUE (FREQ)`` token pair, e.g.
      ``0.320 (0.058) 1.362 (0.063) ...``, preceded by a ``Sep_Freq``
      column (the wind-sea/swell separation frequency; NDBC fills
      9.999 when absent) that this module does not use since swell
      partitioning here is done via our own peak-finding decomposition,
      not NDBC's separation frequency.
  The lead is updating PROVIDER-MANUAL §14.1 to match this module in a
  follow-up commit; this module was built against the verified-live API.

Standard met (.txt) parsing:
  19 whitespace-separated columns: 5 timestamp columns (YY MM DD hh mm,
  always UTC, 4-digit year) + 14 data columns (WDIR WSPD GST WVHT DPD APD
  MWD PRES ATMP WTMP DEWP VIS PTDY TIDE).  First two lines are headers
  (column names, then units) — skipped via the leading ``#``.  Data rows
  are most-recent-first; row 0 (first data line) is used.  ``MM`` marks a
  missing field → ``None`` (not an error).

  Column units match this module's canonical marine base units for every
  group except TIDE: WDIR/MWD are degrees true, WSPD/GST are m/s
  (group_ocean_speed base unit per API-MANUAL §16), WVHT is meters
  (group_wave_height base), DPD/APD are seconds (group_wave_period base,
  single-unit group — no conversion), PRES/PTDY are hPa (group_pressure;
  hPa/mbar are an identity conversion), ATMP/WTMP/DEWP are °C, VIS is
  nautical miles (group_visibility base).  TIDE is feet — the only field
  needing conversion, via ``units.conversion.convert("foot", "meter")``
  to match group_water_level's meter base unit.

Spectral decomposition (no scipy — 46 bands, plain ``math`` module):
  1. Parse energy density VALUE(FREQ) pairs from ``.data_spec``.
  2. Parse mean wave direction (ALPHA1) VALUE(FREQ) pairs from ``.swdir``.
  3. Local-maxima peak detection: energy(f) > energy(f-1) and
     energy(f) > energy(f+1) (interior points only).
  4. Discard peaks below 5% of the dominant peak's energy.
  5. Partition frequencies: boundary between adjacent peaks = the index of
     minimum energy strictly between them; each boundary band is assigned
     to the lower-frequency partition.
  6. Per partition: Hs = 4*sqrt(m0) via trapezoidal integration of energy
     over the partition; Tp = 1/fp (frequency of max energy in the
     partition); direction = energy-weighted circular mean over bands with
     known direction (NDBC direction fill value 999(.0) treated as
     missing, same as ``MM`` in the standard met file); classification
     from Tp (>=12s groundswell, 8-12s swell, <8s wind_swell).
  7. Cap at 4 systems — repeatedly merge the weakest partition (by summed
     energy) into its higher-energy neighbor until <= 4 remain.

Station discovery capability guess (T1.1 lead call 2, 2026-07-09):
  ``activestations.xml`` (verified live) carries NO per-station wave or
  spectral-sensor flag — only ``type`` (buoy/fixed/usv/...) and ``met``
  (has atmospheric sensors).  Full/wave/atmospheric-only differentiation
  is therefore a best-effort heuristic on those two attributes, not ground
  truth.  ``fetch()`` does not trust the guess: it always attempts the
  spectral files when ``include_spectral=True`` and degrades gracefully
  (empty spectral list) on 404 rather than gating on the discovery-time
  guess.

Cache layer (ADR-017, PROVIDER-MANUAL §14.1):
  Caches per file type, post-parse (parsed values, not raw text).
  Key: SHA-256 of (provider_id, station_id, file_type) for observations;
  (provider_id, file_type="activestations") for station discovery.
  TTL: 3600s (60 min) for txt/data_spec/swdir; 86400s (24 hr) for station
  discovery.

Wire-shape Pydantic (security-baseline §3.5):
  _NdbcStandardMetRow validates the 14 parsed data columns (after MM →
  None normalization) before they're mapped to MarineObservation.

ruff: noqa: N815  (field names match canonical camelCase: windSpeed, etc.)
"""

# ruff: noqa: N815

from __future__ import annotations

import hashlib
import json
import logging
import math
import re

import defusedxml.ElementTree as ET  # type: ignore[import-untyped]  # noqa: N817
from pydantic import BaseModel, ConfigDict

from weewx_clearskies_api.models.responses import MarineObservation, SpectralWaveComponent
from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.providers._common.errors import ProviderProtocolError
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter
from weewx_clearskies_api.units.conversion import convert

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "ndbc"
DOMAIN = "buoy"

_NDBC_BASE_URL = "https://www.ndbc.noaa.gov"
_STANDARD_MET_PATH = "/data/realtime2/{station_id}.txt"
# Real extension is `.data_spec`, NOT `.swden` — see module docstring.
_SPECTRAL_DENSITY_PATH = "/data/realtime2/{station_id}.data_spec"
_SPECTRAL_DIRECTION_PATH = "/data/realtime2/{station_id}.swdir"
_ACTIVE_STATIONS_URL = f"{_NDBC_BASE_URL}/activestations.xml"

_OBSERVATION_CACHE_TTL = 3600  # 60 min — all three per-station file types (PROVIDER-MANUAL §14.1)
_STATION_DISCOVERY_CACHE_TTL = 86400  # 24 hr

_API_VERSION = "0.1.0"
_USER_AGENT = f"weewx-clearskies-api/{_API_VERSION} (NDBC buoy provider)"

# NDBC direction fill sentinel — ALPHA1/ALPHA2 valid range is 0-360; NDBC
# uses >= 999 to flag missing direction for a given band. Applied uniformly
# to both spectral files (energy density near 999 m^2/Hz would itself be
# physically absurd, so treating it as a fill value there too is safe).
_NDBC_FILL_SENTINEL = 999.0

# Real NDBC station identifiers are short alphanumeric codes (e.g. "46042",
# "41001", "CDRF1"). Validated before being embedded in a URL path segment.
_STATION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

# Sentinel cached in place of a bare `None` so a deliberately-empty cached
# observation isn't indistinguishable from a cache miss (MemoryCache.get()
# returns None for both "no entry" and "value literally None").
_EMPTY_MARKER = "__ndbc_empty__"

# ---------------------------------------------------------------------------
# Capability declaration (ADR-038 §4)
# ---------------------------------------------------------------------------

CAPABILITY = ProviderCapability(
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    supplied_canonical_fields=(
        "windSpeed",
        "windDirection",
        "windGust",
        "waveHeight",
        "dominantPeriod",
        "averagePeriod",
        "meanWaveDirection",
        "pressure",
        "airTemp",
        "waterTemp",
        "dewpoint",
        "visibility",
        "pressureTendency",
        "tideLevel",
        "spectralComponents",
    ),
    geographic_coverage="us_coastal",
    auth_required=(),
    default_poll_interval_seconds=_OBSERVATION_CACHE_TTL,
    operator_notes=(
        "NOAA NDBC buoy observations. Flat-file HTTP access, no API key required. "
        "US coastal coverage."
    ),
    refresh_interval=_OBSERVATION_CACHE_TTL,
    attribution=ProviderAttribution(
        attribution_required=True,
        display_name="NOAA NDBC",
        attribution_text="Data courtesy of NOAA National Data Buoy Center",
        text_prefix="Data courtesy of",
        text_provider_name="NOAA National Data Buoy Center",
        url="https://www.ndbc.noaa.gov/",
    ),
)

# ---------------------------------------------------------------------------
# Rate limiter (ADR-038 §3) — 1 req/s "be polite" guard (PROVIDER-MANUAL §14.1)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(
    name="ndbc-buoy",
    provider_id=PROVIDER_ID,
    domain=DOMAIN,
    max_calls=1,
    window_seconds=1,
)

# ---------------------------------------------------------------------------
# Wire-shape Pydantic model (security-baseline §3.5)
# ---------------------------------------------------------------------------


class _NdbcStandardMetRow(BaseModel):
    """Parsed values from one NDBC standard met (.txt) data row.

    Values are already normalized (MM → None, strings → float) before
    validation. tideLevelFeet is intentionally NOT yet converted to the
    canonical meter base unit — that conversion happens in
    ``_to_canonical_observation`` so this model reflects the wire value.
    """

    model_config = ConfigDict(extra="ignore")

    year: int
    month: int
    day: int
    hour: int
    minute: int
    windDirection: float | None = None
    windSpeed: float | None = None
    windGust: float | None = None
    waveHeight: float | None = None
    dominantPeriod: float | None = None
    averagePeriod: float | None = None
    meanWaveDirection: float | None = None
    pressure: float | None = None
    airTemp: float | None = None
    waterTemp: float | None = None
    dewpoint: float | None = None
    visibility: float | None = None
    pressureTendency: float | None = None
    tideLevelFeet: float | None = None


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton)
# ---------------------------------------------------------------------------

_http_client: ProviderHTTPClient | None = None


def _get_http_client() -> ProviderHTTPClient:
    """Return the module-level HTTP client, constructing it on first use."""
    global _http_client  # noqa: PLW0603
    if _http_client is None:
        _http_client = ProviderHTTPClient(
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
            user_agent=_USER_AGENT,
        )
    return _http_client


def _reset_http_client_for_tests() -> None:
    """Reset module-level HTTP client singleton. Used in tests only."""
    global _http_client  # noqa: PLW0603
    _http_client = None


# ---------------------------------------------------------------------------
# Input validation (trust boundary — station_id is embedded in a URL path)
# ---------------------------------------------------------------------------


def _validate_station_id(station_id: str) -> None:
    """Reject malformed station IDs before they reach URL construction."""
    if not station_id or not _STATION_ID_RE.match(station_id):
        raise ProviderProtocolError(
            f"Invalid NDBC station_id: {station_id!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )


# ---------------------------------------------------------------------------
# Cache key construction (ADR-017 §Cache key, PROVIDER-MANUAL §14.1)
# ---------------------------------------------------------------------------


def _build_cache_key(station_id: str, file_type: str) -> str:
    """Cache key for one (provider, station, file_type) tuple."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "station_id": station_id, "file_type": file_type},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_station_discovery_cache_key() -> str:
    """Cache key for the activestations.xml station list (shared, no params)."""
    payload = json.dumps(
        {"provider_id": PROVIDER_ID, "file_type": "activestations"},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Flat-file line helpers
# ---------------------------------------------------------------------------


def _data_lines(text: str) -> list[str]:
    """Return non-header, non-blank lines from an NDBC flat-file body.

    Header lines start with ``#``. Data rows are most-recent-first.
    """
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# Standard met (.txt) parsing (PROVIDER-MANUAL §14.1)
# ---------------------------------------------------------------------------

_STANDARD_MET_COLUMN_COUNT = 19  # 5 timestamp + 14 data columns


def _parse_float_or_mm(token: str) -> float | None:
    """Parse a standard-met token; NDBC's ``MM`` marker means missing."""
    if token.upper() == "MM":
        return None
    return float(token)


def _parse_standard_met_row(line: str) -> _NdbcStandardMetRow:
    """Parse one NDBC standard met data row into a validated wire model."""
    tokens = line.split()
    if len(tokens) < _STANDARD_MET_COLUMN_COUNT:
        raise ProviderProtocolError(
            f"NDBC standard met row has {len(tokens)} columns, "
            f"expected {_STANDARD_MET_COLUMN_COUNT}: {line!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    try:
        year = int(tokens[0])
        if year < 100:  # defensive: legacy 2-digit year, not seen in practice
            year += 2000
        return _NdbcStandardMetRow(
            year=year,
            month=int(tokens[1]),
            day=int(tokens[2]),
            hour=int(tokens[3]),
            minute=int(tokens[4]),
            windDirection=_parse_float_or_mm(tokens[5]),
            windSpeed=_parse_float_or_mm(tokens[6]),
            windGust=_parse_float_or_mm(tokens[7]),
            waveHeight=_parse_float_or_mm(tokens[8]),
            dominantPeriod=_parse_float_or_mm(tokens[9]),
            averagePeriod=_parse_float_or_mm(tokens[10]),
            meanWaveDirection=_parse_float_or_mm(tokens[11]),
            pressure=_parse_float_or_mm(tokens[12]),
            airTemp=_parse_float_or_mm(tokens[13]),
            waterTemp=_parse_float_or_mm(tokens[14]),
            dewpoint=_parse_float_or_mm(tokens[15]),
            visibility=_parse_float_or_mm(tokens[16]),
            pressureTendency=_parse_float_or_mm(tokens[17]),
            tideLevelFeet=_parse_float_or_mm(tokens[18]),
        )
    except (ValueError, TypeError) as exc:
        raise ProviderProtocolError(
            f"NDBC standard met row failed to parse: {line!r}: {exc}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        ) from exc


def _to_canonical_observation(row: _NdbcStandardMetRow, station_id: str) -> MarineObservation:
    """Map a parsed standard-met row to a canonical MarineObservation.

    spectralComponents is always None here — spectral data is fetched,
    cached, and decomposed independently in fetch() and merged in after
    both pieces are available.
    """
    time_iso = f"{row.year:04d}-{row.month:02d}-{row.day:02d}T{row.hour:02d}:{row.minute:02d}:00Z"
    tide_meters = (
        convert(row.tideLevelFeet, "foot", "meter") if row.tideLevelFeet is not None else None
    )
    return MarineObservation(
        windSpeed=row.windSpeed,
        windDirection=row.windDirection,
        windGust=row.windGust,
        waveHeight=row.waveHeight,
        dominantPeriod=row.dominantPeriod,
        averagePeriod=row.averagePeriod,
        meanWaveDirection=row.meanWaveDirection,
        pressure=row.pressure,
        airTemp=row.airTemp,
        waterTemp=row.waterTemp,
        dewpoint=row.dewpoint,
        visibility=row.visibility,
        pressureTendency=row.pressureTendency,
        tideLevel=tide_meters,
        stationId=station_id,
        time=time_iso,
        spectralComponents=None,
    )


# ---------------------------------------------------------------------------
# Spectral file parsing — VALUE (FREQ) token-pair format (module docstring)
# ---------------------------------------------------------------------------


# Column offset where VALUE(FREQ) pairs begin, per file type (verified live,
# see module docstring). `.data_spec` has a 6th scalar column (Sep_Freq)
# before the pairs start; `.swdir` does NOT — its pairs begin immediately
# after the 5 timestamp columns. Getting this wrong silently misaligns every
# band by one position, so it is looked up explicitly rather than guessed.
_PAIR_START_OFFSET = {"data_spec": 6, "swdir": 5}


def _parse_spectral_pairs(line: str, *, file_label: str) -> tuple[list[float], list[float | None]]:
    """Parse a NDBC ``.data_spec`` / ``.swdir`` VALUE(FREQ) pair row.

    ``.data_spec`` columns: YY MM DD hh mm Sep_Freq  VALUE1 (FREQ1)  ...
    ``.swdir`` columns:     YY MM DD hh mm            VALUE1 (FREQ1)  ...
    Sep_Freq (data_spec only) is not used by our peak-finding decomposition.

    Returns:
        (frequencies, values) — parallel lists, same order as the row.
        A value is None when the token is NDBC's ``MM`` marker or the
        >= 999 direction/energy fill sentinel.
    """
    offset = _PAIR_START_OFFSET.get(file_label)
    if offset is None:
        raise ProviderProtocolError(
            f"NDBC unknown spectral file_label: {file_label!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    tokens = line.split()
    if len(tokens) < offset + 2:  # timestamp (+ Sep_Freq) + at least one VALUE (FREQ) pair
        raise ProviderProtocolError(
            f"NDBC {file_label} row too short ({len(tokens)} tokens): {line!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )
    pair_tokens = tokens[offset:]
    if len(pair_tokens) % 2 != 0:
        raise ProviderProtocolError(
            f"NDBC {file_label} row has an odd number of value/freq tokens: {line!r}",
            provider_id=PROVIDER_ID,
            domain=DOMAIN,
        )

    freqs: list[float] = []
    values: list[float | None] = []
    for i in range(0, len(pair_tokens), 2):
        val_token = pair_tokens[i]
        freq_token = pair_tokens[i + 1].strip("()")
        try:
            freq = float(freq_token)
        except ValueError as exc:
            raise ProviderProtocolError(
                f"NDBC {file_label} frequency token unparseable: {freq_token!r} in {line!r}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        if val_token.upper() == "MM":
            value: float | None = None
        else:
            try:
                parsed = float(val_token)
            except ValueError as exc:
                raise ProviderProtocolError(
                    f"NDBC {file_label} value token unparseable: {val_token!r} in {line!r}",
                    provider_id=PROVIDER_ID,
                    domain=DOMAIN,
                ) from exc
            value = None if parsed >= _NDBC_FILL_SENTINEL else parsed

        freqs.append(freq)
        values.append(value)

    return freqs, values


def _fetch_and_parse_pairs_file(
    station_id: str, path_template: str, *, file_label: str
) -> tuple[list[float], list[float | None], bool]:
    """GET one spectral flat file and parse its most-recent data row.

    Returns (freqs, values, available). available=False (with an empty
    (freqs, values)) means the file is legitimately not published for this
    station (404) or came back with no usable data — not an error, per
    PROVIDER-MANUAL §14.1's empty-body handling extended to optional
    per-station spectral files.

    A 404 here is deliberately NOT re-raised as a hard failure: unlike the
    required standard-met file, a missing spectral file just means this
    station lacks that sensor. The original ProviderHTTPClient exception is
    inspected (status_code) but never reconstructed — only re-raised as-is
    for any status other than 404, so retry_after/status_code attributes
    are never dropped.
    """
    _rate_limiter.acquire()
    client = _get_http_client()
    url = f"{_NDBC_BASE_URL}{path_template.format(station_id=station_id)}"
    try:
        response = client.get(url)
    except ProviderProtocolError as exc:
        if exc.status_code == 404:
            logger.info(
                "NDBC %s: %s not available for this station (404)", station_id, file_label
            )
            return [], [], False
        raise

    text = response.text
    if not text.strip():
        logger.warning("NDBC %s: empty %s response body", station_id, file_label)
        return [], [], False

    lines = _data_lines(text)
    if not lines:
        logger.warning("NDBC %s: %s file has no data rows", station_id, file_label)
        return [], [], False

    freqs, values = _parse_spectral_pairs(lines[0], file_label=file_label)
    return freqs, values, True


# ---------------------------------------------------------------------------
# Spectral decomposition (PROVIDER-MANUAL §14.1, no scipy)
# ---------------------------------------------------------------------------

_NOISE_THRESHOLD_FRACTION = 0.05
_MAX_SWELL_SYSTEMS = 4
_GROUNDSWELL_PERIOD_SECONDS = 12.0
_SWELL_PERIOD_SECONDS = 8.0


def _find_peak_indices(valid: dict[int, float], n: int) -> list[int]:
    """Interior local maxima: energy(f) > energy(f-1) and energy(f) > energy(f+1).

    ``valid`` maps band index -> energy for bands with a known (non-None)
    value; ``n`` is the total band count (missing bands are simply absent
    from ``valid`` and can never be a peak).
    """
    peaks: list[int] = []
    for i in range(1, n - 1):
        e = valid.get(i)
        left = valid.get(i - 1)
        right = valid.get(i + 1)
        if e is not None and left is not None and right is not None and e > left and e > right:
            peaks.append(i)
    return peaks


def _partition_energy(idxs: list[int], valid: dict[int, float]) -> float:
    return sum(valid[i] for i in idxs if i in valid)


def _partition_indices(freqs: list[float], energies: list[float | None]) -> list[list[int]]:
    """Partition frequency-band indices into up to 4 swell systems.

    Peak detection -> noise threshold -> boundary-at-min-energy partitioning
    -> merge weakest partitions until <= _MAX_SWELL_SYSTEMS remain.
    """
    n = len(freqs)
    valid: dict[int, float] = {i: e for i, e in enumerate(energies) if e is not None}
    if not valid:
        return []

    peaks = _find_peak_indices(valid, n)
    if not peaks:
        # No interior local maxima (e.g. monotonic or single significant
        # bin) — fall back to a single partition anchored at the overall max.
        peaks = [max(valid, key=lambda i: valid[i])]

    dominant_energy = max(valid[i] for i in peaks)
    threshold = _NOISE_THRESHOLD_FRACTION * dominant_energy
    filtered_peaks = sorted(i for i in peaks if valid[i] >= threshold)
    if not filtered_peaks:
        filtered_peaks = [max(valid, key=lambda i: valid[i])]

    partitions: list[list[int]] = []
    start = 0
    for k, peak in enumerate(filtered_peaks):
        if k == len(filtered_peaks) - 1:
            end = n - 1
        else:
            next_peak = filtered_peaks[k + 1]
            between = [i for i in range(peak + 1, next_peak) if i in valid]
            end = min(between, key=lambda i: valid[i]) if between else peak
        partitions.append(list(range(start, end + 1)))
        start = end + 1

    while len(partitions) > _MAX_SWELL_SYSTEMS:
        weakest_pos = min(
            range(len(partitions)), key=lambda k: _partition_energy(partitions[k], valid)
        )
        if weakest_pos == 0:
            merge_target = 1
        elif weakest_pos == len(partitions) - 1:
            merge_target = weakest_pos - 1
        else:
            left_e = _partition_energy(partitions[weakest_pos - 1], valid)
            right_e = _partition_energy(partitions[weakest_pos + 1], valid)
            merge_target = weakest_pos - 1 if left_e >= right_e else weakest_pos + 1

        merged = sorted(partitions[weakest_pos] + partitions[merge_target])
        keep_positions = {weakest_pos, merge_target}
        partitions = [p for k, p in enumerate(partitions) if k not in keep_positions]
        partitions.append(merged)
        partitions.sort(key=lambda idxs: idxs[0])

    return partitions


def _classify_period(period_seconds: float) -> str:
    if period_seconds >= _GROUNDSWELL_PERIOD_SECONDS:
        return "groundswell"
    if period_seconds >= _SWELL_PERIOD_SECONDS:
        return "swell"
    return "wind_swell"


def _valid_points(
    idxs: list[int], freqs: list[float], energies: list[float | None]
) -> list[tuple[float, float]]:
    """Return (freq, energy) pairs for bands in idxs with a known energy value."""
    points: list[tuple[float, float]] = []
    for i in idxs:
        e = energies[i]
        if e is not None:
            points.append((freqs[i], e))
    return points


def _compute_swell_system(
    idxs: list[int],
    freqs: list[float],
    energies: list[float | None],
    directions: list[float | None] | None,
) -> SpectralWaveComponent | None:
    """Compute Hs/Tp/direction/classification for one partition.

    Returns None if the partition has no usable (non-None) energy samples.
    """
    points = _valid_points(idxs, freqs, energies)
    if not points:
        return None
    points.sort(key=lambda t: t[0])

    if len(points) >= 2:
        m0 = 0.0
        for (f0, e0), (f1, e1) in zip(points, points[1:], strict=False):
            m0 += 0.5 * (e0 + e1) * (f1 - f0)
    else:
        # Single-band partition: trapezoidal integration needs >= 2 points.
        # Approximate bandwidth from the overall frequency spacing so a
        # lone contributing band still yields a small non-zero m0.
        f0, e0 = points[0]
        avg_df = (freqs[-1] - freqs[0]) / max(1, len(freqs) - 1)
        m0 = e0 * avg_df

    hs = 4.0 * math.sqrt(max(0.0, m0))
    peak_freq, _peak_energy = max(points, key=lambda t: t[1])
    tp = 1.0 / peak_freq if peak_freq > 0 else 0.0

    direction_deg = 0.0
    if directions is not None:
        sin_sum = 0.0
        cos_sum = 0.0
        have_direction = False
        for i in idxs:
            e = energies[i]
            d = directions[i] if i < len(directions) else None
            if e is None or d is None:
                continue
            have_direction = True
            rad = math.radians(d)
            sin_sum += e * math.sin(rad)
            cos_sum += e * math.cos(rad)
        if have_direction and (sin_sum != 0.0 or cos_sum != 0.0):
            direction_deg = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0

    return SpectralWaveComponent(
        height=hs,
        period=tp,
        direction=direction_deg,
        energy=m0,
        frequencyRange=[points[0][0], points[-1][0]],
        classification=_classify_period(tp),
    )


def _fetch_spectral(station_id: str) -> list[SpectralWaveComponent]:
    """Fetch (or use cached) spectral density + direction and decompose them."""
    density_key = _build_cache_key(station_id, "data_spec")
    cached_density = get_cache().get(density_key)
    if cached_density is not None:
        density_freqs = cached_density["freqs"]
        density_values = cached_density["values"]
        density_available = cached_density["available"]
    else:
        density_freqs, density_values, density_available = _fetch_and_parse_pairs_file(
            station_id, _SPECTRAL_DENSITY_PATH, file_label="data_spec"
        )
        get_cache().set(
            density_key,
            {"freqs": density_freqs, "values": density_values, "available": density_available},
            ttl_seconds=_OBSERVATION_CACHE_TTL,
        )

    if not density_available or not density_freqs:
        return []

    direction_key = _build_cache_key(station_id, "swdir")
    cached_direction = get_cache().get(direction_key)
    if cached_direction is not None:
        direction_freqs = cached_direction["freqs"]
        direction_values = cached_direction["values"]
        direction_available = cached_direction["available"]
    else:
        direction_freqs, direction_values, direction_available = _fetch_and_parse_pairs_file(
            station_id, _SPECTRAL_DIRECTION_PATH, file_label="swdir"
        )
        get_cache().set(
            direction_key,
            {
                "freqs": direction_freqs,
                "values": direction_values,
                "available": direction_available,
            },
            ttl_seconds=_OBSERVATION_CACHE_TTL,
        )

    directions_by_index: list[float | None] | None = None
    if direction_available and direction_freqs and len(direction_freqs) == len(density_freqs):
        directions_by_index = direction_values
    elif direction_available:
        logger.warning(
            "NDBC %s: spectral direction band count (%d) does not match "
            "spectral density band count (%d); direction will default to 0.0",
            station_id,
            len(direction_freqs),
            len(density_freqs),
        )
    else:
        logger.warning(
            "NDBC %s: spectral direction data unavailable; "
            "swell direction will default to 0.0",
            station_id,
        )

    partitions = _partition_indices(density_freqs, density_values)
    components: list[SpectralWaveComponent] = []
    for idxs in partitions:
        component = _compute_swell_system(idxs, density_freqs, density_values, directions_by_index)
        if component is not None:
            components.append(component)
    return components


# ---------------------------------------------------------------------------
# Station discovery (PROVIDER-MANUAL §14.1)
# ---------------------------------------------------------------------------


def _guess_capability(station_type: str, met: bool) -> str:
    """Best-effort station capability guess from activestations.xml attributes.

    See module docstring "Station discovery capability guess" — this is a
    heuristic, not ground truth. fetch() never gates on it.
    """
    if not met:
        return "none"
    if station_type == "fixed":
        return "atmospheric_only"
    return "wave_atmospheric"  # spectral availability undetermined until probed


def discover_stations(lat: float, lon: float, radius_km: float) -> list[dict[str, object]]:
    """Discover NDBC stations within radius_km of (lat, lon).

    Fetches (or reuses a cached copy of) activestations.xml, filters to
    stations reporting atmospheric data, computes haversine distance, and
    returns stations within radius_km sorted nearest-first.

    Returns:
        List of dicts: {stationId, name, lat, lon, type, capabilityGuess,
        distanceKm}.
    """
    cache_key = _build_station_discovery_cache_key()
    cached_stations = get_cache().get(cache_key)
    if cached_stations is not None:
        stations = cached_stations
    else:
        _rate_limiter.acquire()
        client = _get_http_client()
        response = client.get(_ACTIVE_STATIONS_URL)
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise ProviderProtocolError(
                f"NDBC activestations.xml parse failed: {exc}",
                provider_id=PROVIDER_ID,
                domain=DOMAIN,
            ) from exc

        stations = []
        for el in root.findall("station"):
            station_id = el.get("id")
            lat_raw = el.get("lat")
            lon_raw = el.get("lon")
            if not station_id or lat_raw is None or lon_raw is None:
                continue
            try:
                station_lat = float(lat_raw)
                station_lon = float(lon_raw)
            except ValueError:
                logger.warning(
                    "NDBC activestations.xml: skipping station %s with unparseable coordinates",
                    station_id,
                )
                continue
            met = (el.get("met") or "n").lower() == "y"
            station_type = (el.get("type") or "").lower()
            stations.append(
                {
                    "stationId": station_id,
                    "name": el.get("name") or station_id,
                    "lat": station_lat,
                    "lon": station_lon,
                    "type": station_type,
                    "met": met,
                    "capabilityGuess": _guess_capability(station_type, met),
                }
            )
        get_cache().set(cache_key, stations, ttl_seconds=_STATION_DISCOVERY_CACHE_TTL)

    results = []
    for station in stations:
        if not station["met"]:
            continue  # no atmospheric data at all — not useful for MarineObservation
        distance_km = _haversine_km(lat, lon, station["lat"], station["lon"])
        if distance_km <= radius_km:
            results.append({**station, "distanceKm": round(distance_km, 2)})
    results.sort(key=lambda s: s["distanceKm"])
    return results


_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Public fetch entrypoint (ADR-038 §2)
# ---------------------------------------------------------------------------


def fetch(*, station_id: str, include_spectral: bool = True) -> dict[str, object]:
    """Fetch NDBC buoy observations for one station.

    Returns:
        dict with:
          - "observation": MarineObservation | None (None only when the
            standard met file body is empty — not an error condition)
          - "spectral": list[SpectralWaveComponent] (empty when spectral
            data is unavailable for this station or not requested)
          - "station_id": str

    Raises:
        QuotaExhausted: rate limit exceeded.
        TransientNetworkError: network/DNS failure or 5xx after retries.
        ProviderProtocolError: station_id malformed, standard-met file
            404s (non-existent station), or response parsing fails.
    """
    _validate_station_id(station_id)

    met_key = _build_cache_key(station_id, "txt")
    cached_met = get_cache().get(met_key)
    if cached_met is not None:
        if cached_met == _EMPTY_MARKER:
            observation = None
        else:
            observation = MarineObservation.model_validate(cached_met)
    else:
        _rate_limiter.acquire()
        client = _get_http_client()
        met_url = f"{_NDBC_BASE_URL}{_STANDARD_MET_PATH.format(station_id=station_id)}"
        # 404 here (non-existent station) is NOT caught — it propagates as
        # the canonical ProviderProtocolError raised by ProviderHTTPClient,
        # per PROVIDER-MANUAL §14.1's error-handling rule for this file.
        response = client.get(met_url)
        text = response.text
        if not text.strip():
            logger.warning("NDBC %s: empty standard met response body", station_id)
            observation = None
            get_cache().set(met_key, _EMPTY_MARKER, ttl_seconds=_OBSERVATION_CACHE_TTL)
        else:
            lines = _data_lines(text)
            if not lines:
                logger.warning("NDBC %s: standard met file has no data rows", station_id)
                observation = None
                get_cache().set(met_key, _EMPTY_MARKER, ttl_seconds=_OBSERVATION_CACHE_TTL)
            else:
                row = _parse_standard_met_row(lines[0])
                observation = _to_canonical_observation(row, station_id)
                get_cache().set(
                    met_key, observation.model_dump(), ttl_seconds=_OBSERVATION_CACHE_TTL
                )

    spectral: list[SpectralWaveComponent] = []
    if include_spectral:
        try:
            spectral = _fetch_spectral(station_id)
        except Exception:
            logger.warning("NDBC spectral fetch failed for %s; returning observation only", station_id, exc_info=True)
        if observation is not None and spectral:
            observation = observation.model_copy(update={"spectralComponents": spectral})

    logger.info(
        "NDBC fetch complete: station=%s observation=%s spectral_systems=%d",
        station_id,
        "present" if observation is not None else "empty",
        len(spectral),
    )

    return {"observation": observation, "spectral": spectral, "station_id": station_id}
