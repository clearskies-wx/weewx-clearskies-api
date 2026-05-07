"""Provider response cache abstraction with memory and Redis backends (ADR-017).

Backend selection at startup:
  - CLEARSKIES_CACHE_URL unset → MemoryCache (default)
  - CLEARSKIES_CACHE_URL=redis://... → RedisCache
  - Other URI scheme → ConfigError (operator typo → fail-closed)

Per-provider TTL (ADR-017 §Per-provider TTL declaration):
  ADR-017 mandates per-provider TTL.  cachetools.TTLCache requires a single TTL
  per-instance.  MemoryCache works around this by storing (value, expires_at_epoch)
  tuples alongside a long-lived outer TTLCache (TTL=86400s as a safety net).
  At get() time, we check expires_at_epoch against the current time — if elapsed,
  treat as a miss and let the outer TTLCache evict naturally via its own LRU.
  This gives us per-entry TTL semantics at the cost of ~20 LOC.  The approach
  is documented here so future readers don't mistake the 86400s TTL for a design
  choice; it's a container ceiling, not the actual TTL used.

Redis backend uses SET key value EX <ttl_seconds> for native per-key expiry.
Values are JSON-encoded (canonical AlertRecord lists are JSON-serialisable;
future provider domains stick to JSON-friendly shapes per the brief).

Redis fail-closed at startup (ADR-017 §Decision, security-baseline §3.1):
  RedisCache.__init__ calls ping(); unreachable Redis at startup → CRITICAL log
  + exception propagated → __main__.py catches and exits non-zero.
  Same pattern as the read-only DB user probe at startup (ADR-012).

NOT a request-result cache:
  This is the provider response cache per ADR-017 — keyed on
  (provider_id, endpoint, params), populated by the provider module after
  calling the upstream API, consumed before making the upstream call.
  The FastAPI handler layer is unaware of caching.

Cache key construction lives in the provider module, not here:
  import hashlib, json
  key = hashlib.sha256(json.dumps({...}, sort_keys=True).encode()).hexdigest()
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Protocol

from cachetools import TTLCache

# ConfigError is defined in config.settings so it's importable from both the
# cache module and from config.settings directly.  Tests import it from
# config.settings per their test-author's import path.
from weewx_clearskies_api.config.settings import ConfigError  # noqa: F401 (re-exported)

logger = logging.getLogger(__name__)

# Re-export so callers can do:
#   from weewx_clearskies_api.providers._common.cache import ConfigError
# OR:
#   from weewx_clearskies_api.config.settings import ConfigError
__all__ = ["CacheBackend", "MemoryCache", "RedisCache", "ConfigError", "wire_cache_from_env", "get_cache", "reset_cache_for_tests"]


# ---------------------------------------------------------------------------
# Protocol — duck-typed interface (ADR-038 §B rejected ABCs)
# ---------------------------------------------------------------------------


class CacheBackend(Protocol):
    """Protocol implemented by MemoryCache and RedisCache.

    Provider modules type-hint against this protocol so the backend can
    be swapped without changing provider code (ADR-017 §Decision).
    """

    def get(self, key: str) -> Any | None:
        """Return cached value or None on miss/expiry."""
        ...

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store value with per-entry TTL."""
        ...


# ---------------------------------------------------------------------------
# Memory backend (default — ADR-017 §Pluggable)
# ---------------------------------------------------------------------------


