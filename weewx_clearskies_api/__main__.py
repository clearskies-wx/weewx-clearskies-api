"""Entry point for weewx-clearskies-api.

Run with:
    python -m weewx_clearskies_api
    weewx-clearskies-api  (via pyproject.toml scripts entry point)

IPv4/IPv6 dual-stack listener (coding.md §1, ADR-037):
    Default bind = 127.0.0.1 per ADR-037 (loopback, behind the reverse proxy).
    When operator sets [api] bind_host to a non-loopback address, we resolve
    via socket.getaddrinfo to get the full (family, address) set and start
    one uvicorn Server per (family, addr) pair.

    We never use gethostbyname — it is IPv4-only and violates coding.md §1.
    We use ipaddress.ip_address to validate the bind_host only when it looks
    like a bare IP; hostnames are passed to getaddrinfo directly.

Startup warning for cross-host without proxy secret (ADR-008):
    When bind_host is non-loopback and WEEWX_CLEARSKIES_PROXY_SECRET is unset,
    emit a loud WARNING at startup (and schedule a repeat every 60 s).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import threading
import time

import uvicorn

from weewx_clearskies_api.app import create_app
from weewx_clearskies_api.config.settings import Settings, load_settings
from weewx_clearskies_api.health import create_health_app
from weewx_clearskies_api.logging.setup import setup_logging

logger = logging.getLogger(__name__)

_LOOPBACK_PREFIXES = ("127.", "::1", "localhost")


def _is_loopback(host: str) -> bool:
    """Return True if host is a loopback address (IPv4 or IPv6)."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        # Hostname — check well-known loopback names.
        return host in ("localhost",)


def _warn_non_loopback_loop(host: str, interval: int = 60) -> None:
    """Log a loud warning every `interval` seconds when bound non-loopback
    without WEEWX_CLEARSKIES_PROXY_SECRET set (ADR-008).

    Runs in a daemon thread — stops automatically when the main process exits.
    """
    while True:
        time.sleep(interval)
        logger.warning(
            "clearskies-api is bound to a non-loopback address (%s) without "
            "WEEWX_CLEARSKIES_PROXY_SECRET set. Any host that can reach this address "
            "can read this service directly, bypassing your reverse proxy. "
            "See SECURITY.md for the recommended cross-host config.",
            host,
        )


def _resolve_bind_addresses(host: str, port: int) -> list[tuple[str, int]]:
    """Resolve host to all (address, port) pairs via getaddrinfo (coding.md §1).

    Returns a list of (ip_address_string, port) tuples — one per address
    family resolved. For "127.0.0.1" this returns [("127.0.0.1", port)].
    For "localhost" this typically returns both ("127.0.0.1", port) and
    ("::1", port) on dual-stack systems.
    """
    results: list[tuple[str, int]] = []
    try:
        for family, _type, _proto, _cname, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            ip_str = sockaddr[0]
            if (ip_str, port) not in results:
                results.append((ip_str, port))
    except socket.gaierror as exc:
        logger.error("Failed to resolve bind address %r: %s", host, exc)
        raise

    return results


def _run_server(settings: Settings) -> None:
    """Start the public API and health servers.

    Public API: one uvicorn Server per resolved (family, addr) from [api] bind_host.
    Health API: one uvicorn Server per resolved (family, addr) from [health] bind_host.

    Both run concurrently via asyncio.gather in the main thread.
    """
    app = create_app(settings)
    health_app = create_health_app()

    api_addresses = _resolve_bind_addresses(settings.api.bind_host, settings.api.bind_port)
    health_addresses = _resolve_bind_addresses(
        settings.health.bind_host, settings.health.bind_port
    )

    # Cross-host without proxy secret warning (ADR-008).
    if not _is_loopback(settings.api.bind_host):
        proxy_secret = os.environ.get("WEEWX_CLEARSKIES_PROXY_SECRET", "").strip()
        if not proxy_secret:
            logger.warning(
                "clearskies-api is bound to a non-loopback address (%s) without "
                "WEEWX_CLEARSKIES_PROXY_SECRET set. Any host that can reach this address "
                "can read this service directly, bypassing your reverse proxy. "
                "See SECURITY.md for the recommended cross-host config.",
                settings.api.bind_host,
            )
            t = threading.Thread(
                target=_warn_non_loopback_loop,
                args=(settings.api.bind_host,),
                daemon=True,
            )
            t.start()

    log_level = settings.logging.level.lower()

    # Build uvicorn configs for each bind address.
    api_configs = [
        uvicorn.Config(app, host=addr, port=port, log_level=log_level, access_log=False)
        for addr, port in api_addresses
    ]
    health_configs = [
        uvicorn.Config(health_app, host=addr, port=port, log_level=log_level, access_log=False)
        for addr, port in health_addresses
    ]

    all_configs = api_configs + health_configs

    logger.info(
        "Starting weewx-clearskies-api",
        extra={
            "api_addresses": api_addresses,
            "health_addresses": health_addresses,
        },
    )

    async def _serve_all() -> None:
        servers = [uvicorn.Server(cfg) for cfg in all_configs]
        await asyncio.gather(*[server.serve() for server in servers])

    asyncio.run(_serve_all())


def main() -> None:
    """Main entry point."""
    # Bootstrap logging before anything else so config-load errors are JSON.
    setup_logging("INFO")

    settings = load_settings()
    setup_logging(settings.logging.level)

    _run_server(settings)


if __name__ == "__main__":
    main()
