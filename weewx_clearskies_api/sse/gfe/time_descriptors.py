"""Period labels and sub-period timing descriptors (ADR-082, GFE §6).

Ported faithfully from the NWS Graphical Forecast Editor (GFE) text
formatter source (public domain, 17 USC S105), `TimeDescriptor.py`
(761 lines). See
`docs/reference/nws-text-system/gfe-source-code-analysis.md` §6 for the
full source analysis.

All display strings resolve through `i18n.t()` — this module holds no
hardcoded English literals. Locale keys used here (``gfe.time.*``) fall
back to the raw key string via `i18n.t()`'s resolution chain until locale
JSON entries are populated in a later I18N task.

**Known source-documentation gap (flagged to lead 2026-07-05, confirmed
acceptable):** the GFE source analysis document's §6.2 only enumerates 29
of the 42 rows the underlying `timePeriod_descriptor_list` table is said
to contain. The remaining ~13 rows are 1- and 2-hour-granularity
refinements (e.g. "8a-9a", "2a-3a") that the analysis document names by
clock time but does not give offset pairs or phrase text for — porting
them would mean inventing phrase content not present in the source, which
the GFE-fidelity directive (see `thresholds.py` module docstring)
forbids. The 29 documented entries below give full coverage at the
3-hour-aligned resolution this pipeline actually produces (periods split
at 6 AM / 6 PM); the finer sub-3-hour entries are not needed here and are
omitted rather than fabricated.
"""

from __future__ import annotations

import datetime
from typing import Final

from weewx_clearskies_api import i18n

# ---------------------------------------------------------------------------
# Period labels (GFE `getWeekday_descriptor`)
# ---------------------------------------------------------------------------

_WEEKDAY_KEYS: Final[tuple[str, ...]] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def period_label(is_daytime: bool, day_offset: int, locale: str) -> str:
    """Generate a GFE-style period label.

    - ``day_offset == 0``: "Today" (daytime) / "Tonight" (nighttime).
    - ``day_offset == 1``: "Tomorrow" / "Tomorrow Night".
    - ``day_offset >= 2``: the weekday name, e.g. "Saturday"; nighttime
      periods append a night-suffix, e.g. "Saturday Night". The weekday is
      computed from the current calendar date plus *day_offset* days.
    """
    if day_offset == 0:
        return i18n.t("gfe.time.today" if is_daytime else "gfe.time.tonight", locale)

    if day_offset == 1:
        return i18n.t(
            "gfe.time.tomorrow" if is_daytime else "gfe.time.tomorrow_night", locale
        )

    target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
    weekday_name = i18n.t(f"gfe.time.weekday.{_WEEKDAY_KEYS[target_date.weekday()]}", locale)
    if is_daytime:
        return weekday_name

    suffix = i18n.t("gfe.time.night_suffix", locale)
    return f"{weekday_name} {suffix}"


# ---------------------------------------------------------------------------
# Sub-period time descriptors (GFE `timePeriod_descriptor_list`)
# ---------------------------------------------------------------------------

# Each entry is (start_offset_hours, end_offset_hours, phrase_key), all
# offsets in hours relative to a single 6:00 AM local anchor (GFE
# `self.DAY()`, default 6). A day period (6 AM-6 PM) spans anchor offsets
# 0-12; a night period (6 PM-6 AM) spans anchor offsets 12-24 (continuing
# into 24-33 for sub-periods that run into the following morning).
# ``phrase_key == ""`` means "full period, no descriptor needed" — the
# 12-hour period itself carries no sub-period qualifier.
SUB_PERIOD_TABLE: Final[list[tuple[int, int, str]]] = [
    (0, 3, "early_in_the_morning"),
    (0, 6, "in_the_morning"),
    (0, 9, "until_late_afternoon"),
    (0, 12, ""),
    (0, 15, "until_early_evening"),
    (0, 18, "through_the_evening"),
    (3, 6, "late_in_the_morning"),
    (3, 9, "in_the_late_morning_and_early_afternoon"),
    (3, 12, "in_the_late_morning_and_afternoon"),
    (6, 9, "early_in_the_afternoon"),
    (6, 12, "in_the_afternoon"),
    (6, 15, "in_the_afternoon_and_evening"),
    (9, 12, "late_in_the_afternoon"),
    (9, 15, "early_in_the_evening"),
    (9, 18, "in_the_evening"),
    (9, 21, "until_early_morning"),
    (12, 15, "early_in_the_evening"),
    (12, 18, "in_the_evening"),
    (12, 21, "until_early_morning"),
    (12, 24, ""),
    (15, 18, "late_in_the_evening"),
    (15, 21, "in_the_late_evening_and_early_morning"),
    (15, 24, "in_the_late_evening_and_overnight"),
    (18, 21, "after_midnight"),
    (18, 24, "after_midnight"),
    (21, 24, "early_in_the_morning"),
    (21, 27, "early_in_the_morning"),
    (21, 30, "early_in_the_morning"),
    (21, 33, "until_afternoon"),
]


def sub_period_descriptor(start_offset: int, end_offset: int, locale: str) -> str:
    """Look up the sub-period timing phrase for a (start, end) offset pair.

    Offsets are hours relative to the 6:00 AM anchor described in
    `SUB_PERIOD_TABLE`. Returns ``""`` when the pair matches a full-period
    entry (no descriptor needed) or when no entry matches (safer than
    guessing at a wrong descriptor).
    """
    for start, end, phrase_key in SUB_PERIOD_TABLE:
        if start == start_offset and end == end_offset:
            if not phrase_key:
                return ""
            return i18n.t(f"gfe.time.subperiod.{phrase_key}", locale)

    return ""
