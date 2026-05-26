"""Webcam endpoint (Phase 6A).

GET /webcam               — returns webcam config and timelapse frame list.
GET /webcam/timelapse/{filename} — serves a single timelapse image file.

When [webcam] enabled=false the response carries enabled=False and no other
fields; the dashboard hides the webcam UI entirely.

When [webcam] enabled=true:
  - imageUrl is the live snapshot URL from api.conf.
  - refreshInterval is the seconds between dashboard snapshot refreshes.
  - timelapseFrames is the list of filenames (not full paths) from the
    configured timelapse_directory, sorted ascending by name (which for
    timestamp-based filenames is chronological order), capped at
    timelapse_max_frames (most recent N files).  Returns empty list when
    timelapse_directory is absent or the directory does not exist.

Timelapse file serving:
  - Accepts only *.jpg / *.png extensions.
  - Rejects any filename containing '/', '\\', '..', or a null byte
    (path-traversal guard per coding.md §1 and security-baseline §3.5).
  - Constructs the full path as timelapse_directory / filename and verifies
    it resolves strictly inside the timelapse_directory (realpath containment).
  - Returns 404 when the file is missing or the directory is not configured.
  - Returns FileResponse with the inferred image content-type.

Wire pattern: same module-level state + wire_webcam_settings() approach as
branding.py, pages.py, etc.  Called from __main__.py startup sequence.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from weewx_clearskies_api.config.settings import WebcamSettings
from weewx_clearskies_api.errors import build_problem_response
from weewx_clearskies_api.models.responses import WebcamResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level webcam settings — populated at startup from api.conf.
_webcam_settings: WebcamSettings = WebcamSettings({})

# Allowed timelapse image extensions (lower-case).
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})


def wire_webcam_settings(settings_obj: WebcamSettings) -> None:
    """Set the webcam settings from api.conf [webcam].  Called from __main__.py."""
    global _webcam_settings  # noqa: PLW0603
    _webcam_settings = settings_obj


def _list_timelapse_frames(directory: str, max_frames: int) -> list[str]:
    """Return the last `max_frames` image filenames from `directory`, sorted by name.

    Returns an empty list when the directory is absent, empty, or contains no
    image files matching _ALLOWED_EXTENSIONS.  Never raises — a missing or
    unreadable directory is a non-fatal operator config issue.

    Only filenames (not full paths) are returned.  The caller (dashboard)
    constructs the full URL via GET /api/v1/webcam/timelapse/{filename}.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.debug("Timelapse directory %r does not exist or is not a directory", directory)
        return []

    try:
        filenames = sorted(
            name
            for name in os.listdir(dir_path)
            if Path(name).suffix.lower() in _ALLOWED_EXTENSIONS
        )
    except OSError as exc:
        logger.warning("Could not list timelapse directory %r: %s", directory, exc)
        return []

    # Return last max_frames filenames (most recent, assuming name = timestamp).
    return filenames[-max_frames:]


def _is_safe_filename(filename: str) -> bool:
    """Return True when filename is safe to serve from the timelapse directory.

    Rejects:
      - Empty string.
      - Filenames containing '/' or '\\' (directory separator).
      - Filenames containing '..' (parent-traversal component).
      - Filenames containing null bytes (\\x00).
      - Filenames whose extension is not in _ALLOWED_EXTENSIONS.

    This is a fast syntactic check; the realpath containment check in the
    endpoint is the authoritative path-traversal guard.
    """
    if not filename:
        return False
    if "/" in filename or "\\" in filename:
        return False
    if ".." in filename:
        return False
    if "\x00" in filename:
        return False
    if Path(filename).suffix.lower() not in _ALLOWED_EXTENSIONS:
        return False
    return True


# ---------------------------------------------------------------------------
# GET /webcam
# ---------------------------------------------------------------------------


@router.get("/webcam", summary="Webcam configuration and timelapse frames", tags=["Webcam"])
def get_webcam() -> WebcamResponse:
    """Return webcam configuration and the timelapse frame filename list.

    When enabled=false the response carries only enabled=False.
    When enabled=true, also returns imageUrl, refreshInterval, and
    timelapseFrames (filenames from timelapse_directory, last max_frames,
    sorted ascending by name; empty list when directory is absent or empty).
    """
    if not _webcam_settings.enabled:
        return WebcamResponse(enabled=False)

    frames: list[str] = []
    if _webcam_settings.timelapse_directory:
        frames = _list_timelapse_frames(
            _webcam_settings.timelapse_directory,
            _webcam_settings.timelapse_max_frames,
        )

    return WebcamResponse(
        enabled=True,
        imageUrl=_webcam_settings.image_url,
        refreshInterval=_webcam_settings.refresh_interval,
        timelapseFrames=frames,
    )


# ---------------------------------------------------------------------------
# GET /webcam/timelapse/{filename}
# ---------------------------------------------------------------------------


@router.get(
    "/webcam/timelapse/{filename}",
    summary="Serve a single timelapse image frame",
    tags=["Webcam"],
    response_model=None,  # Mixed FileResponse/JSONResponse — let FastAPI pass through.
)
def get_timelapse_frame(filename: str, request: Request) -> Response:
    """Return a timelapse image file from the configured timelapse_directory.

    Path-traversal guard:
      1. Syntactic check: reject filenames containing '/', '\\', '..', null bytes,
         or non-image extensions.
      2. Realpath containment: the resolved path must be strictly inside
         timelapse_directory (rejects symlinks escaping the directory).

    Returns 404 (application/problem+json) when:
      - timelapse_directory is not configured.
      - The filename fails the safety check.
      - The file does not exist.
      - The resolved path escapes the timelapse_directory.

    Args:
        filename: Bare filename (e.g. "2026-05-20T12-00-00.jpg").  No path
            separators; the timelapse_directory is prepended server-side.
    """
    if not _webcam_settings.timelapse_directory:
        return build_problem_response(
            status=404,
            title="Not Found",
            detail="Timelapse directory is not configured.",
            request=request,
        )

    if not _is_safe_filename(filename):
        return build_problem_response(
            status=404,
            title="Not Found",
            detail="Timelapse frame not found.",
            request=request,
        )

    dir_path = Path(_webcam_settings.timelapse_directory).resolve()
    file_path = (dir_path / filename).resolve()

    # Realpath containment: the resolved file path must be inside the resolved
    # directory path (prevents symlink escapes per coding.md §1).
    try:
        file_path.relative_to(dir_path)
    except ValueError:
        # resolve() produced a path outside the timelapse directory.
        logger.warning(
            "Timelapse path traversal attempt rejected: filename=%r resolved to %r "
            "which escapes timelapse_directory=%r",
            filename,
            str(file_path),
            str(dir_path),
        )
        return build_problem_response(
            status=404,
            title="Not Found",
            detail="Timelapse frame not found.",
            request=request,
        )

    if not file_path.is_file():
        return build_problem_response(
            status=404,
            title="Not Found",
            detail="Timelapse frame not found.",
            request=request,
        )

    # Infer content-type from extension (e.g. "image/jpeg", "image/png").
    media_type, _ = mimetypes.guess_type(filename)
    if media_type is None:
        media_type = "application/octet-stream"

    return FileResponse(path=str(file_path), media_type=media_type)
