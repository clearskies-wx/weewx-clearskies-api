"""Packet processor registry — side-effect callbacks for every loop packet.

Processors accumulate state (e.g., ring-buffer samples) but do NOT modify
the packet dict.  Each processor failure is logged and skipped so one broken
enrichment cannot stall the fan-out pipeline.

Usage::

    from weewx_clearskies_api.sse.packet_tap import (
        register_processor,
        process_packet,
    )

    def _accumulate(packet: dict) -> None:
        wind_buf.add(packet.get("windSpeed", 0.0))

    register_processor(_accumulate)

Ported from weewx-clearskies-realtime (ADR-058).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_processors: list[Callable[[dict], None]] = []  # type: ignore[type-arg]


def register_processor(fn: Callable[[dict], None]) -> None:  # type: ignore[type-arg]
    """Register a function to be called for every loop packet.

    The function receives the raw packet dict.  It must not modify it.
    """
    _processors.append(fn)


def clear_processors() -> None:
    """Remove all registered processors.

    Intended for use in tests only — clears module-level state between runs.
    """
    _processors.clear()


def process_packet(packet: dict) -> None:  # type: ignore[type-arg]
    """Invoke all registered processors with the given loop packet.

    Failures are logged at ERROR level and execution continues with the
    remaining processors.  A single broken processor does not stop the others.
    """
    for fn in _processors:
        try:
            fn(packet)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Packet processor raised an exception",
                extra={"processor": getattr(fn, "__name__", repr(fn))},
            )
