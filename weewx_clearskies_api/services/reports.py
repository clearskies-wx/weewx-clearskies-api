"""Reports service — list and read NOAA summary files.

weewx generates:
  - NOAA-YYYY-MM.txt  (monthly summary via [[SummaryByMonth]])
  - NOAA-YYYY.txt     (yearly summary via [[SummaryByYear]])

Both live in a configured directory.  Default: /var/www/html/weewx/NOAA
(stock Debian deb install with SeasonsReport NOAA submodule).

Operators override via [weewx] reports_directory in api.conf.
The production weewx at this project writes to /var/www/weewx/NOAA — that is
an example operator override, not the stock default.

Path-traversal defence: every file access goes through resolve_report_path()
and resolve_yearly_report_path() which use os.path.realpath + containment
assertion.  The configured directory is never derived from request inputs.

Wire startup: call wire_reports_directory(path) once from __main__.py.
The /reports endpoints use get_reports_dir() to get the configured path.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from weewx_clearskies_api.models.responses import (
    NOAAReport,
    NOAAYearlyReport,
    ReportEntry,
    ReportIndex,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — resolved once at startup.
# ---------------------------------------------------------------------------

_reports_dir: Path | None = None

# Filename patterns.
_MONTHLY_RE = re.compile(r"^NOAA-(\d{4})-(\d{2})\.txt$")
_YEARLY_RE = re.compile(r"^NOAA-(\d{4})\.txt$")


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


def wire_reports_directory(directory: str) -> None:
    """Resolve and cache the reports directory path.

    Called once from __main__.py.  Missing directory → WARN, no exit.
    """
    global _reports_dir  # noqa: PLW0603

    resolved = Path(os.path.realpath(directory))
    if not resolved.exists():
        logger.warning(
            "Reports directory not found at %s; /reports endpoints will return "
            "empty/404 until configured. "
            "Set [weewx] reports_directory in api.conf to the correct path.",
            resolved,
        )
    else:
        logger.info(
            "Reports directory wired",
            extra={"path": str(resolved)},
        )
    _reports_dir = resolved


def get_reports_dir() -> Path | None:
    """Return the cached reports directory path (may not exist on disk)."""
    return _reports_dir


# ---------------------------------------------------------------------------
# Filename construction helpers (public for testing)
# ---------------------------------------------------------------------------


def build_report_filename(year: int, month: int) -> str:
    """Return the standard NOAA monthly report filename."""
    return f"NOAA-{year:04d}-{month:02d}.txt"


def build_yearly_report_filename(year: int) -> str:
    """Return the standard NOAA yearly report filename."""
    return f"NOAA-{year:04d}.txt"


# ---------------------------------------------------------------------------
# Path resolution helpers (public for testing)
# ---------------------------------------------------------------------------


def resolve_report_path(reports_dir: Path, year: int, month: int) -> Path | None:
    """Resolve and validate a monthly report file path.

    Constructs the filename, joins with reports_dir, resolves symlinks, and
    asserts containment inside reports_dir.

    Returns:
        Resolved Path if the file exists and is inside reports_dir.
        None if the file does not exist.

    Raises:
        ValueError: If the resolved path escapes the configured directory
            (path traversal attempt).
    """
    if not reports_dir.exists():
        return None

    filename = build_report_filename(year, month)
    candidate = Path(os.path.realpath(str(reports_dir / filename)))
    resolved_dir = Path(os.path.realpath(str(reports_dir)))

    # Containment check — resolved path must be under the reports directory.
    try:
        candidate.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            f"Path traversal attempt detected: resolved path {candidate!r} "
            f"is outside {resolved_dir!r}"
        ) from exc

    if not candidate.exists():
        return None

    return candidate


def resolve_yearly_report_path(reports_dir: Path, year: int) -> Path | None:
    """Resolve and validate a yearly report file path.

    Same containment logic as resolve_report_path.

    Returns None if the file does not exist.
    Raises ValueError on path traversal.
    """
    if not reports_dir.exists():
        return None

    filename = build_yearly_report_filename(year)
    candidate = Path(os.path.realpath(str(reports_dir / filename)))
    resolved_dir = Path(os.path.realpath(str(reports_dir)))

    try:
        candidate.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            f"Path traversal attempt detected: resolved path {candidate!r} "
            f"is outside {resolved_dir!r}"
        ) from exc

    if not candidate.exists():
        return None

    return candidate


# ---------------------------------------------------------------------------
# File mtime helper
# ---------------------------------------------------------------------------


def _mtime_utc_z(path: Path) -> str:
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Listing (accepts optional directory override for testability)
# ---------------------------------------------------------------------------


def _list_report_entries(scan_dir: Path | None) -> list[ReportEntry]:
    """Internal helper: scan a directory and return sorted ReportEntry list."""
    if scan_dir is None or not scan_dir.exists():
        return []

    entries: list[ReportEntry] = []
    try:
        for fname in os.listdir(str(scan_dir)):
            m_monthly = _MONTHLY_RE.match(fname)
            m_yearly = _YEARLY_RE.match(fname)
            if m_monthly:
                year = int(m_monthly.group(1))
                month = int(m_monthly.group(2))
                file_path = scan_dir / fname
                if not file_path.is_file():
                    continue
                entries.append(
                    ReportEntry(
                        kind="monthly",
                        year=year,
                        month=month,
                        filename=fname,
                        modifiedAt=_mtime_utc_z(file_path),
                    )
                )
            elif m_yearly:
                year = int(m_yearly.group(1))
                file_path = scan_dir / fname
                if not file_path.is_file():
                    continue
                entries.append(
                    ReportEntry(
                        kind="yearly",
                        year=year,
                        month=None,
                        filename=fname,
                        modifiedAt=_mtime_utc_z(file_path),
                    )
                )
            # Files matching neither pattern are silently skipped.
    except OSError as exc:
        logger.warning("Could not list reports directory %s: %s", scan_dir, exc)
        return []

    def _sort_key(e: ReportEntry) -> tuple[int, int, int]:
        year_key = -e.year
        # brief: "yearly entries first within a year, then monthly DESC"
        kind_key = 0 if e.kind == "yearly" else 1
        month_key = -(e.month or 0)
        return (year_key, kind_key, month_key)

    entries.sort(key=_sort_key)
    return entries


def list_reports() -> ReportIndex:
    """Return a ReportIndex of all available NOAA report files.

    Uses the module-level configured directory (set by wire_reports_directory).
    Returns an empty ReportIndex if the directory doesn't exist or is inaccessible.
    """
    return ReportIndex(reports=_list_report_entries(_reports_dir))


def get_report_index() -> ReportIndex:
    """Alias for list_reports() — returns ReportIndex from the configured directory."""
    return list_reports()


# ---------------------------------------------------------------------------
# Monthly report read
# ---------------------------------------------------------------------------


def get_monthly_report(year: int, month: int) -> NOAAReport | None:
    """Read a monthly NOAA report file using the configured directory.

    Returns None if the file does not exist OR if path traversal is detected.
    Raises UnicodeDecodeError if the file is not valid UTF-8.
    """
    if _reports_dir is None:
        return None

    try:
        file_path = resolve_report_path(_reports_dir, year, month)
    except ValueError:
        # Path traversal attempt — treat as not found.
        logger.warning(
            "Path traversal attempt in get_monthly_report for %d-%02d", year, month
        )
        return None

    if file_path is None:
        return None

    filename = build_report_filename(year, month)
    raw_bytes = file_path.read_bytes()
    raw_text = raw_bytes.decode("utf-8")
    return NOAAReport(
        year=year,
        month=month,
        filename=filename,
        rawText=raw_text,
        modifiedAt=_mtime_utc_z(file_path),
    )


# ---------------------------------------------------------------------------
# Yearly report read
# ---------------------------------------------------------------------------


def get_yearly_report(year: int) -> NOAAYearlyReport | None:
    """Read a yearly NOAA report file using the configured directory.

    Returns None if the file does not exist OR if path traversal is detected.
    Raises UnicodeDecodeError if the file is not valid UTF-8.
    """
    if _reports_dir is None:
        return None

    try:
        file_path = resolve_yearly_report_path(_reports_dir, year)
    except ValueError:
        logger.warning(
            "Path traversal attempt in get_yearly_report for %d", year
        )
        return None

    if file_path is None:
        return None

    filename = build_yearly_report_filename(year)
    raw_bytes = file_path.read_bytes()
    raw_text = raw_bytes.decode("utf-8")
    return NOAAYearlyReport(
        year=year,
        filename=filename,
        rawText=raw_text,
        modifiedAt=_mtime_utc_z(file_path),
    )
