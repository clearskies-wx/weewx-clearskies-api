"""Unit tests for the HRRR wind provider (T1.1/T1.2 — SWAN-TRUSHORE-PLAN.md).

Covers:
  - PROVIDER_ID, DOMAIN constants
  - CAPABILITY symbol: present when GRIB_AVAILABLE, None otherwise (T1.1 AC-1)
  - _compute_cone_factor(): tangent case (HRRR), secant case
  - _rotate_wind_vector(): identity at lov, 180° reversal, magnitude preserved
  - _build_grib_filter_url(): URL structure, parameter encoding, fhour zero-padding
  - _compute_hrrr_cycle(): normal case (T-1h lag + floor), near-midnight rollback
  - _build_cache_key(): idempotent, cycle-sensitive, bbox-sensitive, 64 hex chars
  - fetch(): success (structured dict), cycle fallback (T1.1 AC-4),
    all-404 raises ProviderUnavailableError (T1.1 AC-5), cache hit skips network

No real GRIB2 files or eccodes are needed — _download_grib and _extract_hrrr_grib
are both monkeypatched so the actual GRIB2 library is never invoked.

Cache is NOT wired in conftest; each test class that exercises fetch() wires its
own in-memory cache via reset_cache_for_tests() + wire_cache_from_env().
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from weewx_clearskies_api.providers.wind.hrrr import (
    CAPABILITY,
    DOMAIN,
    NOMADS_GRIB_FILTER,
    PROVIDER_ID,
    _HrrrGribData,
    _build_cache_key,
    _build_grib_filter_url,
    _compute_cone_factor,
    _compute_hrrr_cycle,
    _rotate_wind_vector,
    _rotate_wind_field,
)
from weewx_clearskies_api.providers._common.cache import (
    reset_cache_for_tests,
    wire_cache_from_env,
    get_cache,
)
from weewx_clearskies_api.providers._common.errors import ProviderUnavailableError


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

# Fixed cycle time used across all fetch() tests for determinism
_FIXED_CYCLE = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)

# Compact 2×3 fake wind grid (nj=2 rows, ni=3 columns)
_NI, _NJ = 3, 2
_FAKE_GRIB = _HrrrGribData(
    u_grid=[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
    v_grid=[[0.5, 1.0, 1.5], [2.0, 2.5, 3.0]],
    lons_2d=None,
    lov=262.5,
    latin1=38.5,
    latin2=38.5,
    ni=_NI,
    nj=_NJ,
    lat_first=34.0,
    lon_first=282.5,   # -77.5 in [0..360] convention
    lat_last=35.0,
    lon_last=283.5,    # -76.5 in [0..360] convention
)

_TEST_BBOX: tuple[float, float, float, float] = (-77.5, 34.0, -76.5, 35.0)
_FAKE_PATH = "/tmp/hrrr_fake_unit_test.grib2"


@pytest.fixture()
def wired_cache():
    """Wire an in-memory cache for tests that exercise fetch() and reset after."""
    reset_cache_for_tests()
    wire_cache_from_env()
    yield get_cache()
    reset_cache_for_tests()


# ---------------------------------------------------------------------------
# T1.1 AC-1: Provider identity constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_provider_id(self) -> None:
        assert PROVIDER_ID == "hrrr"

    def test_domain(self) -> None:
        assert DOMAIN == "wind"

    def test_nomads_url_base(self) -> None:
        assert NOMADS_GRIB_FILTER == (
            "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
        )


# ---------------------------------------------------------------------------
# T1.1 AC-1: CAPABILITY declaration
# ---------------------------------------------------------------------------


class TestCapability:
    """CAPABILITY must be a ProviderCapability when GRIB is available, else None."""

    def test_capability_matches_grib_available(self) -> None:
        import weewx_clearskies_api.providers.wind.hrrr as hrrr_mod

        if hrrr_mod.GRIB_AVAILABLE:
            assert CAPABILITY is not None
            assert CAPABILITY.provider_id == "hrrr"
            assert CAPABILITY.domain == "wind"
            assert CAPABILITY.auth_required == ()
        else:
            assert CAPABILITY is None

    def test_capability_fields_when_available(self) -> None:
        import weewx_clearskies_api.providers.wind.hrrr as hrrr_mod

        if not hrrr_mod.GRIB_AVAILABLE:
            pytest.skip("GRIB2 backend not installed — CAPABILITY is None")

        assert CAPABILITY is not None
        assert "windUEarth" in CAPABILITY.supplied_canonical_fields
        assert "windVEarth" in CAPABILITY.supplied_canonical_fields
        assert CAPABILITY.geographic_coverage == "us"
        assert CAPABILITY.attribution is not None
        assert "NOAA" in CAPABILITY.attribution.display_name


# ---------------------------------------------------------------------------
# T1.2: _compute_cone_factor
# ---------------------------------------------------------------------------


class TestComputeConeFactor:
    """Lambert Conformal cone factor formula (PROVIDER-MANUAL §14.14)."""

    def test_hrrr_tangent_case(self) -> None:
        """HRRR uses latin1=latin2=38.5° (tangent cone). n = sin(38.5°)."""
        n = _compute_cone_factor(38.5, 38.5)
        expected = math.sin(math.radians(38.5))
        assert abs(n - expected) < 1e-10
        # Confirm the approximate value is ~0.6225
        assert abs(n - 0.6225) < 0.001

    def test_secant_case_polar(self) -> None:
        """Polar Stereographic limit: n → 1 as both parallels → 90°."""
        # Using extreme near-polar parallels for sanity check
        n = _compute_cone_factor(89.0, 89.5)
        assert 0.99 < n < 1.01

    def test_secant_case_equatorial(self) -> None:
        """Very low-latitude cone has n close to 0."""
        n = _compute_cone_factor(5.0, 10.0)
        # sin(5°) ≈ 0.087, sin(10°) ≈ 0.174 — secant case will be between them
        assert 0.05 < n < 0.20

    def test_asymmetry_swapped(self) -> None:
        """latin1 and latin2 may be specified in either order; n should be same."""
        n1 = _compute_cone_factor(30.0, 60.0)
        n2 = _compute_cone_factor(60.0, 30.0)
        assert abs(n1 - n2) < 1e-10

    def test_single_parallel_matches_sin(self) -> None:
        """Tangent case always equals sin(latin1)."""
        for lat in [10.0, 30.0, 45.0, 60.0, 80.0]:
            n = _compute_cone_factor(lat, lat)
            assert abs(n - math.sin(math.radians(lat))) < 1e-10


# ---------------------------------------------------------------------------
# T1.2: _rotate_wind_vector
# ---------------------------------------------------------------------------


class TestRotateWindVector:
    """Earth-relative rotation using the full NCEP Lambert Conformal formula."""

    def test_no_rotation_at_lov(self) -> None:
        """At lon == lov, alpha = 0 → wind is unchanged."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        u_e, v_e = _rotate_wind_vector(3.0, 4.0, lov, lov, n)
        assert abs(u_e - 3.0) < 1e-12
        assert abs(v_e - 4.0) < 1e-12

    def test_reversal_at_180_degrees(self) -> None:
        """At alpha = 180°, earth-relative wind reverses sign."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        # 180° of grid rotation means lon - lov = 180/n
        lon_180 = lov + 180.0 / n
        u_e, v_e = _rotate_wind_vector(3.0, 4.0, lon_180, lov, n)
        assert abs(u_e - (-3.0)) < 1e-10
        assert abs(v_e - (-4.0)) < 1e-10

    def test_magnitude_preserved(self) -> None:
        """Rotation is orthogonal: wind speed (magnitude) must be unchanged."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        u_g, v_g = 5.0, 12.0
        mag_grid = math.sqrt(u_g**2 + v_g**2)

        for lon_offset in [0, 10, 30, 45, 60, 90, 135, 180]:
            lon = lov + lon_offset
            u_e, v_e = _rotate_wind_vector(u_g, v_g, lon, lov, n)
            mag_earth = math.sqrt(u_e**2 + v_e**2)
            assert abs(mag_earth - mag_grid) < 1e-9, (
                f"Magnitude not preserved at lon_offset={lon_offset}: "
                f"grid={mag_grid:.6f} earth={mag_earth:.6f}"
            )

    def test_pure_u_at_90_degrees(self) -> None:
        """At alpha = 90°: pure U grid-relative becomes pure V earth-relative."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        lon_90 = lov + 90.0 / n
        u_e, v_e = _rotate_wind_vector(1.0, 0.0, lon_90, lov, n)
        # cos(90°)=0, sin(90°)=1 → U_earth = 0, V_earth = 1
        assert abs(u_e - 0.0) < 1e-10
        assert abs(v_e - 1.0) < 1e-10


# ---------------------------------------------------------------------------
# T1.2: _rotate_wind_field
# ---------------------------------------------------------------------------


class TestRotateWindField:
    """2-D field rotation wrapper (no lons_2d → linear lon approximation)."""

    def test_shape_preserved(self) -> None:
        """Output grids have same [nj][ni] shape as input."""
        u_grid = [[1.0, 2.0], [3.0, 4.0]]
        v_grid = [[0.5, 1.0], [1.5, 2.0]]
        u_e, v_e = _rotate_wind_field(
            u_grid, v_grid, None, 262.5, 263.5, 262.5, 0.6225, ni=2, nj=2
        )
        assert len(u_e) == 2 and len(u_e[0]) == 2
        assert len(v_e) == 2 and len(v_e[0]) == 2

    def test_single_cell_matches_vector(self) -> None:
        """1x1 grid without lons_2d uses lon_first as the grid-point longitude."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        lon = lov  # at lov → no rotation
        u_grid = [[5.0]]
        v_grid = [[3.0]]
        u_e, v_e = _rotate_wind_field(
            u_grid, v_grid, None, lon, lon, lov, n, ni=1, nj=1
        )
        assert abs(u_e[0][0] - 5.0) < 1e-10
        assert abs(v_e[0][0] - 3.0) < 1e-10

    def test_with_lons_2d(self) -> None:
        """When lons_2d is provided, per-point lons are used."""
        lov = 262.5
        n = _compute_cone_factor(38.5, 38.5)
        lons_2d = [[lov, lov]]   # Both points at lov → no rotation
        u_grid = [[3.0, 4.0]]
        v_grid = [[1.0, 2.0]]
        u_e, v_e = _rotate_wind_field(
            u_grid, v_grid, lons_2d, lov, lov + 1, lov, n, ni=2, nj=1
        )
        # Both should be unrotated
        assert abs(u_e[0][0] - 3.0) < 1e-10
        assert abs(u_e[0][1] - 4.0) < 1e-10
        assert abs(v_e[0][0] - 1.0) < 1e-10
        assert abs(v_e[0][1] - 2.0) < 1e-10


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


