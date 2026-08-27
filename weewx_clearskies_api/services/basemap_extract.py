"""Basemap extract service (M1 -- CS-BASEMAP, plan MARINE-AND-MAPS-PLAN-2026-08-27 SS"M1").

Generalises ADR-078's single geographic-features PMTiles file into three
tiered basemap files serving every Clear Skies map surface (marine, seismic,
radar/satellite): a coarse global fallback, a detailed local box around the
station + marine locations, and a box matching the radar provider's declared
coverage. ADR-078's own service/endpoints stay live this round (additive
build) -- this module does not import from or modify them.

Extraction mechanics (today/yesterday Protomaps daily-build fallback, temp
file + atomic move, 1800 s subprocess timeout) mirror
``services/geographic_features.py:108-160`` (ADR-078) -- with both
``--minzoom`` and ``--maxzoom`` per tier, run world -> local -> radar in ONE
background daemon thread per ``POST /setup/basemap/update`` call
(``start_extract_in_background()`` returns ``False`` when one is already
running -- never two threads at once).

Extent derivation ("Lead mechanics -- API side", plan 2026-08-27, verbatim):

- ``compute_local_bounds()`` = union of the seismic box (station from
  ``services/station.get_station_info()``, radius
  ``settings.earthquakes.default_radius_km x 1.15``, km->deg:
  ``lat/111.32``, ``lon/(111.32*cos(lat))``) and the marine box (bounding box
  of the locations returned by
  ``companion_proxy.marine_discovery_get("/marine", {})`` -- the API's only
  marine channel -- padded by 40 px at z15:
  ``40 x 156543.03*cos(lat)/2**15`` m, same km->deg conversion, at the
  station's latitude).
  No marine service configured
  (``companion_proxy.MarineDiscoveryUnconfiguredError``) -> seismic box
  alone (structural, not a failure). Marine service configured but
  unreachable (``companion_proxy.MarineDiscoveryUnavailableError``) ->
  propagates uncaught -- the caller REFUSES that tier's extraction rather
  than silently falling back to the seismic box alone
  (rules/coding.md SS1 "a model runs on all its inputs").
- ``compute_radar_bounds()`` = ``settings.radar.librewxr_bounds`` (CSV
  "south,west,north,east" -- see ``providers/radar/librewxr.py``
  ``configure()``) when set, converted to "west,south,east,north" for
  ``pmtiles extract --bbox``; ``None`` when unset -- the radar tier then
  falls back to the station (seismic) box alone (directive 14).

A tier whose extract fails leaves the previous file in place and records the
error in the module's ``last_error`` state; the other tiers still run.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: tier -> (minzoom, maxzoom), per "Lead mechanics -- API side".
TIERS: dict[str, tuple[int, int]] = {
    "world": (0, 6),
    "local": (7, 15),
    "radar": (0, 12),
}

BASEMAP_DIR = Path("/etc/weewx-clearskies")
PROTOMAPS_BUILD_URL = "https://build.protomaps.com"

#: World tier's fixed extraction box -- not derived from station/marine
#: (M0 measurement used this exact box for the z0-6 global fallback).
_WORLD_BBOX = "-180,-85,180,85"

#: km->deg conversion divisor, per "Lead mechanics -- API side" verbatim.
_KM_PER_DEG_LAT = 111.32

#: Marine-box screen-pixel pad, per "Lead mechanics -- API side" verbatim:
#: 40 px at z15, Web Mercator meters-per-pixel formula.
_MARINE_PAD_PX = 40
_MARINE_PAD_ZOOM = 15
_WEB_MERCATOR_C = 156543.03


def tier_path(tier: str) -> Path:
    """Return the on-disk path for one tier's PMTiles file.

    Derived from ``BASEMAP_DIR`` at call time (not baked into a constant) so
    tests can monkeypatch ``BASEMAP_DIR`` to a ``tmp_path`` -- never
    ``/etc`` (rules/coding.md SS1 API constraint 1).
    """
    return BASEMAP_DIR / f"basemap-{tier}.pmtiles"


# ---------------------------------------------------------------------------
# Extent derivation
# ---------------------------------------------------------------------------


def _bbox_str(box: tuple[float, float, float, float]) -> str:
    west, south, east, north = box
    return f"{west},{south},{east},{north}"


def _compute_seismic_box(settings: Any) -> tuple[float, float, float, float]:
    """(west, south, east, north) -- station +/- (default_radius_km x 1.15)."""
    from weewx_clearskies_api.services.station import get_station_info  # noqa: PLC0415

    info = get_station_info()
    lat, lon = info.latitude, info.longitude

    radius_km = settings.earthquakes.default_radius_km * 1.15
    lat_pad = radius_km / _KM_PER_DEG_LAT
    lon_pad = radius_km / (_KM_PER_DEG_LAT * math.cos(math.radians(lat)))

    return (lon - lon_pad, lat - lat_pad, lon + lon_pad, lat + lat_pad)


def compute_local_bounds(settings: Any) -> str:
    """Union of the seismic box and the marine box, as "west,south,east,north".

    Raises:
        companion_proxy.MarineDiscoveryUnavailableError: propagated when the
            marine service is configured but unreachable -- the local tier's
            extraction refuses rather than falling back to the seismic box.
    """
    from weewx_clearskies_api.services import companion_proxy  # noqa: PLC0415
    from weewx_clearskies_api.services.station import get_station_info  # noqa: PLC0415

    seismic_box = _compute_seismic_box(settings)

    marine_box: tuple[float, float, float, float] | None = None
    try:
        locations = companion_proxy.marine_discovery_get("/marine", {})
    except companion_proxy.MarineDiscoveryUnconfiguredError:
        # No marine service configured -- structural, not a failure.
        locations = None
        # MarineDiscoveryUnavailableError (configured but unreachable) is
        # deliberately NOT caught here -- it propagates to the caller.

    if locations:
        station_lat = get_station_info().latitude
        lats = [loc["coordinates"]["lat"] for loc in locations]
        lons = [loc["coordinates"]["lon"] for loc in locations]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        pad_m = (
            _MARINE_PAD_PX
            * _WEB_MERCATOR_C
            * math.cos(math.radians(station_lat))
            / (2**_MARINE_PAD_ZOOM)
        )
        pad_lat = pad_m / (_KM_PER_DEG_LAT * 1000)
        pad_lon = pad_m / (_KM_PER_DEG_LAT * 1000 * math.cos(math.radians(station_lat)))

        marine_box = (
            min_lon - pad_lon,
            min_lat - pad_lat,
            max_lon + pad_lon,
            max_lat + pad_lat,
        )

    if marine_box is None:
        union_box = seismic_box
    else:
        union_box = (
            min(seismic_box[0], marine_box[0]),
            min(seismic_box[1], marine_box[1]),
            max(seismic_box[2], marine_box[2]),
            max(seismic_box[3], marine_box[3]),
        )

    return _bbox_str(union_box)


def compute_radar_bounds(settings: Any) -> str | None:
    """"west,south,east,north" from settings.radar.librewxr_bounds, or None.

    settings.radar.librewxr_bounds is a "south,west,north,east" CSV string
    (see providers/radar/librewxr.py configure()) -- converted here to the
    "west,south,east,north" order pmtiles extract --bbox expects.
    """
    raw = getattr(settings.radar, "librewxr_bounds", None)
    if not raw or not raw.strip():
        return None

    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        logger.warning(
            "basemap: [radar] librewxr_bounds %r has %d parts (expected 4); "
            "radar tier falls back to the station box",
            raw,
            len(parts),
        )
        return None

    south, west, north, east = (float(p) for p in parts)
    return f"{west},{south},{east},{north}"


def _bbox_for_tier(tier: str, settings: Any) -> str:
    if tier == "world":
        return _WORLD_BBOX
    if tier == "local":
        return compute_local_bounds(settings)
    if tier == "radar":
        radar_box = compute_radar_bounds(settings)
        if radar_box is not None:
            return radar_box
        # No radar coverage box declared -- station (seismic) box, directive 14.
        return _bbox_str(_compute_seismic_box(settings))
    raise ValueError(f"Unknown basemap tier {tier!r}")


# ---------------------------------------------------------------------------
# Extraction mechanics (mirrors services/geographic_features.py:54-192)
# ---------------------------------------------------------------------------


def _extract_one_tier(tier: str, bbox: str) -> dict:
    """Run ``pmtiles extract`` for one tier; temp file + atomic move.

    Returns:
        dict with keys: size_bytes (int), updated_at (ISO 8601 str).

    Raises:
        RuntimeError: pmtiles CLI not found, extraction failed, or OS error.
    """
    minzoom, maxzoom = TIERS[tier]
    output_path = tier_path(tier)

    now = datetime.now(tz=UTC)
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    source_urls = (
        f"{PROTOMAPS_BUILD_URL}/{today}.pmtiles",
        f"{PROTOMAPS_BUILD_URL}/{yesterday}.pmtiles",
    )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create output directory {output_path.parent}: {exc}"
        ) from exc

    tmp_path: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            dir=output_path.parent,
            suffix=f".{tier}.pmtiles.tmp",
        )
        os.close(fd)
        tmp_path = Path(tmp_str)

        result = None
        for url in source_urls:
            cmd = [
                "pmtiles",
                "extract",
                url,
                str(tmp_path),
                f"--bbox={bbox}",
                f"--minzoom={minzoom}",
                f"--maxzoom={maxzoom}",
            ]
            logger.info("basemap extract (%s): %s", tier, " ".join(cmd))

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
                    "basemap extract (%s): build %s not available (404), trying previous day",
                    tier,
                    url,
                )
                continue
            break

        if result is None or result.returncode != 0:
            stderr_excerpt = (
                result.stderr[-2000:] if result and result.stderr else "(no stderr)"
            )
            logger.error(
                "basemap extract (%s) failed (exit %d): %s",
                tier,
                result.returncode if result else -1,
                stderr_excerpt,
            )
            raise RuntimeError(
                f"pmtiles extract failed for tier {tier!r} with exit code "
                f"{result.returncode if result else -1}. stderr: {stderr_excerpt}"
            )

        if result.stdout:
            logger.info("basemap extract (%s) stdout: %s", tier, result.stdout[-1000:])
        if result.stderr:
            logger.debug("basemap extract (%s) stderr: %s", tier, result.stderr[-1000:])

        shutil.move(str(tmp_path), str(output_path))
        tmp_path = None

        logger.info("basemap: tier %r updated at %s", tier, output_path)

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"pmtiles extract for tier {tier!r} timed out after 1800 seconds."
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            "pmtiles CLI not found. Install the Go pmtiles tool: "
            "https://github.com/protomaps/go-pmtiles/releases"
        ) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    stat = output_path.stat()
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"size_bytes": stat.st_size, "updated_at": updated_at}


# ---------------------------------------------------------------------------
# Background orchestration -- one daemon thread, world -> local -> radar
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def _initial_tier_state() -> dict[str, dict[str, Any]]:
    return {
        tier: {
            "available": False,
            "size_bytes": None,
            "updated_at": None,
            "bounds": None,
            "minzoom": minzoom,
            "maxzoom": maxzoom,
        }
        for tier, (minzoom, maxzoom) in TIERS.items()
    }


_state: dict[str, Any] = {
    "updating": False,
    "last_error": None,
    "last_started_at": None,
    "last_finished_at": None,
    "tiers": _initial_tier_state(),
}


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_extract_in_background(settings: Any) -> bool:
    """Start the world -> local -> radar extraction in one daemon thread.

    Returns:
        True if a new extraction was started; False if one was already
        running (never two threads at once).
    """
    with _state_lock:
        if _state["updating"]:
            return False
        _state["updating"] = True
        _state["last_error"] = None
        _state["last_started_at"] = _now_iso()

    thread = threading.Thread(
        target=_run_extract_all,
        args=(settings,),
        daemon=True,
        name="basemap-extract",
    )
    thread.start()
    return True


def _run_extract_all(settings: Any) -> None:
    """Extract world, then local, then radar. A failing tier leaves its
    previous file in place and is recorded in last_error; the other tiers
    still run.
    """
    errors: list[str] = []
    for tier in ("world", "local", "radar"):
        try:
            bbox = _bbox_for_tier(tier, settings)
            result = _extract_one_tier(tier, bbox)
            with _state_lock:
                tier_state = _state["tiers"][tier]
                tier_state["available"] = True
                tier_state["size_bytes"] = result["size_bytes"]
                tier_state["updated_at"] = result["updated_at"]
                tier_state["bounds"] = bbox
        except Exception as exc:  # noqa: BLE001 -- record every tier's own failure
            logger.error("basemap: tier %r extraction failed: %s", tier, exc, exc_info=True)
            errors.append(f"{tier}: {exc}")

    with _state_lock:
        _state["updating"] = False
        _state["last_finished_at"] = _now_iso()
        _state["last_error"] = "; ".join(errors) if errors else None


def get_status() -> dict:
    """Return per-tier availability + extract-run state.

    Availability/size/mtime are read fresh from disk on every call (disk is
    the source of truth, rules/coding.md SS1) -- bounds/minzoom/maxzoom come
    from the in-memory record of the last successful extraction (or the
    tier's static minzoom/maxzoom when it has never run).
    """
    with _state_lock:
        tiers_snapshot = {tier: dict(meta) for tier, meta in _state["tiers"].items()}
        updating = _state["updating"]
        last_error = _state["last_error"]
        last_started_at = _state["last_started_at"]
        last_finished_at = _state["last_finished_at"]

    tiers_out: dict[str, dict[str, Any]] = {}
    for tier, meta in tiers_snapshot.items():
        path = tier_path(tier)
        if path.exists():
            stat = path.stat()
            tiers_out[tier] = {
                "available": True,
                "size_bytes": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "bounds": meta["bounds"],
                "minzoom": meta["minzoom"],
                "maxzoom": meta["maxzoom"],
            }
        else:
            tiers_out[tier] = {
                "available": False,
                "size_bytes": None,
                "updated_at": None,
                "bounds": meta["bounds"],
                "minzoom": meta["minzoom"],
                "maxzoom": meta["maxzoom"],
            }

    return {
        "tiers": tiers_out,
        "updating": updating,
        "last_error": last_error,
        "last_started_at": last_started_at,
        "last_finished_at": last_finished_at,
    }


def reset_state_for_tests() -> None:
    """Reset module-level extraction state to defaults. Used in tests only."""
    with _state_lock:
        _state["updating"] = False
        _state["last_error"] = None
        _state["last_started_at"] = None
        _state["last_finished_at"] = None
        _state["tiers"] = _initial_tier_state()
