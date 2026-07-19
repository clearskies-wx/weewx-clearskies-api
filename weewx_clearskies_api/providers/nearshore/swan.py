"""SWAN nearshore wave model provider (PROVIDER-MANUAL §14.15, ADR-093).

Not a network provider.  This module is a thin provider wrapper around
``services/swan_runner.py``.  It follows the existing provider interface pattern
but runs a local SWAN subprocess instead of making an HTTP request.

Five responsibilities per PROVIDER-MANUAL §1:
  1. Not an outbound API call — orchestrates a SWAN subprocess using inputs
     already in cache (HRRR wind, WW3 boundary conditions, CUDEM bathymetry).
  2. Response parsing — done inside ``services/swan_runner.py`` / swan_formats.py;
     this module converts SWANRunner output dicts into cacheable payloads.
  3. Canonical field translation — MarineForecastPoint carries waveHeight (Hs),
     wavePeriod (Tm01), waveDirection (MWD) in SI units.  No unit conversion
     needed here (SWAN outputs metres and seconds natively).
  4. Capability declaration — CAPABILITY symbol; None when the [nearshore]
     extra is absent.
  5. Error handling — SWANRunError is caught at the run_all_spots() level;
     last-good cache is preserved indefinitely on failure.

Key design decisions:
  - Two cache tiers per spot:
      last_good_key (7-day TTL): always stores the most recent successful run.
        fetch() reads this and computes data_age_seconds.  Stale data is always
        preferred to no data — this key is never invalidated on failure.
      run_marker_key (55-min TTL, same as HRRR): marks that a SWAN run
        completed for the current HRRR cycle, preventing duplicate runs when
        the warmer ticks more than once before HRRR data updates.
  - Temp directory lifecycle: SwanProvider creates the tmpdir itself (via
    _SWANRunnerWithCleanup) and deletes it on success.  On SWANRunError the
    tmpdir is preserved and its path is logged at ERROR level.
  - CUDEM bathymetry: passed as an empty dict when no CUDEM provider data is
    available; cudem_to_swan_bottom() defaults to a uniform 15m ocean depth.
  - WW3 boundary: wavewatch.fetch() called at domain center for scalar TPAR
    parametric boundary conditions.
  - HRRR wind: hrrr.fetch() called with the same 1-degree-margin bbox the
    cache warmer's _warm_hrrr_wind() uses; this guarantees a cache hit.

Active only when [nearshore] pip extra is installed (GRIB_AVAILABLE == True in
providers/wind/hrrr.py).  CAPABILITY is None otherwise so the provider is not
registered.

References:
  - PROVIDER-MANUAL §14.15 (SWAN runner specification)
  - SWAN-CORRECTIONS-PLAN.md
  - services/swan_runner.py (SWANRunner.run() interface)
  - ADR-093 (SWAN nearshore model)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from weewx_clearskies_api.providers._common.cache import get_cache
from weewx_clearskies_api.providers._common.capability import (
    ProviderAttribution,
    ProviderCapability,
)
from weewx_clearskies_api.services.swan_runner import SWANRunner, SWANRunError
from weewx_clearskies_api.services.swan_domain import GridDomain, DomainSizing
from weewx_clearskies_api.enrichment.bathymetry import download_swan_depth_grid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "swan"
DOMAIN = "nearshore"

_API_VERSION = "0.1.0"

# Cache TTLs
_CACHE_TTL_SECONDS = 21600       # 6 hours — extended HRRR cycle interval (4×/day at 00/06/12/18Z)
_LAST_GOOD_TTL_SECONDS = 604800  # 7 days — "stale is always preferred to no data"

# SWAN grid defaults (configurable via [marine] section in future T4.x)
_DEFAULT_SWAN_BINARY = "/usr/local/bin/swan"
_DEFAULT_OUTER_GRID_RESOLUTION_KM = 3.0   # outer (shelf) grid — continental approach
_DEFAULT_GRID_RESOLUTION_M = 200.0         # inner nest resolution — tight nearshore
_DEFAULT_COMPUTE_DT_MIN = 10
_DEFAULT_OUTPUT_INTERVAL_HR = 1.0
_DEFAULT_SWAN_TIMEOUT_S = 3600

# Bounding-box expansion margins (degrees) used to derive the SWAN domain
# and HRRR wind bbox from configured surf spot coordinates.

# CUDEM 2-D bathymetry grid cache path (persistent across restarts).
_CUDEM_GRID_PATH = Path("/etc/weewx-clearskies/swan_bathymetry.json")

# Per-spot bidirectional CUDEM profile cache (180-day TTL).
_PROFILE_CACHE_DIR = Path("/etc/weewx-clearskies/spot_profiles")
_PROFILE_MAX_AGE_S = 180 * 86400  # 180 days

# Per-level CUDEM grid cache paths (Phase 14b).
_CUDEM_GRID_PATH_L1 = Path("/etc/weewx-clearskies/swan_bathymetry_L1.json")
_CUDEM_GRID_PATH_L2 = Path("/etc/weewx-clearskies/swan_bathymetry_L2.json")
_CUDEM_L3_CACHE_DIR = Path("/etc/weewx-clearskies")


def _load_or_download_cudem_grid(
    bbox: tuple[float, float, float, float],
    outer_resolution_km: float,
) -> dict[str, Any]:
    """Load cached CUDEM 2-D grid from disk, or download from NCEI if absent.

    The grid covers the outer SWAN domain at the outer grid resolution.
    ``cudem_to_swan_bottom()`` bilinear-interpolates this onto whichever
    SWAN grid level needs it (outer or inner).
    """
    if _CUDEM_GRID_PATH.exists():
        age_s = time.time() - os.path.getmtime(str(_CUDEM_GRID_PATH))
        if age_s > _PROFILE_MAX_AGE_S:
            logger.info(
                "CUDEM 2-D grid cache is %.0f days old — refreshing",
                age_s / 86400,
            )
        else:
            try:
                grid = json.loads(_CUDEM_GRID_PATH.read_text(encoding="utf-8"))
                if grid.get("depths"):
                    logger.debug("CUDEM 2-D grid loaded from %s", _CUDEM_GRID_PATH)
                    return grid
            except Exception:
                logger.warning(
                    "CUDEM 2-D grid file %s is corrupt; re-downloading",
                    _CUDEM_GRID_PATH,
                    exc_info=True,
                )

    try:
        from weewx_clearskies_api.enrichment.bathymetry import download_swan_depth_grid

        grid = download_swan_depth_grid(bbox, outer_resolution_km * 1000.0)
        _CUDEM_GRID_PATH.write_text(
            json.dumps(grid), encoding="utf-8"
        )
        logger.info(
            "CUDEM 2-D grid downloaded and cached to %s (%d x %d)",
            _CUDEM_GRID_PATH,
            grid["ni"],
            grid["nj"],
        )
        return grid
    except Exception:
        logger.warning(
            "CUDEM 2-D grid download failed; SWAN will use uniform 15m depth",
            exc_info=True,
        )
        return {}


# ---------------------------------------------------------------------------
# Per-level CUDEM bathymetry downloads (Phase 14b — 3-level nesting)
# ---------------------------------------------------------------------------


def download_bathymetry_for_level(domain: GridDomain, level: int) -> dict[str, Any]:
    """Load a cached per-level CUDEM depth grid, or download from NCEI if absent/stale.

    Cache paths:
      Level 1 → /etc/weewx-clearskies/swan_bathymetry_L1.json  (1 km grid)
      Level 2 → /etc/weewx-clearskies/swan_bathymetry_L2.json  (100 m grid)
      Level 3 → /etc/weewx-clearskies/swan_bathymetry_L3_{hash}.json  (10 m grid)

    The Level 3 hash is an 8-character MD5 of the rounded bbox coordinates so
    that each surf-zone cluster gets its own stable cache file.

    The 180-day mtime TTL and fallback-to-empty-dict behaviour match
    ``_load_or_download_cudem_grid()``.  The returned dict format is identical
    to what ``cudem_to_swan_bottom()`` expects:
    ``{"lat_first", "lon_first", "lat_last", "lon_last", "ni", "nj", "depths"}``.

    Args:
        domain: GridDomain from swan_domain.compute_domains() for the target level.
        level:  1, 2, or 3.

    Returns:
        Grid dict on success, empty dict on failure (SWAN falls back to 15 m depth).
    """
    if level == 3:
        bbox_key = (
            f"{domain.lat_min:.4f}_{domain.lat_max:.4f}"
            f"_{domain.lon_min:.4f}_{domain.lon_max:.4f}"
        )
        cluster_hash = hashlib.md5(bbox_key.encode()).hexdigest()[:8]
        cache_path = _CUDEM_L3_CACHE_DIR / f"swan_bathymetry_L3_{cluster_hash}.json"
    elif level == 2:
        cache_path = _CUDEM_GRID_PATH_L2
    else:
        # Level 1 (and any unexpected level value — treat as coarse grid)
        cache_path = _CUDEM_GRID_PATH_L1

    # 180-day TTL check (matches _load_or_download_cudem_grid pattern)
    if cache_path.exists():
        age_s = time.time() - os.path.getmtime(str(cache_path))
        if age_s > _PROFILE_MAX_AGE_S:
            logger.info(
                "CUDEM L%d grid cache is %.0f days old — refreshing (path=%s)",
                level,
                age_s / 86400,
                cache_path,
            )
        else:
            try:
                grid = json.loads(cache_path.read_text(encoding="utf-8"))
                if grid.get("depths"):
                    logger.debug("CUDEM L%d grid loaded from %s", level, cache_path)
                    return grid
            except Exception:
                logger.warning(
                    "CUDEM L%d grid file %s is corrupt; re-downloading",
                    level,
                    cache_path,
                    exc_info=True,
                )

    # download_swan_depth_grid expects (lon_sw, lat_sw, lon_ne, lat_ne)
    bbox = (domain.lon_min, domain.lat_min, domain.lon_max, domain.lat_max)
    center_lat = (domain.lat_min + domain.lat_max) / 2
    center_lon = (domain.lon_min + domain.lon_max) / 2

    # Priority 0: Operator-supplied bathymetry (Phase 24)
    try:
        from weewx_clearskies_api.services.bathymetry_resolver import (
            get_operator_grid,
        )

        op_grid = get_operator_grid(bbox, domain.resolution_m)
        if op_grid is not None:
            cache_path.write_text(json.dumps(op_grid), encoding="utf-8")
            logger.info(
                "CUDEM L%d: using operator-supplied bathymetry (%d x %d)",
                level, op_grid["ni"], op_grid["nj"],
            )
            return op_grid
    except Exception:
        logger.debug("Operator bathymetry check skipped", exc_info=True)

    # Priority 1: NCEI regional DEM via OPeNDAP
    try:
        from weewx_clearskies_api.services.bathymetry_resolver import (
            find_best_dem,
            fetch_opendap_grid,
            normalize_to_msl,
        )

        dem = find_best_dem(bbox)
        if dem is not None:
            grid = fetch_opendap_grid(
                dem["filename"], bbox, domain.resolution_m, dem["elevation_var"]
            )
            grid["depths"] = normalize_to_msl(
                grid["depths"], dem["vertical_datum"], center_lat, center_lon
            )
            grid["source"] = "ncei_regional"
            cache_path.write_text(json.dumps(grid), encoding="utf-8")
            logger.info(
                "CUDEM L%d: using NCEI %s (%.0fm, %s) via OPeNDAP",
                level,
                dem["filename"],
                dem["resolution_m"],
                dem["vertical_datum"],
            )
            return grid
    except Exception:
        logger.warning(
            "CUDEM L%d: NCEI regional DEM failed, trying next source",
            level,
            exc_info=True,
        )

    # Priority 2: USGS Great Lakes DEM
    try:
        from weewx_clearskies_api.services.bathymetry_resolver import (
            is_great_lake,
            _ensure_great_lake_dem,
            fetch_great_lake_grid,
        )

        lake = is_great_lake(center_lat, center_lon)
        if lake is not None:
            dem_path = _ensure_great_lake_dem(lake)
            if dem_path is not None:
                grid = fetch_great_lake_grid(dem_path, bbox, domain.resolution_m)
                grid["source"] = "usgs_great_lakes"
                cache_path.write_text(json.dumps(grid), encoding="utf-8")
                logger.info(
                    "CUDEM L%d: using USGS Great Lakes %s DEM", level, lake
                )
                return grid
    except Exception:
        logger.warning(
            "CUDEM L%d: Great Lakes DEM failed, falling back to CRM",
            level,
            exc_info=True,
        )

    # Priority 3: CRM fallback (download_swan_depth_grid)
    try:
        grid = download_swan_depth_grid(bbox, domain.resolution_m)
        grid["source"] = "crm_fallback"
        cache_path.write_text(json.dumps(grid), encoding="utf-8")
        logger.info(
            "CUDEM L%d grid downloaded and cached to %s (%d x %d)",
            level,
            cache_path,
            grid["ni"],
            grid["nj"],
        )
        return grid
    except Exception:
        logger.warning(
            "CUDEM L%d grid download failed; SWAN will use uniform 15m depth",
            level,
            exc_info=True,
        )
        return {}


def download_all_bathymetry(domains: DomainSizing) -> dict[str, Any]:
    """Download CUDEM bathymetry for all three grid levels.

    Orchestrates per-level downloads using ``download_bathymetry_for_level()``.
    Called by the Phase 14c 3-level run function before launching SWAN.

    Args:
        domains: DomainSizing from swan_domain.compute_domains().

    Returns:
        dict with three keys:
          "level1"  — grid dict for the 1 km coarse grid
          "level2"  — grid dict for the 100 m nearshore grid
          "level3"  — dict mapping cluster_idx (int) → grid dict for 10 m surf grids
        Any individual download failure yields an empty dict for that entry;
        SWAN falls back to uniform 15 m depth for that grid level.
    """
    level1_grid = download_bathymetry_for_level(domains.level1, level=1)
    level2_grid = download_bathymetry_for_level(domains.level2, level=2)

    level3_grids: dict[int, dict[str, Any]] = {}
    for idx, cluster in enumerate(domains.level3_clusters):
        if cluster.grid is None:
            logger.warning(
                "CUDEM L3 cluster %d has no GridDomain; skipping bathymetry download",
                idx,
            )
            continue
        level3_grids[idx] = download_bathymetry_for_level(cluster.grid, level=3)

    return {
        "level1": level1_grid,
        "level2": level2_grid,
        "level3": level3_grids,
    }


# ---------------------------------------------------------------------------
# Remote mode state (T4.2 / T4.3 — set when [swan] service_url is active)
# ---------------------------------------------------------------------------

#: URL of the standalone SWAN service (None = bundled mode).
_remote_url: str | None = None

#: Number of consecutive health check failures since last success.
_remote_consecutive_failures: int = 0

#: True when the remote service is considered healthy (or bundled mode active).
_remote_healthy: bool = True

#: True after we have logged the "unreachable" ERROR (avoid log spam).
_remote_warned_unreachable: bool = False

#: Lock protecting lazy remote-mode initialisation in run_all_spots().
_remote_init_lock = threading.Lock()

#: Lock preventing concurrent SWAN runs.  Acquired non-blocking at the
#: start of run_all_spots() and run_quick_update().  If already held,
#: the caller skips — a SWAN run is already in progress.
_swan_run_lock = threading.Lock()

#: Background health check thread (started by configure_remote_mode()).
_remote_health_thread: threading.Thread | None = None

#: Health check interval in seconds (T4.3).
_REMOTE_HEALTH_INTERVAL_S: int = 60

#: Consecutive failure threshold before ERROR is logged and stale cache is served.
_REMOTE_FAILURE_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# [nearshore] extra availability guard
# ---------------------------------------------------------------------------

try:
    from weewx_clearskies_api.providers.wind.hrrr import GRIB_AVAILABLE as _GRIB_AVAILABLE
    from weewx_clearskies_api.providers.wind import gfs as _gfs_wind
except ImportError:
    _GRIB_AVAILABLE = False
    _gfs_wind = None  # type: ignore[assignment]

_NEARSHORE_AVAILABLE: bool = _GRIB_AVAILABLE


# ---------------------------------------------------------------------------
# Capability declaration (PROVIDER-MANUAL §1, §14.15)
# ---------------------------------------------------------------------------

if _NEARSHORE_AVAILABLE:
    CAPABILITY: ProviderCapability | None = ProviderCapability(
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
        supplied_canonical_fields=(
            "waveHeight",
            "wavePeriod",
            "waveDirection",
        ),
        geographic_coverage="configured_marine_surf_locations",
        auth_required=(),
        default_poll_interval_seconds=_CACHE_TTL_SECONDS,
        operator_notes=(
            "SWAN locally-run nearshore wave model (PROVIDER-MANUAL §14.15). "
            "Requires SWAN binary on PATH (default: /usr/local/bin/swan) and the "
            "[nearshore] pip extra (eccodes or pygrib). "
            "Runs 4× daily on extended HRRR cycles (00/06/12/18Z) — not a network provider. "
            "GFS wind (§14.16) supplements HRRR for 72-hour surf forecast. "
            "No API key required."
        ),
        refresh_interval=_CACHE_TTL_SECONDS,
        attribution=ProviderAttribution(
            attribution_required=False,
            display_name="SWAN",
            attribution_text=(
                "Nearshore wave forecast powered by SWAN (Clear Skies)"
            ),
            text_prefix="Powered by",
            text_provider_name="SWAN",
            url="https://swanmodel.sourceforge.io/",
        ),
    )
else:
    CAPABILITY = None


# ---------------------------------------------------------------------------
# Cache key construction (PROVIDER-MANUAL §14.15)
# ---------------------------------------------------------------------------


def _build_last_good_key(spot_id: str) -> str:
    """SHA-256 key for last-good SWAN output per surf spot.

    This key has a 7-day TTL and is overwritten on every successful SWAN run.
    It provides the stale-preferred-to-nothing fallback that the surf endpoint
    reads.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "spot_id": spot_id,
            "type": "last_good",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_run_marker_key(hrrr_cycle_time: str) -> str:
    """SHA-256 key for a per-HRRR-cycle SWAN run-completion marker.

    Stored with TTL=_CACHE_TTL_SECONDS (55 min) after a successful SWAN run.
    run_all_spots() checks this key to avoid re-running SWAN for the same
    HRRR cycle when the warmer fires more than once before the cycle updates.
    """
    payload = json.dumps(
        {
            "provider_id": PROVIDER_ID,
            "type": "run_marker",
            "hrrr_cycle_time": hrrr_cycle_time,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# _SWANRunnerWithCleanup — subclass exposing tmpdir for caller lifecycle
# ---------------------------------------------------------------------------


class _SWANRunnerWithCleanup(SWANRunner):
    """SWANRunner variant that returns the tmpdir path for caller-managed cleanup.

    SWANRunner.run() creates a tmpdir internally and does not expose it — the
    caller has no path to clean up.  This subclass delegates to the nested grid
    methods (_stitch_wind, _run_outer_grid, _run_inner_nest) with a
    caller-visible tmpdir so SwanProvider can delete on success and
    preserve-plus-log on failure per PROVIDER-MANUAL §14.15.
    """

    def run_with_tmpdir(
        self,
        hrrr_wind_field: dict[str, Any],
        gfs_wind_field: dict[str, Any] | None,
        ww3_boundary: dict[str, Any],
        cudem_bathymetry: dict[str, Any],
        tide_predictions: list[dict] | None = None,
        ofs_currents: list[dict] | None = None,
        structures: list[dict] | None = None,
    ) -> tuple[dict[str, list], Path]:
        """Run nested SWAN and return (results, tmpdir).

        Blends HRRR and GFS wind fields, runs the outer grid (continental shelf
        approach) then the inner nest (tight nearshore), and returns the per-spot
        MarineForecastPoint results alongside the working directory.

        The caller is responsible for deleting tmpdir on success and for
        preserving + logging tmpdir on failure.  Both outer/ and inner/
        subdirectories are created inside tmpdir.

        Args:
            hrrr_wind_field: Return value of hrrr.fetch(). Provides hours 0–48.
            gfs_wind_field:  Return value of gfs.fetch(). Provides hours 48–72.
                             When None, the forecast is shortened to HRRR hours only.
            ww3_boundary:    Return value of wavewatch.fetch().
            cudem_bathymetry: Dict with CUDEM depth grid data.
            tide_predictions: CO-OPS tidal predictions (time, height dicts).
                              When provided, WLEVEL.txt is written and
                              INPGRID WLEVEL is included in SWAN INPUT.
            ofs_currents: OFS surface currents.  When provided, CURRENT.txt is written.
            structures: Coastal structures for OBSTACLE commands.

        Raises:
            SWANRunError: A SWAN subprocess exited with non-zero status or timed out.
            ValueError:   Required config keys are missing or input data is empty.
        """
        blended_wind = self._stitch_wind(hrrr_wind_field, gfs_wind_field)
        # Fixed working directory — no tempfile.  Visible from SSH, survives
        # service restarts, and we control the lifecycle explicitly.
        swan_work = Path("/var/run/weewx-clearskies/swan")
        swan_work.mkdir(parents=True, exist_ok=True)
        tmpdir = swan_work
        # Clean previous run's files (outer/ and inner/ subdirs)
        for sub in ("outer", "inner"):
            sub_path = tmpdir / sub
            if sub_path.exists():
                shutil.rmtree(sub_path)
        self._run_outer_grid(
            tmpdir, blended_wind, ww3_boundary, cudem_bathymetry,
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )
        results = self._run_inner_nest(
            tmpdir, blended_wind, cudem_bathymetry,
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures,
        )
        return results, tmpdir


# ---------------------------------------------------------------------------
# Remote mode — health check thread and startup probe (T4.2 / T4.3)
# ---------------------------------------------------------------------------


def _remote_health_loop(service_url: str, spot_ids: list[str]) -> None:
    """Background daemon thread: poll remote /health every 60 s (T4.3).

    On each successful health check:
      - Resets _remote_consecutive_failures to 0.
      - Fetches fresh forecast data for each configured spot from the remote
        service's /surf/{spot_id}/forecast endpoint.
      - Stores results in the local last-good cache (7-day TTL, same cache
        keys as bundled mode) so fetch() can serve them from the request path.

    On consecutive failures:
      - Increments _remote_consecutive_failures.
      - After _REMOTE_FAILURE_THRESHOLD (3) consecutive failures: logs ERROR
        once, sets _remote_healthy = False.  Subsequent failures are logged
        at DEBUG to avoid log spam.
      - fetch() continues to serve the last-good cache (stale indefinitely per
        PROVIDER-MANUAL §14.15 — stale is always preferred to no data).

    Recovery:
      - When the remote service becomes reachable again: resets state, logs
        INFO "SWAN remote service recovered", resumes fresh data.

    Args:
        service_url: Base URL of the standalone SWAN service (e.g.
                     ``http://192.168.1.50:8767``).
        spot_ids: Surf spot IDs from marine config.  The health response may
                  supply an updated spot list; we merge both to avoid missing
                  spots that were added to the remote config later.
    """
    global _remote_consecutive_failures, _remote_healthy, _remote_warned_unreachable  # noqa: PLW0603

    while True:
        try:
            resp = httpx.get(f"{service_url}/health", timeout=10.0)
            resp.raise_for_status()
            health_data: dict[str, Any] = resp.json()

            # Recovery path: reset failure state.
            recovered = _remote_consecutive_failures >= _REMOTE_FAILURE_THRESHOLD or not _remote_healthy
            _remote_consecutive_failures = 0
            if recovered:
                _remote_healthy = True
                _remote_warned_unreachable = False
                logger.info(
                    "SWAN remote service recovered: %s (last_run=%s)",
                    service_url,
                    health_data.get("last_run"),
                )

            # Merge spot IDs from config and from health response.
            remote_spots: list[str] = health_data.get("spots") or []
            all_spots = list(dict.fromkeys(spot_ids + remote_spots))  # preserve order, deduplicate

            # Refresh per-spot forecast data in local last-good cache.
            cache = get_cache()
            for spot_id in all_spots:
                try:
                    forecast_resp = httpx.get(
                        f"{service_url}/surf/{spot_id}/forecast",
                        timeout=30.0,
                    )
                    if forecast_resp.status_code == 200:
                        data: dict[str, Any] = forecast_resp.json()
                        cache.set(
                            _build_last_good_key(spot_id),
                            {
                                "forecast": data.get("forecast", []),
                                "run_time": data.get("run_time"),
                                "hrrr_cycle_time": data.get("hrrr_cycle_time", ""),
                            },
                            _LAST_GOOD_TTL_SECONDS,
                        )
                        logger.debug(
                            "SWAN remote: refreshed spot %r (run_time=%s)",
                            spot_id,
                            data.get("run_time"),
                        )
                    elif forecast_resp.status_code == 503:
                        # No data yet — first SWAN run may be in progress.
                        logger.debug(
                            "SWAN remote: spot %r has no data yet (503)", spot_id
                        )
                    else:
                        logger.warning(
                            "SWAN remote: /surf/%s/forecast returned %d; skipping",
                            spot_id,
                            forecast_resp.status_code,
                        )
                except Exception:
                    logger.warning(
                        "SWAN remote: failed to refresh spot %r",
                        spot_id,
                        exc_info=True,
                    )

        except Exception:
            _remote_consecutive_failures += 1
            failures = _remote_consecutive_failures

            if failures < _REMOTE_FAILURE_THRESHOLD:
                logger.warning(
                    "SWAN remote health check failed (%d/%d): %s",
                    failures,
                    _REMOTE_FAILURE_THRESHOLD,
                    service_url,
                    exc_info=True,
                )
            elif failures == _REMOTE_FAILURE_THRESHOLD:
                _remote_healthy = False
                _remote_warned_unreachable = True
                logger.error(
                    "SWAN remote service unreachable after %d consecutive "
                    "health check failures: %s — serving stale cache indefinitely",
                    _REMOTE_FAILURE_THRESHOLD,
                    service_url,
                )
            else:
                # Already logged ERROR; stay quiet to avoid log spam.
                logger.debug(
                    "SWAN remote health check still failing (%d failures): %s",
                    failures,
                    service_url,
                )

        time.sleep(_REMOTE_HEALTH_INTERVAL_S)


def configure_remote_mode(service_url: str, marine_config: Any) -> bool:
    """Attempt to activate remote SWAN mode and start the health check thread.

    Called lazily from run_all_spots() on the first invocation when
    ``marine_config.swan.is_remote`` is True (T4.2).  Protected by
    ``_remote_init_lock`` so concurrent callers (e.g. tests) are safe.

    Startup probe:
      Calls ``GET {service_url}/health`` with a 10 s timeout.  On failure:
        - Logs ERROR with fallback notice.
        - Returns False → caller falls through to bundled SWAN mode.
      On success: sets ``_remote_url``, starts the background health thread,
      returns True.

    Args:
        service_url: Base URL of the standalone SWAN service.
        marine_config: MarineConfig from api.conf (supplies spot_ids for the
                       health thread's initial spot list).

    Returns:
        True if remote mode was activated; False if the startup probe failed
        and the caller should fall back to bundled mode.
    """
    global _remote_url, _remote_health_thread  # noqa: PLW0603

    logger.info("SWAN: probing remote service at %s", service_url)
    try:
        resp = httpx.get(f"{service_url}/health", timeout=10.0)
        resp.raise_for_status()
        logger.info(
            "SWAN remote service reachable: %s (last_run=%s)",
            service_url,
            resp.json().get("last_run"),
        )
    except Exception as exc:
        logger.error(
            "SWAN: remote service unreachable at startup (%s: %s); "
            "falling back to bundled SWAN mode if SWAN binary is available",
            service_url,
            exc,
        )
        return False

    spot_ids = list(getattr(marine_config, "surf_spots", {}).keys()) if marine_config else []

    _remote_url = service_url
    _remote_health_thread = threading.Thread(
        target=_remote_health_loop,
        args=(service_url, spot_ids),
        daemon=True,
        name="swan-remote-health",
    )
    _remote_health_thread.start()
    logger.info(
        "SWAN: remote mode active (service_url=%s, %d spot(s), "
        "health check every %ds)",
        service_url,
        len(spot_ids),
        _REMOTE_HEALTH_INTERVAL_S,
    )
    return True


# ---------------------------------------------------------------------------
# Public fetch entrypoint (PROVIDER-MANUAL §14.15)
# ---------------------------------------------------------------------------


def fetch(spot_id: str) -> dict[str, Any] | None:
    """Return the last-good SWAN wave forecast for one surf spot from cache.

    This function is READ-ONLY — it never triggers a SWAN run.  It is intended
    for the surf endpoint (T3.x) to call in the request path.  The cache warmer
    populates the cache via run_all_spots() in the background.

    Args:
        spot_id: Marine location ID (e.g. ``"wrightsville_beach"``).

    Returns:
        dict with keys:
          ``"forecast"`` — list[dict] (MarineForecastPoint.model_dump() shape)
          ``"run_time"`` — ISO-8601 UTC string of when the SWAN run completed
          ``"data_age_seconds"`` — int, seconds elapsed since the run completed
        Or None if no SWAN data has been cached for this spot yet.
    """
    last_good = get_cache().get(_build_last_good_key(spot_id))
    if last_good is None:
        if _remote_url:
            logger.debug(
                "SWAN remote: no cached data for spot %r — health thread has "
                "not yet completed a successful fetch from %s",
                spot_id,
                _remote_url,
            )
        else:
            logger.debug("SWAN: no cached data for spot %r", spot_id)
        return None

    # Compute live data age from the stored run_time.
    run_time_str: str | None = last_good.get("run_time")
    data_age_seconds: int | None = None
    if run_time_str:
        try:
            run_dt = datetime.fromisoformat(run_time_str.replace("Z", "+00:00"))
            data_age_seconds = int(
                (datetime.now(UTC) - run_dt).total_seconds()
            )
        except (ValueError, TypeError):
            pass

    return {
        "forecast": last_good.get("forecast", []),
        # T3.3 — SPECOUT spectral decomposition (list of {time, components} dicts)
        "spectral": last_good.get("spectral", []),
        # T3.4 — full cross-shore transect per timestep (keyed by ISO time string)
        "transect": last_good.get("transect", {}),
        "run_time": run_time_str,
        "data_age_seconds": data_age_seconds,
    }


# ---------------------------------------------------------------------------
# SWAN run + cache (called from cache warmer)
# ---------------------------------------------------------------------------


def run_all_spots(
    marine_config: Any,
    *,
    hrrr_wind_field: dict | None = None,
    gfs_wind_field: dict | None = None,
    swan_binary: str = _DEFAULT_SWAN_BINARY,
    outer_grid_resolution_km: float = _DEFAULT_OUTER_GRID_RESOLUTION_KM,
    inner_nest_resolution_m: float = _DEFAULT_GRID_RESOLUTION_M,
    compute_dt_min: int = _DEFAULT_COMPUTE_DT_MIN,
    output_interval_hr: float = _DEFAULT_OUTPUT_INTERVAL_HR,
    swan_timeout_s: int = _DEFAULT_SWAN_TIMEOUT_S,
) -> None:
    """Run SWAN for all configured surf spots and cache per-spot results.

    Called from BackgroundCacheWarmer._warm_swan() in a background daemon
    thread.  All surf spot locations in ``marine_config`` are processed in a
    two-level nested SWAN run (outer grid + inner nest).  On success the
    per-spot results are stored at the last-good cache key (7-day TTL).  On
    failure the last-good cache is untouched (stale data preserved
    indefinitely) per PROVIDER-MANUAL §14.15.

    Args:
        marine_config: Parsed MarineConfig from config.marine_config.
            Must have .locations (list[MarineLocation]) and .surf_spots
            (dict[str, SurfSpotConfig]).
        hrrr_wind_field: Pre-fetched HRRR wind field dict from the cache
            warmer.  When provided, SWAN uses it directly instead of
            calling _hrrr.fetch() — single fetch, single cache key, no
            divergence.  When None, falls back to fetching (legacy path).
        gfs_wind_field: Pre-fetched GFS wind field dict (hours 48–72) from
            the cache warmer.  When provided, the SWAN runner blends it with
            HRRR for a full 72-hour forecast.  When None (GFS unavailable),
            the forecast is shortened to HRRR hours only (0–48).
        swan_binary: Absolute path to the SWAN executable.
        outer_grid_resolution_km: Outer grid spacing in km (default 3.0).
        inner_nest_resolution_m: Inner nest grid spacing in metres (default 200).
        compute_dt_min: SWAN internal time step in minutes (default 10).
        output_interval_hr: TABLE output timestep in hours (default 1).
        swan_timeout_s: Subprocess timeout in seconds (default 900 / 15 min).

    Returns:
        None.  Results are stored in the cache.

    Raises:
        Nothing.  All exceptions are caught, logged, and the last-good cache
        is left intact.
    """
    # ---------------------------------------------------------------------------
    # Remote mode detection — lazy initialisation on first run_all_spots() call.
    # Protected by _remote_init_lock so parallel test runners are safe.
    # ---------------------------------------------------------------------------
    if _remote_url is None and marine_config is not None:
        swan_cfg = getattr(marine_config, "swan", None)
        if swan_cfg is not None and swan_cfg.is_remote:
            with _remote_init_lock:
                # Double-check after acquiring the lock (another thread may have
                # initialised while we were waiting).
                if _remote_url is None:
                    activated = configure_remote_mode(swan_cfg.service_url, marine_config)
                    if not activated:
                        logger.warning(
                            "SWAN: remote mode startup probe failed; "
                            "continuing with bundled SWAN mode"
                        )
                        # _remote_url stays None → bundled mode continues.

    if _remote_url is not None:
        # Remote mode active: the background health thread fetches forecast data
        # from the remote service and populates the local last-good cache.
        # This function is a no-op — do NOT run SWAN locally.
        logger.debug(
            "SWAN: remote mode active (%s); local SWAN run skipped",
            _remote_url,
        )
        return

    if not _NEARSHORE_AVAILABLE:
        logger.debug(
            "SWAN: [nearshore] extra not installed; skipping SWAN run"
        )
        return

    if marine_config is None:
        return

    if not _swan_run_lock.acquire(blocking=False):
        logger.info("SWAN: run already in progress — skipping this trigger")
        return

    try:
        _run_all_spots_locked(
            marine_config,
            hrrr_wind_field=hrrr_wind_field,
            gfs_wind_field=gfs_wind_field,
            swan_binary=swan_binary,
            outer_grid_resolution_km=outer_grid_resolution_km,
            inner_nest_resolution_m=inner_nest_resolution_m,
            compute_dt_min=compute_dt_min,
            output_interval_hr=output_interval_hr,
            swan_timeout_s=swan_timeout_s,
        )
    finally:
        _swan_run_lock.release()


def _run_all_spots_locked(
    marine_config: Any,
    *,
    hrrr_wind_field: dict | None = None,
    gfs_wind_field: dict | None = None,
    swan_binary: str = _DEFAULT_SWAN_BINARY,
    outer_grid_resolution_km: float = _DEFAULT_OUTER_GRID_RESOLUTION_KM,
    inner_nest_resolution_m: float = _DEFAULT_GRID_RESOLUTION_M,
    compute_dt_min: int = _DEFAULT_COMPUTE_DT_MIN,
    output_interval_hr: float = _DEFAULT_OUTPUT_INTERVAL_HR,
    swan_timeout_s: int = _DEFAULT_SWAN_TIMEOUT_S,
) -> None:
    """Inner implementation of run_all_spots, called while holding _swan_run_lock."""
    # Collect surf locations (locations that have a surf config block).
    surf_spot_ids: set[str] = set(getattr(marine_config, "surf_spots", {}).keys())
    if not surf_spot_ids:
        logger.debug("SWAN: no surf spots configured; skipping SWAN run")
        return

    surf_locations = [
        loc
        for loc in getattr(marine_config, "locations", [])
        if loc.id in surf_spot_ids
    ]
    if not surf_locations:
        logger.debug("SWAN: no surf locations found; skipping SWAN run")
        return

    # Use canonical bboxes from MarineConfig (computed once at parse time).
    # inner_bbox = tight nearshore domain around surf spots
    # outer_bbox = wider HRRR-fetch area for the continental shelf approach
    inner_bbox = marine_config.swan_domain_bbox
    if inner_bbox is None:
        logger.debug("SWAN: no SWAN domain bbox (inner nest); skipping")
        return

    outer_bbox = marine_config.hrrr_bbox
    if outer_bbox is None:
        logger.debug("SWAN: no HRRR bbox (outer grid); skipping")
        return

    # Preserve domain_bbox alias for readability in code below that uses it
    # for WW3 domain centre computation.
    domain_bbox = inner_bbox

    # ------------------------------------------------------------------
    # 1. Use HRRR wind field passed by the cache warmer (single fetch).
    # ------------------------------------------------------------------
    if hrrr_wind_field is None:
        # Legacy fallback: fetch directly (used by manual trigger, tests).
        try:
            from weewx_clearskies_api.providers.wind import hrrr as _hrrr

            hrrr_wind_field = _hrrr.fetch(bbox=outer_bbox, max_forecast_hours=48)
        except Exception:
            logger.error(
                "SWAN: HRRR wind data unavailable; skipping SWAN run",
                exc_info=True,
            )
            return

    # ------------------------------------------------------------------
    # 1b. Fetch GFS wind for hours 48–72 (supplements HRRR extended range).
    #     On failure: set gfs_wind_field = None and log WARNING so the SWAN
    #     runner produces a shortened forecast (HRRR hours only) rather than
    #     aborting the entire run.
    # ------------------------------------------------------------------
    if gfs_wind_field is None and _gfs_wind is not None:
        try:
            gfs_wind_field = _gfs_wind.fetch(bbox=outer_bbox)
            logger.debug(
                "SWAN: GFS wind fetched inline (cycle=%s)",
                gfs_wind_field.get("cycle_time"),
            )
        except Exception:
            logger.warning(
                "SWAN: GFS wind unavailable; SWAN will produce shortened "
                "forecast (HRRR hours 0–48 only)",
                exc_info=True,
            )

    hrrr_cycle_time: str = hrrr_wind_field["cycle_time"]

    # Deduplication: skip if SWAN already ran for this HRRR cycle.
    run_marker_key = _build_run_marker_key(hrrr_cycle_time)
    if get_cache().get(run_marker_key) is not None:
        logger.debug(
            "SWAN: SWAN run already completed for HRRR cycle %s; skipping",
            hrrr_cycle_time,
        )
        return

    # ------------------------------------------------------------------
    # 2. Fetch WW3 boundary conditions (expected cache hit).
    # ------------------------------------------------------------------
    center_lat = (domain_bbox[1] + domain_bbox[3]) / 2.0
    center_lon = (domain_bbox[0] + domain_bbox[2]) / 2.0
    try:
        from weewx_clearskies_api.providers.marine import wavewatch

        ww3_boundary = wavewatch.fetch(lat=center_lat, lon=center_lon)
    except Exception:
        logger.warning(
            "SWAN: WW3 boundary data unavailable; SWAN will use calm boundary",
            exc_info=True,
        )
        ww3_boundary = {"forecast": [], "grid": "unavailable", "model_run": ""}

    # ------------------------------------------------------------------
    # 2b. CO-OPS tide predictions for SWAN WLEVEL input (T2.1).
    #     Fetches harmonic tidal predictions for the nearest CO-OPS station.
    #     On failure: tide_predictions = None → SWAN runs at static MSL.
    # ------------------------------------------------------------------
    tide_predictions: list[dict] | None = None
    for loc in surf_locations:
        coops_ids: list[str] = getattr(loc, "coops_station_ids", [])
        if coops_ids:
            try:
                from weewx_clearskies_api.providers.tides import coops as _coops

                coops_result = _coops.fetch(
                    station_id=coops_ids[0],
                    products=("predictions",),
                )
                preds = coops_result.get("predictions", [])
                if preds:
                    # Convert TidePrediction objects to plain dicts for runner
                    tide_predictions = [
                        {"time": p.time, "height": p.height}
                        for p in preds
                        if p.time and p.height is not None
                    ]
                    logger.info(
                        "SWAN: fetched %d CO-OPS tide predictions "
                        "from station %s for WLEVEL input",
                        len(tide_predictions),
                        coops_ids[0],
                    )
                    break
            except Exception:
                logger.warning(
                    "SWAN: CO-OPS tide predictions unavailable "
                    "(station %s); SWAN will run at static MSL",
                    coops_ids[0],
                    exc_info=True,
                )
                break  # Don't try remaining stations if first fails — same tidal regime

    # OFS gridded surface currents (T2.2).  Fetched as full 2-D U/V arrays
    # via ofs.fetch_surface_currents().  On any failure, falls back to None
    # so SWAN runs without current forcing (safe default per SWAN manual).
    ofs_currents: list[dict] | None = None
    try:
        from weewx_clearskies_api.providers.ocean import ofs as _ofs

        _raw_currents = _ofs.fetch_surface_currents(
            bbox=outer_bbox,
            forecast_hours=72,
        )
        if _raw_currents:
            ofs_currents = _raw_currents
            logger.info(
                "SWAN: OFS surface currents fetched (%d timestep(s))",
                len(ofs_currents),
            )
        else:
            logger.debug(
                "SWAN: OFS surface currents unavailable; SWAN will run "
                "without current forcing"
            )
    except Exception:
        logger.warning(
            "SWAN: OFS surface current fetch failed; SWAN will run "
            "without current forcing",
            exc_info=True,
        )

    # ------------------------------------------------------------------
    # 2c. Build coastal structures for OBSTACLE commands (T2.3).
    #     StructureConfig.coordinates is populated by the wizard Overpass
    #     discovery endpoint.  Structures without coordinates are skipped —
    #     they continue to receive post-processing via wave_transform.py
    #     Supplement 2 until coordinates are available.
    # ------------------------------------------------------------------
    obstacle_structures: list[dict] = []
    for loc in surf_locations:
        surf_cfg = getattr(marine_config, "surf_spots", {}).get(loc.id)
        if surf_cfg is None:
            continue
        for structure in getattr(surf_cfg, "structures", []):
            coords = getattr(structure, "coordinates", None)
            if coords and isinstance(coords, list) and len(coords) >= 2:
                obstacle_structures.append({
                    "type": structure.type,
                    "coordinates": coords,
                })
    structures_for_swan: list[dict] | None = obstacle_structures or None

    # ------------------------------------------------------------------
    # 3. Resolve grid resolution config before CUDEM download (needs resolution).
    # ------------------------------------------------------------------
    surf_spots_config: dict[str, dict[str, float]] = {
        loc.id: {"lon": loc.lon, "lat": loc.lat} for loc in surf_locations
    }

    # T3.1 — build per-spot transect configs for CURVE output.
    # Converts SurfSpotConfig.beach_facing_degrees and bathymetric_profile into
    # the plain-dict shape that build_swan_input() + compute_spot_transect() expect.
    # Also loads or refreshes the bidirectional CUDEM profile (180-day cache) so
    # compute_spot_transect() can extend the transect from the actual coastline
    # (~0 m depth) rather than the operator's pin — ensuring the surf zone is
    # included in the SWAN CURVE output.
    spot_configs_for_runner: dict[str, dict] = {}
    _PROFILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for loc in surf_locations:
        spot_cfg = marine_config.surf_spots.get(loc.id)
        if spot_cfg is None:
            continue
        cfg_dict: dict[str, Any] = {
            "beach_facing_degrees": float(spot_cfg.beach_facing_degrees),
        }

        # Load or refresh the bidirectional CUDEM profile for this spot.
        cache_path = _PROFILE_CACHE_DIR / f"{loc.id}.json"
        runtime_profile: dict | None = None

        if cache_path.exists():
            age_s = time.time() - cache_path.stat().st_mtime
            if age_s < _PROFILE_MAX_AGE_S:
                try:
                    runtime_profile = json.loads(cache_path.read_text(encoding="utf-8"))
                    logger.debug(
                        "SWAN: loaded bidirectional profile for spot %r from cache "
                        "(age %.0f days, %d points).",
                        loc.id,
                        age_s / 86400,
                        len((runtime_profile or {}).get("profile", [])),
                    )
                except Exception:
                    logger.warning(
                        "SWAN: bidirectional profile cache for spot %r is corrupt; "
                        "re-downloading.",
                        loc.id,
                        exc_info=True,
                    )
                    runtime_profile = None

        if runtime_profile is None:
            try:
                from weewx_clearskies_api.enrichment.bathymetry import (  # noqa: PLC0415
                    download_bidirectional_profile,
                )

                runtime_profile = download_bidirectional_profile(
                    loc.lat,
                    loc.lon,
                    float(spot_cfg.beach_facing_degrees),
                )
                cache_path.write_text(json.dumps(runtime_profile), encoding="utf-8")
                logger.info(
                    "SWAN: downloaded and cached bidirectional CUDEM profile for "
                    "spot %r (%d points, coastline=%.6f,%.6f).",
                    loc.id,
                    len(runtime_profile.get("profile", [])),
                    runtime_profile.get("coastline_lat", loc.lat),
                    runtime_profile.get("coastline_lon", loc.lon),
                )
            except Exception:
                logger.warning(
                    "SWAN: bidirectional profile download failed for spot %r; "
                    "falling back to wizard-configured profile for this run.",
                    loc.id,
                    exc_info=True,
                )
                runtime_profile = None

        if runtime_profile is not None:
            cfg_dict["runtime_profile"] = runtime_profile

        spot_configs_for_runner[loc.id] = cfg_dict

    swan_cfg = getattr(marine_config, "swan", None)
    omp_num_threads: int = getattr(swan_cfg, "omp_num_threads", 0) if swan_cfg else 0
    _cfg_outer_km = getattr(swan_cfg, "outer_grid_resolution_km", None) if swan_cfg else None
    _cfg_inner_m = getattr(swan_cfg, "inner_nest_resolution_m", None) if swan_cfg else None
    resolved_outer_km: float = float(_cfg_outer_km) if _cfg_outer_km is not None else outer_grid_resolution_km
    resolved_inner_m: float = float(_cfg_inner_m) if _cfg_inner_m is not None else inner_nest_resolution_m

    # ------------------------------------------------------------------
    # 4. Domain sizing + per-level bathymetry (3-level nesting, Phase 14).
    # ------------------------------------------------------------------
    from weewx_clearskies_api.services.swan_domain import compute_domains

    spot_locations_for_domains = []
    for loc in surf_locations:
        entry: dict[str, Any] = {
            "id": loc.id,
            "lat": loc.lat,
            "lon": loc.lon,
            "beach_facing_degrees": float(
                getattr(marine_config.surf_spots.get(loc.id), "beach_facing_degrees", 0.0)
            ),
        }
        # Extract distance to 15m depth from the cached profile so the Level 3
        # grid extends to the actual 15m contour, not a hardcoded distance.
        runner_cfg = spot_configs_for_runner.get(loc.id, {})
        rt_profile = runner_cfg.get("runtime_profile")
        if isinstance(rt_profile, dict):
            profile_pts = rt_profile.get("profile", [])
            if profile_pts:
                closest = min(profile_pts, key=lambda pt: abs(pt.get("depth_m", 0) - 15.0))
                entry["offshore_distance_m"] = float(closest.get("distance_m", 0))
        spot_locations_for_domains.append(entry)

    domains = compute_domains(spot_locations_for_domains)
    logger.info(
        "SWAN: 3-level domains computed — L1: %d cells, L2: %d cells, L3: %d clusters (%d total cells)",
        domains.level1.cell_count,
        domains.level2.cell_count,
        len(domains.level3_clusters),
        domains.total_cells,
    )

    bathymetry = download_all_bathymetry(domains)

    swan_config: dict[str, Any] = {
        "outer_bbox": list(outer_bbox),
        "inner_bbox": list(inner_bbox),
        "surf_spots": surf_spots_config,
        "swan_binary": swan_binary,
        "outer_grid_resolution_km": resolved_outer_km,
        "inner_nest_resolution_m": resolved_inner_m,
        "compute_dt_min": compute_dt_min,
        "output_interval_hr": output_interval_hr,
        "swan_timeout_s": swan_timeout_s,
        "omp_num_threads": omp_num_threads,
        "spot_configs": spot_configs_for_runner or None,
    }

    runner = _SWANRunnerWithCleanup(swan_config)
    run_time = datetime.now(UTC)

    logger.info(
        "SWAN: starting 3-level SWAN run for %d spot(s) "
        "(HRRR cycle=%s, GFS=%s)",
        len(surf_spots_config),
        hrrr_cycle_time,
        gfs_wind_field.get("cycle_time") if gfs_wind_field else "unavailable",
    )

    try:
        results = runner.run_3level(
            domains,
            bathymetry,
            hrrr_wind_field,
            gfs_wind_field,
            ww3_boundary,
            tide_predictions=tide_predictions,
            ofs_currents=ofs_currents,
            structures=structures_for_swan,
        )
        spectral_results: dict[str, list] = getattr(runner, "_spectral_results", {})
    except SWANRunError as exc:
        logger.error(
            "SWAN: 3-level run failed (returncode=%s); "
            "last-good cache preserved\nSWAN stderr: %s",
            exc.returncode,
            exc.stderr[:2000] if exc.stderr else "(no stderr)",
        )
        return
    except Exception:
        logger.error(
            "SWAN: unexpected error during 3-level run; "
            "last-good cache preserved",
            exc_info=True,
        )
        return

    # ------------------------------------------------------------------
    # 5. Successful run: cache per-spot results and clean up tmpdir.
    # ------------------------------------------------------------------
    run_time_iso = run_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = get_cache()

    # Pre-parse hrrr_cycle_time for windSource attribution (T7.3).
    # Points at or before HRRR hour 48 are wind-forced by HRRR;
    # points beyond hour 48 are wind-forced by GFS (when available).
    try:
        hrrr_cycle_dt = datetime.fromisoformat(hrrr_cycle_time.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        hrrr_cycle_dt = None

    spots_cached = 0
    for spot_id, forecast_points in results.items():
        if not forecast_points:
            logger.warning(
                "SWAN: spot %r returned no valid wave data (all timesteps "
                "failed physical validation); skipping cache update for this spot",
                spot_id,
            )
            continue

        # Build per-point dicts with windSource attribution.
        forecast_dicts: list[dict[str, Any]] = []
        for pt in forecast_points:
            d = pt.model_dump()
            if hrrr_cycle_dt is not None:
                try:
                    pt_dt = datetime.fromisoformat(pt.time.replace("Z", "+00:00"))
                    fhour = (pt_dt - hrrr_cycle_dt).total_seconds() / 3600.0
                    d["windSource"] = "gfs" if fhour > 48.0 else "hrrr"
                except (ValueError, TypeError):
                    d["windSource"] = "hrrr"
            else:
                d["windSource"] = "hrrr"
            forecast_dicts.append(d)

        # T3.4 — Build transect data for beach profile endpoint (T5.1).
        # Groups all cross-shore transect points by timestep (ISO time key).
        # This preserves the full spatial profile at every timestep so T5.1
        # can render the beach bathymetry / wave-height cross-section.
        transect_by_time: dict[str, list] = defaultdict(list)
        for pt in forecast_points:
            _pt_time = getattr(pt, "time", None)
            if not _pt_time:
                continue
            transect_by_time[_pt_time].append({
                "distanceFromShore": getattr(pt, "distanceFromShore", None),
                "depth": getattr(pt, "depth", None),
                "waveHeight": (
                    round(pt.waveHeight, 3)
                    if getattr(pt, "waveHeight", None) is not None
                    else None
                ),
                "swellHeight": (
                    round(pt.swellHeight, 3)
                    if getattr(pt, "swellHeight", None) is not None
                    else None
                ),
                "breakingFraction": (
                    round(pt.breakingFraction, 4)
                    if getattr(pt, "breakingFraction", None) is not None
                    else None
                ),
                "breakingDissipation": (
                    round(pt.breakingDissipation, 3)
                    if getattr(pt, "breakingDissipation", None) is not None
                    else None
                ),
            })

        payload = {
            "forecast": forecast_dicts,
            # T3.3 — per-timestep spectral decomposition from SWAN SPECOUT
            "spectral": spectral_results.get(spot_id, []),
            # T3.4 — full cross-shore transect per timestep (for beach profile endpoint T5.1)
            "transect": dict(transect_by_time),
            "run_time": run_time_iso,
            "hrrr_cycle_time": hrrr_cycle_time,
        }
        cache.set(
            _build_last_good_key(spot_id),
            payload,
            _LAST_GOOD_TTL_SECONDS,
        )
        spots_cached += 1

    # Store the run marker so duplicate runs for this HRRR cycle are skipped.
    # Only store when at least one spot produced valid data — otherwise a
    # failed SWAN run (exit 0 but empty output) would block all future
    # attempts for this cycle.
    if spots_cached > 0:
        cache.set(run_marker_key, {"run_time": run_time_iso}, _CACHE_TTL_SECONDS)
        global _last_full_run_monotonic  # noqa: PLW0603
        import time as _time
        _last_full_run_monotonic = _time.monotonic()
    else:
        logger.warning(
            "SWAN: SWAN exited cleanly but 0/%d spot(s) produced valid "
            "data — run marker NOT stored (will retry on next cycle)",
            len(surf_spots_config),
        )

    # 3-level path uses persistent workdir — no tmpdir cleanup needed.

    elapsed_s = int((datetime.now(UTC) - run_time).total_seconds())
    logger.info(
        "SWAN: SWAN run complete in %ds — %d/%d spot(s) cached "
        "(HRRR cycle=%s)",
        elapsed_s,
        spots_cached,
        len(surf_spots_config),
        hrrr_cycle_time,
    )


# ---------------------------------------------------------------------------
# Quick update — stationary inner nest only (hourly, between full runs)
# ---------------------------------------------------------------------------

#: Timestamp of the last full run completion (monotonic), used to skip
#: quick updates for 30 minutes after a full run.
_last_full_run_monotonic: float = 0.0
_QUICK_UPDATE_COOLDOWN_S: float = 1800.0  # 30 min


def run_quick_update(
    marine_config: Any,
    *,
    hrrr_wind_field: dict | None = None,
) -> None:
    """Run a stationary SWAN Level 3 update with the latest HRRR wind.

    Produces a single "current snapshot" per surf spot and merges it into the
    existing forecast cache (replaces the entry closest to the snapshot time).
    Reuses the Level 2 NESTOUT boundary file from the last full 3-level run.

    This is the 3-level equivalent of the old ``run_stationary_inner()`` path.
    It uses the same domain sizing (``compute_domains()``) and per-cluster
    bathymetry as the full run, but only executes Level 3 (stationary mode).

    Skipped when:
      - A full run completed within the last 30 minutes
      - No previous full run has produced a Level 2 NESTOUT file
      - [nearshore] extra not installed
      - No surf spots configured
    """
    global _last_full_run_monotonic  # noqa: PLW0602 — read-only here

    import time as _time

    if _time.monotonic() - _last_full_run_monotonic < _QUICK_UPDATE_COOLDOWN_S:
        logger.debug("SWAN quick update: skipped (full run completed %.0fs ago)",
                     _time.monotonic() - _last_full_run_monotonic)
        return

    if not _NEARSHORE_AVAILABLE:
        return
    if marine_config is None:
        return
    if _remote_url is not None:
        return

    if not _swan_run_lock.acquire(blocking=False):
        logger.info("SWAN quick update: full run in progress — skipping")
        return

    try:
        _run_quick_update_locked(marine_config, hrrr_wind_field=hrrr_wind_field)
    finally:
        _swan_run_lock.release()


def _run_quick_update_locked(
    marine_config: Any,
    *,
    hrrr_wind_field: dict | None = None,
) -> None:
    """Inner implementation of run_quick_update, called while holding _swan_run_lock."""
    surf_spot_ids = set(getattr(marine_config, "surf_spots", {}).keys())
    if not surf_spot_ids:
        return

    surf_locations = [
        loc for loc in getattr(marine_config, "locations", [])
        if loc.id in surf_spot_ids
    ]
    if not surf_locations:
        return

    inner_bbox = marine_config.swan_domain_bbox
    outer_bbox = marine_config.hrrr_bbox
    if inner_bbox is None or outer_bbox is None:
        return

    if hrrr_wind_field is None:
        return

    # ── 3-level domain sizing (same math as full run, no I/O) ──
    from weewx_clearskies_api.services.swan_domain import compute_domains

    spot_locations_for_domains = []
    for loc in surf_locations:
        entry: dict[str, Any] = {
            "id": loc.id,
            "lat": loc.lat,
            "lon": loc.lon,
            "beach_facing_degrees": float(
                getattr(marine_config.surf_spots.get(loc.id), "beach_facing_degrees", 0.0)
            ),
        }
        cache_path = _PROFILE_CACHE_DIR / f"{loc.id}.json"
        if cache_path.exists():
            try:
                profile_data = json.loads(cache_path.read_text(encoding="utf-8"))
                profile_pts = profile_data.get("profile", [])
                if profile_pts:
                    closest = min(profile_pts, key=lambda pt: abs(pt.get("depth_m", 0) - 15.0))
                    entry["offshore_distance_m"] = float(closest.get("distance_m", 0))
            except Exception:
                pass
        spot_locations_for_domains.append(entry)
    domains = compute_domains(spot_locations_for_domains)

    # ── Check that Level 2 NESTOUT exists from the last full 3-level run ──
    # The file is named nest_out.dat (not the old nest_boundary.dat) per the
    # nesting file convention (SWAN-FIXES-PLAN Bug 1, 2026-07-19).
    swan_work = Path("/var/run/weewx-clearskies/swan")
    l2_nestout = swan_work / "level2" / "nest_out.dat"
    if not l2_nestout.exists():
        logger.debug(
            "SWAN quick update: no Level 2 NESTOUT at %s; skipping", l2_nestout,
        )
        return

    # ── Per-cluster Level 3 bathymetry (cached from last full run) ──
    l3_bathymetry: dict[int, dict] = {}
    for idx, cluster in enumerate(domains.level3_clusters):
        if cluster.grid is not None:
            l3_bathymetry[idx] = download_bathymetry_for_level(cluster.grid, level=3)
    bathymetry = {"level3": l3_bathymetry}

    # ── Build runner and spot configs ──
    swan_cfg = getattr(marine_config, "swan", None)
    omp_threads = getattr(swan_cfg, "omp_num_threads", 0) if swan_cfg else 0
    _cfg_outer_km = getattr(swan_cfg, "outer_grid_resolution_km", None) if swan_cfg else None
    _cfg_inner_m = getattr(swan_cfg, "inner_nest_resolution_m", None) if swan_cfg else None
    resolved_outer_km = float(_cfg_outer_km) if _cfg_outer_km is not None else _DEFAULT_OUTER_GRID_RESOLUTION_KM
    resolved_inner_m = float(_cfg_inner_m) if _cfg_inner_m is not None else _DEFAULT_GRID_RESOLUTION_M

    surf_spots_config = {loc.id: {"lon": loc.lon, "lat": loc.lat} for loc in surf_locations}

    # Build per-spot transect configs (same as full run — reuses cached profiles)
    spot_configs_for_runner: dict[str, dict] = {}
    for loc in surf_locations:
        spot_cfg = marine_config.surf_spots.get(loc.id)
        if spot_cfg is None:
            continue
        cfg_dict: dict[str, Any] = {
            "beach_facing_degrees": float(spot_cfg.beach_facing_degrees),
        }
        cache_path = _PROFILE_CACHE_DIR / f"{loc.id}.json"
        if cache_path.exists():
            try:
                cfg_dict["runtime_profile"] = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        spot_configs_for_runner[loc.id] = cfg_dict

    swan_config: dict[str, Any] = {
        "outer_bbox": list(outer_bbox),
        "inner_bbox": list(inner_bbox),
        "surf_spots": surf_spots_config,
        "swan_binary": _DEFAULT_SWAN_BINARY,
        "outer_grid_resolution_km": resolved_outer_km,
        "inner_nest_resolution_m": resolved_inner_m,
        "compute_dt_min": _DEFAULT_COMPUTE_DT_MIN,
        "output_interval_hr": _DEFAULT_OUTPUT_INTERVAL_HR,
        "swan_timeout_s": 120,
        "omp_num_threads": omp_threads,
        "spot_configs": spot_configs_for_runner or None,
    }

    runner = _SWANRunnerWithCleanup(swan_config)
    run_time = datetime.now(UTC)

    hrrr_cycle = hrrr_wind_field.get("cycle_time", "")
    logger.info(
        "SWAN quick update: stationary Level 3 for %d spot(s) (%d clusters, HRRR cycle=%s)",
        len(surf_spots_config), len(domains.level3_clusters), hrrr_cycle,
    )

    try:
        results = runner.run_stationary_level3(
            swan_work, hrrr_wind_field, domains, bathymetry,
        )
    except SWANRunError as exc:
        logger.warning(
            "SWAN quick update failed: %s\nSWAN stderr: %s",
            exc, exc.stderr[:500] if exc.stderr else "",
        )
        return
    except Exception:
        logger.warning("SWAN quick update failed unexpectedly", exc_info=True)
        return

    # Merge results into the existing forecast cache
    run_time_iso = run_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = get_cache()
    spots_updated = 0

    for spot_id, forecast_points in results.items():
        if not forecast_points:
            continue

        # Build the snapshot entry
        snapshot = forecast_points[0]
        snapshot_dict = snapshot.model_dump()
        snapshot_dict["windSource"] = "hrrr"

        # Read existing forecast cache
        last_good = cache.get(_build_last_good_key(spot_id))
        if last_good is None:
            continue

        existing_forecast: list[dict] = last_good.get("forecast", [])
        if not existing_forecast:
            continue

        # Find the entry closest to the snapshot time and replace it
        snapshot_time = snapshot.time
        best_idx = 0
        best_diff = float("inf")
        for idx, entry in enumerate(existing_forecast):
            entry_time = entry.get("time", "")
            if entry_time and snapshot_time:
                try:
                    t1 = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
                    diff = abs((t1 - t2).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best_idx = idx
                except (ValueError, TypeError):
                    pass

        existing_forecast[best_idx] = snapshot_dict
        last_good["forecast"] = existing_forecast
        last_good["run_time"] = run_time_iso
        cache.set(_build_last_good_key(spot_id), last_good, _LAST_GOOD_TTL_SECONDS)
        spots_updated += 1

    elapsed_s = int((datetime.now(UTC) - run_time).total_seconds())
    logger.info(
        "SWAN quick update complete in %ds — %d spot(s) updated (HRRR cycle=%s)",
        elapsed_s, spots_updated, hrrr_cycle,
    )
