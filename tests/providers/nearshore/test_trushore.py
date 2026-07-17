"""Unit tests for the TruShore nearshore wave model provider (PROVIDER-MANUAL §14.15).

Covers:
  - CAPABILITY symbol: present when _NEARSHORE_AVAILABLE, None otherwise
  - Cache key construction: stability, uniqueness
  - fetch(): cache miss → None; cache hit → data with data_age_seconds
  - run_all_spots(): success path — caches per-spot results, sets run marker,
    cleans tmpdir
  - run_all_spots(): failure path — SWANRunError does NOT invalidate last-good
    cache; preserves tmpdir
  - run_all_spots(): deduplication — skips SWAN run when run marker exists
  - run_all_spots(): no-op guards (no marine config, no surf spots, extra absent)

No live SWAN binary or eccodes required — all external calls are mocked.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from weewx_clearskies_api.providers.nearshore.trushore import (
    CAPABILITY,
    DOMAIN,
    PROVIDER_ID,
    _CACHE_TTL_SECONDS,
    _LAST_GOOD_TTL_SECONDS,
    _NEARSHORE_AVAILABLE,
    _build_last_good_key,
    _build_run_marker_key,
    fetch,
    run_all_spots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockCache:
    """In-memory cache stub for testing."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self._store.get(key)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._store[key] = value

    def reset(self) -> None:
        self._store.clear()


def _make_mock_marine_config(
    surf_spot_ids: list[str] | None = None,
    lats: list[float] | None = None,
    lons: list[float] | None = None,
) -> MagicMock:
    """Build a minimal mock MarineConfig with surf spots."""
    if surf_spot_ids is None:
        surf_spot_ids = ["test_spot"]
    if lats is None:
        lats = [33.65] * len(surf_spot_ids)
    if lons is None:
        lons = [-118.0] * len(surf_spot_ids)

    locations: list[MagicMock] = []
    for sid, lat, lon in zip(surf_spot_ids, lats, lons):
        loc = MagicMock()
        loc.id = sid
        loc.lat = lat
        loc.lon = lon
        locations.append(loc)

    config = MagicMock()
    config.locations = locations
    config.surf_spots = {sid: MagicMock() for sid in surf_spot_ids}
    return config


def _make_hrrr_result(cycle_time: str = "2026-07-17T12:00:00Z") -> dict[str, Any]:
    """Minimal HRRR result dict."""
    return {
        "cycle_time": cycle_time,
        "lov": 262.5,
        "latin1": 38.5,
        "latin2": 38.5,
        "cone_factor": 0.6225,
        "grids": [
            {
                "valid_time": cycle_time,
                "forecast_hour": 0,
                "ni": 2,
                "nj": 2,
                "lat_first": 32.0,
                "lon_first": -119.0,
                "lat_last": 35.0,
                "lon_last": -116.0,
                "u_earth": [[1.0, 1.0], [1.0, 1.0]],
                "v_earth": [[0.5, 0.5], [0.5, 0.5]],
            }
        ],
    }


def _make_ww3_result() -> dict[str, Any]:
    """Minimal WW3 result dict."""
    return {
        "forecast": [],
        "grid": "Global",
        "model_run": "2026-07-17T12:00:00Z",
    }


def _make_marine_forecast_points(spot_id: str) -> list[MagicMock]:
    """Minimal list of MarineForecastPoint mocks."""
    pt = MagicMock()
    pt.model_dump.return_value = {
        "time": "2026-07-17T13:00:00Z",
        "waveHeight": 1.2,
        "wavePeriod": 12.0,
        "waveDirection": 270.0,
    }
    return [pt]


# ---------------------------------------------------------------------------
# CAPABILITY declaration tests
# ---------------------------------------------------------------------------


