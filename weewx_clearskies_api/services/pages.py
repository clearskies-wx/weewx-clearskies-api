"""Pages service — built-in page list per ADR-024 (3a-2).

The 9 built-in pages are baked as a constant. Page visibility filtering
is the dashboard's responsibility via pages.json (static config served
by Caddy). GET /pages returns all 9 pages unconditionally.

Custom pages are out of scope for 3a-2 (Phase 4 config UI).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# ---------------------------------------------------------------------------
# Built-in page definitions (ADR-024)
# ---------------------------------------------------------------------------


@dataclass
class PageEntry:
    """One page metadata entry."""

    slug: str
    name: str
    icon: str
    nav_position: int
    built_in: bool


_BUILTIN_PAGES: Final[tuple[PageEntry, ...]] = (
    PageEntry(slug="now", name="Now", icon="house", nav_position=1, built_in=True),
    PageEntry(
        slug="forecast", name="Forecast", icon="cloud-sun-rain", nav_position=2, built_in=True
    ),
    PageEntry(slug="charts", name="Charts", icon="chart-line", nav_position=3, built_in=True),
    PageEntry(slug="almanac", name="Almanac", icon="moon", nav_position=4, built_in=True),
    PageEntry(
        slug="earthquakes", name="Earthquakes", icon="activity", nav_position=5, built_in=True
    ),
    PageEntry(slug="records", name="Records", icon="trophy", nav_position=6, built_in=True),
    PageEntry(slug="reports", name="Reports", icon="file-text", nav_position=7, built_in=True),
    PageEntry(slug="about", name="About", icon="info", nav_position=8, built_in=True),
    PageEntry(slug="legal", name="Legal", icon="scale", nav_position=9, built_in=True),
)

# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_all_pages() -> list[PageEntry]:
    """Return all 9 built-in pages unconditionally.

    Page visibility filtering is the dashboard's responsibility via
    pages.json (static config served by Caddy, read by the dashboard
    at boot). GET /pages returns all pages; the dashboard decides which
    to show in navigation.
    """
    return list(_BUILTIN_PAGES)
