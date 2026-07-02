"""Locale loading, string lookup, and number formatting (I18N T3.1).

Loads translation dictionaries from ``weewx_clearskies_api/locales/*.json``
into memory and exposes lookup helpers used by the enrichment pipeline and
endpoint handlers to render translated strings.

This module only provides the infrastructure — loading locale files and
looking up strings. Wiring it into startup (loading the operator's
configured locale) and threading it through endpoint responses are separate
tasks (T3.5 and later). Nothing in this module is imported by the running
application yet.

Resolution order for both ``t()`` and ``t_case()``: requested locale ->
``"en"`` fallback -> the key itself (so a missing translation never raises
and never renders blank).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from babel.numbers import format_decimal

# ---------------------------------------------------------------------------
# Module-level storage
# ---------------------------------------------------------------------------

_locales: dict[str, dict[str, Any]] = {}
_active_locale: str = "en"


# ---------------------------------------------------------------------------
# Loading and active-locale management
# ---------------------------------------------------------------------------


def load_locales(locale_dir: Path | None = None) -> None:
    """Load all locale JSON files from *locale_dir* into memory.

    Defaults to the ``locales/`` directory bundled alongside this module.
    Each file's stem (e.g. ``"en"``, ``"de"``, ``"pt-BR"``) becomes its
    locale code. Re-running this replaces any previously loaded locales.
    """
    if locale_dir is None:
        locale_dir = Path(__file__).parent / "locales"

    for path in sorted(locale_dir.glob("*.json")):
        locale_code = path.stem
        with path.open(encoding="utf-8") as f:
            _locales[locale_code] = json.load(f)


def set_active_locale(locale: str) -> None:
    """Set the active locale for all subsequent unscoped lookups.

    Falls back to ``"en"`` when *locale* has not been loaded.
    """
    global _active_locale  # noqa: PLW0603
    _active_locale = locale if locale in _locales else "en"


def get_active_locale() -> str:
    """Return the currently active locale code."""
    return _active_locale


# ---------------------------------------------------------------------------
# String lookup
# ---------------------------------------------------------------------------


def _ensure_locales_loaded() -> None:
    """Lazily populate ``_locales`` on first lookup if nothing has loaded yet.

    Production startup (``__main__.py``, I18N T3.5) calls ``load_locales()``
    explicitly before serving any request. Callers that resolve strings
    outside that startup sequence — unit tests that exercise a single
    function directly, or any module-level code that runs before app
    startup — would otherwise see every lookup fall through to the raw key
    (``_locales`` empty -> no ``"en"`` entry -> key returned as-is).
    Loading is idempotent and cheap (13 small JSON files), so calling it
    defensively here is safe; a later explicit ``load_locales()`` call at
    startup simply repopulates the same dict.
    """
    if not _locales:
        load_locales()


def t(key: str, locale: str | None = None) -> str:
    """Look up a translated string by dot-separated *key*.

    Resolution: *locale* (or the active locale) -> ``"en"`` fallback -> the
    key itself. Key format: ``"beaufort.0"``, ``"temperature.cold"``,
    ``"composition.connector_and"``.

    An empty string (the placeholder value in not-yet-translated locale
    skeletons) is treated as "not translated" and falls through to the next
    stage in the resolution chain — it is never returned as-is.
    """
    _ensure_locales_loaded()
    loc = locale or _active_locale

    value = _resolve_key(_locales.get(loc, {}), key)
    if isinstance(value, str) and value:
        return value

    if loc != "en":
        value = _resolve_key(_locales.get("en", {}), key)
        if isinstance(value, str) and value:
            return value

    return key


def t_case(key: str, case: str = "nominative", locale: str | None = None) -> str:
    """Look up a translated string with grammatical case support (for Russian).

    When the value at *key* is a dict of case -> string (e.g.
    ``{"nominative": "...", "genitive": "..."}``), returns the requested
    *case*, falling back to ``"nominative"`` when the requested case is
    absent. When the value is a plain string, returns it regardless of
    *case*. Falls back to English, then to the key itself, using the same
    case-or-string handling at each stage.

    As with :func:`t`, an empty string is treated as "not translated" and
    falls through to the next stage.
    """
    _ensure_locales_loaded()
    loc = locale or _active_locale

    value = _resolve_key(_locales.get(loc, {}), key)
    resolved = _case_or_string(value, case, key)
    if resolved is not None:
        return resolved

    if loc != "en":
        value = _resolve_key(_locales.get("en", {}), key)
        resolved = _case_or_string(value, case, key)
        if resolved is not None:
            return resolved

    return key


def _case_or_string(value: Any, case: str, key: str) -> str | None:
    """Extract a display string from a resolved locale value, or None.

    An empty string (either the leaf value itself or a case entry within a
    dict) is treated as "not translated" and yields None so the caller
    falls through to the next resolution stage.
    """
    if isinstance(value, dict):
        result = value.get(case) or value.get("nominative")
        return str(result) if result else None
    if isinstance(value, str) and value:
        return value
    return None


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def format_number(value: float, decimals: int, locale: str | None = None) -> str:
    """Format *value* with locale-correct decimal separator using Babel.

    *decimals* is the fixed number of digits after the decimal point.
    Babel expects underscore-separated locale tags (``pt_BR``), so we
    normalise from the BCP-47 hyphen form (``pt-BR``) used everywhere else.
    """
    loc = (locale or _active_locale).replace("-", "_")
    pattern = "#,##0." + ("0" * decimals) if decimals > 0 else "#,##0"
    return str(format_decimal(value, format=pattern, locale=loc))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_key(data: dict[str, Any], key: str) -> Any:
    """Walk a dot-separated *key* path through nested dict *data*.

    Returns the value at the path when it is a string, dict, or list,
    or None when the path does not resolve to a leaf of one of those types.
    """
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current if isinstance(current, str | dict | list) else None
