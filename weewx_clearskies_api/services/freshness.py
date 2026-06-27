"""Data freshness envelope builder (ADR-075 §4).

Computes the freshness block for cacheable API responses.
Each domain has a configured refresh interval; the freshness block
tells the dashboard when to refetch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from weewx_clearskies_api.models.responses import FreshnessInfo, utc_isoformat

# Module-level reference to FreshnessSettings, set at startup.
_settings = None


def configure(settings) -> None:
    """Store the FreshnessSettings reference. Called once at startup."""
    global _settings  # noqa: PLW0603
    _settings = settings


def build_freshness(
    domain: str,
    provider_refresh_interval: int | None = None,
) -> FreshnessInfo:
    """Build a FreshnessInfo block for the given domain.

    Args:
        domain: One of the domain keys from FreshnessSettings
                (e.g., "current_observation", "forecast", "alerts").
        provider_refresh_interval: Optional provider-specific interval.
                When supplied, the effective interval is
                min(config_default, provider_refresh_interval).

    Returns:
        FreshnessInfo with generatedAt, validUntil, and refreshInterval.
    """
    # Fallback before configure() is called (shouldn't happen in prod)
    config_interval = 300 if _settings is None else getattr(_settings, domain, 300)

    if provider_refresh_interval is not None:
        effective_interval = min(config_interval, provider_refresh_interval)
    else:
        effective_interval = config_interval

    now = datetime.now(tz=UTC)
    valid_until = now + timedelta(seconds=effective_interval)

    return FreshnessInfo(
        generatedAt=utc_isoformat(now),
        validUntil=utc_isoformat(valid_until),
        refreshInterval=effective_interval,
    )
