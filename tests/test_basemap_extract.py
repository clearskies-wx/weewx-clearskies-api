"""Tests for services/basemap_extract.py (Phase M, task M1 — CS-BASEMAP).

Contract: docs/planning/MARINE-AND-MAPS-PLAN-2026-08-27.md §M1, "Lead mechanics
— API side" (2026-08-27 coordinator design, binding). Module under test does
not exist yet at the commit these tests were first written against (cf0318d)
— see PRE-CHANGE FAILURE TRANSCRIPT below. Written by clearskies-test-author,
M1-API round.

Design pinned by the plan text (verbatim formulas — not this test's choice):
  - Seismic box: station lat/lon (services.station.get_station_info()),
    radius = settings.earthquakes.default_radius_km * 1.15, km->deg via
    lat_delta = radius_km / 111.32, lon_delta = radius_km / (111.32 * cos(lat)).
  - Marine box: bounding box of companion_proxy.marine_discovery_get("/marine",
    {}) locations' {"coordinates": {"lat","lon"}}, padded by 40 px at z15,
    where 1 px = 156543.03 * cos(lat) / 2**15 meters, at the STATION's own
    latitude (brief's own KAT text: "the marine 40-px pad at z15/lat 33.66" —
    33.66 is the station's latitude, not any marine location's).
  - No marine service configured (MarineDiscoveryUnconfiguredError) -> seismic
    box alone (structural, not a failure).
  - Marine service configured but unreachable (MarineDiscoveryUnavailableError)
    -> the extract REFUSES loudly (raises), no file is written.
  - compute_radar_bounds() = settings.radar.librewxr_bounds ("south,west,
    north,east" CSV per RadarSettings/providers/radar/librewxr.py configure())
    REORDERED to "west,south,east,north" (the pmtiles --bbox convention
    compute_local_bounds() also returns) when set, else None (station-box
    fallback happens one level up, in the caller — directive 14).

Known-answer test note (rules/verification.md): the "independent route" this
suite uses is a from-scratch re-implementation of the SAME formula the plan
pins (lat/111.32 planar km->deg, not a geodesic/pyproj circle). A true
geodesic distance at this latitude/radius (~230 km) diverges from the pinned
planar approximation by roughly 0.007 degrees at 33.66N — more than the
1e-3-degree tolerance the brief specifies — so pyproj would fail a CORRECT
implementation of the pinned formula. The guard here is independent in code
provenance (a fresh expression in this file, never calling the module's own
arithmetic) rather than independent in method, because the method itself is
the thing under contract.

Signature assumptions -- confirmed by the lead (2026-08-27, relayed to
m1api-dev as the binding contract) after this test-author's SendMessage:
  - compute_local_bounds(settings) -> str "west,south,east,north"
  - compute_radar_bounds(settings) -> str | None ("west,south,east,north",
    reordered from the settings CSV -- not a verbatim passthrough; this
    test-author's first draft assumed verbatim and that was a test-side bug,
    corrected against the dev's WIP module, per this round's re-run step)
  - start_extract_in_background(settings) -> bool (False if already running)
  - tier_path(tier: str) -> Path -- the per-tier file path function (lead-
    named exactly this; monkeypatched in the file-write tests below instead
    of a directory constant)

get_basemap_status() -- FINDING reported to the lead 2026-08-27, RULED same
day: this test-author's brief names the status function
`get_basemap_status()` verbatim; the dev's WIP module (uncommitted at the
time this note was written) had it as `get_status()`. Lead ruling:
`get_basemap_status()` is the pinned name; the dev renames before
committing. These tests call `get_basemap_status()` accordingly.

PRE-CHANGE FAILURE TRANSCRIPT (run at HEAD cf0318d; the dev's module did not
exist as a git-tracked file yet, and starts fully absent for a truly clean
checkout of this commit; the transcript below is the raw run captured by
this test-author at the time these tests were first exercised — see the
closeout report for the exact commit-relative provenance and the full raw
output).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from weewx_clearskies_api.services.station import StationInfo

# ---------------------------------------------------------------------------
# Independent reference formulas (hand-coded, NOT imported from the module
# under test) -- rules/verification.md known-answer mandate.
# ---------------------------------------------------------------------------

_KM_PER_DEGREE_LAT = 111.32
_MERCATOR_ORIGIN_SHIFT = 156543.03  # meters/pixel at zoom 0, equator
_PAD_PX = 40
_PAD_ZOOM = 15


def _reference_seismic_box(
    lat: float, lon: float, radius_km: float
) -> tuple[float, float, float, float]:
    """Independent seismic-box reference: (west, south, east, north)."""
    lat_delta = radius_km / _KM_PER_DEGREE_LAT
    lon_delta = radius_km / (_KM_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return (lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)


def _reference_marine_pad_degrees(station_lat: float) -> tuple[float, float]:
    """Independent 40px@z15 pad reference, in (pad_lat_deg, pad_lon_deg).

    156543.03 * cos(lat) / 2**15 meters/pixel at the station latitude,
    converted to degrees via the same planar km->deg convention the plan
    pins for the seismic box (lat/111.32, lon/(111.32*cos lat)).
    """
    pad_m = _PAD_PX * _MERCATOR_ORIGIN_SHIFT * math.cos(math.radians(station_lat)) / (2**_PAD_ZOOM)
    pad_km = pad_m / 1000.0
    pad_lat_deg = pad_km / _KM_PER_DEGREE_LAT
    pad_lon_deg = pad_km / (_KM_PER_DEGREE_LAT * math.cos(math.radians(station_lat)))
    return pad_lat_deg, pad_lon_deg


def _reference_marine_box(
    locations: list[dict[str, Any]], station_lat: float
) -> tuple[float, float, float, float]:
    """Independent marine-box reference: bounding box + 40px@z15 pad."""
    lats = [loc["coordinates"]["lat"] for loc in locations]
    lons = [loc["coordinates"]["lon"] for loc in locations]
    pad_lat_deg, pad_lon_deg = _reference_marine_pad_degrees(station_lat)
    return (
        min(lons) - pad_lon_deg,
        min(lats) - pad_lat_deg,
        max(lons) + pad_lon_deg,
        max(lats) + pad_lat_deg,
    )


def _reference_union(
    box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    return (
        min(box_a[0], box_b[0]),
        min(box_a[1], box_b[1]),
        max(box_a[2], box_b[2]),
        max(box_a[3], box_b[3]),
    )


def _parse_bounds_str(bounds: str) -> tuple[float, float, float, float]:
    parts = [float(p.strip()) for p in bounds.split(",")]
    assert len(parts) == 4, f"expected 'west,south,east,north', got {bounds!r}"
    return (parts[0], parts[1], parts[2], parts[3])


def _assert_boxes_close(
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
    tol: float = 1e-3,
) -> None:
    for a, e, label in zip(actual, expected, ("west", "south", "east", "north"), strict=True):
        assert a == pytest.approx(e, abs=tol), f"{label}: {a} != {e} (tol {tol})"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_STATION_LAT = 33.65683
_STATION_LON = -117.98267
_DEFAULT_RADIUS_KM = 200.0
_RADIUS_FACTOR = 1.15

_LIVE_RADAR_BOUNDS = "26.75,-129.5,40.75,-105.5"  # M0 measured, brief header


def _fake_station_info() -> StationInfo:
    return StationInfo(
        station_id="TESTSTN",
        name="Test Station",
        latitude=_STATION_LAT,
        longitude=_STATION_LON,
        altitude=10.0,
        timezone="America/Los_Angeles",
        timezone_offset_minutes=-480,
        unit_system="US",
        hardware="test-hardware",
    )


@dataclass
class _FakeEarthquakesSettings:
    default_radius_km: float = _DEFAULT_RADIUS_KM


@dataclass
class _FakeRadarSettings:
    librewxr_bounds: str | None = None


@dataclass
class _FakeSettings:
    earthquakes: _FakeEarthquakesSettings
    radar: _FakeRadarSettings

    def __init__(
        self,
        default_radius_km: float = _DEFAULT_RADIUS_KM,
        librewxr_bounds: str | None = None,
    ) -> None:
        self.earthquakes = _FakeEarthquakesSettings(default_radius_km=default_radius_km)
        self.radar = _FakeRadarSettings(librewxr_bounds=librewxr_bounds)


def _patch_station(monkeypatch: pytest.MonkeyPatch, info: StationInfo) -> None:
    """basemap_extract does `from ...services.station import get_station_info`
    LOCALLY inside each function body (not a module-level name in
    basemap_extract's own namespace) -- patch the source attribute so the
    fresh import picks it up."""
    import weewx_clearskies_api.services.station as station_module

    monkeypatch.setattr(station_module, "get_station_info", lambda: info)


def _patch_marine_discovery(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    """Same reasoning as _patch_station -- basemap_extract calls
    `companion_proxy.marine_discovery_get(...)` module-qualified after a
    local `from ...services import companion_proxy` import; patch the
    source module's attribute."""
    import weewx_clearskies_api.services.companion_proxy as companion_proxy_module

    monkeypatch.setattr(companion_proxy_module, "marine_discovery_get", fn)


def _marine_locations_pushing_north_and_west() -> list[dict[str, Any]]:
    """Two marine locations chosen so ONE pushes the union's north edge and
    the OTHER pushes the union's west edge, while south/east stay from the
    seismic box -- isolates both axes of the pad + union logic in one KAT."""
    return [
        {"locationId": "north-point", "coordinates": {"lat": 41.0, "lon": -118.0}},
        {"locationId": "west-point", "coordinates": {"lat": 33.0, "lon": -121.0}},
    ]


# ---------------------------------------------------------------------------
# Bounds KAT — compute_local_bounds()
# ---------------------------------------------------------------------------


class TestSeismicBoxOnlyNoMarineConfigured:
    """No marine service configured -> seismic box alone (structural, not a
    failure) -- plan text verbatim."""

    def test_seismic_box_matches_independent_reference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnconfiguredError,
        )

        _patch_station(monkeypatch, _fake_station_info())

        def _raise_unconfigured(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnconfiguredError("marine_service_url is not configured")

        _patch_marine_discovery(monkeypatch, _raise_unconfigured)

        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        bounds_str = basemap_extract.compute_local_bounds(settings)

        expected = _reference_seismic_box(
            _STATION_LAT, _STATION_LON, _DEFAULT_RADIUS_KM * _RADIUS_FACTOR
        )
        _assert_boxes_close(_parse_bounds_str(bounds_str), expected)


class TestMarineBoxUnion:
    """Marine configured and reachable -> union(seismic_box, padded marine
    box). Locations chosen so north/west come from marine+pad and south/east
    stay from the seismic box (isolates both the pad math and the union
    min/max logic in one KAT)."""

    def test_union_matches_independent_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from weewx_clearskies_api.services import basemap_extract

        _patch_station(monkeypatch, _fake_station_info())

        locations = _marine_locations_pushing_north_and_west()

        def _return_locations(path: str, params: dict[str, Any]) -> Any:
            assert path == "/marine"
            assert params == {}
            return locations

        _patch_marine_discovery(monkeypatch, _return_locations)

        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        bounds_str = basemap_extract.compute_local_bounds(settings)

        seismic = _reference_seismic_box(
            _STATION_LAT, _STATION_LON, _DEFAULT_RADIUS_KM * _RADIUS_FACTOR
        )
        marine = _reference_marine_box(locations, _STATION_LAT)
        expected = _reference_union(seismic, marine)
        _assert_boxes_close(_parse_bounds_str(bounds_str), expected)

    def test_north_edge_comes_from_marine_not_seismic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation-style isolation: the north-point location (lat 41) is far
        outside the seismic box's own north edge (~35.72) -- if the union
        silently dropped the marine box, this would fail."""
        from weewx_clearskies_api.services import basemap_extract

        _patch_station(monkeypatch, _fake_station_info())
        locations = _marine_locations_pushing_north_and_west()
        _patch_marine_discovery(monkeypatch, lambda path, params: locations)

        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        _, _, _, north = _parse_bounds_str(basemap_extract.compute_local_bounds(settings))

        seismic_north = _reference_seismic_box(
            _STATION_LAT, _STATION_LON, _DEFAULT_RADIUS_KM * _RADIUS_FACTOR
        )[3]
        assert north > seismic_north + 1.0, (
            "north edge must come from the marine location (lat 41), not the "
            f"seismic-only box (north={seismic_north})"
        )


class TestMarineUnreachableRefusesLoudly:
    """Marine configured but unreachable -> the extract REFUSES loudly (no
    silent fallback to the seismic box) -- plan text verbatim, rules/coding.md
    "a model runs on all its inputs"."""

    def test_raises_when_marine_service_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnavailableError,
        )

        _patch_station(monkeypatch, _fake_station_info())

        def _raise_unavailable(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnavailableError("The marine service is unreachable: timeout")

        _patch_marine_discovery(monkeypatch, _raise_unavailable)

        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        with pytest.raises(Exception):  # noqa: B017 -- exact exception type is dev's to name
            basemap_extract.compute_local_bounds(settings)

    def test_no_file_written_when_marine_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End-to-end: start_extract_in_background() must not leave a partial
        or stale-looking file behind when the bounds computation itself
        refuses (rules/coding.md security constraint 1 -- never write outside
        /etc/weewx-clearskies/ or /tmp; here we additionally check nothing
        lands in the tmp basemap dir at all)."""
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnavailableError,
        )

        monkeypatch.setattr(
            basemap_extract, "tier_path", lambda tier: tmp_path / f"basemap-{tier}.pmtiles"
        )
        _patch_station(monkeypatch, _fake_station_info())

        def _raise_unavailable(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnavailableError("The marine service is unreachable: timeout")

        _patch_marine_discovery(monkeypatch, _raise_unavailable)

        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        started = basemap_extract.start_extract_in_background(settings)

        # Whether start_extract_in_background() raises synchronously or
        # returns and fails on its own daemon thread, either way no tile
        # file may exist in the (monkeypatched) basemap directory once the
        # attempt is done. Poll briefly for the async-thread case.
        import time

        if started:
            for _ in range(50):
                if not basemap_extract.get_basemap_status().get("updating", False):
                    break
                time.sleep(0.1)

        assert list(tmp_path.glob("basemap-*.pmtiles")) == []


# ---------------------------------------------------------------------------
# Radar box passthrough KAT — compute_radar_bounds()
# ---------------------------------------------------------------------------


class TestRadarBoxPassthrough:
    """compute_radar_bounds() reorders settings.radar.librewxr_bounds
    ("south,west,north,east" per RadarSettings) to "west,south,east,north"
    (the same order compute_local_bounds() returns) when set, else None --
    the station-box fallback happens one level up, in the caller, per
    directive 14 -- not this function's job."""

    def test_reorders_configured_librewxr_bounds_to_west_south_east_north(self) -> None:
        from weewx_clearskies_api.services import basemap_extract

        settings = _FakeSettings(librewxr_bounds=_LIVE_RADAR_BOUNDS)
        result = basemap_extract.compute_radar_bounds(settings)

        south, west, north, east = (float(p) for p in _LIVE_RADAR_BOUNDS.split(","))
        expected = f"{west},{south},{east},{north}"
        assert result == expected

    def test_returns_none_when_librewxr_bounds_unset(self) -> None:
        from weewx_clearskies_api.services import basemap_extract

        settings = _FakeSettings(librewxr_bounds=None)
        result = basemap_extract.compute_radar_bounds(settings)
        assert result is None


# ---------------------------------------------------------------------------
# TIERS
# ---------------------------------------------------------------------------


class TestTiers:
    def test_tiers_zoom_ranges_match_the_plan(self) -> None:
        from weewx_clearskies_api.services import basemap_extract

        assert basemap_extract.TIERS == {
            "world": (0, 6),
            "local": (7, 15),
            "radar": (0, 12),
        }


# ===========================================================================
# PRE-CHANGE FAILURE TRANSCRIPT -- git HEAD for this test file is still
# cf0318d (unchanged; the API repo's git-tracked state has no basemap_extract
# module). This test-author and m1api-dev worked concurrently in the same
# shared working directory; m1api-dev's basemap_extract.py existed on disk
# (uncommitted) by the time this suite's environment was working, so the
# FIRST real run against a truly absent module was masked by an unrelated
# local toolchain failure (uv defaulted to a Python 3.14 interpreter with no
# prebuilt pydantic-core wheel, forcing a Rust sdist build that failed for
# want of a working MSVC link.exe -- resolved by pinning `--python 3.13`;
# no production file touched to fix this). The first run against the actual
# WIP module (before this test-author's own patch-target/reorder fixes)
# genuinely failed 6/8, proving the suite is not vacuously green:
#
# $ uv run --frozen --extra dev pytest tests/test_basemap_extract.py -q --tb=short
# FFFFFF..                                                                 [100%]
# ================================== FAILURES ===================================
# _ TestSeismicBoxOnlyNoMarineConfigured.test_seismic_box_matches_independent_reference _
# tests\test_basemap_extract.py:213: in test_seismic_box_matches_independent_reference
#     monkeypatch.setattr(basemap_extract, "get_station_info", _fake_station_info)
# E   AttributeError: <module '...basemap_extract'> has no attr 'get_station_info'
# _________ TestMarineBoxUnion.test_union_matches_independent_reference _________
# E   AttributeError: ... has no attribute 'get_station_info'
# ______ TestMarineBoxUnion.test_north_edge_comes_from_marine_not_seismic _______
# E   AttributeError: ... has no attribute 'get_station_info'
# _ TestMarineUnreachableRefusesLoudly.test_raises_when_marine_service_unreachable _
# E   AttributeError: ... has no attribute 'get_station_info'
# _ TestMarineUnreachableRefusesLoudly.test_no_file_written_when_marine_unreachable _
# E   AttributeError: ... has no attribute 'get_station_info'
# _ TestRadarBoxPassthrough.test_passes_through_configured_librewxr_bounds_verbatim _
# tests\test_basemap_extract.py:361: in test_passes_through_configured_librewxr_bounds_verbatim
#     assert result == _LIVE_RADAR_BOUNDS
# E   AssertionError: assert '-129.5,26.75,-105.5,40.75' == '26.75,-129.5,40.75,-105.5'
# 6 failed, 2 passed in 0.95s
#
# The AttributeError failures were this test-author's own bug (patched the
# wrong namespace -- basemap_extract.get_station_info instead of the source
# module, since the dev's module does a LOCAL `from ...station import
# get_station_info` inside each function body) -- fixed via _patch_station()/
# _patch_marine_discovery() above. The radar-bounds AssertionError was this
# test-author's self-contradictory first draft (see the module docstring's
# "compute_radar_bounds()" note) -- fixed to expect the reordered
# "west,south,east,north" string, which is what the lead's ruling actually
# pins. Both are test-side bugs, not contract deviations -- see the closeout
# report for the full accounting, including the one genuine FINDING
# (get_status() vs get_basemap_status() naming, ruled by the lead same day).
# ===========================================================================


class TestMarineConfiguredButNoLocations:
    """Gate M1-API finding F1 (2026-08-27): the marine service answers HTTP 404
    on GET /marine when it is installed but has no locations configured yet
    (weewx_clearskies_marine/endpoints/marine.py list_marine_locations). That
    is the same structural "no marine box" state as unconfigured -- the local
    tier must use the seismic box alone, not refuse forever. Any other status
    (or a network failure, status_code None) is still a genuine outage and
    still refuses. Pre-change (6fda155): the 404 case raised
    MarineDiscoveryUnavailableError out of compute_local_bounds()."""

    def test_404_from_marine_means_no_locations_seismic_box_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnavailableError,
        )

        _patch_station(monkeypatch, _fake_station_info())

        def _raise_404(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnavailableError(
                "The marine service returned HTTP 404 for /marine", status_code=404
            )

        _patch_marine_discovery(monkeypatch, _raise_404)
        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)

        got = _parse_bounds_str(basemap_extract.compute_local_bounds(settings))
        want = _reference_seismic_box(
            _STATION_LAT, _STATION_LON, _DEFAULT_RADIUS_KM * _RADIUS_FACTOR
        )
        for g, w in zip(got, want, strict=True):
            assert abs(g - w) < 1e-3

    def test_non_404_status_still_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnavailableError,
        )

        _patch_station(monkeypatch, _fake_station_info())

        def _raise_503(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnavailableError(
                "The marine service returned HTTP 503 for /marine", status_code=503
            )

        _patch_marine_discovery(monkeypatch, _raise_503)
        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        with pytest.raises(MarineDiscoveryUnavailableError):
            basemap_extract.compute_local_bounds(settings)

    def test_network_failure_has_no_status_and_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from weewx_clearskies_api.services import basemap_extract
        from weewx_clearskies_api.services.companion_proxy import (
            MarineDiscoveryUnavailableError,
        )

        _patch_station(monkeypatch, _fake_station_info())

        def _raise_net(path: str, params: dict[str, Any]) -> Any:
            raise MarineDiscoveryUnavailableError("The marine service is unreachable: timeout")

        _patch_marine_discovery(monkeypatch, _raise_net)
        settings = _FakeSettings(default_radius_km=_DEFAULT_RADIUS_KM)
        with pytest.raises(MarineDiscoveryUnavailableError) as excinfo:
            basemap_extract.compute_local_bounds(settings)
        assert excinfo.value.status_code is None
