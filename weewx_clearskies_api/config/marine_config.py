"""Marine location config schema and loader (Phase 0C, T0C.2).

Config loading pattern mirrors config/settings.py: hand-rolled classes with
an ``__init__`` that reads from a configobj section dict — not pydantic.

The ``[marine]`` section of api.conf is additive and optional per
OPERATIONS-MANUAL.md "Marine location configuration" — its absence has zero
impact on the rest of the API.  ``load_marine_config()`` returns ``None``
when the section is absent, and an empty ``MarineConfig`` when the section
is present but has no locations configured.

Example api.conf shape (see OPERATIONS-MANUAL.md for the full schema table):

    [marine]
      [[locations]]
        [[[wrightsville_beach]]]
          name = Wrightsville Beach
          lat = 34.2085
          lon = -77.7964
          activities = surf, beach_safety, fishing
          ndbc_station_ids = 41110, 41037
          coops_station_ids = 8658163
          nws_marine_zone_id = AMZ250
          nwps_wfo = ILM
          nwps_cg_grid = CG1
          # Optional overrides for the SRF provider (T1.4, Marine
          # Remediation Plan) -- only needed when the spot's coordinates
          # resolve to a marine zone or a WFO that doesn't issue its SRF:
          # nws_srf_zone_id = CAZ552
          # nws_srf_wfo = SGX

          [[[[surf]]]]
            beach_facing_degrees = 135
            bottom_type = sand
            topographic_feature = straight_beach
            directional_exposure = N:false, NE:false, E:true, SE:true,
                S:true, SW:true, W:false, NW:false

          [[[[fishing]]]]
            target_categories = saltwater_inshore, bottom_fish

          [[[[beach_safety]]]]
            [[[[[external_links]]]]]
              [[[[[[water_quality]]]]]]
                label = NC Beach Water Quality
                url = https://ncdeq.gov/beach-water-quality
"""

from __future__ import annotations

from typing import Any

import configobj

# ---------------------------------------------------------------------------
# Valid value sets (OPERATIONS-MANUAL.md "Marine location configuration")
# ---------------------------------------------------------------------------
_VALID_ACTIVITIES: frozenset[str] = frozenset({"marine", "surf", "fishing", "beach_safety"})
_VALID_BOTTOM_TYPES: frozenset[str] = frozenset({"sand", "rock", "coral_reef", "mixed"})
_VALID_TOPOGRAPHIC_FEATURES: frozenset[str] = frozenset(
    {"point_break", "bay_break", "headland", "straight_beach"}
)
_VALID_TARGET_CATEGORIES: frozenset[str] = frozenset(
    {"saltwater_inshore", "bottom_fish", "freshwater_sport", "salmonids"}
)
_VALID_STRUCTURE_TYPES: frozenset[str] = frozenset(
    {"jetty", "pier", "breakwater", "seawall", "groin"}
)
_VALID_STRUCTURE_MATERIALS: frozenset[str] = frozenset(
    {"impermeable", "semi_permeable", "permeable"}
)
_COMPASS_DIRECTIONS: tuple[str, ...] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


# ---------------------------------------------------------------------------
# Small parsing helpers (configobj returns either a bare str or a list
# depending on whether the INI value contained a comma)
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a plain dict for a configobj (sub)section, or {} if absent."""
    if isinstance(value, dict):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[str]:
    """Normalise a configobj value (bare str or list) into list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    raw = str(value).strip()
    return [raw] if raw else []


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _opt_str(section: dict[str, Any], key: str) -> str | None:
    raw = str(section.get(key, "")).strip()
    return raw if raw else None


def _opt_float(section: dict[str, Any], key: str) -> float | None:
    raw = section.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    return float(raw)


