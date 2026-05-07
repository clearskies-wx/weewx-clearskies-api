"""Pages endpoint (3a-2).

GET /pages — dashboard navigation list per ADR-024.

Returns built-in pages minus operator-hidden ones (from api.conf [pages] hidden).
Hidden pages are excluded from the response (not returned as hidden=true).
No query params.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.models.responses import (
    PageList,
    PageListResponse,
    PageMetadata,
    utc_isoformat,
)
from weewx_clearskies_api.services.pages import get_visible_pages

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
