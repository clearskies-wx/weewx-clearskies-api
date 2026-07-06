"""Marine wave, chop, and wind phrase resolution (GFE SS17, ADR-082).

Tables-only port. Clear Skies has no marine data provider yet, so these
functions exist for completeness (ADR-082 settled decision #6) and are
exercised directly rather than wired into any enrichment pipeline.

Full analysis: `docs/reference/nws-text-system/gfe-source-code-analysis.md`
SS17. Thresholds: `sse/gfe/thresholds.py` SS7 (marine).
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    CHOP_CATEGORIES,
    MARINE_WIND_DESCRIPTORS,
    WAVE_HEIGHT_RANGES,
)


def wave_height_phrase(avg_ft: float, locale: str) -> str:
    """Resolve wave height text for *avg_ft* (GFE `wave_range` table).

    ``WAVE_HEIGHT_RANGES`` is a sparse table of sample points, not a
    contiguous set of bins — the GFE source selects whichever documented
    point is nearest the sampled average, so this does the same.
    """
    nearest = min(WAVE_HEIGHT_RANGES, key=lambda row: abs(row.average_ft - avg_ft))
    return i18n.t(f"gfe.marine.wave_height.{nearest.phrase_key}", locale)


def chop_phrase(wind_kt: float, locale: str) -> str:
    """Resolve sea chop category text from wind speed (GFE `chop_words`)."""
    for category in CHOP_CATEGORIES:
        if wind_kt <= category.upper_kt:
            return i18n.t(f"gfe.marine.chop.{category.phrase_key}", locale)
    # CHOP_CATEGORIES' final row uses math.inf as its upper bound, so the
    # loop above always returns for any finite wind_kt. This is an
    # unreachable defensive fallback for mypy's benefit.
    return i18n.t(f"gfe.marine.chop.{CHOP_CATEGORIES[-1].phrase_key}", locale)


def marine_wind_descriptor(speed_kt: float, locale: str) -> str | None:
    """Resolve a marine wind descriptor (gales/storm force/hurricane force).

    ``MARINE_WIND_DESCRIPTORS`` is ascending with inclusive lower bounds;
    the highest threshold *speed_kt* meets or exceeds wins. Returns
    ``None`` when *speed_kt* is below the lowest threshold (34 kt) — the
    caller omits the descriptor entirely in that case.
    """
    descriptor: str | None = None
    for lower_kt, phrase_key in MARINE_WIND_DESCRIPTORS:
        if speed_kt >= lower_kt:
            descriptor = phrase_key

    if descriptor is None:
        return None
    return i18n.t(f"gfe.marine.wind.{descriptor}", locale)
