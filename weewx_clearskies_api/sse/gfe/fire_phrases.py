"""Fire weather phrase resolution (GFE SS18, ADR-082).

Tiered activation per ADR-082 settled decision #6:

* Tier 1 (active) -- `humidity_recovery` and `lal_from_weather` work with
  data Clear Skies already collects from providers (relative humidity,
  weather codes, coverage).
* Tier 2/3 (tables built, not wired) -- `smoke_dispersal` and
  `haines_index_phrase` are faithful ports of the GFE tables, but Clear
  Skies has no VentRate or Haines Index data source yet. They are
  callable and correct; nothing in the enrichment pipeline invokes them
  until such a provider exists.

Full analysis: `docs/reference/nws-text-system/gfe-source-code-analysis.md`
SS18. Thresholds: `sse/gfe/thresholds.py` SS8 (fire weather).
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    COVERAGE_TO_LAL,
    DRY_THUNDERSTORM_LAL_OVERRIDE,
    HAINES_INDEX_CATEGORIES,
    HUMIDITY_RECOVERY_CATEGORIES,
    HUMIDITY_RECOVERY_MAXRH_SHORTCUT,
    SMOKE_DISPERSAL_CATEGORIES,
)

# GFE weather type code for thunderstorms (`WX_TYPE_HIERARCHY` in
# thresholds.py). Codes in `lal_from_weather`'s *weather_codes* may be
# attribute-qualified as "T:Dry" for dry lightning (GFE
# `wxAttributeDescriptors`, `Dry` = "dry thunderstorms").
_THUNDERSTORM_TYPE_CODE = "T"
_DRY_ATTRIBUTE = "Dry"

# LAL for "no thunderstorms present" (GFE `no_tstms`).
_NO_THUNDERSTORM_LAL = 1

# Default (minimum) LAL when thunderstorm codes are present but the
# coverage code does not resolve via COVERAGE_TO_LAL.
_DEFAULT_THUNDERSTORM_LAL = 2


def humidity_recovery(max_rh: float, prev_24h_rh: float | None, locale: str) -> str:
    """Resolve humidity recovery category (GFE SS18.3).

    *max_rh* above ``HUMIDITY_RECOVERY_MAXRH_SHORTCUT`` (50%) is
    "excellent" immediately, regardless of the 24h-prior reading. When
    *prev_24h_rh* is unavailable the category cannot be computed, so
    "unknown" is returned rather than guessing.
    """
    if max_rh > HUMIDITY_RECOVERY_MAXRH_SHORTCUT:
        return i18n.t("gfe.fire.humidity_recovery.excellent", locale)

    if prev_24h_rh is None:
        return i18n.t("gfe.fire.humidity_recovery.unknown", locale)

    diff = max_rh - prev_24h_rh
    for category in HUMIDITY_RECOVERY_CATEGORIES:
        if diff <= category.upper_diff_pct:
            return i18n.t(f"gfe.fire.humidity_recovery.{category.phrase_key}", locale)
    # HUMIDITY_RECOVERY_CATEGORIES' final row tops out at 100, which any
    # realistic RH-percentage diff satisfies. Unreachable defensive
    # fallback for mypy's benefit.
    last = HUMIDITY_RECOVERY_CATEGORIES[-1]
    return i18n.t(f"gfe.fire.humidity_recovery.{last.phrase_key}", locale)


def lal_from_weather(weather_codes: list[str], coverage: str | None, locale: str) -> int:
    """Estimate Lightning Activity Level (LAL) from weather codes and coverage.

    Heuristic (ADR-082 settled decision #6): Clear Skies has no direct LAL
    feed, so LAL is inferred from GFE-style weather type codes (see
    `_THUNDERSTORM_TYPE_CODE` above) plus the coverage code reported
    alongside them, via ``COVERAGE_TO_LAL``.

    Returns the raw LAL integer (1-6). *locale* is accepted for signature
    parity with the other functions in this module but is currently
    unused -- this function returns a level, not display text. Resolve
    display text separately via ``LAL_LEVELS`` in `thresholds.py`.
    """
    has_thunderstorm = False
    has_dry_thunderstorm = False
    for code in weather_codes:
        base, _, attribute = code.partition(":")
        if base == _THUNDERSTORM_TYPE_CODE:
            has_thunderstorm = True
            if attribute == _DRY_ATTRIBUTE:
                has_dry_thunderstorm = True

    if has_dry_thunderstorm:
        return DRY_THUNDERSTORM_LAL_OVERRIDE

    if not has_thunderstorm:
        return _NO_THUNDERSTORM_LAL

    if coverage is not None and coverage in COVERAGE_TO_LAL:
        _, upper_level = COVERAGE_TO_LAL[coverage]
        return upper_level

    return _DEFAULT_THUNDERSTORM_LAL


def smoke_dispersal(vent_rate: float, locale: str) -> str:
    """Resolve smoke dispersal category from VentRate (knot-ft).

    Tier 2 -- table faithfully ported, not yet wired to any provider
    (Clear Skies has no VentRate data source).
    """
    for category in SMOKE_DISPERSAL_CATEGORIES:
        if vent_rate < category.upper_bound:
            return i18n.t(f"gfe.fire.smoke_dispersal.{category.phrase_key}", locale)
    # Final row uses math.inf as its upper bound, so the loop above always
    # returns for any finite vent_rate. Unreachable defensive fallback.
    last = SMOKE_DISPERSAL_CATEGORIES[-1]
    return i18n.t(f"gfe.fire.smoke_dispersal.{last.phrase_key}", locale)


def haines_index_phrase(index: int, locale: str) -> str:
    """Resolve Haines Index fire growth potential text.

    Tier 3 -- table faithfully ported, not yet wired to any provider
    (Clear Skies has no Haines Index data source).
    """
    for category in HAINES_INDEX_CATEGORIES:
        if index <= category.upper_index:
            return i18n.t(f"gfe.fire.haines_index.{category.phrase_key}", locale)
    # HAINES_INDEX_CATEGORIES' final row tops out at 10, the maximum
    # defined Haines Index value. Unreachable defensive fallback.
    last = HAINES_INDEX_CATEGORIES[-1]
    return i18n.t(f"gfe.fire.haines_index.{last.phrase_key}", locale)
