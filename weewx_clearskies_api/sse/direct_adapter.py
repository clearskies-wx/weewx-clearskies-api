"""Direct-mode input adapter — ADR-058.

Connects to the Unix domain socket served by ClearSkiesLoopRelay (weewx_ext.py),
reads newline-terminated JSON lines, and pushes loop-packet dicts into an
asyncio.Queue for the SSE emitter to broadcast.

Auto-reconnects with exponential backoff (1 s -> 2 s -> 4 s -> ... -> 120 s max)
on connection loss or socket-not-yet-present (FileNotFoundError).

Does NOT import weewx — this file runs in the API service process.

Ported from weewx-clearskies-realtime adapters/direct.py (ADR-058).
Adapted: DirectSettings -> InputSettings; realtime health import -> API health.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from weewx_clearskies_api.config.settings import InputSettings
from weewx_clearskies_api.health import ProbeResult

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 120.0


class DirectAdapter:
    """Asyncio Unix-socket reader that fans received loop packets into an asyncio.Queue.

    Accepts InputSettings (the API's settings class) rather than the realtime
    service's DirectSettings.  All reconnection logic is preserved exactly.
    """

    def __init__(self, settings: InputSettings, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._settings = settings
        self._queue = queue
        self._connected = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Schedule the connection task on *loop*.

        Must be called from within a running asyncio event loop context.
        """
        self._stop_event.clear()
        self._task = loop.create_task(self._run(), name="clearskies-direct-adapter")

    def stop(self) -> None:
        """Signal the connection task to stop and cancel it."""
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    def health_probe(self) -> ProbeResult:
        """Readiness probe registered with the health sub-system."""
        if self._connected:
            return ProbeResult(name="direct", status="ok", messages=[])
        return ProbeResult(
            name="direct",
            status="warning",
            messages=[f"Not connected to weewx relay socket {self._settings.socket_path}"],
        )

    # ------------------------------------------------------------------
    # Internal — connection loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Connect, read packets, and reconnect on failure until stopped."""
        backoff = _BACKOFF_BASE
        while not self._stop_event.is_set():
            try:
                await self._connect_and_read()
                # _connect_and_read returns only when the connection closes
                # cleanly (EOF).  Treat as a reconnect trigger.
                if not self._stop_event.is_set():
                    logger.warning(
                        "Weewx relay socket closed (EOF); reconnecting in %.0fs", backoff
                    )
                    self._connected = False
            except FileNotFoundError:
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "Relay socket %s not found; weewx may not be running yet. Retrying in %.0fs",
                    self._settings.socket_path,
                    backoff,
                )
                self._connected = False
            except (ConnectionResetError, ConnectionRefusedError, OSError) as exc:
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "Lost connection to relay socket: %s. Reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                self._connected = False
            except asyncio.CancelledError:
                break

            if self._stop_event.is_set():
                break

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._stop_event.wait()), timeout=backoff)

            backoff = min(backoff * 2, _BACKOFF_MAX)

        self._connected = False
        logger.info("DirectAdapter stopped")

    async def _connect_and_read(self) -> None:
        """Open the Unix socket, mark connected, and read until EOF."""
        reader, writer = await asyncio.open_unix_connection(self._settings.socket_path)
        self._connected = True
        logger.info("Connected to weewx relay socket %s", self._settings.socket_path)

        try:
            while not self._stop_event.is_set():
                raw = await reader.readline()
                if not raw:
                    # EOF -- weewx side closed the connection.
                    break
                self._handle_line(raw)
        finally:
            self._connected = False
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _handle_line(self, raw: bytes) -> None:
        """Parse one JSON line and push to the queue; skip malformed input."""
        line = raw.decode(errors="replace").strip()
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Malformed JSON from relay socket -- skipping", extra={"error": str(exc)}
            )
            return

        if not isinstance(payload, dict):
            logger.warning(
                "Relay socket payload is not a JSON object -- skipping",
                extra={"type": type(payload).__name__},
            )
            return

        self._queue.put_nowait(payload)