class TestBuildGribFilterUrl:
    """NOMADS Grib Filter URL construction (PROVIDER-MANUAL §14.14)."""

    def test_url_starts_with_nomads_base(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 0, _TEST_BBOX)
        assert url.startswith(NOMADS_GRIB_FILTER + "?")

    def test_forecast_hour_zero_padding_single_digit(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 3, _TEST_BBOX)
        assert "wrfsfcf03.grib2" in url

    def test_forecast_hour_two_digits(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 18, _TEST_BBOX)
        assert "wrfsfcf18.grib2" in url

    def test_cycle_hour_zero_padded(self) -> None:
        cycle = datetime(2026, 7, 16, 6, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 0, _TEST_BBOX)
        assert "hrrr.t06z" in url

    def test_date_in_dir(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 0, _TEST_BBOX)
        assert "dir=/hrrr.20260716/conus" in url

    def test_var_params_present(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 0, _TEST_BBOX)
        assert "var_UGRD=on" in url
        assert "var_VGRD=on" in url
        assert "lev_10_m_above_ground=on" in url

    def test_bbox_params_encoded(self) -> None:
        cycle = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url = _build_grib_filter_url(cycle, 0, _TEST_BBOX)
        lon_min, lat_min, lon_max, lat_max = _TEST_BBOX
        assert f"leftlon={lon_min}" in url
        assert f"rightlon={lon_max}" in url
        assert f"toplat={lat_max}" in url
        assert f"bottomlat={lat_min}" in url

    def test_different_cycles_produce_different_urls(self) -> None:
        c1 = datetime(2026, 7, 16, 14, 0, 0, tzinfo=UTC)
        c2 = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        url1 = _build_grib_filter_url(c1, 0, _TEST_BBOX)
        url2 = _build_grib_filter_url(c2, 0, _TEST_BBOX)
        assert url1 != url2


