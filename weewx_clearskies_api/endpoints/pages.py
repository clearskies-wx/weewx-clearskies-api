"""Pages endpoint (3a-2 / ADR-024).

GET /pages               — dashboard navigation list per ADR-024.
GET /pages/{slug}/content — operator-authored markdown for a named page.

Returns built-in pages minus operator-hidden ones (from api.conf [pages] hidden).
Hidden pages are excluded from the navigation response but their content is still
servable (a hidden page may still have content configured).
No query params on either endpoint.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.models.responses import (
    MarkdownContent,
    MarkdownResponse,
    PageList,
    PageListResponse,
    PageMetadata,
    utc_isoformat,
)
from weewx_clearskies_api.services.content import read_page_content_file
from weewx_clearskies_api.services.pages import get_all_pages, get_visible_pages

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level hidden-pages list — set at startup from settings.
_hidden_pages: list[str] = []


def wire_hidden_pages(hidden: list[str]) -> None:
    """Set the operator-configured hidden pages list.  Called from __main__.py."""
    global _hidden_pages  # noqa: PLW0603
    _hidden_pages = list(hidden)


@router.get("/pages", summary="Dashboard navigation list", tags=["Pages"])
def get_pages() -> PageListResponse:
    """Return built-in pages excluding operator-hidden ones.

    Custom pages are empty in 3a-2 (Phase 4 config UI).
    """
    visible = get_visible_pages(_hidden_pages)

    pages = [
        PageMetadata(
            slug=p.slug,
            name=p.name,
            icon=p.icon,
            navPosition=p.nav_position,
            builtIn=p.built_in,
            hidden=False,
        )
        for p in visible
    ]

    return PageListResponse(
        data=PageList(pages=pages),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )


@router.get(
    "/pages/{slug}/content",
    summary="Page markdown content",
    tags=["Pages"],
)
def get_page_content(slug: str) -> MarkdownResponse:
    """Return operator-authored markdown content for a named page (ADR-024).

    The slug must identify a known page (built-in or custom).  Hidden pages
    are still servable — content can be pre-authored before the page is made
    visible.

    Returns an empty-string markdown with null updatedAt when the page exists
    but no content file has been placed in the content directory.  This is not
    an error; it means the operator has not authored content for that page yet.

    Args:
        slug: Page slug (e.g. "now", "about", "legal").

    Raises:
        HTTPException 404: slug is not a known page.
        HTTPException 500: content file is too large, has a UTF-8 error, or is
            not readable.
    """
    known_slugs = {p.slug for p in get_all_pages()}
    if slug not in known_slugs:
        raise HTTPException(
            status_code=404,
            detail=f"No page with slug {slug!r} exists.",
        )

    try:
        result = read_page_content_file(slug)
    except ValueError as exc:
        logger.critical(
            "Page content file %r exceeds 1 MiB limit: %s",
            f"{slug}.md",
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file is too large. Contact the site operator.",
        ) from exc
    except UnicodeDecodeError as exc:
        logger.critical(
            "Page content file %r is not valid UTF-8: %s",
            f"{slug}.md",
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file has an encoding error. Contact the site operator.",
        ) from exc
    except PermissionError as exc:
        logger.error(
            "Page content file %r is not readable: %s",
            f"{slug}.md",
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file is not readable. Contact the site operator.",
        ) from exc

    if result is None:
        return MarkdownResponse(
            data=MarkdownContent(markdown="", updatedAt=None),
            generatedAt=utc_isoformat(datetime.now(tz=UTC)),
        )

    return MarkdownResponse(
        data=MarkdownContent(
            markdown=result.markdown,
            updatedAt=result.updated_at,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
