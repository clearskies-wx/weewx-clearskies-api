"""Fishing species data tables (API-MANUAL.md §17 "Fishing scorer",
Phase 4 T4.2; externalized to YAML at T8.2).

Data-only module: biogeographic region classification, per-region species
lists, per-species scoring profiles, and seasonal behavior multipliers
consumed by ``enrichment/fishing_scorer.py``. No external API calls.

T8.2 (2026-07-12): the four data tables below (``BIOGEOGRAPHIC_REGIONS``,
``SPECIES_BY_REGION``, ``SPECIES_PROFILES``, ``SEASONAL_BEHAVIOR``) are no
longer hardcoded Python dicts — they are loaded once at import time from
``data/species.yaml`` via ``_load_species_data()``, using ``yaml.safe_load()``
(never ``yaml.load()``). This module's public API is unchanged: the four
module-level dicts, ``classify_region()``, ``SpeciesProfile``, and
``SeasonalEntry`` all still exist with the same names and shapes. See
``data/species.yaml`` for the schema documentation and operator-editable
data itself.

Future enhancement (not implemented in T8.2): an ``api.conf [fishing]
species_data_path`` config override so operators can point the loader at
their own file instead of the bundled default. ``_load_species_data()``
already accepts an explicit ``path`` argument for exactly this purpose —
wiring a config value into that argument at startup is the only remaining
step, deferred to a future task.

Two distinct data shapes serve two distinct purposes:

  - ``SPECIES_BY_REGION`` is descriptive metadata used to auto-populate a
    fishing spot's ``species`` config list from its biogeographic region
    and ``target_category`` (config/marine_config.py ``FishingSpotConfig``).
    It intentionally includes some regionally-common species that do not
    have a full ``SPECIES_PROFILES`` entry — the scorer degrades gracefully
    for those (see ``fishing_scorer._DEFAULT_PROFILE``).

  - ``SPECIES_PROFILES`` / ``SEASONAL_BEHAVIOR`` are the scoring lookup
    tables — every species referenced by the fishing scorer's test coverage
    and the API-MANUAL §17 species profile table has a full entry here.

All temperature ranges are in degrees Fahrenheit. Ranges are nested
(optimal subset of good subset of marginal) so a temperature reading
falls into exactly one bucket when checked in optimal -> good -> marginal
-> outside order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

import yaml

_DEFAULT_SPECIES_DATA_PATH = Path(__file__).parent.parent / "data" / "species.yaml"


class _RegionBBox(TypedDict):
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


class SpeciesProfile(TypedDict):
    pressure_sensitivity: float  # 0.0-1.0; 0.1 = no swim bladder (tuna), 0.8 = large (redfish)
    temp_optimal: tuple[float, float]  # degrees F
    temp_good: tuple[float, float]  # degrees F, superset of temp_optimal
    temp_marginal: tuple[float, float]  # degrees F, superset of temp_good
    tide_preference: str  # "incoming" | "outgoing" | "slack" | "any"
    tide_multiplier: float
    time_preference: str  # "dawn" | "dusk" | "night" | "any"
    time_multiplier: float


class SeasonalEntry(TypedDict, total=False):
    spawning_multiplier: float
    pre_spawn_multiplier: float
    closed: bool


_REQUIRED_TOP_LEVEL_KEYS = (
    "regions",
    "species_by_region",
    "species_profiles",
    "seasonal_behavior",
)
_REQUIRED_BBOX_KEYS = ("lat_min", "lat_max", "lon_min", "lon_max")
_REQUIRED_PROFILE_KEYS = (
    "pressure_sensitivity",
    "temp_optimal",
    "temp_good",
    "temp_marginal",
    "tide_preference",
    "tide_multiplier",
    "time_preference",
    "time_multiplier",
)


def _validate_and_build(raw: dict[str, Any], source: Path) -> tuple[
    dict[str, _RegionBBox],
    dict[str, dict[str, list[str]]],
    dict[str, SpeciesProfile],
    dict[str, dict[int, SeasonalEntry]],
]:
    """Validate the raw YAML structure and convert it into the module's
    public data shapes (notably: temp range lists -> tuples).

    Raises ValueError with a descriptive message on any structural problem
    — this runs at import time, so a malformed species.yaml fails loudly
    and immediately rather than surfacing as a confusing scoring bug later.
    """
    missing_top = [k for k in _REQUIRED_TOP_LEVEL_KEYS if k not in raw]
    if missing_top:
        raise ValueError(f"{source}: missing top-level key(s) {missing_top}")

    regions: dict[str, _RegionBBox] = {}
    for name, bbox in raw["regions"].items():
        missing = [k for k in _REQUIRED_BBOX_KEYS if k not in bbox]
        if missing:
            raise ValueError(f"{source}: region {name!r} missing key(s) {missing}")
        regions[name] = {
            "lat_min": float(bbox["lat_min"]),
            "lat_max": float(bbox["lat_max"]),
            "lon_min": float(bbox["lon_min"]),
            "lon_max": float(bbox["lon_max"]),
        }

    species_by_region: dict[str, dict[str, list[str]]] = {}
    for region, categories in raw["species_by_region"].items():
        species_by_region[region] = {
            category: list(names) for category, names in categories.items()
        }

    species_profiles: dict[str, SpeciesProfile] = {}
    for name, profile in raw["species_profiles"].items():
        missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in profile]
        if missing:
            raise ValueError(f"{source}: species profile {name!r} missing key(s) {missing}")
        species_profiles[name] = {
            "pressure_sensitivity": float(profile["pressure_sensitivity"]),
            "temp_optimal": tuple(float(v) for v in profile["temp_optimal"]),
            "temp_good": tuple(float(v) for v in profile["temp_good"]),
            "temp_marginal": tuple(float(v) for v in profile["temp_marginal"]),
            "tide_preference": str(profile["tide_preference"]),
            "tide_multiplier": float(profile["tide_multiplier"]),
            "time_preference": str(profile["time_preference"]),
            "time_multiplier": float(profile["time_multiplier"]),
        }

    seasonal_behavior: dict[str, dict[int, SeasonalEntry]] = {}
    for name, months in raw["seasonal_behavior"].items():
        month_entries: dict[int, SeasonalEntry] = {}
        for month, entry in months.items():
            seasonal_entry: SeasonalEntry = {}
            if "spawning_multiplier" in entry:
                seasonal_entry["spawning_multiplier"] = float(entry["spawning_multiplier"])
            if "pre_spawn_multiplier" in entry:
                seasonal_entry["pre_spawn_multiplier"] = float(entry["pre_spawn_multiplier"])
            if "closed" in entry:
                seasonal_entry["closed"] = bool(entry["closed"])
            month_entries[int(month)] = seasonal_entry
        seasonal_behavior[name] = month_entries

    return regions, species_by_region, species_profiles, seasonal_behavior


def _load_species_data(
    path: Path | None = None,
) -> tuple[
    dict[str, _RegionBBox],
    dict[str, dict[str, list[str]]],
    dict[str, SpeciesProfile],
    dict[str, dict[int, SeasonalEntry]],
]:
    """Load and validate the species data tables from a YAML file.

    Args:
        path: Path to the species YAML file. None = bundled default
            (``data/species.yaml``, shipped with the package).

    Returns:
        (regions, species_by_region, species_profiles, seasonal_behavior)
        tuple, in the module's public dict shapes (temp ranges as tuples).

    Raises:
        FileNotFoundError: the YAML file does not exist.
        ValueError: the YAML content is malformed (not a mapping, missing
            required top-level keys, or a region/profile entry is missing
            required fields).
    """
    resolved = path or _DEFAULT_SPECIES_DATA_PATH
    with resolved.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"{resolved}: root must be a YAML mapping")

    return _validate_and_build(raw, resolved)


(
    BIOGEOGRAPHIC_REGIONS,
    SPECIES_BY_REGION,
    SPECIES_PROFILES,
    SEASONAL_BEHAVIOR,
) = _load_species_data()

_FALLBACK_REGION = "atlantic_se"  # most common US coastal region


def _lon_in_range(lon: float, lon_min: float, lon_max: float) -> bool:
    """Longitude containment, handling antimeridian wraparound.

    A normal box has lon_min <= lon_max. A wraparound box (lon_min >
    lon_max, e.g. pacific_territories) contains lon if it's east of
    lon_min OR west of lon_max.
    """
    if lon_min <= lon_max:
        return lon_min <= lon <= lon_max
    return lon >= lon_min or lon <= lon_max


def _region_centroid(bbox: _RegionBBox) -> tuple[float, float]:
    lat_c = (bbox["lat_min"] + bbox["lat_max"]) / 2.0
    lon_min, lon_max = bbox["lon_min"], bbox["lon_max"]
    if lon_min <= lon_max:
        lon_c = (lon_min + lon_max) / 2.0
    else:
        # Wraparound: average across the shorter arc through +/-180.
        lon_c = ((lon_min + lon_max + 360.0) / 2.0 + 180.0) % 360.0 - 180.0
    return lat_c, lon_c


def classify_region(lat: float, lon: float) -> str:
    """Classify (lat, lon) into one of the 11 US biogeographic regions.

    Simple bounding-box containment. When multiple regions' boxes contain
    the point (coarse boxes overlap at coastlines), the region whose
    centroid is nearest wins. Falls back to "atlantic_se" — the most
    common US coastal region — when no box contains the point.
    """
    matches = [
        name
        for name, bbox in BIOGEOGRAPHIC_REGIONS.items()
        if bbox["lat_min"] <= lat <= bbox["lat_max"]
        and _lon_in_range(lon, bbox["lon_min"], bbox["lon_max"])
    ]

    if not matches:
        return _FALLBACK_REGION
    if len(matches) == 1:
        return matches[0]

    def _dist2(name: str) -> float:
        lat_c, lon_c = _region_centroid(BIOGEOGRAPHIC_REGIONS[name])
        return (lat - lat_c) ** 2 + (lon - lon_c) ** 2

    return min(matches, key=_dist2)
