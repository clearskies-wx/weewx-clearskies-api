"""Per-provider sliding-window rate limiter (ADR-038 §3).

Provider modules instantiate one RateLimiter at module-load time.
ProviderHTTPClient (or the provider's fetch() function) calls acquire()
before each outbound request.

NWS configuration: RateLimiter("nws", max_calls=5, window_seconds=1) as a
"be polite" guard.  With 5-min cache TTL and single-worker default, we make
roughly 1 request per 5 minutes per station — this limit never trips in
normal use.

Thread-safety note: sync FastAPI / sync uvicorn worker doesn't multiplex
inside a single worker, so the deque is single-threaded by construction.
No lock is needed for the default deployment.  If an async execution path
lands in a future round, revisit.

Multi-worker note: like the memory cache, this is per-worker state.  With
N workers, the effective rate limit is N × max_calls per window.  For NWS
at 5 req/s limit and ~1 req per 5 min cache TTL, this is irrelevant at any
sane worker count.
"""

from __future__ import annotations

import collections
import time

from weewx_clearskies_api.providers._common.errors import QuotaExhausted


class RateLimiter:
    """In-process sliding-window rate limiter.

    Provider modules instantiate one at module-load time; the provider's
    fetch() function calls acquire() before each outbound call.  On limit
    exceeded, raises QuotaExhausted with retry_after_seconds set to the time
    until the earliest call ages out of the window.
    """

    def __init__(
        self,
        *,
        name: str,
        max_calls: int,
        window_seconds: int,
        provider_id: str = "",
        domain: str = "",
    ) -> None:
        self._name = name
        self._provider_id = provider_id
        self._domain = domain
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        # deque of monotonic timestamps of recent calls
        self._calls: collections.deque[float] = collections.deque()

    def acquire(self) -> None:
        """Check rate limit and raise QuotaExhausted if exceeded.

        Implementation: deque of recent call timestamps.  At acquire time,
        pop expired entries (older than window_seconds ago), then check count.
        If count >= max_calls, raise QuotaExhausted with retry_after set to
        the time until the earliest call ages out.
        """
        now = time.monotonic()
        cutoff = now - self._window_seconds

        # Evict timestamps older than the window.
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

        if len(self._calls) >= self._max_calls:
            # Earliest call in window; that's when a slot opens up.
            retry_after = int(self._window_seconds - (now - self._calls[0])) + 1
            raise QuotaExhausted(
                f"Rate limit exceeded for {self._name}: "
                f"{self._max_calls} calls per {self._window_seconds}s",
                provider_id=self._provider_id,
                domain=self._domain,
                retry_after_seconds=max(1, retry_after),
            )

        self._calls.append(now)
