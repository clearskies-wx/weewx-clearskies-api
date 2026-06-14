"""10-minute wall-clock rolling window for wind speed and gust.

Computes two BFF-derived fields from a true wall-clock rolling window of
incoming loop packets:

* ``windSpeedAvg10m`` — arithmetic mean of ``windSpeed`` over the last 10 min.
* ``windGustMax10m``  — maximum ``windGust`` over the last 10 min.

The window is time-based (entries older than WINDOW_SECONDS are evicted on
each packet), not sample-count-based.  This means the reported values reflect
true meteorological 10-minute averages rather than a fixed number of samples
that drifts with packet frequency.

A minimum coverage guard (MIN_COVERAGE_SECONDS) prevents the averages from
being reported before enough wall-clock time has elapsed — avoiding misleading
"averages" computed from only one or two seconds of data.

Integration points:
- ``process_packet`` is registered via ``register_processor`` in __main__.py.
  It must NOT mutate the packet (read-only).
- ``enrich_wind_rolling_average`` is registered via ``register_enrichment`` for
  the "current" endpoint.  It injects ``windSpeedAvg10m`` / ``windGustMax10m``
  as ConvertedValue dicts into the /current response.  Fields are omitted (not
  null) before min-coverage elapsed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SECONDS: int = 600       # 10-minute rolling window
MIN_COVERAGE_SECONDS: int = 60  # suppress output before 60 s of data

# Speed units the converter knows about.  Used to validate before passing to
# convert() so we don't surface a KeyError from the conversion registry.
_VALID_SPEED_UNITS: frozenset[str] = frozenset(
    {"mile_per_hour", "km_per_hour", "knot", "meter_per_second"}
)

# ---------------------------------------------------------------------------
# Module-level transformer reference (set by configure() at startup)
# ---------------------------------------------------------------------------

_transformer = None


def configure(transformer: object) -> None:
    """Set the UnitTransformer reference.  Called once at startup."""
    global _transformer  # noqa: PLW0603
    _transformer = transformer


# ---------------------------------------------------------------------------
# TimeWindowedBuffer
# ---------------------------------------------------------------------------


class TimeWindowedBuffer:
    """Thread-safe time-windowed deque of (timestamp, value) pairs.

    Unlike RingBuffer (fixed capacity), this buffer evicts by *age*: any entry
    whose timestamp is older than ``now - window_seconds`` is removed.  The
    window size is fixed at construction time.

    All public methods are protected by a single ``threading.Lock`` (same
    pattern as ring_buffer.RingBuffer).
    """

    def __init__(self, window_seconds: int = WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._data: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, timestamp: float, value: float) -> None:
        """Append a (timestamp, value) pair to the right end of the deque."""
        with self._lock:
            self._data.append((timestamp, value))

    def evict(self, now: float) -> None:
        """Remove entries older than ``now - window_seconds`` from the left."""
        cutoff = now - self._window_seconds
        with self._lock:
            while self._data and self._data[0][0] < cutoff:
                self._data.popleft()

    def reset(self) -> None:
        """Clear all entries.  For test isolation only."""
        with self._lock:
            self._data.clear()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def mean(self) -> float | None:
        """Arithmetic mean of buffered values, or None if the buffer is empty."""
        with self._lock:
            if not self._data:
                return None
            total = sum(v for _, v in self._data)
            return total / len(self._data)

    def max_val(self) -> float | None:
        """Maximum of buffered values, or None if the buffer is empty."""
        with self._lock:
            if not self._data:
                return None
            return max(v for _, v in self._data)

    def span(self) -> float:
        """Elapsed seconds between the oldest and newest entries.

        Returns 0.0 when fewer than two entries are present.
        """
        with self._lock:
            if len(self._data) < 2:
                return 0.0
            return self._data[-1][0] - self._data[0][0]


# ---------------------------------------------------------------------------
# Module-level buffer instances
# ---------------------------------------------------------------------------

_speed_buffer = TimeWindowedBuffer(WINDOW_SECONDS)
_gust_buffer = TimeWindowedBuffer(WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Packet processor (registered via register_processor)
# ---------------------------------------------------------------------------


def process_packet(packet: dict[str, Any]) -> None:
    """Feed wind values from a loop packet into the rolling-window buffers.

    Called for every loop packet via ``register_processor``.  Read-only —
    MUST NOT mutate the packet.

    Handles both raw float values and ConvertedValue dicts ``{value: float, …}``
    (the shape produced by transform_field after MQTT conversion).  Also handles
    MQTT suffixed field names via ``strip_suffix`` so direct-mode and MQTT-mode
    packets are treated identically.

    Skips None and non-numeric values silently.
    """
    now = time.time()
    ts = packet.get("dateTime")
    try:
        timestamp = float(ts) if ts is not None else now
    except (TypeError, ValueError):
        timestamp = now

    for field, buf in (("windSpeed", _speed_buffer), ("windGust", _gust_buffer)):
        raw = _extract_field(packet, field)
        if raw is None:
            continue
        # Extract numeric value from ConvertedValue dict or use directly.
        if isinstance(raw, dict):
            raw = raw.get("value")
        if raw is None:
            continue
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        buf.evict(now)
        buf.add(timestamp, value)


def _extract_field(packet: dict[str, Any], field: str) -> Any:
    """Return the raw value for *field* from *packet*, handling MQTT suffixes.

    Checks the bare field name first (direct-mode), then scans for a
    suffix-bearing variant using strip_suffix (MQTT-mode).  Returns None if
    not found.
    """
    from weewx_clearskies_api.sse.field_utils import strip_suffix as _strip  # noqa: PLC0415

    # Bare name (direct mode, or already-normalised MQTT).
    val = packet.get(field)
    if val is not None:
        return val
    # MQTT suffix scan: look for e.g. "windSpeed_mph", "windGust_mps".
    for key, raw_val in packet.items():
        base, _ = _strip(key)
        if base == field:
            return raw_val
    return None


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def get_wind_avg() -> float | None:
    """Return the 10-minute mean wind speed, or None before min coverage.

    Returns None until at least MIN_COVERAGE_SECONDS of data has accumulated.
    """
    if _speed_buffer.span() < MIN_COVERAGE_SECONDS:
        return None
    return _speed_buffer.mean()


def get_gust_max() -> float | None:
    """Return the 10-minute max gust, or None before min coverage.

    Returns None until at least MIN_COVERAGE_SECONDS of data has accumulated.
    """
    if _gust_buffer.span() < MIN_COVERAGE_SECONDS:
        return None
    return _gust_buffer.max_val()


# ---------------------------------------------------------------------------
# /current enrichment
# ---------------------------------------------------------------------------


def enrich_wind_rolling_average(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``windSpeedAvg10m`` and ``windGustMax10m`` into a /current response.

    Resolves the operator's target speed unit from the transformer's
    ``group_speed`` setting (same resolution pattern as barometer_trend's
    pressure unit), then converts the raw average/max values from the
    station's native unit (assumed mile_per_hour for US, km_per_hour for
    Metric/MetricWX) to the display unit and injects a ConvertedValue dict.

    Fields are **omitted** (not injected as null) before MIN_COVERAGE_SECONDS
    of data is available.  This prevents the dashboard from receiving
    misleading zeros or nulls during warm-up.

    Never raises — any error is logged and the function returns data unchanged.
    """
    try:
        avg = get_wind_avg()
        gust = get_gust_max()

        # Nothing to inject yet.
        if avg is None and gust is None:
            return data

        target_unit = _resolve_speed_unit()
        source_unit = _infer_source_speed_unit(data)

        if avg is not None:
            data["windSpeedAvg10m"] = _make_converted_value(avg, source_unit, target_unit)
        if gust is not None:
            data["windGustMax10m"] = _make_converted_value(gust, source_unit, target_unit)

    except Exception:  # noqa: BLE001
        logger.exception("enrich_wind_rolling_average: unexpected error; data returned unchanged")

    return data


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------


