"""Pages service — built-in page list per ADR-024 (3a-2).

The 9 built-in pages are baked as a constant. Operator can hide
built-in pages via [pages] hidden in api.conf (comma-separated slugs).
'now' cannot be hidden (ADR-024: "Now cannot be unchecked"); if the
operator adds it to the hidden list, a WARNING is logged and it's ignored.

Custom pages are out of scope for 3a-2 (Phase 4 config UI).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


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
    PageEntry(slug="now",         name="Now",         icon="house",          nav_position=1, built_in=True),
    PageEntry(slug="forecast",    name="Forecast",    icon="cloud-sun-rain", nav_position=2, built_in=True),
    PageEntry(slug="charts",      name="Charts",      icon="chart-line",     nav_position=3, built_in=True),
    PageEntry(slug="almanac",     name="Almanac",     icon="moon",           nav_position=4, built_in=True),
    PageEntry(slug="earthquakes", name="Earthquakes", icon="activity",       nav_position=5, built_in=True),
    PageEntry(slug="records",     name="Records",     icon="trophy",         nav_position=6, built_in=True),
    PageEntry(slug="reports",     name="Reports",     icon="file-text",      nav_position=7, built_in=True),
    PageEntry(slug="about",       name="About",       icon="info",           nav_position=8, built_in=True),
    PageEntry(slug="legal",       name="Legal",       icon="scale",          nav_position=9, built_in=True),
)

_UNHIDEABLE_SLUG = "now"


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_visible_pages(hidden_slugs: list[str]) -> list[PageEntry]:
    """Return built-in pages excluding operator-hidden ones.

    Args:
        hidden_slugs: Slug strings to hide (from api.conf [pages] hidden).

    Returns:
        List of visible PageEntry objects in navPosition order.
    """
    hidden_set: set[str] = set()
    for slug in hidden_slugs:
        slug = slug.strip()
        if not slug:
            continue
        if slug == _UNHIDEABLE_SLUG:
            logger.warning(
                "api.conf [pages] hidden contains %r; 'now' cannot be hidden "
                "(ADR-024: 'Now cannot be unchecked'). Ignoring.",
                slug,
            )
        else:
            hidden_set.add(slug)

    return [p for p in _BUILTIN_PAGES if p.slug not in hidden_set]