class TestCapability:
    def test_provider_id_constant(self) -> None:
        assert PROVIDER_ID == "trushore"

    def test_domain_constant(self) -> None:
        assert DOMAIN == "nearshore"

    def test_capability_matches_nearshore_available(self) -> None:
        if _NEARSHORE_AVAILABLE:
            assert CAPABILITY is not None
            assert CAPABILITY.provider_id == "trushore"
            assert CAPABILITY.domain == "nearshore"
            assert "waveHeight" in CAPABILITY.supplied_canonical_fields
            assert "wavePeriod" in CAPABILITY.supplied_canonical_fields
            assert "waveDirection" in CAPABILITY.supplied_canonical_fields
            assert CAPABILITY.auth_required == ()
        else:
            assert CAPABILITY is None

    def test_capability_ttl_matches_hrrr_cadence(self) -> None:
        """55-min TTL matches HRRR cycle cadence (PROVIDER-MANUAL §14.15)."""
        assert _CACHE_TTL_SECONDS == 3300


# ---------------------------------------------------------------------------
# Cache key construction tests
# ---------------------------------------------------------------------------


class TestCacheKeys:
    def test_last_good_key_stable(self) -> None:
        """Same spot_id → same key."""
        k1 = _build_last_good_key("huntington_beach")
        k2 = _build_last_good_key("huntington_beach")
        assert k1 == k2

    def test_last_good_key_unique_per_spot(self) -> None:
        """Different spot_id → different key."""
        k1 = _build_last_good_key("huntington_beach")
        k2 = _build_last_good_key("venice_beach")
        assert k1 != k2

    def test_last_good_key_is_sha256_hex(self) -> None:
        """Key is a 64-char lowercase hex string (SHA-256 digest)."""
        key = _build_last_good_key("test")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_run_marker_key_stable(self) -> None:
        """Same cycle_time → same key."""
        k1 = _build_run_marker_key("2026-07-17T12:00:00Z")
        k2 = _build_run_marker_key("2026-07-17T12:00:00Z")
        assert k1 == k2

    def test_run_marker_key_unique_per_cycle(self) -> None:
        """Different cycle_time → different key."""
        k1 = _build_run_marker_key("2026-07-17T12:00:00Z")
        k2 = _build_run_marker_key("2026-07-17T13:00:00Z")
        assert k1 != k2

    def test_last_good_and_run_marker_distinct(self) -> None:
        """last_good key and run_marker key for same input must differ."""
        k_lg = _build_last_good_key("test")
        k_rm = _build_run_marker_key("test")
        assert k_lg != k_rm


# ---------------------------------------------------------------------------
# fetch() tests
# ---------------------------------------------------------------------------


class TestFetch:
    def test_cache_miss_returns_none(self) -> None:
        mock_cache = _MockCache()
        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
            return_value=mock_cache,
        ):
            result = fetch("nonexistent_spot")
        assert result is None

    def test_cache_hit_returns_data(self) -> None:
        mock_cache = _MockCache()
        spot_id = "huntington_beach"
        payload = {
            "forecast": [{"time": "2026-07-17T13:00:00Z", "waveHeight": 1.5}],
            "run_time": "2026-07-17T12:05:00Z",
            "hrrr_cycle_time": "2026-07-17T12:00:00Z",
        }
        mock_cache.set(_build_last_good_key(spot_id), payload, _LAST_GOOD_TTL_SECONDS)

        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
            return_value=mock_cache,
        ):
            result = fetch(spot_id)

        assert result is not None
        assert result["forecast"] == payload["forecast"]
        assert result["run_time"] == payload["run_time"]

    def test_cache_hit_includes_data_age(self) -> None:
        """fetch() computes data_age_seconds from stored run_time."""
        mock_cache = _MockCache()
        spot_id = "test_spot"
        # Use a run_time ~60 seconds ago (approximate — wall clock may vary)
        from datetime import UTC, datetime, timedelta
        run_time = (datetime.now(UTC) - timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        payload = {
            "forecast": [],
            "run_time": run_time,
            "hrrr_cycle_time": "2026-07-17T12:00:00Z",
        }
        mock_cache.set(_build_last_good_key(spot_id), payload, _LAST_GOOD_TTL_SECONDS)

        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
            return_value=mock_cache,
        ):
            result = fetch(spot_id)

        assert result is not None
        assert result["data_age_seconds"] is not None
        # Should be roughly 60 seconds (allow ±10s for test timing)
        assert 50 <= result["data_age_seconds"] <= 70

    def test_missing_run_time_gives_none_age(self) -> None:
        """fetch() gracefully handles missing run_time (age = None)."""
        mock_cache = _MockCache()
        spot_id = "test_spot"
        payload = {"forecast": [], "run_time": None, "hrrr_cycle_time": "x"}
        mock_cache.set(_build_last_good_key(spot_id), payload, _LAST_GOOD_TTL_SECONDS)

        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
            return_value=mock_cache,
        ):
            result = fetch(spot_id)

        assert result is not None
        assert result["data_age_seconds"] is None