class MemoryCache:
    """In-process LRU+TTL cache backed by cachetools.TTLCache.

    Default backend per ADR-017.  Per ADR-017 §Worker-count guidance:
    multi-worker deploys SHOULD switch to RedisCache to avoid burning
    operator API quotas N-times-over.

    Per-entry TTL pattern (see module docstring for rationale):
      We store (value, expires_at_epoch) tuples inside a TTLCache with a
      large ceiling TTL (86400s).  get() checks the inner expires_at_epoch;
      if elapsed, returns None (cache miss) — the TTLCache will evict the
      stale tuple on its own clock or at next access.
    """

    def __init__(self, *, max_size: int = 1000) -> None:
        # ceiling TTL = 1 day; actual per-entry TTL is enforced via expires_at.
        # This avoids holding stale entries indefinitely if set() is never called
        # again for a key.
        self._cache: TTLCache[str, tuple[Any, float]] = TTLCache(
            maxsize=max_size,
            ttl=86400,
        )

    def get(self, key: str) -> Any | None:
        """Return the cached value if present and not expired; else None."""
        entry = self._cache.get(key)
        if entry is None:
            logger.debug("Cache miss: %s", key)
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            logger.debug("Cache expired: %s", key)
            # The outer TTLCache will handle eviction on its schedule.
            return None
        logger.debug("Cache hit: %s", key)
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store value with per-entry TTL."""
        expires_at = time.monotonic() + ttl_seconds
        self._cache[key] = (value, expires_at)
        logger.debug("Cache set: %s ttl=%ds", key, ttl_seconds)


# ---------------------------------------------------------------------------
# Redis backend (optional — ADR-017 §Pluggable)
# ---------------------------------------------------------------------------


class RedisCache:
    """Redis-backed cache. Activates when CLEARSKIES_CACHE_URL is set.

    Operator points at a Redis server via env var:
        CLEARSKIES_CACHE_URL=redis://localhost:6379/0

    Multi-worker deploys must use this backend per ADR-017 §Worker-count
    guidance — in-process MemoryCache is per-worker, so N workers each
    maintain a separate cache and burn N× the provider API quota.

    Values serialise via JSON (canonical AlertRecord lists are
    JSON-serialisable; future provider domains must stick to JSON-friendly
    shapes to use this backend).

    Per-key TTL via Redis's native EXPIRE (set with EX seconds).
    decode_responses=False: we control encoding (binary keys + JSON bytes).
    """

    def __init__(self, *, url: str) -> None:
        try:
            import redis as redis_lib  # lazy import — only needed for Redis backend
        except ImportError as exc:
            raise ImportError(
                "redis-py is required for the Redis cache backend. "
                "Install it with: pip install redis"
            ) from exc

        self._client = redis_lib.Redis.from_url(url, decode_responses=False)
        # Verify connectivity at construction; fail fast on misconfig.
        # This is the fail-closed startup pattern (ADR-012 spirit applied to cache).
        try:
            self._client.ping()
        except Exception as exc:
            # Catch broad here because redis-py raises different exception types
            # depending on whether the server is unreachable, auth fails, etc.
            # (ConnectionError, ResponseError, AuthenticationError, etc.)
            # We translate to a single clear message for __main__.py to log+exit.
            raise RuntimeError(
                f"Redis ping failed for CLEARSKIES_CACHE_URL={url!r}: {exc}. "
                "Verify the Redis server is reachable and the URL is correct."
            ) from exc
        logger.info("Redis cache connected: %s", url)

    def get(self, key: str) -> Any | None:
        """Return cached value or None on miss/expiry."""
        raw = self._client.get(key.encode())
        if raw is None:
            logger.debug("Cache miss: %s", key)
            return None
        try:
            value = json.loads(raw)
            logger.debug("Cache hit: %s", key)
            return value
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Cache JSON decode error for key %s: %s", key, exc)
            return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store value with per-entry TTL via Redis native EXPIRE."""
        raw = json.dumps(value)
        self._client.set(key.encode(), raw.encode(), ex=ttl_seconds)
        logger.debug("Cache set: %s ttl=%ds", key, ttl_seconds)


# ---------------------------------------------------------------------------
# Module-level singleton and wire function
# ---------------------------------------------------------------------------

_cache: CacheBackend | None = None


def wire_cache_from_env() -> None:
    """Construct the cache backend from CLEARSKIES_CACHE_URL.

    Unset → MemoryCache().
    Set with redis://... or rediss://... → RedisCache(url=...).
    Other URI scheme → ConfigError (operator typo — fail-closed).

    Called from __main__.py before uvicorn starts, after settings load.
    """
    global _cache  # noqa: PLW0603
    url = os.environ.get("CLEARSKIES_CACHE_URL", "").strip()
    if not url:
        _cache = MemoryCache()
        logger.info("Cache backend: MemoryCache (single-worker default)")
        return
    if url.startswith("redis://") or url.startswith("rediss://"):
        _cache = RedisCache(url=url)
        logger.info("Cache backend: RedisCache url=%s", url)
        return
    raise ConfigError(
        f"Unsupported CLEARSKIES_CACHE_URL scheme: {url!r}. "
        "Supported schemes: redis://, rediss://. "
        "Unset CLEARSKIES_CACHE_URL to use the in-process MemoryCache."
    )


def get_cache() -> CacheBackend:
    """Return the wired cache backend.

    Raises:
        RuntimeError: wire_cache_from_env() has not been called yet.
    """
    if _cache is None:
        raise RuntimeError(
            "Cache not initialised. "
            "Call wire_cache_from_env() at startup before serving requests."
        )
    return _cache


def reset_cache_for_tests() -> None:
    """Reset module-level cache singleton.  Used in tests only."""
    global _cache  # noqa: PLW0603
    _cache = None
