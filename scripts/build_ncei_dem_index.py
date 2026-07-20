#!/usr/bin/env python3
"""Build a static JSON index of NCEI regional coastal DEMs.

One-time build script — not shipped with the API package. Fetches the THREDDS
catalog XML and each file's OPeNDAP .das metadata to extract bounding boxes,
resolutions, vertical datums, and elevation variable names.

Output: weewx_clearskies_api/data/ncei_regional_dem_index.json

Usage:
    python scripts/build_ncei_dem_index.py

Requires: aiohttp (pip install aiohttp)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is required. Install with: pip install aiohttp", file=sys.stderr)
    sys.exit(1)

CATALOG_URL = "https://www.ngdc.noaa.gov/thredds/catalog/regional/catalog.xml"
THREDDS_NS = "{http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0}"
OPENDAP_BASE = "https://www.ngdc.noaa.gov/thredds/dodsC/regional"

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "weewx_clearskies_api" / "data" / "ncei_regional_dem_index.json"

MAX_CONCURRENT = 20
REQUEST_TIMEOUT = 30

# Filename-based resolution patterns (arc-seconds)
RESOLUTION_PATTERNS: list[tuple[str, float]] = [
    (r"_13_", 1.0 / 3),        # 1/3 arc-second (~10m)
    (r"_19_", 1.0 / 9),        # 1/9 arc-second (~3.4m)
    (r"_815_", 8.0 / 15),      # 8/15 arc-second (~17m)
    (r"_83_", 8.0 / 3),        # 8/3 arc-second (~85m)
    (r"_1_", 1.0),             # 1 arc-second (~30m)
    (r"_3_", 3.0),             # 3 arc-second (~90m)
]

# Vertical datum patterns from filenames
DATUM_PATTERNS: list[tuple[str, str]] = [
    ("_navd88_", "NAVD88"),
    ("_mhw_", "MHW"),
    ("_mhhw_", "MHHW"),
    ("_mllw_", "MLLW"),
]

# Fallback lookup for DEMs whose .das metadata doesn't expose a vertical datum.
# Determined by querying NCEI XML metadata endpoints (EPSG:5866 = MLLW) and
# NCEI island DEM program conventions (MSL for non-US islands).
# ADR-098: all DEMs must have a known datum for match-at-source to work.
DATUM_FALLBACK: dict[str, str] = {
    # US coast tsunami-inundation DEMs (MLLW, EPSG:5866) — verified via
    # https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/
    # gov.noaa.ngdc.mgg.dem:{id}/xml
    "bar_harbor_N036_2018.nc": "MLLW",
    "barataria_bay_G200_2018.nc": "MLLW",
    "biscayne_bay_S200_2018.nc": "MLLW",
    "casco_bay_N100_2018.nc": "MLLW",
    "charleston_harbor_S080_2017.nc": "MLLW",
    "charlotte_harbor_G050_2018.nc": "MLLW",
    "choctawhatchee_bay_G120_2018.nc": "MLLW",
    "corpus_christi_bay_G310_2018.nc": "MLLW",
    "galveston_bay_G260_2018.nc": "MLLW",
    "indian_river_S190_2018.nc": "MLLW",
    "monterey_bay_P080_2018.nc": "MLLW",
    "ossabaw_sound_S130_2018.nc": "MLLW",
    "pensacola_bay_G130_2018.nc": "MLLW",
    "perdido_bay_G140_2018.nc": "MLLW",
    "san_diego_bay_P020_2017.nc": "MLLW",
    "san_pedro_bay_P050_2018.nc": "MLLW",
    "santa_monica_bay_P060_2018.nc": "MLLW",
    "sarasota_bay_G060_2017.nc": "MLLW",
    "southern_florida_F010_2018.nc": "MLLW",
    "tampa_bay_G070_2017.nc": "MLLW",
    "tomales_bay_P110_2018.nc": "MLLW",
    # Island DEMs (MSL) — NCEI island DEM convention for non-US territories
    "easter_island_3_isl_2016.nc": "MSL",
    "galapagos_1_isl_2016.nc": "MSL",
    "galapagos_3_isl_2016.nc": "MSL",
    "grenada_1_isl_2017.nc": "MSL",
    "lesser_antilles_3_isl_2018.nc": "MSL",
    "marquesas_islands_3_isl_2017.nc": "MSL",
    "niue_3_isl_2016.nc": "MSL",
    "rarotonga_1_isl_2016.nc": "MSL",
    "society_islands_3_isl_2016.nc": "MSL",
    "society_islands_leeward_1_isl_2016.nc": "MSL",
    "society_islands_windward_1_isl_2016.nc": "MSL",
    # Special cases
    "mariana_trench_6_msl_2012.nc": "MSL",
    "san_juan_19_prvd02_2015.nc": "PRVD02",
}


def _parse_resolution_from_filename(filename: str) -> float | None:
    lower = filename.lower()
    for pattern, arcsec in RESOLUTION_PATTERNS:
        if re.search(pattern, lower):
            return arcsec
    return None


def _parse_datum_from_filename(filename: str) -> str | None:
    lower = filename.lower()
    for pattern, datum in DATUM_PATTERNS:
        if pattern in lower:
            return datum
    return None


def _parse_das_attribute(das_text: str, attr_name: str) -> str | None:
    pattern = rf'{attr_name}\s+"([^"]*)"'
    match = re.search(pattern, das_text)
    if match:
        return match.group(1)
    pattern_float = rf'{attr_name}\s+([\d.eE+-]+)'
    match = re.search(pattern_float, das_text)
    if match:
        return match.group(1)
    return None


def _parse_actual_range(das_text: str, var_name: str) -> tuple[float, float] | None:
    """Extract actual_range from a coordinate variable block like:
        lat {
            ...
            Float64 actual_range 32.619, 33.850;
        }
    """
    block_pattern = rf'{var_name}\s*\{{(.*?)\}}'
    block_match = re.search(block_pattern, das_text, re.DOTALL)
    if not block_match:
        return None
    block = block_match.group(1)
    range_pattern = r'actual_range\s+([\d.eE+-]+)\s*,\s*([\d.eE+-]+)'
    range_match = re.search(range_pattern, block)
    if not range_match:
        return None
    return float(range_match.group(1)), float(range_match.group(2))


def _detect_elevation_var(das_text: str) -> str:
    if re.search(r"\bz\s*\{", das_text):
        return "z"
    if re.search(r"\bBand1\s*\{", das_text):
        return "Band1"
    if re.search(r"\belevation\s*\{", das_text):
        return "elevation"
    return "z"


def _arcsec_to_meters(arcsec: float) -> float:
    return arcsec * (1852.0 * 60.0 / 3600.0)


async def _fetch_catalog(session: aiohttp.ClientSession) -> list[str]:
    print(f"Fetching catalog: {CATALOG_URL}")
    async with session.get(CATALOG_URL, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        text = await resp.text()

    root = ET.fromstring(text)
    filenames = []
    for dataset in root.iter(f"{THREDDS_NS}dataset"):
        name = dataset.get("name", "")
        url_path = dataset.get("urlPath", "")
        if url_path and name.endswith(".nc"):
            filenames.append(name)

    print(f"Found {len(filenames)} NetCDF files in catalog")
    return filenames


async def _fetch_das(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    filename: str,
) -> dict | None:
    url = f"{OPENDAP_BASE}/{filename}.das"
    async with sem:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    print(f"  SKIP {filename}: HTTP {resp.status}")
                    return None
                das_text = await resp.text()
        except Exception as e:
            print(f"  FAIL {filename}: {e}")
            return None

    lat_min = _parse_das_attribute(das_text, "geospatial_lat_min")
    lat_max = _parse_das_attribute(das_text, "geospatial_lat_max")
    lon_min = _parse_das_attribute(das_text, "geospatial_lon_min")
    lon_max = _parse_das_attribute(das_text, "geospatial_lon_max")
    lat_res = _parse_das_attribute(das_text, "geospatial_lat_resolution")
    vert_crs = _parse_das_attribute(das_text, "geospatial_bounds_vertical_crs")

    # Fallback: extract bbox from coordinate variable actual_range blocks
    if not all([lat_min, lat_max, lon_min, lon_max]):
        lat_range = _parse_actual_range(das_text, "lat")
        lon_range = _parse_actual_range(das_text, "lon")
        if lat_range and lon_range:
            lat_min = str(lat_range[0])
            lat_max = str(lat_range[1])
            lon_min = str(lon_range[0])
            lon_max = str(lon_range[1])

    if not all([lat_min, lat_max, lon_min, lon_max]):
        print(f"  SKIP {filename}: missing bbox attributes")
        return None

    elevation_var = _detect_elevation_var(das_text)

    # Resolution: prefer filename convention, fall back to .das attribute
    resolution_arcsec = _parse_resolution_from_filename(filename)
    if resolution_arcsec is None and lat_res is not None:
        try:
            resolution_arcsec = float(lat_res) * 3600.0  # degrees to arcsec
        except ValueError:
            pass
    if resolution_arcsec is None:
        resolution_arcsec = 3.0  # default to 3 arcsec (~90m)

    # Vertical datum: prefer filename, fall back to .das attribute
    vertical_datum = _parse_datum_from_filename(filename)
    if vertical_datum is None and vert_crs:
        crs_upper = vert_crs.upper()
        if "NAVD88" in crs_upper or "NAVD 88" in crs_upper:
            vertical_datum = "NAVD88"
        elif "MEAN HIGH WATER" in crs_upper or "MHW" in crs_upper:
            if "HIGHER" in crs_upper or "MHHW" in crs_upper:
                vertical_datum = "MHHW"
            else:
                vertical_datum = "MHW"
        elif "MEAN LOWER LOW" in crs_upper or "MLLW" in crs_upper:
            vertical_datum = "MLLW"
        elif "MSL" in crs_upper or "MEAN SEA LEVEL" in crs_upper:
            vertical_datum = "MSL"
        else:
            vertical_datum = vert_crs

    if vertical_datum is None:
        vertical_datum = DATUM_FALLBACK.get(filename, "UNKNOWN")

    return {
        "filename": filename,
        "lat_min": float(lat_min),
        "lat_max": float(lat_max),
        "lon_min": float(lon_min),
        "lon_max": float(lon_max),
        "resolution_arcsec": round(resolution_arcsec, 4),
        "resolution_m_approx": round(_arcsec_to_meters(resolution_arcsec), 1),
        "vertical_datum": vertical_datum,
        "elevation_var": elevation_var,
    }


async def main() -> None:
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        filenames = await _fetch_catalog(session)
        if not filenames:
            print("ERROR: No files found in catalog", file=sys.stderr)
            sys.exit(1)

        print(f"Fetching .das metadata for {len(filenames)} files ({MAX_CONCURRENT} concurrent)...")
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        tasks = [_fetch_das(session, sem, fn) for fn in filenames]
        results = await asyncio.gather(*tasks)

    dems = [r for r in results if r is not None]
    dems.sort(key=lambda d: d["filename"])

    index = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": CATALOG_URL,
        "count": len(dems),
        "dems": dems,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")

    print(f"\nWrote {len(dems)} DEM entries to {OUTPUT_PATH}")

    # Verification: check HB Pier coverage
    hb_lat, hb_lon = 33.65, -118.00
    covering = [
        d for d in dems
        if d["lat_min"] <= hb_lat <= d["lat_max"]
        and d["lon_min"] <= hb_lon <= d["lon_max"]
    ]
    if covering:
        best = min(covering, key=lambda d: d["resolution_arcsec"])
        print(f"HB Pier covered by {best['filename']} ({best['resolution_m_approx']}m, {best['vertical_datum']})")
    else:
        print("WARNING: No DEM covers HB Pier (33.65N, 118.00W)")

    # Stats
    datums = {}
    for d in dems:
        datums[d["vertical_datum"]] = datums.get(d["vertical_datum"], 0) + 1
    print(f"Datums: {datums}")

    vars_count = {}
    for d in dems:
        vars_count[d["elevation_var"]] = vars_count.get(d["elevation_var"], 0) + 1
    print(f"Elevation vars: {vars_count}")


if __name__ == "__main__":
    asyncio.run(main())
