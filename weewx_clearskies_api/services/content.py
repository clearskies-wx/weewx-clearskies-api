"""Content service — operator markdown file passthrough (3a-2).

Default directory: /etc/weewx-clearskies/content/
Expected filenames: about.md and legal.md (for /content/* endpoints);
any validated page slug .md (for /pages/{slug}/content endpoint).

Wire at startup via wire_content_directory(path) — mirrors wire_reports_directory().
Missing directory → WARN at startup; endpoints return 404 until files are placed.

Security: for read_content_file, filenames are hardcoded (no user input).
For read_page_content_file, the slug is caller-validated against the known page
list before this function is called; this function additionally enforces a slug
format regex and runs path-traversal containment checks.

Markdown is returned raw — sanitization happens in the dashboard per
security-baseline §5.  The api does NOT sanitize.

File size guard: stat the file before reading; if > 1 MiB log CRITICAL and
return an error condition (caller raises 500).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_CONTENT_BYTES = 1 * 1024 * 1024  # 1 MiB

# Module-level cached directory path.
_content_dir: Path | None = None

# Valid filenames — hardcoded; no user input flows in.
_VALID_FILENAMES: frozenset[str] = frozenset({"about.md", "legal.md"})

# Slug format: lowercase letters, digits, hyphens only.  Anchored so traversal
# sequences ("../", absolute paths) cannot match.
_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


def wire_content_directory(directory: str) -> None:
    """Resolve and cache the content directory path.

    Called once from __main__.py.  Missing directory → WARN, no exit (same
    pattern as wire_reports_directory).
    """
    global _content_dir  # noqa: PLW0603

    resolved = Path(os.path.realpath(directory))
    if not resolved.exists():
        logger.warning(
            "Content directory not found at %s; /content/* endpoints will return "
            "404 until the directory is created and files are placed there. "
            "Set [content] directory in api.conf to the correct path.",
            resolved,
        )
    else:
        logger.info(
            "Content directory wired",
            extra={"path": str(resolved)},
        )
    _content_dir = resolved


def get_content_dir() -> Path | None:
    """Return the cached content directory (may not exist on disk)."""
    return _content_dir


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ContentFile:
    """Result of reading a content markdown file."""

    markdown: str
    updated_at: str | None  # UTC ISO-8601 with Z, or None if mtime unreadable


# ---------------------------------------------------------------------------
# File read
# ---------------------------------------------------------------------------


def read_content_file(filename: str) -> ContentFile | None:
    """Read a content markdown file from the configured directory.

    Args:
        filename: One of "about.md" or "legal.md" (hardcoded; no user input).

    Returns:
        ContentFile if found and readable.
        None if the file does not exist or path-traversal attempt detected.

    Raises:
        ValueError: file is too large (> 1 MiB) — caller returns 500.
        UnicodeDecodeError: file is not valid UTF-8 — caller returns 500.
        PermissionError: file is not readable — caller returns 500.
    """
    if filename not in _VALID_FILENAMES:
        # Belt-and-suspenders: only the two known filenames are valid.
        logger.warning("Invalid content filename requested: %r", filename)
        return None

    if _content_dir is None:
        return None

    # Containment check: resolve symlinks and ensure path stays inside the dir.
    candidate = Path(os.path.realpath(str(_content_dir / filename)))
    resolved_dir = Path(os.path.realpath(str(_content_dir)))

    try:
        candidate.relative_to(resolved_dir)
    except ValueError:
        logger.warning(
            "Path traversal attempt detected for content file %r: resolved path %s is outside %s",
            filename,
            candidate,
            resolved_dir,
        )
        return None

    if not candidate.exists():
        return None

    # File size guard.
    file_size = candidate.stat().st_size
    if file_size > _MAX_CONTENT_BYTES:
        # Raise so the endpoint returns 500.
        raise ValueError(
            f"Content file {filename!r} is too large ({file_size} bytes, "
            f"limit {_MAX_CONTENT_BYTES} bytes). "
            "This is likely a misconfigured or malformed file."
        )

    # Read mtime.
    updated_at: str | None = None
    try:
        mtime = candidate.stat().st_mtime
        updated_at = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        pass  # mtime unreadable → emit null per spec

    # Read text (raises UnicodeDecodeError on bad UTF-8, PermissionError if locked).
    markdown = candidate.read_text(encoding="utf-8")

    return ContentFile(markdown=markdown, updated_at=updated_at)


def read_page_content_file(slug: str) -> ContentFile | None:
    """Read a page-level markdown file from the configured content directory.

    Called from the /pages/{slug}/content endpoint after the slug has been
    validated against the known page list.  Accepts any slug conforming to
    the slug format (lowercase letters/digits/hyphens), reads ``{slug}.md``.

    Args:
        slug: A page slug already validated as a known page.  Must match
              ``[a-z][a-z0-9-]*`` — rejected otherwise.

    Returns:
        ContentFile if found and readable.
        None if the file does not exist, the content dir is not wired, or a
        path-traversal attempt is detected.

    Raises:
        ValueError: file is too large (> 1 MiB) — caller returns 500.
        UnicodeDecodeError: file is not valid UTF-8 — caller returns 500.
        PermissionError: file is not readable — caller returns 500.
    """
    if not _SLUG_RE.match(slug):
        logger.warning("Invalid page slug for content read: %r", slug)
        return None

    if _content_dir is None:
        return None

    filename = f"{slug}.md"

    # Containment check: resolve symlinks and ensure path stays inside the dir.
    candidate = Path(os.path.realpath(str(_content_dir / filename)))
    resolved_dir = Path(os.path.realpath(str(_content_dir)))

    try:
        candidate.relative_to(resolved_dir)
    except ValueError:
        logger.warning(
            "Path traversal attempt detected for page content file %r: "
            "resolved path %s is outside %s",
            filename,
            candidate,
            resolved_dir,
        )
        return None

    if not candidate.exists():
        return None

    # File size guard.
    file_size = candidate.stat().st_size
    if file_size > _MAX_CONTENT_BYTES:
        raise ValueError(
            f"Page content file {filename!r} is too large ({file_size} bytes, "
            f"limit {_MAX_CONTENT_BYTES} bytes). "
            "This is likely a misconfigured or malformed file."
        )

    # Read mtime.
    updated_at: str | None = None
    try:
        mtime = candidate.stat().st_mtime
        updated_at = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        pass  # mtime unreadable → emit null per spec

    # Read text (raises UnicodeDecodeError on bad UTF-8, PermissionError if locked).
    markdown = candidate.read_text(encoding="utf-8")

    return ContentFile(markdown=markdown, updated_at=updated_at)