def _parse_directional_exposure(raw: Any) -> dict[str, bool]:
    """Parse ``directional_exposure`` into a dict of all 8 compass directions.

    configobj turns ``N:false, NE:false, ...`` into a list of ``"DIR:bool"``
    strings (comma-separated, no brackets).  Directions not mentioned default
    to False.
    """
    result: dict[str, bool] = dict.fromkeys(_COMPASS_DIRECTIONS, False)
    if isinstance(raw, dict):
        for direction, value in raw.items():
            if direction in _COMPASS_DIRECTIONS:
                result[direction] = _as_bool(value)
        return result
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    for item in items:
        item_str = str(item).strip()
        if not item_str or ":" not in item_str:
            continue
        direction, _, value = item_str.partition(":")
        direction = direction.strip()
        if direction in _COMPASS_DIRECTIONS:
            result[direction] = _as_bool(value)
    return result


# ---------------------------------------------------------------------------
# Leaf dataclasses
# ---------------------------------------------------------------------------


class BathymetryPoint:
    """One point in a surf spot's bathymetric profile (CUDEM-derived)."""

    distance_m: float
    depth_m: float

    def __init__(self, section: dict[str, Any]) -> None:
        self.distance_m = float(section.get("distance_m", 0.0))
        self.depth_m = float(section.get("depth_m", 0.0))


class ExternalLink:
    """Operator-provided informational link (beach safety resources)."""

    label: str
    url: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.label = str(section.get("label", "")).strip()
        self.url = str(section.get("url", "")).strip()


class StructureConfig:
    """A coastal structure (jetty/pier/breakwater/seawall/groin) near a surf spot."""

    type: str
    material: str
    length_m: float
    bearing_degrees: float
    distance_m: float
    #: Bearing (degrees true) from the structure's nearest point to the surf
    #: spot. Optional — enables the directional shadow-zone check in
    #: enrichment/wave_transform.py's _structure_kt_effective() (T7.2).
    #: Auto-populated by GET /setup/marine/discover-structures; None for
    #: manually-entered structures unless the operator supplies it.
    bearing_to_spot_degrees: float | None

    def __init__(self, section: dict[str, Any]) -> None:
        self.type = str(section.get("type", "")).strip()
        self.material = str(section.get("material", "")).strip()
        self.length_m = float(section.get("length_m", 0.0))
        self.bearing_degrees = float(section.get("bearing_degrees", 0.0))
        self.distance_m = float(section.get("distance_m", 0.0))
        self.bearing_to_spot_degrees = _opt_float(section, "bearing_to_spot_degrees")

    def validate(self, location_id: str) -> None:
        """Raise ValueError naming the field + location on bad values."""
        if self.type not in _VALID_STRUCTURE_TYPES:
            raise ValueError(
                f"[marine.locations.{location_id}.surf.structures] type "
                f"{self.type!r} not in {sorted(_VALID_STRUCTURE_TYPES)}"
            )
        if self.material not in _VALID_STRUCTURE_MATERIALS:
            raise ValueError(
                f"[marine.locations.{location_id}.surf.structures] material "
                f"{self.material!r} not in {sorted(_VALID_STRUCTURE_MATERIALS)}"
            )
        if not (0 <= self.bearing_degrees < 360):
            raise ValueError(
                f"[marine.locations.{location_id}.surf.structures] bearing_degrees "
                f"{self.bearing_degrees!r} out of range [0, 360)"
            )
        if self.bearing_to_spot_degrees is not None and not (
            0 <= self.bearing_to_spot_degrees < 360
        ):
            raise ValueError(
                f"[marine.locations.{location_id}.surf.structures] "
                f"bearing_to_spot_degrees {self.bearing_to_spot_degrees!r} "
                "out of range [0, 360)"
            )