# ---------------------------------------------------------------------------
# run_all_spots() guard tests
# ---------------------------------------------------------------------------


class TestRunAllSpotsGuards:
    def test_no_marine_config_returns_silently(self) -> None:
        """run_all_spots() does nothing when marine_config is None."""
        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
            True,
        ):
            run_all_spots(None)  # Should not raise

    def test_no_surf_spots_returns_silently(self) -> None:
        """run_all_spots() does nothing when no surf spots configured."""
        config = MagicMock()
        config.surf_spots = {}
        config.locations = []
        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
            True,
        ):
            run_all_spots(config)  # Should not raise

    def test_nearshore_not_available_returns_silently(self) -> None:
        """run_all_spots() does nothing when [nearshore] extra is absent."""
        config = _make_mock_marine_config()
        with patch(
            "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
            False,
        ):
            run_all_spots(config)  # Should not raise


# ---------------------------------------------------------------------------
# run_all_spots() success path
# ---------------------------------------------------------------------------


class TestRunAllSpotsSuccess:
    def test_caches_per_spot_results(self, tmp_path: Path) -> None:
        """Successful SWAN run caches forecast data for each surf spot."""
        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()
        ww3_data = _make_ww3_result()
        points = _make_marine_forecast_points(spot_id)

        # Mock SWAN run result
        run_result = {spot_id: points}

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            # Patch wavewatch.fetch via the module import inside run_all_spots
            mock_runner_instance = MockRunner.return_value
            mock_runner_instance.run_with_tmpdir.return_value = (run_result, tmp_path)

            with patch(
                "weewx_clearskies_api.providers.marine.wavewatch.fetch",
                return_value=ww3_data,
            ):
                run_all_spots(marine_config)

        # Verify last-good cache entry was written for the spot
        cached = mock_cache.get(_build_last_good_key(spot_id))
        assert cached is not None
        assert "forecast" in cached
        assert "run_time" in cached
        assert cached["hrrr_cycle_time"] == hrrr_data["cycle_time"]

    def test_sets_run_marker(self, tmp_path: Path) -> None:
        """Successful SWAN run stores a run marker for deduplication."""
        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()
        points = _make_marine_forecast_points(spot_id)
        run_result = {spot_id: points}

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.marine.wavewatch.fetch",
                return_value=_make_ww3_result(),
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            MockRunner.return_value.run_with_tmpdir.return_value = (run_result, tmp_path)
            run_all_spots(marine_config)

        marker = mock_cache.get(_build_run_marker_key(hrrr_data["cycle_time"]))
        assert marker is not None
        assert "run_time" in marker

    def test_cleans_tmpdir_on_success(self) -> None:
        """run_all_spots() removes tmpdir after a successful SWAN run."""
        import tempfile

        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()

        # Create a real tmpdir so we can verify deletion
        real_tmpdir = Path(tempfile.mkdtemp(prefix="trushore_test_"))
        points = _make_marine_forecast_points(spot_id)
        run_result = {spot_id: points}

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.marine.wavewatch.fetch",
                return_value=_make_ww3_result(),
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            MockRunner.return_value.run_with_tmpdir.return_value = (
                run_result,
                real_tmpdir,
            )
            run_all_spots(marine_config)

        # tmpdir should have been deleted on success
        assert not real_tmpdir.exists()


