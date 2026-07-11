"""Fishing species data tables (API-MANUAL.md §17 "Fishing scorer", Phase 4 T4.2).

Data-only module: biogeographic region classification, per-region species
lists, per-species scoring profiles, and seasonal behavior multipliers
consumed by ``enrichment/fishing_scorer.py``. No external API calls — all
tables are hardcoded per API-MANUAL.md §17: "Species data is hardcoded
lookup tables in enrichment/fishing_species.py, keyed by biogeographic
region and target category. No external API."

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

from typing import TypedDict

# ---------------------------------------------------------------------------
# Biogeographic regions (API-MANUAL.md §17: "species auto-populated from 11
# US biogeographic regions")
# ---------------------------------------------------------------------------


class _RegionBBox(TypedDict):
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


# Bounding boxes are deliberately coarse (same caveat as
# bathymetry.classify_region): real coastline geography does not tile into
# non-overlapping rectangles. classify_region() below breaks ties by
# nearest centroid.
#
# pacific_territories crosses the antimeridian (Guam/CNMI ~144-146E,
# American Samoa ~-170 to -172, i.e. 188-190 measured continuously
# eastward from Guam). Represented here with lon_min=144.0 (Guam side) and
# lon_max=-172.0 (Samoa side, in the standard -180..180 convention) —
# lon_min > lon_max signals a wraparound box to classify_region().
BIOGEOGRAPHIC_REGIONS: dict[str, _RegionBBox] = {
    "atlantic_ne": {"lat_min": 41.0, "lat_max": 47.0, "lon_min": -74.0, "lon_max": -66.0},
    "atlantic_se": {"lat_min": 25.0, "lat_max": 41.0, "lon_min": -82.0, "lon_max": -71.0},
    "gulf": {"lat_min": 25.0, "lat_max": 31.0, "lon_min": -98.0, "lon_max": -80.0},
    "pacific_sw": {"lat_min": 32.0, "lat_max": 34.0, "lon_min": -121.0, "lon_max": -117.0},
    "pacific_central": {"lat_min": 34.0, "lat_max": 38.0, "lon_min": -124.0, "lon_max": -120.0},
    "pacific_nw": {"lat_min": 38.0, "lat_max": 49.0, "lon_min": -126.0, "lon_max": -122.0},
    "alaska": {"lat_min": 55.0, "lat_max": 90.0, "lon_min": -170.0, "lon_max": -130.0},
    "hawaii": {"lat_min": 18.0, "lat_max": 23.0, "lon_min": -161.0, "lon_max": -154.0},
    "great_lakes": {"lat_min": 41.0, "lat_max": 49.0, "lon_min": -93.0, "lon_max": -76.0},
    "caribbean": {"lat_min": 17.0, "lat_max": 19.0, "lon_min": -68.0, "lon_max": -64.0},
    "pacific_territories": {"lat_min": -15.0, "lat_max": 20.0, "lon_min": 144.0, "lon_max": -172.0},
}

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


# ---------------------------------------------------------------------------
# Species by region and target category (API-MANUAL.md §17 target categories:
# saltwater_inshore, bottom_fish, freshwater_sport, salmonids — same set
# validated by config/marine_config.py FishingSpotConfig).
#
# Descriptive metadata for config auto-population. Not every listed species
# has a SPECIES_PROFILES entry (see module docstring); the scorer falls
# back to a neutral default profile for species not covered below. Not all
# categories apply to all regions (e.g. Hawaii has no freshwater_sport;
# Alaska has no meaningful saltwater_inshore warmwater list).
# ---------------------------------------------------------------------------

SPECIES_BY_REGION: dict[str, dict[str, list[str]]] = {
    "atlantic_ne": {
        "saltwater_inshore": [
            "striped bass",
            "bluefish",
            "flounder",
            "sheepshead",
            "black sea bass",
        ],
        "bottom_fish": ["tautog", "cod", "haddock", "pollock", "sheepshead"],
        "freshwater_sport": ["largemouth bass", "smallmouth bass", "walleye", "catfish", "pike"],
        "salmonids": ["atlantic salmon", "trout", "steelhead", "landlocked salmon"],
    },
    "atlantic_se": {
        "saltwater_inshore": [
            "redfish",
            "speckled trout",
            "flounder",
            "snook",
            "sheepshead",
            "cobia",
        ],
        "bottom_fish": ["grouper", "snapper", "sheepshead", "king mackerel", "amberjack"],
        "freshwater_sport": ["largemouth bass", "catfish", "crappie", "bream"],
    },
    "gulf": {
        "saltwater_inshore": [
            "redfish",
            "speckled trout",
            "flounder",
            "snook",
            "cobia",
            "bluefish",
        ],
        "bottom_fish": ["grouper", "snapper", "king mackerel", "sheepshead", "triggerfish"],
        "freshwater_sport": ["largemouth bass", "catfish", "crappie", "bream"],
    },
    "pacific_sw": {
        "saltwater_inshore": ["halibut", "corbina", "surfperch", "sheepshead", "yellowtail"],
        "bottom_fish": ["grouper", "sheepshead", "lingcod", "rockfish", "sculpin"],
        "freshwater_sport": ["largemouth bass", "catfish", "trout"],
    },
    "pacific_central": {
        "saltwater_inshore": ["halibut", "surfperch", "striped bass", "corbina"],
        "bottom_fish": ["lingcod", "rockfish", "sheepshead", "cabezon"],
        "freshwater_sport": ["largemouth bass", "trout", "catfish"],
        "salmonids": ["salmon", "steelhead", "trout"],
    },
    "pacific_nw": {
        "saltwater_inshore": ["surfperch", "striped bass", "flounder", "sturgeon"],
        "bottom_fish": ["lingcod", "rockfish", "halibut", "cabezon"],
        "freshwater_sport": ["smallmouth bass", "walleye", "catfish", "pike"],
        "salmonids": ["salmon", "steelhead", "trout"],
    },
    "alaska": {
        "bottom_fish": ["halibut", "lingcod", "rockfish", "sablefish"],
        "salmonids": ["salmon", "steelhead", "trout", "arctic char"],
    },
    "hawaii": {
        "saltwater_inshore": ["bonefish", "trevally", "mahi-mahi", "bluefish"],
        "bottom_fish": ["grouper", "snapper", "tuna", "wahoo"],
    },
    "great_lakes": {
        "freshwater_sport": [
            "walleye",
            "largemouth bass",
            "smallmouth bass",
            "catfish",
            "pike",
            "muskie",
        ],
        "salmonids": ["salmon", "steelhead", "trout", "lake trout"],
    },
    "caribbean": {
        "saltwater_inshore": ["bonefish", "tarpon", "snook", "mahi-mahi"],
        "bottom_fish": ["grouper", "snapper", "tuna", "mahi-mahi", "wahoo"],
    },
    "pacific_territories": {
        "saltwater_inshore": ["trevally", "bonefish", "mahi-mahi"],
        "bottom_fish": ["grouper", "snapper", "tuna", "mahi-mahi", "wahoo"],
    },
}


# ---------------------------------------------------------------------------
# Species scoring profiles (API-MANUAL.md §17 species profile table)
# ---------------------------------------------------------------------------


class SpeciesProfile(TypedDict):
    pressure_sensitivity: float  # 0.0-1.0; 0.1 = no swim bladder (tuna), 0.8 = large (redfish)
    temp_optimal: tuple[float, float]  # degrees F
    temp_good: tuple[float, float]  # degrees F, superset of temp_optimal
    temp_marginal: tuple[float, float]  # degrees F, superset of temp_good
    tide_preference: str  # "incoming" | "outgoing" | "slack" | "any"
    tide_multiplier: float
    time_preference: str  # "dawn" | "dusk" | "night" | "any"
    time_multiplier: float


SPECIES_PROFILES: dict[str, SpeciesProfile] = {
    "redfish": {
        "pressure_sensitivity": 0.8,
        "temp_optimal": (68.0, 80.0),
        "temp_good": (60.0, 85.0),
        "temp_marginal": (50.0, 90.0),
        "tide_preference": "outgoing",
        "tide_multiplier": 1.2,
        "time_preference": "dawn",
        "time_multiplier": 1.3,
    },
    "speckled trout": {
        "pressure_sensitivity": 0.7,
        "temp_optimal": (65.0, 80.0),
        "temp_good": (58.0, 85.0),
        "temp_marginal": (48.0, 90.0),
        "tide_preference": "incoming",
        "tide_multiplier": 1.15,
        "time_preference": "dusk",
        "time_multiplier": 1.3,
    },
    "flounder": {
        "pressure_sensitivity": 0.6,
        "temp_optimal": (60.0, 75.0),
        "temp_good": (52.0, 80.0),
        "temp_marginal": (45.0, 85.0),
        "tide_preference": "outgoing",
        "tide_multiplier": 1.2,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "snook": {
        "pressure_sensitivity": 0.75,
        "temp_optimal": (75.0, 88.0),
        "temp_good": (70.0, 92.0),
        "temp_marginal": (65.0, 95.0),
        "tide_preference": "incoming",
        "tide_multiplier": 1.25,
        "time_preference": "night",
        "time_multiplier": 1.4,
    },
    "striped bass": {
        "pressure_sensitivity": 0.65,
        "temp_optimal": (55.0, 68.0),
        "temp_good": (48.0, 72.0),
        "temp_marginal": (40.0, 78.0),
        "tide_preference": "incoming",
        "tide_multiplier": 1.2,
        "time_preference": "dawn",
        "time_multiplier": 1.3,
    },
    "tuna": {
        "pressure_sensitivity": 0.1,
        "temp_optimal": (68.0, 82.0),
        "temp_good": (62.0, 86.0),
        "temp_marginal": (55.0, 90.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "mahi-mahi": {
        "pressure_sensitivity": 0.1,
        "temp_optimal": (75.0, 86.0),
        "temp_good": (70.0, 90.0),
        "temp_marginal": (65.0, 92.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "grouper": {
        "pressure_sensitivity": 0.3,
        "temp_optimal": (70.0, 82.0),
        "temp_good": (63.0, 86.0),
        "temp_marginal": (55.0, 90.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "snapper": {
        "pressure_sensitivity": 0.3,
        "temp_optimal": (68.0, 82.0),
        "temp_good": (60.0, 86.0),
        "temp_marginal": (52.0, 90.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "dusk",
        "time_multiplier": 1.2,
    },
    "walleye": {
        "pressure_sensitivity": 0.7,
        "temp_optimal": (55.0, 70.0),
        "temp_good": (48.0, 75.0),
        "temp_marginal": (40.0, 80.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "dusk",
        "time_multiplier": 1.3,
    },
    "largemouth bass": {
        "pressure_sensitivity": 0.75,
        "temp_optimal": (65.0, 75.0),
        "temp_good": (58.0, 80.0),
        "temp_marginal": (50.0, 88.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "dawn",
        "time_multiplier": 1.3,
    },
    "salmon": {
        "pressure_sensitivity": 0.5,
        "temp_optimal": (50.0, 60.0),
        "temp_good": (45.0, 65.0),
        "temp_marginal": (38.0, 70.0),
        "tide_preference": "incoming",
        "tide_multiplier": 1.2,
        "time_preference": "dawn",
        "time_multiplier": 1.2,
    },
    "steelhead": {
        "pressure_sensitivity": 0.5,
        "temp_optimal": (45.0, 58.0),
        "temp_good": (40.0, 62.0),
        "temp_marginal": (35.0, 68.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "dawn",
        "time_multiplier": 1.2,
    },
    "trout": {
        "pressure_sensitivity": 0.5,
        "temp_optimal": (50.0, 65.0),
        "temp_good": (44.0, 70.0),
        "temp_marginal": (38.0, 75.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "dawn",
        "time_multiplier": 1.2,
    },
    "catfish": {
        "pressure_sensitivity": 0.6,
        "temp_optimal": (70.0, 85.0),
        "temp_good": (62.0, 88.0),
        "temp_marginal": (55.0, 92.0),
        "tide_preference": "any",
        "tide_multiplier": 1.0,
        "time_preference": "night",
        "time_multiplier": 1.4,
    },
    "sheepshead": {
        "pressure_sensitivity": 0.3,
        "temp_optimal": (60.0, 80.0),
        "temp_good": (52.0, 85.0),
        "temp_marginal": (45.0, 90.0),
        "tide_preference": "outgoing",
        "tide_multiplier": 1.1,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "tautog": {
        "pressure_sensitivity": 0.3,
        "temp_optimal": (50.0, 65.0),
        "temp_good": (45.0, 70.0),
        "temp_marginal": (40.0, 75.0),
        "tide_preference": "slack",
        "tide_multiplier": 1.15,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
    "king mackerel": {
        "pressure_sensitivity": 0.5,
        "temp_optimal": (72.0, 85.0),
        "temp_good": (65.0, 88.0),
        "temp_marginal": (58.0, 92.0),
        "tide_preference": "outgoing",
        "tide_multiplier": 1.1,
        "time_preference": "dawn",
        "time_multiplier": 1.2,
    },
    "bluefish": {
        "pressure_sensitivity": 0.6,
        "temp_optimal": (60.0, 75.0),
        "temp_good": (52.0, 80.0),
        "temp_marginal": (45.0, 85.0),
        "tide_preference": "outgoing",
        "tide_multiplier": 1.15,
        "time_preference": "dawn",
        "time_multiplier": 1.2,
    },
    "cobia": {
        "pressure_sensitivity": 0.5,
        "temp_optimal": (68.0, 82.0),
        "temp_good": (62.0, 86.0),
        "temp_marginal": (55.0, 90.0),
        "tide_preference": "incoming",
        "tide_multiplier": 1.1,
        "time_preference": "any",
        "time_multiplier": 1.0,
    },
}


# ---------------------------------------------------------------------------
# Seasonal behavior (API-MANUAL.md §17: "Spawning season multipliers
# (2.0-3.0x during peak runs)")
# ---------------------------------------------------------------------------


class SeasonalEntry(TypedDict, total=False):
    spawning_multiplier: float
    pre_spawn_multiplier: float
    closed: bool


# Keyed species -> month (1-12) -> SeasonalEntry. Months not present carry
# no seasonal adjustment (multiplier 1.0, not closed).
SEASONAL_BEHAVIOR: dict[str, dict[int, SeasonalEntry]] = {
    "redfish": {
        9: {"pre_spawn_multiplier": 1.5},
        10: {"spawning_multiplier": 2.5},
    },
    "striped bass": {
        4: {"pre_spawn_multiplier": 1.5},
        5: {"spawning_multiplier": 3.0},
    },
    "flounder": {
        10: {"pre_spawn_multiplier": 1.5},
        11: {"spawning_multiplier": 2.0},
    },
    "snook": {
        # Regulatory closure (FL Atlantic/Gulf summer closed season) —
        # score 0 regardless of environmental conditions.
        6: {"closed": True},
        7: {"closed": True},
        8: {"closed": True},
    },
    "king mackerel": {
        # Migration windows treated as activity boosts via the same
        # spawning_multiplier field (schema has no dedicated "migration"
        # field; reused here for the southward fall run and northward
        # spring run).
        4: {"spawning_multiplier": 1.5},
        5: {"spawning_multiplier": 1.5},
        10: {"spawning_multiplier": 1.5},
        11: {"spawning_multiplier": 1.5},
    },
    "salmon": {
        8: {"pre_spawn_multiplier": 1.5},
        9: {"spawning_multiplier": 2.5},
        10: {"spawning_multiplier": 2.5},
    },
    "walleye": {
        3: {"pre_spawn_multiplier": 1.5},
        4: {"spawning_multiplier": 2.0},
    },
    "largemouth bass": {
        3: {"pre_spawn_multiplier": 1.5},
        4: {"spawning_multiplier": 2.0},
        5: {"spawning_multiplier": 2.0},
    },
}