class SurfSpotConfig:
    """``[[surf]]`` sub-block settings for a marine location."""

    beach_facing_degrees: float
    bottom_type: str
    beach_slope: float | None
    structures: list[StructureConfig]
    bathymetric_profile: list[BathymetryPoint] | None
    topographic_feature: str
    directional_exposure: dict[str, bool]

    def __init__(self, section: dict[str, Any]) -> None:
        self.beach_facing_degrees = float(section.get("beach_facing_degrees", 0.0))
        self.bottom_type = str(section.get("bottom_type", "")).strip()
        self.beach_slope = _opt_float(section, "beach_slope")
        self.topographic_feature = str(section.get("topographic_feature", "")).strip()
        self.directional_exposure = _parse_directional_exposure(
            section.get("directional_exposure", {})
        )

        raw_structures = _as_dict(section.get("structures", {}))
        self.structures = [StructureConfig(_as_dict(v)) for v in raw_structures.values()]

        raw_profile = _as_dict(section.get("bathymetric_profile", {}))
        self.bathymetric_profile = (
            [BathymetryPoint(_as_dict(v)) for v in raw_profile.values()]
            if raw_profile
            else None
        )

    def validate(self, location_id: str) -> None:
        """Raise ValueError naming the field + location on bad values."""
        if self.bottom_type not in _VALID_BOTTOM_TYPES:
            raise ValueError(
                f"[marine.locations.{location_id}.surf] bottom_type "
                f"{self.bottom_type!r} not in {sorted(_VALID_BOTTOM_TYPES)}"
            )
        if self.topographic_feature not in _VALID_TOPOGRAPHIC_FEATURES:
            raise ValueError(
                f"[marine.locations.{location_id}.surf] topographic_feature "
                f"{self.topographic_feature!r} not in {sorted(_VALID_TOPOGRAPHIC_FEATURES)}"
            )
        if not (0 <= self.beach_facing_degrees < 360):
            raise ValueError(
                f"[marine.locations.{location_id}.surf] beach_facing_degrees "
                f"{self.beach_facing_degrees!r} out of range [0, 360)"
            )
        for structure in self.structures:
            structure.validate(location_id)


class FishingSpotConfig:
    """``[[fishing]]`` sub-block settings for a marine location.

    ``target_categories`` (T6.3, 2026-07-11) replaces the earlier single
    ``target_category: str`` — anglers may target species from multiple
    categories at the same spot (e.g. saltwater_inshore + bottom_fish).
    Backward compat: existing api.conf files with a bare
    ``target_category = saltwater_inshore`` key still load without error,
    normalized to a one-element list.
    """

    target_categories: list[str]
    species: list[str]
    biogeographic_region: str

    def __init__(self, section: dict[str, Any]) -> None:
        if "target_categories" in section:
            self.target_categories = _as_list(section.get("target_categories", []))
        elif "target_category" in section:
            # Old single-value config key — wrap in a list.
            raw = str(section.get("target_category", "")).strip()
            self.target_categories = [raw] if raw else []
        else:
            self.target_categories = []
        # Auto-populated from biogeographic region based on coordinates —
        # may be pre-empty at config-parse time and filled in later.
        self.species = _as_list(section.get("species", []))
        self.biogeographic_region = str(section.get("biogeographic_region", "")).strip()

    def validate(self, location_id: str) -> None:
        """Raise ValueError naming the field + location on bad values."""
        if not self.target_categories:
            raise ValueError(
                f"[marine.locations.{location_id}.fishing] target_categories must not be empty"
            )
        for category in self.target_categories:
            if category not in _VALID_TARGET_CATEGORIES:
                raise ValueError(
                    f"[marine.locations.{location_id}.fishing] target_categories entry "
                    f"{category!r} not in {sorted(_VALID_TARGET_CATEGORIES)}"
                )


class BeachSafetyConfig:
    """``[[beach_safety]]`` sub-block settings for a marine location."""

    external_links: list[ExternalLink]

    def __init__(self, section: dict[str, Any]) -> None:
        raw_links = _as_dict(section.get("external_links", {}))
        self.external_links = [ExternalLink(_as_dict(v)) for v in raw_links.values()]