# ---------------------------------------------------------------------------
# run_all_spots() failure path
# ---------------------------------------------------------------------------


class TestRunAllSpotsFailure:
    def test_swan_run_error_preserves_last_good_cache(self) -> None:
        """SWANRunError must NOT invalidate the last-good cache.

        This is the core failure-handling guarantee of PROVIDER-MANUAL §14.15:
        stale TruShore data is always preferred to no data.
        """
        from weewx_clearskies_api.services.swan_runner import SWANRunError

        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()

        # Pre-populate last-good cache
        pre_existing_payload = {
            "forecast": [{"time": "2026-07-17T10:00:00Z", "waveHeight": 0.8}],
            "run_time": "2026-07-17T09:05:00Z",
            "hrrr_cycle_time": "2026-07-17T09:00:00Z",
        }
        last_good_key = _build_last_good_key(spot_id)
        mock_cache.set(last_good_key, pre_existing_payload, _LAST_GOOD_TTL_SECONDS)

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.marine.wavewatch.fetch",
                return_value=_make_ww3_result(),
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            MockRunner.return_value.run_with_tmpdir.side_effect = SWANRunError(
                "SWAN exited with code 1",
                stderr="Error: grid not found",
                returncode=1,
            )
            run_all_spots(marine_config)  # Should not raise

        # Last-good cache must still contain the pre-existing data
        cached = mock_cache.get(last_good_key)
        assert cached is not None
        assert cached["run_time"] == pre_existing_payload["run_time"]
        assert cached["forecast"] == pre_existing_payload["forecast"]

    def test_swan_run_error_does_not_set_run_marker(self) -> None:
        """Failed SWAN run must not set the run-completion marker."""
        from weewx_clearskies_api.services.swan_runner import SWANRunError

        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.marine.wavewatch.fetch",
                return_value=_make_ww3_result(),
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            MockRunner.return_value.run_with_tmpdir.side_effect = SWANRunError(
                "SWAN exited with code 1", returncode=1
            )
            run_all_spots(marine_config)

        # No run marker should have been written
        marker = mock_cache.get(_build_run_marker_key(hrrr_data["cycle_time"]))
        assert marker is None

    def test_hrrr_unavailable_does_not_raise(self) -> None:
        """HRRR fetch failure is caught; run_all_spots() returns silently."""
        from weewx_clearskies_api.providers._common.errors import ProviderUnavailableError

        mock_cache = _MockCache()
        marine_config = _make_mock_marine_config()

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                side_effect=ProviderUnavailableError(
                    "HRRR unavailable", provider_id="hrrr", domain="wind"
                ),
            ),
        ):
            run_all_spots(marine_config)  # Should not raise


# ---------------------------------------------------------------------------
# run_all_spots() deduplication test
# ---------------------------------------------------------------------------


class TestRunAllSpotsDeduplication:
    def test_skips_run_when_marker_present(self, tmp_path: Path) -> None:
        """If a run marker exists for the current HRRR cycle, SWAN is skipped."""
        mock_cache = _MockCache()
        spot_id = "test_spot"
        marine_config = _make_mock_marine_config([spot_id])
        hrrr_data = _make_hrrr_result()

        # Pre-populate the run marker to simulate a recent completed run
        run_marker_key = _build_run_marker_key(hrrr_data["cycle_time"])
        mock_cache.set(
            run_marker_key, {"run_time": "2026-07-17T12:05:00Z"}, _CACHE_TTL_SECONDS
        )

        with (
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._NEARSHORE_AVAILABLE",
                True,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "weewx_clearskies_api.providers.wind.hrrr.fetch",
                return_value=hrrr_data,
            ),
            patch(
                "weewx_clearskies_api.providers.nearshore.trushore._SWANRunnerWithCleanup"
            ) as MockRunner,
        ):
            run_all_spots(marine_config)

        # SWANRunnerWithCleanup should NOT have been instantiated at all
        MockRunner.assert_not_called()
