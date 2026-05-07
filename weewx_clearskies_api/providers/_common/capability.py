"""Provider capability registry (ADR-038 §4).

Each provider module exports a static CAPABILITY symbol of type
ProviderCapability.  wire_providers() is called once from __main__.py after
config-load to register the configured providers.

The registry is read by:
  - /capabilities endpoint to populate the providers list (ADR-038 §4)
  - canonicalFieldsAvailable computation (union of stock-column canonical
    fields + provider-supplied canonical fields per CapabilityRegistry schema)

Column registry vs. provider registry:
  DO NOT collapse into the same module.  The column registry (db/registry.py)
  is DB-backed (built from schema reflection at startup); the provider registry
  is config-backed (built from operator api.conf settings).  They serve different
  consumers (DB endpoints vs. provider endpoints) and have different update
  frequencies.  Keep them separate per the brief explicit instruction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderCapability:
    """Static capability declaration per ADR-038 §4.

    Each provider module exports one of these as its CAPABILITY symbol.
    Fields match the OpenAPI CapabilityDeclaration schema.
    """

    provider_id: str
    domain: str  # "forecast" | "alerts" | "aqi" | "earthquakes" | "radar"
    supplied_canonical_fields: tuple[str, ...]
    geographic_coverage: str  # "global" or operator-meaningful descriptor
    auth_required: tuple[str, ...] = field(default_factory=tuple)
    default_poll_interval_seconds: int = 300
    operator_notes: str | None = None


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_provider_registry: list[ProviderCapability] = []


def wire_providers(declarations: list[ProviderCapability]) -> None:
    """Register configured providers' capability declarations.

    Called once from __main__.py after config-load.  Tests may call directly
    with hand-built declarations.

    Args:
        declarations: List of ProviderCapability objects, one per configured
            provider.  Empty list → no providers configured; /alerts returns
            source="none" per ADR-016 §Out-of-scope.

    Raises:
        ValueError: Two providers share the same (domain, provider_id) pair.
    """
    global _provider_registry  # noqa: PLW0603

    # Sanity check: no two providers may share (domain, provider_id).
    seen: set[tuple[str, str]] = set()
    for d in declarations:
        key = (d.domain, d.provider_id)
        if key in seen:
            raise ValueError(
                f"Duplicate provider capability declaration: domain={d.domain!r}, "
                f"provider_id={d.provider_id!r}.  Each (domain, provider_id) pair "
                "may appear at most once."
            )
        seen.add(key)

    _provider_registry = list(declarations)
    logger.info(
        "Provider registry wired: %d provider(s)",
        len(_provider_registry),
        extra={"providers": [(d.domain, d.provider_id) for d in _provider_registry]},
    )


def get_provider_registry() -> list[ProviderCapability]:
    """Return the current provider registry.

    Returns an empty list before wire_providers() is called (safe to call
    before startup is complete — returns empty rather than raising).
    """
    return list(_provider_registry)


def reset_provider_registry_for_tests() -> None:
    """Reset module-level registry.  Used in tests only."""
    global _provider_registry  # noqa: PLW0603
    _provider_registry = []
