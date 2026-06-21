"""Unit tests for pages list logic (services/pages.py) and page content
endpoint (GET /api/v1/pages/{slug}/content per ADR-024 Gap #7).

Covers per the 3a-2 brief:
  - All 9 pages returned unconditionally from get_all_pages().
  - PageEntry fields match OpenAPI PageMetadata schema.

Covers for the page content endpoint:
  - 200 with a valid built-in slug that has a corresponding .md file.
  - 200 with a valid slug but no content file (empty markdown response).
  - 404 for an unknown slug.
  - Response shape matches MarkdownResponse (data.markdown, data.updatedAt,
    generatedAt).

ADR references: ADR-024 (page taxonomy — 9 built-ins; custom-page content
endpoint). Page visibility is handled by the dashboard via pages.json.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Pages list tests
# ---------------------------------------------------------------------------

_ALL_9_SLUGS = {
    "now",
    "forecast",
    "charts",
    "almanac",
    "earthquakes",
    "records",
    "reports",
    "about",
    "legal",
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
    """get_all_pages() returns all 9 pages unconditionally in navPosition order."""

    def test_default_returns_all_9_pages(self) -> None:
        """get_all_pages() returns all 9 built-in pages."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        slugs = {p.slug for p in pages}
        assert slugs == _ALL_9_SLUGS, f"Expected all 9 built-in page slugs, got {slugs!r}"

    def test_default_pages_are_in_nav_position_order(self) -> None:
        """Pages are returned in ascending nav_position order."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        nav_positions = [p.nav_position for p in pages]
        assert nav_positions == sorted(nav_positions), (
            f"Pages must be in ascending nav_position order, got {nav_positions}"
        )

    def test_now_page_has_nav_position_1(self) -> None:
        """'now' page has nav_position=1 per ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        now_page = next((p for p in pages if p.slug == "now"), None)
        assert now_page is not None
        assert now_page.nav_position == 1

    def test_all_pages_have_required_fields(self) -> None:
        """Every page entry has slug, name, icon, nav_position, built_in per OpenAPI."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        for page in pages:
            assert page.slug, f"Page missing slug: {page}"
            assert page.name, f"Page missing name: {page}"
            assert page.icon, f"Page missing icon: {page}"
            assert page.nav_position > 0, f"Page missing nav_position: {page}"
            assert page.built_in is True, f"Page built_in must be True: {page}"

    def test_all_built_in_pages_have_built_in_true(self) -> None:
        """All 9 built-in pages have built_in=True."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        for page in pages:
            assert page.built_in is True, (
                f"Page {page.slug!r} has built_in={page.built_in!r} (expected True)"
            )

    def test_nav_position_matches_adr024_table(self) -> None:
        """nav_position for every slug matches the ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_all_pages

        pages = get_all_pages()
        slug_to_nav = {p.slug: p.nav_position for p in pages}
        for slug, expected_pos in _EXPECTED_NAV_ORDER:
            assert slug_to_nav.get(slug) == expected_pos, (
                f"Page {slug!r} nav_position must be {expected_pos}, got {slug_to_nav.get(slug)!r}"
            )

    def test_correct_icons_per_adr024(self) -> None:
        """Icon values match the ADR-024 table."""
        from weewx_clearskies_api.services.pages import get_all_pages

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
        pages = get_all_pages()
        slug_to_icon = {p.slug: p.icon for p in pages}
        for slug, expected_icon in expected_icons.items():
            assert slug_to_icon.get(slug) == expected_icon, (
                f"Page {slug!r} icon must be {expected_icon!r}, got {slug_to_icon.get(slug)!r}"
            )


# ---------------------------------------------------------------------------
# Page content endpoint tests
# ---------------------------------------------------------------------------


def _make_page_content_client(tmp_path: Path) -> TestClient:
    """Build a TestClient with the content directory wired to tmp_path."""
    from weewx_clearskies_api.app import create_app
    from weewx_clearskies_api.config.settings import (
        ApiSettings,
        DatabaseSettings,
        HealthSettings,
        LoggingSettings,
        Settings,
    )
    from weewx_clearskies_api.services.content import wire_content_directory

    wire_content_directory(str(tmp_path))

    settings = Settings(
        api=ApiSettings({}),
        health=HealthSettings({}),
        logging_settings=LoggingSettings({}),
        database=DatabaseSettings({}),
    )
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


class TestPageContentEndpoint:
    """GET /api/v1/pages/{slug}/content endpoint (ADR-024 Gap #7)."""

    def test_200_with_content_file_present(self, tmp_path: Path) -> None:
        """Valid built-in slug with a corresponding .md file returns 200 and content."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "about.md").write_text("# About\n\nHello.\n", encoding="utf-8")
        client = _make_page_content_client(content_dir)

        resp = client.get("/api/v1/pages/about/content")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["markdown"] == "# About\n\nHello.\n"
        assert body["data"]["updatedAt"] is not None

    def test_200_with_no_content_file_returns_empty_markdown(self, tmp_path: Path) -> None:
        """Valid slug with no .md file returns 200 with empty markdown and null updatedAt."""
        content_dir = tmp_path / "content2"
        content_dir.mkdir()
        client = _make_page_content_client(content_dir)

        resp = client.get("/api/v1/pages/now/content")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["markdown"] == ""
        assert body["data"]["updatedAt"] is None

    def test_404_for_unknown_slug(self, tmp_path: Path) -> None:
        """Unknown slug (not in any page list) returns 404 Problem response."""
        content_dir = tmp_path / "content3"
        content_dir.mkdir()
        client = _make_page_content_client(content_dir)

        resp = client.get("/api/v1/pages/nonexistent-slug/content")
        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == 404

    def test_response_shape_matches_markdown_response(self, tmp_path: Path) -> None:
        """Response has data.markdown, data.updatedAt, and generatedAt (MarkdownResponse)."""
        content_dir = tmp_path / "content4"
        content_dir.mkdir()
        (content_dir / "forecast.md").write_text("# Forecast\n", encoding="utf-8")
        client = _make_page_content_client(content_dir)

        resp = client.get("/api/v1/pages/forecast/content")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body, "Response must have 'data' key"
        assert "generatedAt" in body, "Response must have 'generatedAt' key"
        assert "markdown" in body["data"], "data must have 'markdown' key"
        assert "updatedAt" in body["data"], "data must have 'updatedAt' key"

    def test_404_problem_response_is_application_problem_json(self, tmp_path: Path) -> None:
        """404 for unknown slug returns application/problem+json per ADR-018."""
        content_dir = tmp_path / "content5"
        content_dir.mkdir()
        client = _make_page_content_client(content_dir)

        resp = client.get("/api/v1/pages/unknown/content")
        assert resp.status_code == 404
        assert "problem+json" in resp.headers.get("content-type", "")

    def test_all_9_builtin_slugs_return_200(self, tmp_path: Path) -> None:
        """All 9 built-in slugs return 200 (with empty content when no file present)."""
        content_dir = tmp_path / "content6"
        content_dir.mkdir()
        client = _make_page_content_client(content_dir)

        builtin_slugs = [
            "now",
            "forecast",
            "charts",
            "almanac",
            "earthquakes",
            "records",
            "reports",
            "about",
            "legal",
        ]
        for slug in builtin_slugs:
            resp = client.get(f"/api/v1/pages/{slug}/content")
            assert resp.status_code == 200, f"Slug {slug!r} must return 200, got {resp.status_code}"