# ---------------------------------------------------------------------------
# Cycle determination
# ---------------------------------------------------------------------------


class TestComputeHrrrCycle:
    """_compute_hrrr_cycle: 1-hour lag + floor to hour."""

    def test_normal_case(self) -> None:
        """16:30Z → lag 1h → 15:30Z → floor → 15Z."""
        now = datetime(2026, 7, 16, 16, 30, 0, tzinfo=UTC)
        cycle = _compute_hrrr_cycle(now)
        assert cycle == datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)

    def test_near_midnight_rollback(self) -> None:
        """00:30Z → lag 1h → 23:30Z yesterday → floor → 23Z yesterday."""
        now = datetime(2026, 7, 16, 0, 30, 0, tzinfo=UTC)
        cycle = _compute_hrrr_cycle(now)
        assert cycle == datetime(2026, 7, 15, 23, 0, 0, tzinfo=UTC)

    def test_exactly_on_hour(self) -> None:
        """15:00Z → lag 1h → 14:00Z → floor → 14Z."""
        now = datetime(2026, 7, 16, 15, 0, 0, tzinfo=UTC)
        cycle = _compute_hrrr_cycle(now)
        assert cycle == datetime(2026, 7, 16, 14, 0, 0, tzinfo=UTC)

    def test_zero_seconds_and_microseconds(self) -> None:
        """Output always has minute=0, second=0, microsecond=0."""
        now = datetime(2026, 7, 16, 16, 45, 30, 123456, tzinfo=UTC)
        cycle = _compute_hrrr_cycle(now)
        assert cycle.minute == 0
        assert cycle.second == 0
        assert cycle.microsecond == 0


# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------


class TestBuildCacheKey:
    """_build_cache_key: SHA-256 digest, provider-scoped, deterministic."""

    def test_returns_64_hex_chars(self) -> None:
        key = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_idempotent(self) -> None:
        key1 = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        key2 = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        assert key1 == key2

    def test_different_cycle_different_key(self) -> None:
        other_cycle = _FIXED_CYCLE + timedelta(hours=1)
        key1 = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        key2 = _build_cache_key(_TEST_BBOX, other_cycle)
        assert key1 != key2

    def test_different_bbox_different_key(self) -> None:
        other_bbox: tuple[float, float, float, float] = (-78.0, 34.0, -77.0, 35.0)
        key1 = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        key2 = _build_cache_key(other_bbox, _FIXED_CYCLE)
        assert key1 != key2

    def test_small_bbox_difference_different_key(self) -> None:
        """Bbox coordinates rounded to 2 d.p. — changes at >=0.01° produce new keys."""
        bbox_a: tuple[float, float, float, float] = (-77.50, 34.00, -76.50, 35.00)
        bbox_b: tuple[float, float, float, float] = (-77.51, 34.00, -76.50, 35.00)
        key_a = _build_cache_key(bbox_a, _FIXED_CYCLE)
        key_b = _build_cache_key(bbox_b, _FIXED_CYCLE)
        assert key_a != key_b


