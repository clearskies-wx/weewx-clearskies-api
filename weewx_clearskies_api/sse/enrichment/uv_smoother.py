"""UV index smoother — 10-minute rolling average for GET /api/v1/current.

Maintains a fixed-capacity ring buffer for the ``UV`` loop-packet field.
Registered as a packet_tap processor so every loop packet feeds the buffer.
Applied as an enrichment on the ``current`` endpoint to replace the raw UV
value with the smoothed mean.

Buffer capacity assumes ~5-second loop packet intervals (120 samples = 10 min).

SSE path: smoothing is applied to REST only (v0.1).  The SSE broadcast carries
raw loop-packet UV values.  Full SSE smoothing is deferred to a follow-on task.
"""

from __future__ import annotations

import logging

from weewx_clearskies_api.sse.ring_buffer import RingBuffer

logger = logging.getLogger(__name__)

# 10 minutes at ~5-second loop packet intervals.
_UV_BUFFER_CAPACITY: int = 120

# Minimum samples before smoothing is applied.  Below this threshold the raw
# packet value is returned unchanged so the dashboard is not left with stale
# data during warm-up (~50 seconds after startup).
_MIN_SAMPLES: int = 10

_buffer: RingBuffer = RingBuffer(_UV_BUFFER_CAPACITY)


def accumulate_uv(packet: dict) -> None:  # type: ignore[type-arg]
    """Feed the UV value from a loop packet into the ring buffer.

    Called by packet_tap for every loop packet.  Non-numeric and None values
    are silently skipped.  Must not modify the packet dict.
    """
    raw = packet.get("UV")
    if raw is None:
        return
    # Packets may be unit-converted dicts {value, label, formatted}.
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value is None:
        return
    try:
        _buffer.add(float(value))
    except (TypeError, ValueError):
        pass


def enrich_uv(data: dict) -> dict:  # type: ignore[type-arg]
    """Replace UV in the /current response with the 10-minute smoothed mean.

    Operates on the ``/current`` response envelope shape::

        {
            "data": {"UV": <raw_value>, ...},
            "units": {...},
            ...
        }

    Replacement rules:
    - Fewer than ``_MIN_SAMPLES`` buffered: raw UV passed through unchanged.
    - ``_MIN_SAMPLES`` or more buffered: smoothed mean rounded to 1 decimal.
    - Buffer is empty (all-null night, no UV sensor): UV set to ``None``.
    - ``data["data"]`` absent or not a dict: envelope returned unchanged.
    """
    obs = data.get("data")
    if not isinstance(obs, dict):
        return data

    smoothed = get_smoothed_uv()
    if smoothed is not None:
        obs["UV"] = smoothed
    elif _buffer.count >= _MIN_SAMPLES:
        # Enough samples but all summed to zero or buffer is genuinely empty.
        # get_smoothed_uv() returns None only when count < _MIN_SAMPLES or
        # buffer.count == 0; reaching here means count >= _MIN_SAMPLES and
        # the mean() call itself returned a value — this branch is unreachable
        # in practice, but guards against future API changes.
        obs["UV"] = None

    return data


def get_smoothed_uv() -> float | None:
    """Return the smoothed UV mean, or None if insufficient data.

    Returns:
        Smoothed mean rounded to 1 decimal place when at least
        ``_MIN_SAMPLES`` samples are buffered, otherwise ``None``.
    """
    if _buffer.count < _MIN_SAMPLES:
        return None
    try:
        return round(_buffer.mean(), 1)
    except ValueError:
        return None


def reset() -> None:
    """Clear the UV ring buffer.  For test isolation only."""
    _buffer.clear()
