"""Input smoother for conditions engine stability (ADR-044 §8).

Maintains rolling-average ring buffers for all conditions inputs.
Registered as a packet_tap processor so every loop packet feeds the buffers.

Buffer capacities assume ~5-second loop packet intervals.
"""

from __future__ import annotations

from weewx_clearskies_api.sse.ring_buffer import RingBuffer
from weewx_clearskies_api.sse.field_utils import strip_suffix

# Buffer capacities (samples at ~5-second interval per ADR-044 §8)
_buffers: dict[str, RingBuffer] = {
    "appTemp":   RingBuffer(120),  # 10 min
    "dewpoint":  RingBuffer(120),  # 10 min
    "outTemp":   RingBuffer(120),  # 10 min
    "windSpeed": RingBuffer(60),   # 5 min
    "windGust":  RingBuffer(60),   # 5 min
    "rainRate":  RingBuffer(24),   # 2 min
    "heatindex": RingBuffer(120),  # 10 min
    "windchill": RingBuffer(120),  # 10 min
}

# Minimum number of samples before get_smoothed() returns a value.
_MIN_SAMPLES: int = 10


def process_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Feed a loop packet into all smoothing buffers.

    Called by packet_tap for every loop packet.  Extracts known fields
    and adds non-None values to the corresponding ring buffer.  Bad values
    (non-numeric) are silently skipped.

    MQTT packets carry suffixed field names (e.g. ``outTemp_F``, ``windSpeed_mph``).
    We strip the suffix to obtain the canonical base name before doing the
    buffer lookup, so both MQTT-mode and direct-mode packets feed the same
    buffers.  For direct-mode packets the suffix strip is a no-op.
    """
    # Build a canonical-name → raw-value view of the packet.
    # strip_suffix returns (base_name, unit); we only need the base_name here.
    canonical: dict[str, object] = {}
    for field_name, raw_value in packet.items():
        base_name, _ = strip_suffix(field_name)
        # When multiple suffixed variants map to the same base (should not
        # happen in practice) keep the first encountered value.
        canonical.setdefault(base_name, raw_value)

    for field, buf in _buffers.items():
        raw = canonical.get(field)
        if raw is None:
            continue
        # Packets may be unit-converted dicts {value, label, formatted}.
        value = raw.get("value") if isinstance(raw, dict) else raw  # type: ignore[union-attr]
        if value is not None:
            try:
                buf.add(float(value))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass


def get_smoothed(field: str) -> float | None:
    """Return the smoothed (mean) value for a field, or None if insufficient data.

    Returns None when:
    - *field* is not tracked by any buffer, or
    - the buffer has fewer than ``_MIN_SAMPLES`` samples (roughly 50 seconds
      of data — not enough for a meaningful average).

    Args:
        field: weewx loop-packet field name (e.g. ``"appTemp"``).

    Returns:
        Arithmetic mean of buffered samples, or ``None``.
    """
    buf = _buffers.get(field)
    if buf is None or buf.count < _MIN_SAMPLES:
        return None
    try:
        return buf.mean()
    except ValueError:
        return None


def backfill(records: list[dict[str, float | None]]) -> None:
    """Seed smoothing buffers from archive records for immediate classification.

    Each record is a dict with field names matching the buffer keys (appTemp,
    dewpoint, outTemp, windSpeed, windGust, rainRate, heatindex, windchill).
    Records should be in chronological order.  None values are skipped.

    Called once at startup from __main__.py.  Archive records are already
    averaged over the archive interval — each becomes one buffer entry.
    """
    for record in records:
        for field, buf in _buffers.items():
            value = record.get(field)
            if value is not None:
                try:
                    buf.add(float(value))
                except (TypeError, ValueError):
                    pass


def reset() -> None:
    """Clear all buffers.  For test isolation only."""
    for buf in _buffers.values():
        buf.clear()
