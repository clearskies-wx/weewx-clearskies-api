"""24-hour rolling buffer for lightning strike events.

Detects new lightning strikes from weewx loop packets by watching the
``lightning_strike_count`` field for increments, records each strike's
timestamp and distance, and exposes the last 24 hours of events as a list.

The buffer is intentionally simple: it is a pure event log with no
aggregation, no statistics, and no minimum-coverage guard.  An empty list
is a valid and expected state when no strikes have occurred.

Integration points:
- ``process_packet`` is registered via ``register_processor`` in __main__.py.
  It must NOT mutate the packet (read-only).
- ``enrich_lightning_history`` is registered via ``register_enrichment`` for
  the "current" endpoint.  It injects ``lightningStrikeHistory: [...]`` into
  the /current response.  The field is always present (empty list when no
  strikes have occurred in the window).
- ``get_strike_history`` is called from transformer.add_derived_fields() to
  inject the same list into every SSE loop-packet event (no unit conversion
  needed — distance passes through in the station's configured unit).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SECONDS: int = 86400  # 24-hour rolling window


# ---------------------------------------------------------------------------
# LightningStrikeBuffer
# ---------------------------------------------------------------------------


class LightningStrikeBuffer:
    """Thread-safe 24-hour rolling buffer of lightning strike events.

    Each entry stored internally is a ``(timestamp: float, entry: dict)``
    pair where ``entry`` is ``{"time": iso_string, "distance": float}``.

    All public methods are protected by a single ``threading.Lock`` (same
    pattern as wind_rolling_window.TimeWindowedBuffer).
    """

    def __init__(self, window_seconds: int = WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._data: deque[tuple[float, dict[str, Any]]] = deque()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, timestamp: float, distance: float) -> None:
        """Append a strike event to the right end of the deque.

        Args:
            timestamp: Unix epoch seconds (packet dateTime or wall-clock now).
            distance:  Lightning distance in the station's configured unit.
        """
        iso_time = datetime.fromtimestamp(timestamp, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entry: dict[str, Any] = {"time": iso_time, "distance": distance}
        with self._lock:
            self._data.append((timestamp, entry))

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
    # Accessor
    # ------------------------------------------------------------------

    def get_history(self) -> list[dict[str, Any]]:
        """Return all buffered strike events as a list of dicts.

        Each dict has the shape ``{"time": str, "distance": float}``.
        Returns an empty list when no strikes are in the window — this is
        a valid state, not an error condition.
        """
        with self._lock:
            return [entry for _ts, entry in self._data]


# ---------------------------------------------------------------------------
# Module-level buffer instance and strike-count tracker
# ---------------------------------------------------------------------------

_strike_buffer = LightningStrikeBuffer()

# Track the last known strike count so we can detect increments.
# None means we have not yet seen a packet with lightning_strike_count.
_last_strike_count: float | None = None


# ---------------------------------------------------------------------------
# Packet processor (registered via register_processor)
# ---------------------------------------------------------------------------


def process_packet(packet: dict[str, Any]) -> None:
    """Detect new lightning strikes from a loop packet and record them.

    Called for every loop packet via ``register_processor``.  Read-only —
    MUST NOT mutate the packet.

    A new strike is detected when ``lightning_strike_count`` in the current
    packet is strictly greater than the last known count.  This handles the
    common case where a single packet arrival coincides with one or more new
    strikes.

    Handles both raw float values and ConvertedValue dicts
    ``{"value": float, ...}`` (the shape produced by transform_field after
    MQTT conversion), and MQTT suffix-bearing field names (e.g.
    ``lightning_strike_count_count``) via ``_extract_field``.

    Skips gracefully if fields are missing or None.
    """
    global _last_strike_count  # noqa: PLW0603

    import time as _time  # noqa: PLC0415

    now = _time.time()

    # Resolve packet timestamp.
    ts = packet.get("dateTime")
    try:
        timestamp = float(ts) if ts is not None else now
    except (TypeError, ValueError):
        timestamp = now

    # Extract strike count.
    raw_count = _extract_field(packet, "lightning_strike_count")
    if raw_count is None:
        # No lightning data in this packet — still reset count if needed.
        return

    # Unwrap ConvertedValue dict.
    if isinstance(raw_count, dict):
        raw_count = raw_count.get("value")
    if raw_count is None:
        return

    try:
        current_count = float(raw_count)
    except (TypeError, ValueError):
        return

    # First packet with strike data — record the baseline, no strike logged.
    if _last_strike_count is None:
        _last_strike_count = current_count
        return

    # Detect a new strike: count strictly increased.
    if current_count > _last_strike_count:
        # Extract distance for this strike (may be None if not provided).
        raw_dist = _extract_field(packet, "lightning_distance")
        distance: float = 0.0
        if raw_dist is not None:
            if isinstance(raw_dist, dict):
                raw_dist = raw_dist.get("value")
            if raw_dist is not None:
                try:
                    distance = float(raw_dist)
                except (TypeError, ValueError):
                    distance = 0.0

        _strike_buffer.evict(now)
        _strike_buffer.add(timestamp, distance)

        logger.debug(
            "Lightning strike detected",
            extra={
                "lightning_distance": distance,
                "lightning_strike_count": current_count,
                "prev_count": _last_strike_count,
            },
        )

    _last_strike_count = current_count


def _extract_field(packet: dict[str, Any], field: str) -> Any:
    """Return the raw value for *field* from *packet*, handling MQTT suffixes.

    Checks the bare field name first (direct-mode, or already-normalised MQTT),
    then scans for a suffix-bearing variant using strip_suffix (MQTT-mode).
    Returns None if not found.

    Mirrors the identical helper in wind_rolling_window.py.
    """
    from weewx_clearskies_api.sse.field_utils import strip_suffix as _strip  # noqa: PLC0415

    # Bare name (direct mode, or already-normalised MQTT).
    val = packet.get(field)
    if val is not None:
        return val
    # MQTT suffix scan: look for e.g. "lightning_strike_count_count".
    for key, raw_val in packet.items():
        base, _ = _strip(key)
        if base == field:
            return raw_val
    return None


# ---------------------------------------------------------------------------
# Public accessor
# ---------------------------------------------------------------------------


def get_strike_history() -> list[dict[str, Any]]:
    """Return the last 24 hours of lightning strike events.

    Returns an empty list when no strikes have occurred in the window.
    This is a valid state — callers must not omit the field on empty list.
    """
    return _strike_buffer.get_history()


# ---------------------------------------------------------------------------
# /current enrichment
# ---------------------------------------------------------------------------


def enrich_lightning_history(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``lightningStrikeHistory`` into a /current response.

    The field is always injected, even when the list is empty — the dashboard
    uses the empty list to clear a previously displayed strike overlay.

    Distance values pass through in the station's configured unit (no
    conversion performed here — the BFF does not know the operator's
    preferred distance unit at enrichment time, and the dashboard receives
    the unit context from the surrounding /current envelope).

    Never raises — any exception is logged and the function returns *data*
    unchanged so downstream enrichments are not disrupted.
    """
    try:
        data["lightningStrikeHistory"] = get_strike_history()
    except Exception:  # noqa: BLE001
        logger.exception(
            "enrich_lightning_history: unexpected error; data returned unchanged"
        )
    return data


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


def reset() -> None:
    """Clear the strike buffer and reset the count tracker.  For test isolation only."""
    global _last_strike_count  # noqa: PLW0603
    _strike_buffer.reset()
    _last_strike_count = None
