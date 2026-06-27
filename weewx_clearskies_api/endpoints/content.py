"""Content endpoints (3a-2).

GET /content/about  — About page markdown content
GET /content/legal  — Legal/privacy page markdown content

Shared handler reads from the configured content directory.
No query params.  Sanitization is dashboard-side per security-baseline §5.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from weewx_clearskies_api.models.responses import (
    MarkdownContent,
    MarkdownResponse,
    utc_isoformat,
)
from weewx_clearskies_api.services.content import read_content_file
from weewx_clearskies_api.services.freshness import build_freshness
from weewx_clearskies_api.services.station import build_station_clock

logger = logging.getLogger(__name__)

router = APIRouter()


def _serve_content(filename: str) -> MarkdownResponse:
    """Shared handler for /content/about and /content/legal.

    Args:
        filename: One of "about.md" or "legal.md".

    Returns:
        MarkdownResponse envelope.

    Raises:
        HTTPException 404: File not found.
        HTTPException 500: File too large or UTF-8 decode error.
    """
    try:
        result = read_content_file(filename)
    except ValueError as exc:
        # File too large (> 1 MiB).
        logger.critical(
            "Content file %r exceeds 1 MiB limit: %s",
            filename,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file is too large. Contact the site operator.",
        ) from exc
    except UnicodeDecodeError as exc:
        logger.critical(
            "Content file %r is not valid UTF-8: %s",
            filename,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file has an encoding error. Contact the site operator.",
        ) from exc
    except PermissionError as exc:
        logger.error(
            "Content file %r is not readable: %s",
            filename,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Content file is not readable. Contact the site operator.",
        ) from exc

    if result is None:
        # File not found (or path traversal attempt treated as not-found).
        name_part = filename.replace(".md", "")
        raise HTTPException(
            status_code=404,
            detail=f"No {name_part}.md found in the configured content directory.",
        )

    return MarkdownResponse(
        data=MarkdownContent(
            markdown=result.markdown,
            updatedAt=result.updated_at,
        ),
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
        stationClock=build_station_clock(),
        freshness=build_freshness("station_metadata"),
    )


@router.get("/content/about", summary="About-page markdown content", tags=["Content"])
def get_about_content() -> MarkdownResponse:
    """Return the operator-authored About page markdown content."""
    return _serve_content("about.md")


@router.get("/content/legal", summary="Legal/privacy page markdown content", tags=["Content"])
def get_legal_content() -> MarkdownResponse:
    """Return the operator-authored Legal/privacy page markdown content."""
    return _serve_content("legal.md")
