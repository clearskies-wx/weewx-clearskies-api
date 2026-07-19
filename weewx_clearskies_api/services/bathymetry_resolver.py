"""Bathymetry data source resolver for SWAN nearshore modeling.

Resolves the best available bathymetry data source for a given bounding box,
implementing a priority chain:

  1. NCEI Regional Coastal DEMs (via OPeNDAP) — ~10m resolution
  2. USGS Great Lakes seamless DEMs (local GeoTIFF via rasterio) — ~3-5m
  3. CRM / DEM_all fallback — ~90m resolution (handled by the caller in swan.py)

The wiring into ``swan.py``'s ``download_bathymetry_for_level()`` is a separate
task (Phase 20, T20.3).  This module exposes the resolver and fetcher functions
only.

Public API:
    find_best_dem(bbox)           — T19.2: find finest NCEI regional DEM
    fetch_opendap_grid(...)       — T20.1: OPeNDAP subset via xarray
    normalize_to_msl(...)         — T20.2: apply VDatum datum correction
    is_great_lake(lat, lon)       — T21.1: check if point is on a Great Lake
    fetch_great_lake_grid(...)    — T21.1: rasterio windowed read from USGS DEM

Private helpers:
    _load_dem_index()             — lazy-load the NCEI DEM index JSON
    _query_vdatum_offset(...)     — query NOAA VDatum REST API (cached, rate-limited)
    _detect_vdatum_region(...)    — map lat/lon to VDatum region string
    _ensure_great_lake_dem(lake)  — download + cache USGS Great Lakes GeoTIFF
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Optional heavy dependencies
# xarray: installed via [marine] extra — needed for OPeNDAP fetching.
# rasterio: NOT currently in dependencies (added separately) — needed for
#   Great Lakes GeoTIFF windowed reads.  Both are imported conditionally.
# ---------------------------------------------------------------------------

try:
    import xarray as xr  # type: ignore[import-untyped]

    _XARRAY_AVAILABLE: bool = True
except ImportError:
    _XARRAY_AVAILABLE = False

_RASTERIO_AVAILABLE: bool = False
try:
    import rasterio  # type: ignore[import-untyped]
    import rasterio.windows  # type: ignore[import-untyped]

    _RASTERIO_AVAILABLE = True
except ImportError:
    logger_init = logging.getLogger(__name__)
    logger_init.info(
        "rasterio not installed — USGS Great Lakes DEM path disabled. "
        "Install rasterio (add to [nearshore] extra) to enable Great Lakes bathymetry."
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths and URL constants
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent.parent / "data"
_DEM_INDEX_PATH = _DATA_DIR / "ncei_regional_dem_index.json"

_OPENDAP_BASE_URL = "https://www.ngdc.noaa.gov/thredds/dodsC/regional/{filename}"
_OPENDAP_TIMEOUT_S: float = 30.0

_VDATUM_API_URL = "https://vdatum.noaa.gov/vdatumweb/api/convert"
_VDATUM_RATE_LIMIT_S: float = 1.0  # conservative — government service, no published limit

_SCIENCEBASE_API_URL = "https://www.sciencebase.gov/catalog/item/{item_id}?format=json"
_GREAT_LAKES_CACHE_DIR = Path("/etc/weewx-clearskies/great_lakes")
_GREAT_LAKES_TTL_S: float = 365 * 86400.0  # 365-day TTL

# ---------------------------------------------------------------------------
# Module-level mutable state (caches)
# ---------------------------------------------------------------------------

_DEM_INDEX: list[dict[str, Any]] | None = None

# VDatum offset cache: (lat_rounded_to_0.1, lon_rounded_to_0.1, datum) → offset_m
_VDATUM_CACHE: dict[tuple[float, float, str], float] = {}
_last_vdatum_request_time: float = 0.0

# ---------------------------------------------------------------------------
# Great Lakes bounding boxes (lat_min, lat_max, lon_min, lon_max)
# Generous margins to ensure all nearshore pixels are captured.
#
# IMPORTANT: iteration order matters.  Lake St. Clair (42.2-42.7N,
# 82.3-82.9W) lies geographically inside Erie's generous bbox, so St. Clair
# must appear BEFORE Erie in this dict.  All other bboxes are sufficiently
# non-overlapping that order is irrelevant.
# ---------------------------------------------------------------------------

_GREAT_LAKE_BOXES: dict[str, tuple[float, float, float, float]] = {
    "superior": (46.4, 49.0, -92.2, -84.3),
    "michigan": (41.6, 46.1, -87.8, -84.7),
    "huron": (43.0, 46.3, -84.8, -79.7),
    "st_clair": (42.2, 42.7, -82.9, -82.3),   # before erie — bbox overlaps
    "erie": (41.3, 42.9, -83.6, -78.8),
    "ontario": (43.1, 44.3, -79.9, -75.7),
}

# ScienceBase item IDs (Rohweder 2025, DOI: 10.5066/P1DA6L6U)
_GREAT_LAKE_SCIENCEBASE_IDS: dict[str, str] = {
    "michigan": "669041b8d34e341cbf15576c",
    "erie": "66903b2dd34e7f6636ec211b",
    "huron": "66904150d34e341cbf15576a",
    "ontario": "669041edd34e341cbf15576e",
    "superior": "6690427ad34e341cbf155772",
    "st_clair": "66904251d34e341cbf155770",
}

# Our datum names → NOAA VDatum frame identifiers
# Supports tidal datums (MHW, MLLW, MHHW), geoid-based (NAVD88, NGVD29),
# and Great Lakes (IGLD85).
_DATUM_TO_VDATUM_FRAME: dict[str, str] = {
    "NAVD88": "NAVD88",
    "NGVD29": "NGVD29",
    "MHW": "MHW",
    "MLLW": "MLLW",
    "MHHW": "MHHW",
    "MTL": "MTL",
    "DTL": "DTL",
    "MLW": "MLW",
    "IGLD85": "IGLD85",
    "MSL": "LMSL",
    "LMSL": "LMSL",
}

_DEFAULT_OCEAN_DEPTH_M: float = -15.0  # CUDEM sign: negative = ocean


# ===========================================================================
# §1 — DEM Index loader
# ===========================================================================


def _load_dem_index() -> list[dict[str, Any]]:
    """Lazily load and cache the NCEI regional DEM index JSON.

    The index is built offline by ``scripts/build_ncei_dem_index.py`` and
    ships with the API package at ``data/ncei_regional_dem_index.json``.

    Returns:
        List of DEM metadata dicts.  Empty list if the file does not exist
        (e.g. during development before the build script is run).
    """
    global _DEM_INDEX
    if _DEM_INDEX is not None:
        return _DEM_INDEX

    if not _DEM_INDEX_PATH.exists():
        logger.warning(
            "NCEI DEM index not found at %s — regional DEMs unavailable. "
            "Run scripts/build_ncei_dem_index.py to generate it.",
            _DEM_INDEX_PATH,
        )
        _DEM_INDEX = []
        return _DEM_INDEX

    try:
        raw = _DEM_INDEX_PATH.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
        _DEM_INDEX = data.get("dems", [])
        logger.debug(
            "Loaded NCEI DEM index: %d entries from %s",
            len(_DEM_INDEX),
            _DEM_INDEX_PATH,
        )
    except Exception:
        logger.warning(
            "Failed to parse NCEI DEM index from %s",
            _DEM_INDEX_PATH,
            exc_info=True,
        )
        _DEM_INDEX = []

    return _DEM_INDEX


# ===========================================================================
# §2 — find_best_dem  (T19.2)
# ===========================================================================


def find_best_dem(
    bbox: tuple[float, float, float, float],
) -> dict[str, Any] | None:
    """Find the finest-resolution NCEI regional DEM that fully contains *bbox*.

    Scans the shipped DEM index for entries whose bounding box fully
    encloses the query bbox, then returns the one with the smallest
    (finest) ``resolution_arcsec``.

    Partial containment is intentionally excluded — SWAN requires a
    complete depth grid covering the domain.  Callers fall back to CRM when
    this function returns ``None``.

    Args:
        bbox: ``(lon_min, lat_min, lon_max, lat_max)`` — query bounding box.

    Returns:
        Dict with keys: ``filename``, ``resolution_m``, ``vertical_datum``,
        ``elevation_var`` — or ``None`` if no DEM fully covers *bbox*.

    Example::

        >>> find_best_dem((-118.06, 33.60, -117.98, 33.67))
        {'filename': 'orange_county_13_navd88_2015.nc',
         'resolution_m': 10.0, 'vertical_datum': 'NAVD88', 'elevation_var': 'Band1'}
    """
    lon_min, lat_min, lon_max, lat_max = bbox

    candidates: list[dict[str, Any]] = [
        entry
        for entry in _load_dem_index()
        if (
            float(entry["lat_min"]) <= lat_min
            and float(entry["lat_max"]) >= lat_max
            and float(entry["lon_min"]) <= lon_min
            and float(entry["lon_max"]) >= lon_max
        )
    ]

    if not candidates:
        return None

    # Finest resolution wins (smallest arcsec value = most detail)
    best = min(candidates, key=lambda e: float(e["resolution_arcsec"]))
    return {
        "filename": str(best["filename"]),
        "resolution_m": float(best["resolution_m_approx"]),
        "vertical_datum": str(best["vertical_datum"]),
        "elevation_var": str(best["elevation_var"]),
    }


# ===========================================================================
# §3 — fetch_opendap_grid  (T20.1)
# ===========================================================================


def fetch_opendap_grid(
    filename: str,
    bbox: tuple[float, float, float, float],
    resolution_m: float,
    elevation_var: str,
) -> dict[str, Any]:
    """Fetch a depth grid subset from an NCEI regional DEM via OPeNDAP.

    Uses xarray to open the remote NetCDF via OPeNDAP and fetch only the
    requested bbox — no full-file download.  A 30-second timeout is applied
    via a ThreadPoolExecutor; a timed-out thread is abandoned (daemon) and
    a ``RuntimeError`` is raised so the caller falls back to CRM.

    Sign convention: CUDEM/regional DEMs use negative = ocean, positive = land.
    ``cudem_to_swan_bottom()`` expects this convention — no sign flip is done here.

    Args:
        filename: NetCDF filename in the NCEI THREDDS ``regional`` catalog
            (e.g. ``"orange_county_13_navd88_2015.nc"``).
        bbox: ``(lon_min, lat_min, lon_max, lat_max)`` — target domain.
        resolution_m: Target grid resolution in metres.  If coarser than the
            DEM's native resolution the subset is coarsened *before* triggering
            the OPeNDAP download so the wire transfer stays small.
        elevation_var: Name of the elevation variable in the NetCDF file.
            ``"Band1"`` for pre-~2015 DEMs, ``"z"`` for newer ones.

    Returns:
        Grid dict compatible with ``cudem_to_swan_bottom()``::

            {
                "lat_first": float, "lon_first": float,
                "lat_last":  float, "lon_last":  float,
                "ni": int,   "nj": int,
                "depths": list[list[float]],   # [nj][ni], neg=ocean
            }

    Raises:
        RuntimeError: If xarray is unavailable, or on OPeNDAP timeout /
            network failure / missing variable.  The caller (swan.py) catches
            this and falls back to CRM.
    """
    if not _XARRAY_AVAILABLE:
        raise RuntimeError(
            "xarray is not available — install weewx-clearskies-api[marine] "
            "to enable OPeNDAP bathymetry fetching"
        )

    lon_min, lat_min, lon_max, lat_max = bbox
    url = _OPENDAP_BASE_URL.format(filename=filename)

    def _inner() -> dict[str, Any]:
        """Run inside a thread so the ThreadPoolExecutor can enforce timeout."""
        import numpy as np  # type: ignore[import-untyped]

        ds = xr.open_dataset(url, engine="netcdf4", mask_and_scale=False)
        try:
            # Ensure lat is south-to-north for consistent slicing
            if "lat" not in ds.coords:
                raise ValueError(
                    f"Dataset {filename!r} has no 'lat' coordinate. "
                    f"Found: {list(ds.coords)}"
                )
            ds_sorted = ds.sortby("lat")

            # Detect 0-360 longitude convention
            lon_coord = ds_sorted.coords["lon"]
            uses_360 = float(lon_coord.min()) >= 0.0 and float(lon_coord.max()) > 180.0
            q_lon_min = (lon_min + 360.0) if (uses_360 and lon_min < 0.0) else lon_min
            q_lon_max = (lon_max + 360.0) if (uses_360 and lon_max < 0.0) else lon_max

            # Validate that the requested variable exists
            if elevation_var not in ds_sorted.data_vars:
                available = list(ds_sorted.data_vars)
                raise ValueError(
                    f"Elevation variable {elevation_var!r} not found in "
                    f"{filename!r}. Available: {available}"
                )

            # Subset to bbox (only downloads metadata at this point)
            subset = ds_sorted[elevation_var].sel(
                lat=slice(lat_min, lat_max),
                lon=slice(q_lon_min, q_lon_max),
            )

            if subset.sizes.get("lat", 0) < 2 or subset.sizes.get("lon", 0) < 2:
                raise ValueError(
                    f"OPeNDAP subset for {filename!r} at bbox={bbox} is too small: "
                    f"{dict(subset.sizes)}"
                )

            # Determine native resolution to decide whether to coarsen
            lat_vals_raw = subset.coords["lat"].values
            native_dlat_deg = float(abs(lat_vals_raw[1] - lat_vals_raw[0]))
            native_res_m = native_dlat_deg * 111_320.0

            # Coarsen before download if requested resolution is notably coarser
            if resolution_m > native_res_m * 1.5:
                coarsen_factor = max(1, int(round(resolution_m / native_res_m)))
                max_factor = max(
                    1, min(subset.sizes["lat"] // 2, subset.sizes["lon"] // 2)
                )
                coarsen_factor = min(coarsen_factor, max_factor)
                if coarsen_factor > 1:
                    subset = subset.coarsen(
                        lat=coarsen_factor,
                        lon=coarsen_factor,
                        boundary="trim",
                    ).mean()

            # Trigger the actual OPeNDAP download
            values: np.ndarray = subset.values.astype(float)

            # Replace NaN / large sentinel values with default ocean depth
            values[np.isnan(values)] = _DEFAULT_OCEAN_DEPTH_M
            values[values > 1e10] = _DEFAULT_OCEAN_DEPTH_M
            values[values < -1e10] = _DEFAULT_OCEAN_DEPTH_M

            # Final coordinate arrays (after any coarsening)
            lat_out = subset.coords["lat"].values
            lon_out = subset.coords["lon"].values

            lat_first = float(lat_out.min())
            lat_last = float(lat_out.max())
            lon_first = float(lon_out.min())
            lon_last = float(lon_out.max())

            # Convert 0-360 back to -180/180 if needed
            if uses_360:
                if lon_first > 180.0:
                    lon_first -= 360.0
                if lon_last > 180.0:
                    lon_last -= 360.0

            # Ensure values are south-to-north (lat_out is sorted ascending
            # because we called sortby("lat") above, so this is a no-op check)
            if len(lat_out) >= 2 and lat_out[0] > lat_out[-1]:
                values = values[::-1, :]

            nj, ni = values.shape

        finally:
            ds.close()

        return {
            "lat_first": lat_first,
            "lon_first": lon_first,
            "lat_last": lat_last,
            "lon_last": lon_last,
            "ni": ni,
            "nj": nj,
            "depths": values.tolist(),
        }

    # Run with a hard timeout; if the OPeNDAP server hangs, abandon the thread
    # and raise so the caller falls back to CRM.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_inner)
        return future.result(timeout=_OPENDAP_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(
            f"OPeNDAP fetch timed out after {_OPENDAP_TIMEOUT_S:.0f}s "
            f"for {url!r} at bbox={bbox}"
        )
    except Exception as exc:
        raise RuntimeError(
            f"OPeNDAP fetch failed for {url!r} at bbox={bbox}: {exc}"
        ) from exc
    finally:
        # cancel_futures=True (Python 3.9+) discards pending-but-not-started tasks.
        # A running thread cannot be killed; it becomes a daemon and eventually
        # completes naturally after the timeout.
        executor.shutdown(wait=False, cancel_futures=True)


# ===========================================================================
# §4 — VDatum datum normalisation  (T20.2)
# ===========================================================================


def _detect_vdatum_region(lat: float, lon: float) -> str:
    """Return the NOAA VDatum region identifier for *lat*/*lon*.

    VDatum partitions the US into non-overlapping horizontal regions.
    Each region has a separate tidal datum model.

    Regions returned:
        ``"contiguous"`` — CONUS (default)
        ``"gl"``          — Great Lakes (IGLD85 datum)
        ``"westcoast_ak"`` — Alaska
        ``"hi"``           — Hawaii
        ``"prvi"``         — Puerto Rico / USVI
    """
    # Alaska: mainland (lat > 54.5) or Aleutian/Southeast (lat > 51 west of -130)
    if lat > 54.5 or (lat > 51.0 and lon < -130.0):
        return "westcoast_ak"
    # Hawaii
    if 18.0 <= lat <= 23.0 and -162.0 <= lon <= -154.0:
        return "hi"
    # Puerto Rico / USVI
    if 17.0 <= lat <= 19.0 and -68.5 <= lon <= -64.0:
        return "prvi"
    # Great Lakes
    if is_great_lake(lat, lon) is not None:
        return "gl"
    # Default: contiguous US
    return "contiguous"


def _query_vdatum_offset(lat: float, lon: float, source_datum: str) -> float:
    """Query NOAA VDatum REST API for the vertical offset from *source_datum* to LMSL.

    The offset is the height (in metres) of elevation-0 in the source datum
    relative to Local Mean Sea Level (LMSL).  Apply as::

        depth_lmsl = depth_source + offset

    For NAVD88 at San Diego, VDatum returns ``t_z ≈ -0.764``, meaning the
    NAVD88 datum surface is 0.764 m below LMSL.  A 2 m depth below NAVD88
    therefore becomes 2.764 m below LMSL.

    Results are cached by ``(round(lat, 1), round(lon, 1), datum)`` — datum
    offsets vary slowly over ~10 km scales, so one query per grid is sufficient.

    Rate-limited to 1 request/second.  On any API failure, logs a warning and
    returns 0.0 so the SWAN run proceeds without datum correction.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        source_datum: Our datum name (``"NAVD88"``, ``"MHW"``, ``"MLLW"``,
            ``"MHHW"``, ``"IGLD85"``, etc.).

    Returns:
        Vertical offset in metres (source datum → LMSL).
        Returns 0.0 on API failure.
    """
    global _last_vdatum_request_time

    # Round for cache key granularity (~0.1° ≈ 10 km)
    lat_r = round(lat, 1)
    lon_r = round(lon, 1)
    cache_key = (lat_r, lon_r, source_datum)

    if cache_key in _VDATUM_CACHE:
        return _VDATUM_CACHE[cache_key]

    vdatum_frame = _DATUM_TO_VDATUM_FRAME.get(source_datum.upper())
    if vdatum_frame is None:
        logger.warning(
            "Unknown datum %r — no VDatum mapping available; using 0.0m offset",
            source_datum,
        )
        _VDATUM_CACHE[cache_key] = 0.0
        return 0.0

    # Datum is already LMSL — offset is zero by definition
    if vdatum_frame == "LMSL":
        _VDATUM_CACHE[cache_key] = 0.0
        return 0.0

    region = _detect_vdatum_region(lat, lon)

    # Enforce rate limit: at most 1 request per second
    elapsed = time.monotonic() - _last_vdatum_request_time
    if elapsed < _VDATUM_RATE_LIMIT_S:
        time.sleep(_VDATUM_RATE_LIMIT_S - elapsed)

    params: dict[str, str] = {
        "s_x": f"{lon:.6f}",
        "s_y": f"{lat:.6f}",
        "s_z": "0",
        "s_v_frame": vdatum_frame,
        "t_v_frame": "LMSL",
        "region": region,
    }

    try:
        _last_vdatum_request_time = time.monotonic()
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(_VDATUM_API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        t_z = data.get("t_z")
        if t_z is None:
            raise ValueError(
                f"VDatum response missing 't_z' field. Response: {data!r}"
            )
        offset = float(t_z)
        _VDATUM_CACHE[cache_key] = offset
        return offset

    except Exception:
        logger.warning(
            "VDatum API unavailable for datum=%r at (%.2f, %.2f) region=%r — "
            "no datum correction applied. "
            "Bathymetry may have up to ~1m vertical bias.",
            source_datum,
            lat,
            lon,
            region,
            exc_info=True,
        )
        _VDATUM_CACHE[cache_key] = 0.0
        return 0.0


def normalize_to_msl(
    depths: list[list[float]],
    datum: str,
    center_lat: float,
    center_lon: float,
) -> list[list[float]]:
    """Apply a vertical datum correction so all depths are relative to MSL.

    SWAN expects depth relative to Mean Sea Level.  Regional DEMs use varying
    datums (NAVD88, MHW, MLLW, MHHW) whose offsets from MSL vary spatially by
    up to ~1 m.  A single VDatum query at the grid centre is accurate enough —
    offsets vary slowly over the typical Level 2/3 grid extent (~5 km).

    Sign convention is preserved: negative = ocean, positive = land (CUDEM).

    Args:
        depths: ``[nj][ni]`` depth grid in the source datum.
        datum: Source datum name (``"NAVD88"``, ``"MHW"``, ``"MLLW"``, etc.).
        center_lat: Grid centre latitude for the VDatum API query.
        center_lon: Grid centre longitude for the VDatum API query.

    Returns:
        Depth grid with the same shape, corrected to MSL.
        Returns *depths* unchanged when datum is already MSL/LMSL or when the
        offset is zero (includes VDatum API failure with a prior warning).
    """
    if datum.upper() in ("MSL", "LMSL"):
        return depths

    offset = _query_vdatum_offset(center_lat, center_lon, datum)

    logger.info(
        "Applied %s to MSL offset: %.3fm via VDatum at (%.2f, %.2f)",
        datum,
        offset,
        center_lat,
        center_lon,
    )

    if offset == 0.0:
        # Either the datum maps to LMSL, is unknown, or the API failed
        # (_query_vdatum_offset already logged a warning in the failure case)
        return depths

    return [[cell + offset for cell in row] for row in depths]


# ===========================================================================
# §5 — Great Lakes functions  (T21.1)
# ===========================================================================


def is_great_lake(lat: float, lon: float) -> str | None:
    """Return the lake name if *(lat, lon)* falls within a Great Lake bbox.

    Uses generous bbox margins so nearshore points along the lake shore are
    captured.  Checked in geographic-area order (Superior first, Ontario last).

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        Lake name string (``"michigan"``, ``"erie"``, ``"huron"``,
        ``"ontario"``, ``"superior"``, ``"st_clair"``) or ``None``.

    Examples::

        >>> is_great_lake(41.85, -87.62)
        'michigan'
        >>> is_great_lake(33.66, -118.00)
        None
    """
    for lake, (lat_min, lat_max, lon_min, lon_max) in _GREAT_LAKE_BOXES.items():
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return lake
    return None


def _ensure_great_lake_dem(lake: str) -> Path | None:
    """Ensure the USGS Great Lakes DEM GeoTIFF is cached locally.

    Downloads from ScienceBase (Rohweder 2025) if the file is absent or
    older than 365 days.  The download is streamed to avoid holding multi-GB
    files in memory (Michigan is 1.4 GB compressed).

    Cache location: ``/etc/weewx-clearskies/great_lakes/{lake}.tif``

    Args:
        lake: Lake name (``"michigan"``, ``"erie"``, etc.).

    Returns:
        Path to the cached GeoTIFF on success, or ``None`` on failure.
    """
    item_id = _GREAT_LAKE_SCIENCEBASE_IDS.get(lake)
    if item_id is None:
        logger.error("No ScienceBase ID configured for lake %r", lake)
        return None

    cache_path = _GREAT_LAKES_CACHE_DIR / f"{lake}.tif"

    # Check for a fresh cached file
    if cache_path.exists():
        age_s = time.time() - cache_path.stat().st_mtime
        if age_s < _GREAT_LAKES_TTL_S:
            logger.debug("Great Lakes DEM cache hit: %s (age %.0f days)", cache_path, age_s / 86400)
            return cache_path
        logger.info(
            "Great Lakes DEM cache expired (%.0f days old) for %s — re-downloading",
            age_s / 86400,
            lake,
        )

    # Ensure the cache directory exists
    try:
        _GREAT_LAKES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.error(
            "Cannot create Great Lakes cache directory %s",
            _GREAT_LAKES_CACHE_DIR,
            exc_info=True,
        )
        return None

    # Resolve the GeoTIFF download URL from the ScienceBase catalog API
    sciencebase_url = _SCIENCEBASE_API_URL.format(item_id=item_id)
    try:
        with httpx.Client(timeout=30.0) as client:
            meta_resp = client.get(sciencebase_url)
        meta_resp.raise_for_status()
        meta: dict[str, Any] = meta_resp.json()
    except Exception:
        logger.error(
            "Failed to fetch ScienceBase metadata for lake=%r (item %s) from %s",
            lake,
            item_id,
            sciencebase_url,
            exc_info=True,
        )
        return None

    # Find the first .tif file entry
    files: list[dict[str, Any]] = meta.get("files", [])
    download_url: str | None = None
    for f in files:
        name: str = f.get("name", "")
        url: str = f.get("url", "")
        if url and name.lower().endswith(".tif"):
            download_url = url
            break

    if download_url is None:
        logger.error(
            "No .tif file found in ScienceBase item %s for lake=%r. "
            "Files listed: %s",
            item_id,
            lake,
            [f.get("name") for f in files],
        )
        return None

    logger.info(
        "Downloading USGS Great Lakes DEM for lake=%r from %s", lake, download_url
    )

    # Stream the download in 1 MB chunks to keep memory low
    tmp_path = cache_path.with_suffix(".tmp")
    try:
        with httpx.Client(timeout=None, follow_redirects=True) as client:
            with client.stream("GET", download_url) as stream:
                stream.raise_for_status()
                total_bytes = int(stream.headers.get("content-length", 0))
                downloaded_bytes = 0
                with tmp_path.open("wb") as fout:
                    for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
                        fout.write(chunk)
                        downloaded_bytes += len(chunk)
                        # Log progress every ~100 MB
                        if total_bytes and (downloaded_bytes % (100 * 1024 * 1024)) < len(chunk):
                            logger.info(
                                "Downloading %s DEM: %.0f / %.0f MB (%.0f%%)",
                                lake,
                                downloaded_bytes / 1024 / 1024,
                                total_bytes / 1024 / 1024,
                                100.0 * downloaded_bytes / total_bytes,
                            )
        # Atomically replace the cached file
        tmp_path.replace(cache_path)
    except Exception:
        logger.error(
            "Download failed for lake=%r from %s", lake, download_url, exc_info=True
        )
        tmp_path.unlink(missing_ok=True)
        return None

    logger.info("Great Lakes DEM for lake=%r cached at %s", lake, cache_path)
    return cache_path


def fetch_great_lake_grid(
    dem_path: Path,
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> dict[str, Any]:
    """Read a depth grid from a USGS Great Lakes DEM GeoTIFF via windowed read.

    Uses rasterio's tiled windowed read so only the bbox footprint is loaded
    from the (potentially multi-GB) GeoTIFF — typically well under 50 MB for a
    SWAN grid bbox.

    CRS: The USGS DEMs use NAD83 geographic (EPSG:4269), effectively identical
    to WGS84 for our purposes.

    Sign convention: positive = land above lake surface, negative = below lake
    surface (bathymetry).  Matches CUDEM convention expected by
    ``cudem_to_swan_bottom()``.

    Row ordering: rasterio reads north-to-south (row 0 = northernmost).
    This function flips rows to south-to-north before returning, matching the
    ``lat_first`` = south convention of ``cudem_to_swan_bottom()``.

    Args:
        dem_path: Path to the cached GeoTIFF.
        bbox: ``(lon_min, lat_min, lon_max, lat_max)`` — target domain.
        resolution_m: Target grid resolution in metres.  If coarser than the
            DEM's native resolution, the data is block-averaged after reading.

    Returns:
        Grid dict compatible with ``cudem_to_swan_bottom()``.

    Raises:
        RuntimeError: If rasterio is not available or the read fails.
    """
    if not _RASTERIO_AVAILABLE:
        raise RuntimeError(
            "rasterio is not available — Great Lakes bathymetry is disabled. "
            "Install rasterio to enable this data source."
        )

    import numpy as np  # type: ignore[import-untyped]

    lon_min, lat_min, lon_max, lat_max = bbox

    with rasterio.open(dem_path) as src:  # type: ignore[union-attr]
        # Build a rasterio Window from geographic bounds
        window = rasterio.windows.from_bounds(  # type: ignore[union-attr]
            lon_min, lat_min, lon_max, lat_max,
            transform=src.transform,
        )

        # Read band 1 from the window
        data: np.ndarray = src.read(1, window=window).astype(float)  # type: ignore[no-untyped-call]
        nodata = src.nodata

        # Window transform: affine mapping from pixel → geographic coordinates
        win_transform = rasterio.windows.transform(window, src.transform)  # type: ignore[union-attr]
        nj, ni = data.shape

    if data.size == 0:
        raise RuntimeError(
            f"Great Lakes windowed read returned an empty grid "
            f"for {dem_path} at bbox={bbox}"
        )

    # Replace nodata / NaN with default ocean depth
    if nodata is not None:
        data[data == nodata] = _DEFAULT_OCEAN_DEPTH_M
    data[np.isnan(data)] = _DEFAULT_OCEAN_DEPTH_M
    # Reject absurdly large sentinel values
    data[data > 1e10] = _DEFAULT_OCEAN_DEPTH_M
    data[data < -1e10] = _DEFAULT_OCEAN_DEPTH_M

    # Rasterio convention: row 0 is the northernmost row.
    # cudem_to_swan_bottom() expects row 0 = southernmost (lat_first = south).
    data = data[::-1, :]

    # Compute geographic corners from the window transform.
    #   win_transform.a  = pixel width  (positive, east direction)
    #   win_transform.e  = pixel height (negative, south direction)
    #   win_transform.c  = west edge longitude (upper-left corner)
    #   win_transform.f  = north edge latitude  (upper-left corner)
    pixel_width: float = win_transform.a
    pixel_height: float = win_transform.e  # negative
    lon_first: float = win_transform.c
    lat_north: float = win_transform.f
    lon_last: float = lon_first + ni * pixel_width
    lat_south: float = lat_north + nj * pixel_height  # adds negative value

    # After row flip: row 0 is south, so lat_first = south, lat_last = north
    lat_first_out: float = lat_south
    lat_last_out: float = lat_north

    # Optional coarsening if requested resolution is notably coarser than native
    native_res_m = abs(pixel_height) * 111_320.0
    if resolution_m > native_res_m * 1.5 and nj >= 4 and ni >= 4:
        coarsen_factor = max(1, int(round(resolution_m / native_res_m)))
        coarsen_factor = min(coarsen_factor, min(nj, ni) // 2)
        if coarsen_factor > 1:
            nj_trim = (nj // coarsen_factor) * coarsen_factor
            ni_trim = (ni // coarsen_factor) * coarsen_factor
            data = (
                data[:nj_trim, :ni_trim]
                .reshape(
                    nj_trim // coarsen_factor, coarsen_factor,
                    ni_trim // coarsen_factor, coarsen_factor,
                )
                .mean(axis=(1, 3))
            )
            nj, ni = data.shape

    return {
        "lat_first": lat_first_out,
        "lon_first": lon_first,
        "lat_last": lat_last_out,
        "lon_last": lon_last,
        "ni": ni,
        "nj": nj,
        "depths": data.tolist(),
    }


# ---------------------------------------------------------------------------
# Priority 0: Operator-supplied bathymetry (Phase 24, T24.1 + T24.2)
# ---------------------------------------------------------------------------

_OPERATOR_BATHY_DIR = Path("/etc/weewx-clearskies/operator_bathymetry")


def get_operator_grid(
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> dict[str, Any] | None:
    """Load operator-supplied bathymetry for *bbox* if available.

    The operator uploads a GeoTIFF, NetCDF, or ASCII XYZ file via the admin UI.
    After upload, the file is validated, datum-corrected, and cached as a
    GeoTIFF at ``/etc/weewx-clearskies/operator_bathymetry/operator.tif``.

    This function is the highest priority in the resolver chain — if operator
    data exists and covers the requested bbox, it is used unconditionally.

    Args:
        bbox: ``(lon_min, lat_min, lon_max, lat_max)`` query bbox.
        resolution_m: Target resolution in metres (for coarsening).

    Returns:
        Grid dict on success, ``None`` when no operator file exists or the file
        does not cover the requested bbox.
    """
    tif_path = _OPERATOR_BATHY_DIR / "operator.tif"
    if not tif_path.exists():
        return None

    if not _RASTERIO_AVAILABLE:
        logger.warning(
            "Operator bathymetry file exists at %s but rasterio is not "
            "installed — cannot read GeoTIFF. Skipping operator data.",
            tif_path,
        )
        return None

    try:
        grid = _read_geotiff_grid(tif_path, bbox, resolution_m)
        if grid is not None:
            grid["source"] = "operator"
            logger.info(
                "Using operator-supplied bathymetry (%d x %d, %.0fm)",
                grid["ni"], grid["nj"], resolution_m,
            )
        return grid
    except Exception:
        logger.warning(
            "Failed to read operator bathymetry from %s — skipping",
            tif_path, exc_info=True,
        )
        return None


def _read_geotiff_grid(
    path: Path,
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> dict[str, Any] | None:
    """Read a GeoTIFF file and extract a grid for the requested bbox.

    Uses rasterio windowed read — same approach as ``fetch_great_lake_grid()``.
    Returns None if the file does not cover the bbox.
    """
    import numpy as np

    lon_min, lat_min, lon_max, lat_max = bbox

    with rasterio.open(path) as src:
        bounds = src.bounds
        if (lon_min < bounds.left or lon_max > bounds.right
                or lat_min < bounds.bottom or lat_max > bounds.top):
            return None

        window = rasterio.windows.from_bounds(
            lon_min, lat_min, lon_max, lat_max, src.transform
        )
        data = src.read(1, window=window).astype(np.float64)
        nodata = src.nodata
        win_transform = rasterio.windows.transform(window, src.transform)

    if nodata is not None:
        data[data == nodata] = -15.0

    nj, ni = data.shape
    if nj == 0 or ni == 0:
        return None

    # Flip north-to-south → south-to-north
    data = np.flipud(data)

    pixel_width = win_transform.a
    pixel_height = win_transform.e
    lon_first = win_transform.c
    lat_north = win_transform.f
    lon_last = lon_first + ni * pixel_width
    lat_south = lat_north + nj * pixel_height

    # Optional coarsening
    native_res_m = abs(pixel_height) * 111_320.0
    if resolution_m > native_res_m * 1.5 and nj >= 4 and ni >= 4:
        coarsen_factor = max(1, int(round(resolution_m / native_res_m)))
        coarsen_factor = min(coarsen_factor, min(nj, ni) // 2)
        if coarsen_factor > 1:
            nj_trim = (nj // coarsen_factor) * coarsen_factor
            ni_trim = (ni // coarsen_factor) * coarsen_factor
            data = (
                data[:nj_trim, :ni_trim]
                .reshape(
                    nj_trim // coarsen_factor, coarsen_factor,
                    ni_trim // coarsen_factor, coarsen_factor,
                )
                .mean(axis=(1, 3))
            )
            nj, ni = data.shape

    return {
        "lat_first": lat_south,
        "lon_first": lon_first,
        "lat_last": lat_north,
        "lon_last": lon_last,
        "ni": ni,
        "nj": nj,
        "depths": data.tolist(),
    }
