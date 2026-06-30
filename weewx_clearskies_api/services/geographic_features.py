"""Geographic features service — PMTiles file management (ADR-078).

Serves a pre-extracted PMTiles file containing geographic features
(rivers, lakes, roads, coastlines, land cover) for the dashboard map overlay.

The PMTiles file is extracted from the Protomaps daily planet build using the
``pmtiles extract`` CLI (Go binary).  The CLI reads only the needed byte ranges
from the remote URL via HTTP Range requests — it does NOT download the full
planet file.  This is the key design point (ADR-078 §Go CLI).

File path: /etc/weewx-clearskies/geographic-features.pmtiles
Download source: https://build.protomaps.com/{YYYYMMDD}.pmtiles (daily build)

Extraction is synchronous — the POST /setup/geographic-features/update endpoint
blocks during download.  For v0.1 this is acceptable; the admin UI shows a
loading spinner (ADR-078 §v0.1 scope).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

PMTILES_PATH = Path("/etc/weewx-clearskies/geographic-features.pmtiles")
PROTOMAPS_BUILD_URL = "https://build.protomaps.com"


def get_pmtiles_status() -> dict:
    """Return availability status of the PMTiles file.

    Returns a dict with keys:
      - available (bool): whether the PMTiles file exists
      - size_bytes (int|None): file size in bytes, null if not available
      - updated_at (str|None): file modification time as ISO 8601 UTC string,
        null if not available
    """
    if not PMTILES_PATH.exists():
        return {"available": False, "size_bytes": None, "updated_at": None}

    stat = PMTILES_PATH.stat()
    size_bytes = stat.st_size
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {"available": True, "size_bytes": size_bytes, "updated_at": updated_at}


def download_and_extract(bounds: str, maxzoom: int = 12) -> dict:
    """Download and extract a BBOX-clipped PMTiles file from the Protomaps CDN.

    Constructs the URL for today's Protomaps daily planet build, runs
    ``pmtiles extract`` to fetch only the tiles within the given bounding box
    up to the given zoom level, and atomically replaces the PMTiles file.

    The ``pmtiles extract`` CLI uses HTTP Range requests to read only the
    needed byte ranges from the remote URL — it does NOT download the full
    planet file.  Requires the Go ``pmtiles`` binary on PATH.

    Args:
        bounds: Bounding box as "west,south,east,north" (comma-separated floats).
        maxzoom: Maximum zoom level to extract (0-15, default 12).

    Returns:
        dict with keys: status ("ok"), size_bytes (int), updated_at (ISO 8601 str).

    Raises:
        RuntimeError: pmtiles CLI not found, extraction failed, or OS error.
    """
    now = datetime.now(tz=UTC)
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    source_url = f"{PROTOMAPS_BUILD_URL}/{today}.pmtiles"

    logger.info(
        "Starting PMTiles extraction: url=%s bounds=%r maxzoom=%d output=%s",
        source_url,
        bounds,
        maxzoom,
        PMTILES_PATH,
    )

    # Ensure the output directory exists.
    try:
        PMTILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create output directory {PMTILES_PATH.parent}: {exc}"
        ) from exc

    # Write to a temp file in the same directory so shutil.move() is atomic
    # (same filesystem).  Never leave a partial file at PMTILES_PATH.
    tmp_path: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            dir=PMTILES_PATH.parent,
            suffix=".pmtiles.tmp",
        )
        import os
        os.close(fd)
        tmp_path = Path(tmp_str)

        # Try today's build first; fall back to yesterday's if today's
        # hasn't been published yet (daily builds may lag behind UTC midnight).
        result = None
        for url in (source_url, f"{PROTOMAPS_BUILD_URL}/{yesterday}.pmtiles"):
            cmd = [
                "pmtiles",
                "extract",
                url,
                str(tmp_path),
                f"--bbox={bounds}",
                f"--maxzoom={maxzoom}",
            ]

            logger.info("Running: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
            )

            if result.returncode == 0:
                break
            stderr_text = result.stderr or result.stdout or ""
            if "404" in stderr_text or "Failed to create range reader" in stderr_text:
                logger.warning(
                    "Build %s not available (404), trying previous day", url
                )
                continue
            break

        if result is None or result.returncode != 0:
            stderr_excerpt = (result.stderr[-2000:] if result and result.stderr else "(no stderr)")
            logger.error(
                "pmtiles extract failed (exit %d): %s",
                result.returncode if result else -1,
                stderr_excerpt,
            )
            raise RuntimeError(
                f"pmtiles extract failed with exit code "
                f"{result.returncode if result else -1}. "
                f"stderr: {stderr_excerpt}"
            )

        if result.stdout:
            logger.info("pmtiles extract stdout: %s", result.stdout[-1000:])
        if result.stderr:
            logger.debug("pmtiles extract stderr: %s", result.stderr[-1000:])

        # Atomic move: replace the final path only after successful extraction.
        shutil.move(str(tmp_path), str(PMTILES_PATH))
        tmp_path = None  # Moved — no longer needs cleanup.

        logger.info("PMTiles file updated: %s", PMTILES_PATH)

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "pmtiles extract timed out after 1800 seconds. "
            "Consider using a tighter bounding box or lower maxzoom."
        )
    except FileNotFoundError as exc:
        # pmtiles binary not on PATH.
        raise RuntimeError(
            "pmtiles CLI not found. Install the Go pmtiles tool: "
            "https://github.com/protomaps/go-pmtiles/releases"
        ) from exc
    finally:
        # Clean up temp file if extraction failed before atomic move.
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    stat = PMTILES_PATH.stat()
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    return {
        "status": "ok",
        "size_bytes": stat.st_size,
        "updated_at": updated_at,
    }
