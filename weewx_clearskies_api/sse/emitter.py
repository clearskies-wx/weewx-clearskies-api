"""SSE emitter — broadcast pattern for multiple concurrent SSE clients.

One asyncio.Queue receives loop packets from the active input adapter.
A fan-out background task reads from that queue and copies each packet
into every connected client's private subscriber queue.

Each SSE client (GET /sse) gets its own subscriber queue created on connect
and removed on disconnect.  Clients that fall behind and overflow their queue
are silently dropped (nowait put; overflow = drop).

Event format:
  event: loop
  data: <JSON-serialised loop-packet dict>

Ported from weewx-clearskies-realtime (ADR-058).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Subscriber queues larger than this are considered stalled; new packets
# are dropped for that subscriber rather than blocking the fan-out task.
_SUBSCRIBER_QUEUE_MAX = 64

# Maximum number of concurrent SSE subscribers.  Connections beyond this limit
# receive a 503 response (handled in the endpoint layer).  Prevents an attacker
# from exhausting memory with unbounded subscriber queues.
_MAX_SUBSCRIBERS = 500

# ADR-002: SSE keepalive comment emitted when no real event arrives within
# this window.  Prevents corporate proxies and mobile-network NAT from
# treating idle SSE connections as dead and closing them.
KEEPALIVE_INTERVAL_SECONDS = 15


class SSEEmitter:
    """Fan-out from a single source queue to N subscriber queues."""

    def __init__(
        self,
        source: asyncio.Queue[dict[str, Any]],
        *,
        on_packet: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._source = source
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._fanout_task: asyncio.Task[None] | None = None
        # Optional side-effect callback invoked for every packet before fan-out.
        # Used to feed the enrichment packet-tap (process_packet).
        self._on_packet = on_packet

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the fan-out background task on the running event loop."""
        self._fanout_task = asyncio.get_running_loop().create_task(
            self._fanout(), name="sse-fanout"
        )
        logger.info("SSE fan-out task started")

    def stop(self) -> None:
        """Cancel the fan-out task and notify all subscribers to disconnect."""
        if self._fanout_task and not self._fanout_task.done():
            self._fanout_task.cancel()
            logger.info("SSE fan-out task cancelled")
        # Signal every subscriber to close its generator cleanly.
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)  # sentinel

    # ------------------------------------------------------------------
    # Subscriber management
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        """Create and register a new subscriber queue for one SSE client.

        Raises:
            RuntimeError: When the subscriber count is at or above _MAX_SUBSCRIBERS.
        """
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            raise RuntimeError("Too many SSE subscribers")
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        logger.debug("SSE subscriber added", extra={"total": len(self._subscribers)})
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Remove a subscriber queue when its client disconnects."""
        self._subscribers.discard(q)
        logger.debug("SSE subscriber removed", extra={"total": len(self._subscribers)})

    # ------------------------------------------------------------------
    # SSE event generator (one per connected client)
    # ------------------------------------------------------------------

    async def event_generator(
        self, q: asyncio.Queue[dict[str, Any] | None]
    ) -> AsyncIterator[dict[str, str]]:
        """Async generator that yields SSE events from a subscriber queue.

        Yields dicts expected by sse-starlette's EventSourceResponse:
          {"event": "loop", "data": "<json>"}   — real loop-packet event
          {"comment": "keepalive"}               — idle keepalive (ADR-002)

        Stops when a None sentinel is received (client disconnect or shutdown).
        """
        try:
            while True:
                try:
                    packet = await asyncio.wait_for(
                        q.get(), timeout=KEEPALIVE_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    # No packet arrived within the keepalive window.  Emit an
                    # SSE comment so proxies and mobile networks do not treat
                    # the idle connection as dead and close it (ADR-002).
                    yield {"comment": "keepalive"}
                    continue
                if packet is None:
                    # Sentinel — emitter is shutting down or client disconnected.
                    break
                yield {"event": "loop", "data": json.dumps(packet, default=str)}
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Fan-out task
    # ------------------------------------------------------------------

    async def _fanout(self) -> None:
        """Read packets from the source queue and copy to all subscriber queues."""
        while True:
            try:
                packet = await self._source.get()
            except asyncio.CancelledError:
                break

            # Invoke the optional packet callback (e.g., enrichment tap) before
            # broadcasting.  Failures are logged and do not interrupt fan-out.
            if self._on_packet is not None:
                try:
                    self._on_packet(packet)
                except Exception:  # noqa: BLE001
                    logger.exception("Packet callback failed")

            if not self._subscribers:
                continue

            stalled: list[asyncio.Queue[dict[str, Any] | None]] = []
            for sub_q in list(self._subscribers):
                try:
                    sub_q.put_nowait(packet)
                except asyncio.QueueFull:
                    # Subscriber's buffer is full — it's too slow.  Drop and log.
                    stalled.append(sub_q)
                    logger.warning(
                        "SSE subscriber queue full; dropping packet for stalled client"
                    )

            # Remove stalled subscribers so they don't keep piling up dropped packets.
            for sub_q in stalled:
                self.unsubscribe(sub_q)
