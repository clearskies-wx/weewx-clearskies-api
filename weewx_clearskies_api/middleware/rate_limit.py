"""Per-IP rate-limit middleware.

Default: 60 requests per minute per IP for unauthenticated paths.
Bypassed when ProxyAuthMiddleware marks the request as "trusted" via the
request.state.proxy_trusted flag (set to True after a valid
X-Clearskies-Proxy-Auth header).

Storage backend: in-process cachetools TTLCache.
IMPORTANT: This is single-worker-only. A multi-worker deployment (multiple
uvicorn processes) will deliver N × the documented rate budget because each
worker maintains its own independent counter. Production multi-worker deploys
MUST use Redis per ADR-017 and security baseline §8. A future task will
add the Redis backend gated on CLEARSKIES_CACHE_URL at startup.
TODO (Task N — Redis rate-limit backend): when worker count > 1 and
CLEARSKIES_CACHE_URL is unset, log a CRITICAL warning at startup.

Returns RFC 9457 application/problem+json with status 429 and Retry-After
header per security baseline §3.1.
"""

from __future__ import annotations

import logging
import math
import time

import cachetools
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from weewx_clearskies_api.errors import PROBLEM_BASE_URI, build_problem_response

logger = logging.getLogger(__name__)

_DEFAULT_RPM = 60
_DEFAULT_WINDOW = 60  # seconds

# Only trust X-Forwarded-For when the direct TCP connection comes from one of
# these addresses.  Loopback covers Caddy-on-localhost and same-host Docker.
# An attacker connecting directly (not through the reverse proxy) cannot forge
# a privileged client IP via XFF — their direct IP won't be in this set.
_TRUSTED_PROXIES: frozenset[str] = frozenset({"127.0.0.1", "::1"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limiter with proxy-auth bypass.

    Uses a cachetools.TTLCache to track request counts per IP.
    Each cache entry is a list of timestamps within the window.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = _DEFAULT_RPM,
        window_seconds: int = _DEFAULT_WINDOW,
    ) -> None:
        super().__init__(app)
        self._rpm = requests_per_minute
        self._window = window_seconds
        # TTLCache size cap: 65536 unique IPs before LRU eviction.
        # TTL = window_seconds so stale IP entries are automatically expired.
        self._cache: cachetools.TTLCache[str, list[float]] = cachetools.TTLCache(
            maxsize=65536, ttl=window_seconds
        )
        # defaultdict wraps the cache so missing keys auto-initialize to [].
        # We must NOT use defaultdict directly here — we want the TTL expiry
        # from the TTLCache. Access pattern: get then set.

    def _get_client_ip(self, request: Request) -> str:
        """Extract the client IP, trusting X-Forwarded-For only from trusted proxies.

        XFF is only honoured when the direct TCP connection originates from a
        known proxy address (_TRUSTED_PROXIES — loopback by default, covering
        Caddy-on-localhost and same-host Docker deployments).

        If the connection arrives from any other address, XFF is ignored and the
        direct IP is used.  This prevents an external attacker from spoofing a
        privileged client IP via a crafted X-Forwarded-For header, which would
        otherwise let them bypass per-IP rate limiting.
        """
        direct_ip = request.client.host if request.client else "unknown"

        # Only trust X-Forwarded-For when the request arrives from a known proxy.
        if direct_ip in _TRUSTED_PROXIES:
            xff = request.headers.get("X-Forwarded-For", "").strip()
            if xff:
                # First entry is the original client IP.
                return xff.split(",")[0].strip()

        return direct_ip

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Bypass: request is trusted (valid proxy secret already verified).
        if getattr(request.state, "proxy_trusted", False):
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        now = time.monotonic()
        window_start = now - self._window

        timestamps: list[float] = self._cache.get(client_ip, [])
        # Keep only timestamps within the current window.
        timestamps = [ts for ts in timestamps if ts > window_start]

        if len(timestamps) >= self._rpm:
            # Oldest request in the window tells us when the window resets.
            retry_after = math.ceil(self._window - (now - timestamps[0]))
            logger.warning(
                "Rate limit exceeded",
                extra={"client_ip": client_ip, "retry_after": retry_after},
            )
            return self._rate_limited(request, retry_after)

        timestamps.append(now)
        self._cache[client_ip] = timestamps

        return await call_next(request)

    def _rate_limited(self, request: Request, retry_after: int) -> Response:
        response = build_problem_response(
            status=429,
            title="Too Many Requests",
            detail=(
                f"Rate limit of {self._rpm} requests per {self._window}s exceeded. "
                f"Retry after {retry_after} seconds."
            ),
            request=request,
            problem_type=f"{PROBLEM_BASE_URI}/rate-limit-exceeded",
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