class MarineLocation:
    """One entry under ``[marine] [[locations]]``."""

    id: str
    name: str
    lat: float
    lon: float
    activities: list[str]
    ndbc_station_ids: list[str]
    coops_station_ids: list[str]
    nws_marine_zone_id: str | None
    nwps_wfo: str | None
    #: NWS public forecast (county) zone ID for the Surf Zone Forecast (SRF)
    #: text product (e.g. "CAZ552" for Orange County, CA). Optional — when
    #: unset, providers/marine/nws_srf.py auto-resolves the zone from
    #: (lat, lon) via /points, with a marine-zone retry (T1.4, Marine
    #: Remediation Plan). Set explicitly for shoreline spots whose
    #: coordinates resolve to a marine zone instead of the covering county.
    nws_srf_zone_id: str | None
    #: WFO office ID that issues the SRF for this location (e.g. "SGX"),
    #: when it differs from the WFO resolved from coordinates or from the
    #: nws_srf_zone_id's own cwa property (T1.4, Marine Remediation Plan —
    #: observed for Huntington Beach, CA: coordinate CWA is LOX, but Orange
    #: County's SRF is issued by SGX). Optional; passed as nws_srf.fetch()'s
    #: wfo_override kwarg when set.
    nws_srf_wfo: str | None
    #: Parsed and stored, but NOT currently read by any provider (T4.5,
    #: Phase 4 cleanup). providers/marine/nwps.py's fetch() hardcodes the
    #: NWPS grid to "CG1" everywhere (v1 simplification: CG1 only, per that
    #: module's docstring) and takes no cg_grid parameter. This field is
    #: reserved for future CG2-5 support and has zero runtime effect today.
    nwps_cg_grid: str | None
    #: Computed at config time (haversine from weewx station to this location).
    station_distance_km: float
    #: OFS model assigned at config time via find_ofs_model(lat, lon).
    ofs_model: str | None
    ofs_fallback: str | None
    ofs_region: str | None

    def __init__(self, location_id: str, section: dict[str, Any]) -> None:
        self.id = location_id
        self.name = str(section.get("name", "")).strip()
        self.lat = float(section.get("lat", 0.0))
        self.lon = float(section.get("lon", 0.0))
        self.activities = _as_list(section.get("activities", []))
        self.ndbc_station_ids = _as_list(section.get("ndbc_station_ids", []))
        self.coops_station_ids = _as_list(section.get("coops_station_ids", []))
        self.nws_marine_zone_id = _opt_str(section, "nws_marine_zone_id")
        nwps_wfo = _opt_str(section, "nwps_wfo")
        self.nwps_wfo = nwps_wfo.lower() if nwps_wfo is not None else None
        self.nws_srf_zone_id = _opt_str(section, "nws_srf_zone_id")
        nws_srf_wfo = _opt_str(section, "nws_srf_wfo")
        self.nws_srf_wfo = nws_srf_wfo.lower() if nws_srf_wfo is not None else None
        self.nwps_cg_grid = _opt_str(section, "nwps_cg_grid")
        self.station_distance_km = float(section.get("station_distance_km", 0.0))

    def validate(self) -> None:
        """Raise ValueError naming the field + location on bad values."""
        if not (-90 <= self.lat <= 90):
            raise ValueError(
                f"[marine.locations.{self.id}] lat {self.lat!r} out of range [-90, 90]"
            )
        if not (-180 <= self.lon <= 180):
            raise ValueError(
                f"[marine.locations.{self.id}] lon {self.lon!r} out of range [-180, 180]"
            )
        if not self.activities:
            raise ValueError(
                f"[marine.locations.{self.id}] activities must not be empty"
            )
        for activity in self.activities:
            if activity not in _VALID_ACTIVITIES:
                raise ValueError(
                    f"[marine.locations.{self.id}] activities value {activity!r} "
                    f"not in {sorted(_VALID_ACTIVITIES)}"
                )


_VALID_FORECAST_TTL_HOURS = {1, 3, 6}
_VALID_OBSERVATION_TTL_MINUTES = {15, 30, 60}