def _resolve_speed_unit() -> str | None:
    """Return the configured display unit for group_speed, or None.

    Resolution order:
    1. _transformer._targets["group_speed"] — validated at construction.
    2. None (no fallback label lookup needed for speed).
    """
    if _transformer is None:
        logger.warning("enrich_wind_rolling_average: _transformer not configured; skipping unit resolution")
        return None
    try:
        targets = getattr(_transformer, "_targets", {})
        unit = targets.get("group_speed")
        if unit and unit in _VALID_SPEED_UNITS:
            return unit
    except Exception:  # noqa: BLE001
        pass
    return None


# Mapping from the label strings in the upstream API ``units`` block to
# weewx unit strings for group_speed.  Defined at module level (not inside
# _infer_source_speed_unit) to satisfy ruff N806 (no uppercase locals).
_SPEED_LABEL_TO_UNIT: dict[str, str] = {
    "mph":   "mile_per_hour",
    "km/h":  "km_per_hour",
    "knots": "knot",
    "m/s":   "meter_per_second",
}


def _infer_source_speed_unit(data: dict[str, Any]) -> str:
    """Infer the source (station native) speed unit from the /current envelope.

    Reads the ``units`` block from the upstream response and maps the windSpeed
    label to a weewx unit string.  Falls back to ``"mile_per_hour"`` (US default)
    when the unit cannot be determined.
    """
    try:
        units_block = data.get("units")
        if isinstance(units_block, dict):
            raw_label = str(units_block.get("windSpeed", "")).strip()
            # Labels may include a leading space: " mph" → "mph"
            for label, unit in _SPEED_LABEL_TO_UNIT.items():
                if raw_label.strip().lower() == label.lower():
                    return unit
    except Exception:  # noqa: BLE001
        pass
    return "mile_per_hour"


def _make_converted_value(
    raw: float,
    source_unit: str,
    target_unit: str | None,
) -> dict[str, Any]:
    """Convert *raw* from *source_unit* to *target_unit* and return a ConvertedValue dict.

    When *target_unit* is None (no transformer configured) the value is
    returned in its source unit.  Mirrors the pass-through branches in
    UnitTransformer._transform_single_obs.
    """
    from weewx_clearskies_api.units.conversion import convert  # noqa: PLC0415
    from weewx_clearskies_api.units.labels import format_value, get_label  # noqa: PLC0415

    effective_unit = target_unit if target_unit else source_unit
    if target_unit and target_unit != source_unit:
        converted = convert(raw, source_unit, target_unit)
        if converted is None:
            converted = raw
            effective_unit = source_unit
    else:
        converted = raw

    return {
        "value": converted,
        "label": get_label(effective_unit),
        "formatted": format_value(converted, effective_unit),
    }


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


def reset() -> None:
    """Clear both rolling-window buffers.  For test isolation only."""
    _speed_buffer.reset()
    _gust_buffer.reset()