# ---------------------------------------------------------------------------
# T1.1 AC-2, AC-3, AC-4, AC-5: fetch() integration
# ---------------------------------------------------------------------------


class TestFetch:
    """fetch() end-to-end behaviour with _download_grib and _extract_hrrr_grib mocked."""

    # Patch targets — must match the module where the names are looked up
    _MOD = "weewx_clearskies_api.providers.wind.hrrr"
    _PATCH_DOWNLOAD = f"{_MOD}._download_grib"
    _PATCH_EXTRACT = f"{_MOD}._extract_hrrr_grib"
    _PATCH_CYCLE = f"{_MOD}._compute_hrrr_cycle"
    _PATCH_GRIB_AVAIL = f"{_MOD}.GRIB_AVAILABLE"
    _PATCH_OS_UNLINK = f"{_MOD}.os.unlink"

    @pytest.fixture(autouse=True)
    def _cache(self, wired_cache) -> None:  # noqa: ANN001 — wired_cache fixture
        """Ensure each test in this class starts with a clean cache."""

    def test_successful_fetch_returns_required_keys(self) -> None:
        """T1.1 AC-2: fetch() returns structured dict with wind grids."""
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, return_value=_FAKE_PATH),
            patch(self._PATCH_EXTRACT, return_value=_FAKE_GRIB),
            patch(self._PATCH_OS_UNLINK),
        ):
            result = __import__(
                "weewx_clearskies_api.providers.wind.hrrr", fromlist=["fetch"]
            ).fetch(bbox=_TEST_BBOX, max_forecast_hours=1)

        # Top-level keys
        assert "cycle_time" in result
        assert "lov" in result
        assert "latin1" in result
        assert "latin2" in result
        assert "cone_factor" in result
        assert "grids" in result

        assert result["cycle_time"] == "2026-07-16T15:00:00Z"
        assert result["lov"] == _FAKE_GRIB.lov
        assert result["latin1"] == _FAKE_GRIB.latin1
        assert result["latin2"] == _FAKE_GRIB.latin2
        # cone_factor for 38.5/38.5 = sin(38.5°) ≈ 0.6225
        assert abs(result["cone_factor"] - math.sin(math.radians(38.5))) < 1e-10

    def test_successful_fetch_grid_entries(self) -> None:
        """T1.1 AC-3: Each grid entry has valid_time, forecast_hour, u_earth, v_earth."""
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, return_value=_FAKE_PATH),
            patch(self._PATCH_EXTRACT, return_value=_FAKE_GRIB),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            result = fetch(bbox=_TEST_BBOX, max_forecast_hours=2)

        grids = result["grids"]
        assert len(grids) == 3  # f00, f01, f02

        for hour, entry in enumerate(grids):
            assert entry["forecast_hour"] == hour
            assert "valid_time" in entry
            assert "u_earth" in entry
            assert "v_earth" in entry
            assert len(entry["u_earth"]) == _NJ
            assert len(entry["u_earth"][0]) == _NI
            assert len(entry["v_earth"]) == _NJ
            assert len(entry["v_earth"][0]) == _NI
            assert entry["ni"] == _NI
            assert entry["nj"] == _NJ

    def test_successful_fetch_valid_times(self) -> None:
        """valid_time must advance by 1h per forecast hour."""
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, return_value=_FAKE_PATH),
            patch(self._PATCH_EXTRACT, return_value=_FAKE_GRIB),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            result = fetch(bbox=_TEST_BBOX, max_forecast_hours=2)

        expected = [
            "2026-07-16T15:00:00Z",
            "2026-07-16T16:00:00Z",
            "2026-07-16T17:00:00Z",
        ]
        actual = [g["valid_time"] for g in result["grids"]]
        assert actual == expected

    def test_successful_fetch_result_cached(self) -> None:
        """After a successful fetch, the result is stored in the cache."""
        mock_download = MagicMock(return_value=_FAKE_PATH)
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, mock_download),
            patch(self._PATCH_EXTRACT, return_value=_FAKE_GRIB),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            result1 = fetch(bbox=_TEST_BBOX, max_forecast_hours=0)
            download_count_after_first = mock_download.call_count

            # Second call — should hit cache, not download again
            result2 = fetch(bbox=_TEST_BBOX, max_forecast_hours=0)

        assert mock_download.call_count == download_count_after_first, (
            "Second fetch() call should serve from cache without downloading"
        )
        assert result1["cycle_time"] == result2["cycle_time"]

    def test_cycle_fallback_when_first_cycle_unavailable(self) -> None:
        """T1.1 AC-4: When current cycle's f00 returns 404 (None), try previous cycle."""
        # First call (cycle T, f00) → 404; remaining calls → success
        side_effects = [None, _FAKE_PATH]  # f00 of cycle-0 fails; f00 of cycle-1 succeeds
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, side_effect=side_effects),
            patch(self._PATCH_EXTRACT, return_value=_FAKE_GRIB),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            result = fetch(bbox=_TEST_BBOX, max_forecast_hours=0)

        # cycle_time must be the fallback cycle (1 hour earlier)
        expected_fallback_cycle = (_FIXED_CYCLE - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert result["cycle_time"] == expected_fallback_cycle
        assert len(result["grids"]) >= 1

    def test_all_cycles_404_raises_provider_unavailable(self) -> None:
        """T1.1 AC-5: ProviderUnavailableError when all cycle fallbacks return 404."""
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, return_value=None),  # all 404
            patch(self._PATCH_EXTRACT),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            with pytest.raises(ProviderUnavailableError) as exc_info:
                fetch(bbox=_TEST_BBOX, max_forecast_hours=0)

        err = exc_info.value
        assert err.provider_id == "hrrr"
        assert err.domain == "wind"

    def test_cache_hit_skips_download(self) -> None:
        """A warm cache must be served without calling _download_grib."""
        # Pre-populate the cache with a sentinel value
        sentinel: dict[str, Any] = {
            "cycle_time": "2026-07-16T15:00:00Z",
            "lov": 262.5,
            "latin1": 38.5,
            "latin2": 38.5,
            "cone_factor": 0.6225,
            "grids": [],
        }
        from weewx_clearskies_api.providers.wind.hrrr import _build_cache_key
        from weewx_clearskies_api.providers._common.cache import get_cache

        cache_key = _build_cache_key(_TEST_BBOX, _FIXED_CYCLE)
        get_cache().set(cache_key, sentinel, ttl_seconds=3600)

        mock_download = MagicMock()
        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, mock_download),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            result = fetch(bbox=_TEST_BBOX, max_forecast_hours=0)

        assert mock_download.call_count == 0
        assert result is sentinel

    def test_grib_unavailable_raises_runtime_error(self) -> None:
        """fetch() with no GRIB2 backend raises RuntimeError."""
        with patch(self._PATCH_GRIB_AVAIL, False):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            with pytest.raises(RuntimeError, match="eccodes or pygrib"):
                fetch(bbox=_TEST_BBOX)

    def test_grib_extraction_failure_raises_protocol_error(self) -> None:
        """GRIB2 parse failure → ProviderProtocolError (not a bare Exception)."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError

        with (
            patch(self._PATCH_GRIB_AVAIL, True),
            patch(self._PATCH_CYCLE, return_value=_FIXED_CYCLE),
            patch(self._PATCH_DOWNLOAD, return_value=_FAKE_PATH),
            patch(self._PATCH_EXTRACT, side_effect=ValueError("corrupt GRIB2")),
            patch(self._PATCH_OS_UNLINK),
        ):
            from weewx_clearskies_api.providers.wind.hrrr import fetch
            with pytest.raises(ProviderProtocolError) as exc_info:
                fetch(bbox=_TEST_BBOX, max_forecast_hours=0)

        assert exc_info.value.provider_id == "hrrr"