class MarineWeatherConfig:
    """Parsed ``[[weather]]`` subsection under ``[marine]``."""

    forecast_ttl_hours: int
    observation_ttl_minutes: int
    dedup_radius_km: float

    def __init__(
        self,
        forecast_ttl_hours: int = 3,
        observation_ttl_minutes: int = 30,
        dedup_radius_km: float = 2.5,
    ) -> None:
        if forecast_ttl_hours not in _VALID_FORECAST_TTL_HOURS:
            raise ValueError(
                f"[marine.weather] forecast_ttl_hours must be one of "
                f"{sorted(_VALID_FORECAST_TTL_HOURS)}, got {forecast_ttl_hours}"
            )
        if observation_ttl_minutes not in _VALID_OBSERVATION_TTL_MINUTES:
            raise ValueError(
                f"[marine.weather] observation_ttl_minutes must be one of "
                f"{sorted(_VALID_OBSERVATION_TTL_MINUTES)}, got {observation_ttl_minutes}"
            )
        if dedup_radius_km <= 0:
            raise ValueError(
                f"[marine.weather] dedup_radius_km must be > 0, got {dedup_radius_km}"
            )
        self.forecast_ttl_hours = forecast_ttl_hours
        self.observation_ttl_minutes = observation_ttl_minutes
        self.dedup_radius_km = dedup_radius_km


class MarineConfig:
    """Top-level parsed ``[marine]`` section."""

    locations: list[MarineLocation]
    surf_spots: dict[str, SurfSpotConfig]
    fishing_spots: dict[str, FishingSpotConfig]
    beach_safety: dict[str, BeachSafetyConfig]
    weather: MarineWeatherConfig

    def __init__(
        self,
        locations: list[MarineLocation] | None = None,
        surf_spots: dict[str, SurfSpotConfig] | None = None,
        fishing_spots: dict[str, FishingSpotConfig] | None = None,
        beach_safety: dict[str, BeachSafetyConfig] | None = None,
        weather: MarineWeatherConfig | None = None,
    ) -> None:
        self.locations = locations if locations is not None else []
        self.surf_spots = surf_spots if surf_spots is not None else {}
        self.fishing_spots = fishing_spots if fishing_spots is not None else {}
        self.beach_safety = beach_safety if beach_safety is not None else {}
        self.weather = weather if weather is not None else MarineWeatherConfig()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_marine_config(config: configobj.ConfigObj) -> MarineConfig | None:
    """Parse the ``[marine]`` section of api.conf into a MarineConfig.

    Returns:
        None if no ``[marine]`` section is present (marine features are
        fully optional — OPERATIONS-MANUAL.md "Marine location configuration").
        An empty MarineConfig if ``[marine]`` is present but has no locations.

    Raises:
        ValueError: A config value failed validation. The message names the
        offending field and location id.
    """
    if "marine" not in config:
        return None

    marine_section = _as_dict(config["marine"])
    locations_section = _as_dict(marine_section.get("locations", {}))

    locations: list[MarineLocation] = []
    surf_spots: dict[str, SurfSpotConfig] = {}
    fishing_spots: dict[str, FishingSpotConfig] = {}
    beach_safety: dict[str, BeachSafetyConfig] = {}

    for location_id, raw_location in locations_section.items():
        location_dict = _as_dict(raw_location)

        location = MarineLocation(location_id, location_dict)
        location.validate()
        locations.append(location)

        raw_surf = _as_dict(location_dict.get("surf", {}))
        if raw_surf:
            surf = SurfSpotConfig(raw_surf)
            surf.validate(location_id)
            surf_spots[location_id] = surf

        raw_fishing = _as_dict(location_dict.get("fishing", {}))
        if raw_fishing:
            fishing = FishingSpotConfig(raw_fishing)
            fishing.validate(location_id)
            fishing_spots[location_id] = fishing

        raw_beach_safety = _as_dict(location_dict.get("beach_safety", {}))
        if raw_beach_safety:
            beach_safety[location_id] = BeachSafetyConfig(raw_beach_safety)

    weather_section = _as_dict(marine_section.get("weather", {}))
    if weather_section:
        weather = MarineWeatherConfig(
            forecast_ttl_hours=int(weather_section.get("forecast_ttl_hours", 3)),
            observation_ttl_minutes=int(
                weather_section.get("observation_ttl_minutes", 30)
            ),
            dedup_radius_km=float(weather_section.get("dedup_radius_km", 2.5)),
        )
    else:
        weather = MarineWeatherConfig()

    return MarineConfig(
        locations=locations,
        surf_spots=surf_spots,
        fishing_spots=fishing_spots,
        beach_safety=beach_safety,
        weather=weather,
    )
