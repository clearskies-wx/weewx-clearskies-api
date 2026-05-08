"""Canonical provider exception taxonomy (ADR-038 §5).

Provider modules raise from this hierarchy; no upstream exception type
(httpx, redis, etc.) leaks out of a provider module into the rest of the
codebase.  All translation from upstream exceptions to canonical taxonomy
happens inside the calling provider module or in ProviderHTTPClient.

HTTP status mapping (wired in weewx_clearskies_api/errors.py exception handler):
  QuotaExhausted           → 503 + Retry-After header
  GeographicallyUnsupported→ 503
  KeyInvalid               → 502
  FieldUnsupported         → 502
  TransientNetworkError    → 502
  ProviderProtocolError    → 502 (log at ERROR for triage)
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for the canonical provider taxonomy.

    Subclasses must NOT carry upstream provider exception types.
    All translation happens in the calling provider module.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        domain: str,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.domain = domain
        self.retry_after_seconds = retry_after_seconds
        # status_code is set by ProviderHTTPClient when the exception comes from
        # an HTTP boundary (4xx/5xx).  Provider modules that need to dispatch
        # on HTTP status (e.g. forecast/nws translates /points 404 →
        # GeographicallyUnsupported) should match against `exc.status_code`,
        # NOT against the human-readable message string.  None when raised
        # outside an HTTP context (e.g. wire-shape validation).
        self.status_code = status_code


class QuotaExhausted(ProviderError):
    """Rate-limit or daily-cap exceeded; transient, retry after backoff.

    Maps to HTTP 503 + Retry-After header.
    """


class KeyInvalid(ProviderError):
    """Auth-time failure; permanent until operator updates config.

    Maps to HTTP 502.
    NWS is keyless so this only fires on a hypothetical UA-block (exotic).
    The path lands for free with the canonical taxonomy.
    """


class GeographicallyUnsupported(ProviderError):
    """Provider doesn't cover operator's location.

    Maps to HTTP 503.
    NWS returns empty features for non-US locations (200 + empty list),
    so this class is reserved for providers that explicitly reject the request.
    """


class FieldUnsupported(ProviderError):
    """Provider doesn't supply the requested data type.

    Maps to HTTP 502.
    """


class TransientNetworkError(ProviderError):
    """DNS/TCP/TLS/5xx; retry with backoff.

    Maps to HTTP 502.
    Raised after all retry attempts are exhausted.
    """


class ProviderProtocolError(ProviderError):
    """Response format unexpected (provider changed API silently).

    Maps to HTTP 502. Logged at ERROR for operator triage.
    """
