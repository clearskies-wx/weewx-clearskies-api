"""Shared provider infrastructure — public API re-exports.

Every provider module imports from here.  The common package owns:
  - Canonical error taxonomy (ADR-038 §5)
  - HTTP client wrapper with retry/backoff (ADR-038 §3)
  - Cache abstraction: MemoryCache + RedisCache (ADR-017)
  - Rate-limiter primitive (ADR-038 §3)
  - Capability registry (ADR-038 §4)
"""

from weewx_clearskies_api.providers._common.cache import (
    CacheBackend,
    ConfigError,
    MemoryCache,
    RedisCache,
    get_cache,
    reset_cache_for_tests,
    wire_cache_from_env,
)
from weewx_clearskies_api.providers._common.capability import (
    ProviderCapability,
    get_provider_registry,
    reset_provider_registry_for_tests,
    wire_providers,
)
from weewx_clearskies_api.providers._common.errors import (
    FieldUnsupported,
    GeographicallyUnsupported,
    KeyInvalid,
    ProviderError,
    ProviderProtocolError,
    QuotaExhausted,
    TransientNetworkError,
)
from weewx_clearskies_api.providers._common.http import ProviderHTTPClient
from weewx_clearskies_api.providers._common.rate_limiter import RateLimiter

__all__ = [
    # errors
    "ProviderError",
    "QuotaExhausted",
    "KeyInvalid",
    "GeographicallyUnsupported",
    "FieldUnsupported",
    "TransientNetworkError",
    "ProviderProtocolError",
    # http
    "ProviderHTTPClient",
    # cache
    "CacheBackend",
    "MemoryCache",
    "RedisCache",
    "ConfigError",
    "get_cache",
    "wire_cache_from_env",
    "reset_cache_for_tests",
    # rate_limiter
    "RateLimiter",
    # capability
    "ProviderCapability",
    "get_provider_registry",
    "wire_providers",
    "reset_provider_registry_for_tests",
]
