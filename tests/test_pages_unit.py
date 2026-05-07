"""Unit tests for pages list logic (services/pages.py).

Covers per the 3a-2 brief:
  - With hidden=['forecast', 'records']: response omits those two, includes other 7.
  - With hidden=['now']: WARN log emitted and 'now' still present in response.
  - Default (no hidden): all 9 pages returned in navPosition order.
  - PageEntry fields match OpenAPI PageMetadata schema.

ADR references: ADR-024 (page taxonomy — 9 built-ins; 'now' cannot be hidden).
"""

from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# Pages list tests
# ---------------------------------------------------------------------------

_ALL_9_SLUGS = {
    "now", "forecast", "charts", "almanac", "earthquakes",
    "records", "reports", "about", "legal",
}

_EXPECTED_NAV_ORDER = [
    ("now", 1),
    ("forecast", 2),
    ("charts", 3),
    ("almanac", 4),
    ("earthquakes", 5),
    ("records", 6),
    ("reports", 7),
    ("about", 8),
    ("legal", 9),
]


class TestPagesListDefault:
    """Default (no hidden pages) returns all 9 pages in navPosition order."""

    def test_default_returns_all_9_pages(self) -> None:
        """get_visible_pages() with no hidden list returns all 9 built-in pages."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        slugs = {p.slug for p in pages}
        assert slugs == _ALL_9_SLUGS, (
            f"Expected all 9 built-in page slugs, got {slugs!r}"
        )

    def test_default_pages_are_in_nav_position_order(self) -> None:
        """Pages are returned in ascending nav_position order."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        nav_positions = [p.nav_position for p in pages]
        assert nav_positions == sorted(nav_positions), (
            f"Pages must be in ascending nav_position order, got {nav_positions}"
        )

    def test_now_page_has_nav_position_1(self) -> None:
        """'now' page has nav_position=1 per ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        now_page = next((p for p in pages if p.slug == "now"), None)
        assert now_page is not None
        assert now_page.nav_position == 1

    def test_all_pages_have_required_fields(self) -> None:
        """Every page entry has slug, name, icon, nav_position, built_in per OpenAPI."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        for page in pages:
            assert page.slug, f"Page missing slug: {page}"
            assert page.name, f"Page missing name: {page}"
            assert page.icon, f"Page missing icon: {page}"
            assert page.nav_position > 0, f"Page missing nav_position: {page}"
            assert page.built_in is True, f"Page built_in must be True: {page}"

    def test_all_built_in_pages_have_built_in_true(self) -> None:
        """All 9 built-in pages have built_in=True."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        for page in pages:
            assert page.built_in is True, (
                f"Page {page.slug!r} has built_in={page.built_in!r} (expected True)"
            )

    def test_nav_position_matches_adr024_table(self) -> None:
        """nav_position for every slug matches the ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        slug_to_nav = {p.slug: p.nav_position for p in pages}
        for slug, expected_pos in _EXPECTED_NAV_ORDER:
            assert slug_to_nav.get(slug) == expected_pos, (
                f"Page {slug!r} nav_position must be {expected_pos}, "
                f"got {slug_to_nav.get(slug)!r}"
            )

    def test_correct_icons_per_adr024(self) -> None:
        """Icon values match the ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        expected_icons = {
            "now": "house",
            "forecast": "cloud-sun-rain",
            "charts": "chart-line",
            "almanac": "moon",
            "earthquakes": "activity",
            "records": "trophy",
            "reports": "file-text",
            "about": "info",
            "legal": "scale",
        }
        pages = get_visible_pages(hidden_slugs=[])
        slug_to_icon = {p.slug: p.icon for p in pages}
        for slug, expected_icon in expected_icons.items():
            assert slug_to_icon.get(slug) == expected_icon, (
                f"Page {slug!r} icon must be {expected_icon!r}, "
                f"got {slug_to_icon.get(slug)!r}"
            )


class TestPagesListHiddenSlugs:
    """Hidden slugs are excluded from the response."""

    def test_hidden_forecast_and_records_returns_7_pages(self) -> None:
        """hidden=['forecast', 'records'] → 7 pages returned (2 excluded)."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=["forecast", "records"])
        slugs = {p.slug for p in pages}
        assert len(slugs) == 7, (
            f"Expected 7 pages after hiding forecast+records, got {len(slugs)}: {slugs}"
        )
        assert "forecast" not in slugs, "'forecast' must be excluded when hidden"
        assert "records" not in slugs, "'records' must be excluded when hidden"

    def test_hidden_forecast_and_records_preserves_others(self) -> None:
        """Hiding forecast+records keeps the other 7 slugs."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=["forecast", "records"])
        remaining_slugs = {p.slug for p in pages}
        expected_remaining = _ALL_9_SLUGS - {"forecast", "records"}
        assert remaining_slugs == expected_remaining, (
            f"After hiding forecast+records, expected {expected_remaining}, "
            f"got {remaining_slugs}"
        )

    def test_hiding_legal_returns_8_pages(self) -> None:
        """hidden=['legal'] → 8 pages returned."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=["legal"])
        slugs = {p.slug for p in pages}
        assert len(slugs) == 8
        assert "legal" not in slugs

    def test_hiding_now_is_ignored_with_warn_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """hidden=['now'] → WARN log emitted and 'now' still present (ADR-024 constraint)."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        with caplog.at_level(logging.WARNING):
            pages = get_visible_pages(hidden_slugs=["now"])
        slugs = {p.slug for p in pages}
        # 'now' must remain
        assert "now" in slugs, (
            "'now' must remain visible even when operator adds it to hidden list "
            "(ADR-024: 'Now' cannot be unchecked)"
        )
        # A WARNING must have been emitted
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, (
            "Attempting to hide 'now' must emit at least one WARNING log"
        )

    def test_hiding_now_does_not_reduce_page_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """hidden=['now'] → still 9 pages returned (now not excluded)."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        with caplog.at_level(logging.WARNING):
            pages = get_visible_pages(hidden_slugs=["now"])
        assert len(pages) == 9, (
            f"'now' cannot be hidden — expected 9 pages, got {len(pages)}"
        )

    def test_hiding_now_alongside_others_only_hides_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """hidden=['now', 'legal'] → only legal hidden; now stays; 8 pages returned."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        with caplog.at_level(logging.WARNING):
            pages = get_visible_pages(hidden_slugs=["now", "legal"])
        slugs = {p.slug for p in pages}
        assert "now" in slugs, "'now' must remain despite being in hidden list"
        assert "legal" not in slugs, "'legal' must be hidden"
        assert len(slugs) == 8

    def test_empty_hidden_list_returns_all_9(self) -> None:
        """hidden=[] returns all 9 pages."""
        from weewx_clearskies_api.services.pages import get_visible_pages
        pages = get_visible_pages(hidden_slugs=[])
        assert len(pages) == 9
