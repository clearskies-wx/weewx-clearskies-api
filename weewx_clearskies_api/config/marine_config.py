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
_VALID_BREAKER_FORMULAS: frozenset[str] = frozenset({"komar_gaughan", "caldwell"})
_VALID_SURF_HEIGHT_DISPLAYS: frozenset[str] = frozenset({"face", "hawaiian"})
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
# [swan] section constants and config class (T4.2)
# ---------------------------------------------------------------------------

#: Sentinel value for [swan] service_url that means "bundled mode" (run
#: SWAN as a local subprocess inside the API process).  Any other non-empty
#: value activates remote mode (SwanProvider calls the remote HTTP endpoint
#: instead of running SWAN locally).
_SWAN_BUNDLED_SENTINEL = "http://localhost:swan"


class SwanConfig:
    """Parsed ``[swan]`` top-level config section (T4.2, T7.2).

    The ``[swan]`` section is optional.  When absent, all fields take their
    documented defaults (bundled mode, all cores).

    Config keys:
      service_url (str):
        URL of the standalone SWAN service.  Default:
        ``http://localhost:swan`` (the bundled-mode sentinel — means "run
        SWAN locally as a subprocess").  Set to ``http://<remote-host>:8767``
        to activate remote mode.
      omp_num_threads (int, default 0):
        Number of OpenMP threads for SWAN.  0 = let OpenMP decide (uses all
        available CPU cores — the OpenMP default).  Set to a positive integer
        to cap SWAN's CPU usage, e.g. ``2`` on a busy shared host.
      outer_grid_resolution_km (float, default 3.0):
        Resolution of the outer SWAN grid in kilometres.  Matches the NWPS
        CG1 pattern (~2–3 km).  Valid range: 1.0–10.0.  Controls the shelf
        approach domain (uses ``hrrr_bbox``).
      inner_nest_resolution_m (float, default 200.0):
        Resolution of the inner nested SWAN grid in metres.  Controls the
        nearshore domain tight around surf spots (uses ``swan_domain_bbox``).
        Valid range: 50–1000.

    Example api.conf (remote mode, 4 threads, custom resolutions):
      [swan]
        service_url = http://192.168.1.50:8767
        omp_num_threads = 4
        outer_grid_resolution_km = 3.0
        inner_nest_resolution_m = 200
    """

    service_url: str
    omp_num_threads: int
    outer_grid_resolution_km: float
    inner_nest_resolution_m: float

    def __init__(self, section: dict[str, Any]) -> None:
        self.service_url = str(
            section.get("service_url", _SWAN_BUNDLED_SENTINEL)
        ).strip()
        raw_threads = section.get("omp_num_threads")
        if raw_threads is not None and str(raw_threads).strip():
            self.omp_num_threads = int(raw_threads)
        else:
            self.omp_num_threads = 0  # 0 = let OpenMP use all cores (default)

        # T7.2 — nested grid resolution config keys
        raw_outer_km = section.get("outer_grid_resolution_km")
        if raw_outer_km is not None and str(raw_outer_km).strip():
            self.outer_grid_resolution_km = float(raw_outer_km)
        else:
            self.outer_grid_resolution_km = 3.0  # km — matches NWPS CG1 pattern

        raw_inner_m = section.get("inner_nest_resolution_m")
        if raw_inner_m is not None and str(raw_inner_m).strip():
            self.inner_nest_resolution_m = float(raw_inner_m)
        else:
            self.inner_nest_resolution_m = 200.0  # metres — matches current behaviour

    @property
    def is_remote(self) -> bool:
        """True when service_url is set to a non-sentinel, non-empty value.

        In remote mode, SwanProvider fetches wave forecasts from the
        configured standalone SWAN service via HTTP instead of running
        SWAN locally.
        """
        return bool(self.service_url) and self.service_url != _SWAN_BUNDLED_SENTINEL

    def validate(self) -> None:
        """Raise ValueError on bad config values."""
        if self.omp_num_threads < 0:
            raise ValueError(
                f"[swan] omp_num_threads must be >= 0 (0 = all cores), "
                f"got {self.omp_num_threads}"
            )
        if not (1.0 <= self.outer_grid_resolution_km <= 10.0):
            raise ValueError(
                f"[swan] outer_grid_resolution_km must be 1.0–10.0, "
                f"got {self.outer_grid_resolution_km}"
            )
        if not (50.0 <= self.inner_nest_resolution_m <= 1000.0):
            raise ValueError(
                f"[swan] inner_nest_resolution_m must be 50–1000, "
                f"got {self.inner_nest_resolution_m}"
            )
        if self.is_remote:
            if not self.service_url.startswith(("http://", "https://")):
                raise ValueError(
                    f"[swan] service_url must start with http:// or https://, "
                    f"got {self.service_url!r}"
                )


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
    #: spot. Optional — used by the SWAN OBSTACLE shadow-zone geometry to
    #: orient the OBSTACLE LINE command correctly. Auto-populated by
    #: GET /setup/marine/discover-structures; None for manually-entered
    #: structures unless the operator supplies it.
    bearing_to_spot_degrees: float | None
    #: Line geometry for the SWAN OBSTACLE command — list of [lon, lat] pairs.
    #: When empty, the OBSTACLE command is skipped for this structure.
    coordinates: list[list[float]]

    def __init__(self, section: dict[str, Any]) -> None:
        self.type = str(section.get("type", "")).strip()
        self.material = str(section.get("material", "")).strip()
        self.length_m = float(section.get("length_m", 0.0))
        self.bearing_degrees = float(section.get("bearing_degrees", 0.0))
        self.distance_m = float(section.get("distance_m", 0.0))
        self.bearing_to_spot_degrees = _opt_float(section, "bearing_to_spot_degrees")
        self.coordinates = [
            [float(c[0]), float(c[1])] for c in section.get("coordinates", [])
        ]

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
    # T2.6 — breaker height pipeline config keys
    #: Formula used by enrichment/breaker_height.py ``hsig_to_face_height()``.
    #: ``"komar_gaughan"`` (default) — Komar & Gaughan (1973), general purpose.
    #: ``"caldwell"`` — Caldwell & Aucan (2007) H1/10 for steep volcanic coasts.
    breaker_formula: str
    #: Convention for the height displayed in the surf card UI.
    #: ``"face"`` (default) — trough-to-crest face height (Western scale).
    #: ``"hawaiian"`` — back-of-wave scale (= face × 0.5).
    surf_height_display: str

    def __init__(self, section: dict[str, Any]) -> None:
        self.beach_facing_degrees = float(section.get("beach_facing_degrees", 0.0))
        self.bottom_type = str(section.get("bottom_type", "")).strip()
        self.beach_slope = _opt_float(section, "beach_slope")
        self.topographic_feature = str(section.get("topographic_feature", "")).strip()
        self.directional_exposure = _parse_directional_exposure(
            section.get("directional_exposure", {})
        )
        # T2.6 — breaker formula and display convention
        self.breaker_formula = str(section.get("breaker_formula", "komar_gaughan")).strip()
        self.surf_height_display = str(section.get("surf_height_display", "face")).strip()

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
        # T2.6 — validate new breaker pipeline config
        if self.breaker_formula not in _VALID_BREAKER_FORMULAS:
            raise ValueError(
                f"[marine.locations.{location_id}.surf] breaker_formula "
                f"{self.breaker_formula!r} not in {sorted(_VALID_BREAKER_FORMULAS)}"
            )
        if self.surf_height_display not in _VALID_SURF_HEIGHT_DISPLAYS:
            raise ValueError(
                f"[marine.locations.{location_id}.surf] surf_height_display "
                f"{self.surf_height_display!r} not in {sorted(_VALID_SURF_HEIGHT_DISPLAYS)}"
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
        nws_marine_zone_id = _opt_str(section, "nws_marine_zone_id")
        self.nws_marine_zone_id = nws_marine_zone_id.upper() if nws_marine_zone_id is not None else None
        self.nws_srf_zone_id = _opt_str(section, "nws_srf_zone_id")
        nws_srf_wfo = _opt_str(section, "nws_srf_wfo")
        self.nws_srf_wfo = nws_srf_wfo.lower() if nws_srf_wfo is not None else None
        self.station_distance_km = float(section.get("station_distance_km", 0.0))
        self.ofs_model = _opt_str(section, "ofs_model")
        self.ofs_fallback = _opt_str(section, "ofs_fallback")
        self.ofs_region = _opt_str(section, "ofs_region")

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


_HRRR_MARGIN_DEG = 1.0
_SWAN_DOMAIN_MARGIN_DEG = 0.1  # ±0.1° ≈ ±11 km — matches NWPS SGX inner nest scale


class MarineConfig:
    """Top-level parsed ``[marine]`` section, with optional ``[swan]`` settings."""

    locations: list[MarineLocation]
    surf_spots: dict[str, SurfSpotConfig]
    fishing_spots: dict[str, FishingSpotConfig]
    beach_safety: dict[str, BeachSafetyConfig]
    weather: MarineWeatherConfig
    #: Parsed ``[swan]`` section (T4.2).  Always present — defaults to
    #: bundled mode (sentinel service_url, omp_num_threads=0) when the section
    #: is absent from api.conf.
    swan: SwanConfig
    #: Canonical HRRR wind bbox — computed once from ALL marine locations
    #: with a 1° margin.  Both the cache warmer and SWAN read this
    #: instead of computing their own.  None when no locations configured.
    hrrr_bbox: tuple[float, float, float, float] | None
    #: SWAN computational domain — tighter ±0.5° around surf locations only.
    #: None when no surf spots configured.
    swan_domain_bbox: tuple[float, float, float, float] | None

    def __init__(
        self,
        locations: list[MarineLocation] | None = None,
        surf_spots: dict[str, SurfSpotConfig] | None = None,
        fishing_spots: dict[str, FishingSpotConfig] | None = None,
        beach_safety: dict[str, BeachSafetyConfig] | None = None,
        weather: MarineWeatherConfig | None = None,
        swan: SwanConfig | None = None,
    ) -> None:
        self.locations = locations if locations is not None else []
        self.surf_spots = surf_spots if surf_spots is not None else {}
        self.fishing_spots = fishing_spots if fishing_spots is not None else {}
        self.beach_safety = beach_safety if beach_safety is not None else {}
        self.weather = weather if weather is not None else MarineWeatherConfig()
        self.swan = swan if swan is not None else SwanConfig({})

        all_lats = [loc.lat for loc in self.locations]
        all_lons = [loc.lon for loc in self.locations]
        if all_lats and all_lons:
            self.hrrr_bbox = (
                min(all_lons) - _HRRR_MARGIN_DEG,
                min(all_lats) - _HRRR_MARGIN_DEG,
                max(all_lons) + _HRRR_MARGIN_DEG,
                max(all_lats) + _HRRR_MARGIN_DEG,
            )
        else:
            self.hrrr_bbox = None

        surf_ids = set(self.surf_spots.keys())
        surf_locs = [loc for loc in self.locations if loc.id in surf_ids]
        if surf_locs:
            s_lats = [loc.lat for loc in surf_locs]
            s_lons = [loc.lon for loc in surf_locs]
            self.swan_domain_bbox = (
                min(s_lons) - _SWAN_DOMAIN_MARGIN_DEG,
                min(s_lats) - _SWAN_DOMAIN_MARGIN_DEG,
                max(s_lons) + _SWAN_DOMAIN_MARGIN_DEG,
                max(s_lats) + _SWAN_DOMAIN_MARGIN_DEG,
            )
        else:
            self.swan_domain_bbox = None


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

    # [swan] — optional top-level section alongside [marine] (T4.2).
    swan_config = load_swan_config(config)

    return MarineConfig(
        locations=locations,
        surf_spots=surf_spots,
        fishing_spots=fishing_spots,
        beach_safety=beach_safety,
        weather=weather,
        swan=swan_config,
    )


def load_swan_config(config: configobj.ConfigObj) -> SwanConfig:
    """Parse the ``[swan]`` top-level section of api.conf.

    Returns a SwanConfig with defaults (bundled mode, omp_num_threads=0)
    when the section is absent.  Called from load_marine_config() and also
    directly by the standalone service's config loader.

    Raises:
        ValueError: A config value failed validation (e.g. negative omp_num_threads).
    """
    swan_section = _as_dict(config.get("swan", {}))
    swan = SwanConfig(swan_section)
    swan.validate()
    return swan
