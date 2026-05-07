"""Module-level ColumnRegistry holder for FastAPI DI (ADR-012, ADR-035).

The registry is populated once at startup via wire_registry() and accessed
in endpoints via get_registry() (as a FastAPI dependency or direct call).

Kept separate from session.py so the engine wiring and the registry wiring
don't create circular imports.
"""

from __future__ import annotations

from weewx_clearskies_api.db.reflection import ColumnRegistry

# Set by wire_registry() during startup — never directly by endpoint code.
_registry: ColumnRegistry | None = None


def wire_registry(registry: ColumnRegistry) -> None:
    """Register the ColumnRegistry for use by get_registry.

    Called once from __main__.py after schema reflection completes.
    Tests may call this with a hand-built registry.
    """
    global _registry  # noqa: PLW0603
    _registry = registry


def get_registry() -> ColumnRegistry:
    """Return the registered ColumnRegistry.

    Raises:
        RuntimeError: Registry has not been wired (startup sequence bug).
    """
    if _registry is None:
        raise RuntimeError(
            "Column registry is not initialised. "
            "wire_registry() must be called before the first request. "
            "This is a startup-sequence bug — check __main__.py."
        )
    return _registry
