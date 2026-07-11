"""Live-network integration tests for the WaveWatch III (ERDDAP) provider (T8.1).

Exercises ``weewx_clearskies_api/providers/marine/wavewatch.py`` against the
real PacIOOS ERDDAP host (``pae-paha.pacioos.hawaii.edu``) — no respx/HTTP
mocking. Covers:

  1. Cape Hatteras, NC (35.2, -75.5) — verifies 25 timesteps are returned
     (72h forecast at 3h intervals) and that the single global grid is used.
  2. Hawaii (21.3, -157.8) — verifies the same global grid handles Pacific
     coordinates correctly.

Per API-MANUAL.md §16 MarineForecastPoint field table and PROVIDER-MANUAL
§14.3's single-global-grid design.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.models.responses import MarineForecastPoint
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.marine import wavewatch

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

_HATTERAS_LAT = 35.2
_HATTERAS_LON = -75.5

_HAWAII_LAT = 21.3
_HAWAII_LON = -157.8

_EXPECTED_TIMESTEPS = 25  # 72h at 3h intervals


@pytest.fixture(autouse=True)
def _reset_wavewatch_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


class TestWavewatchGridSelection:
    """Grid-selection routing (module-internal `_select_grid`, no network call)."""

    def test_cape_hatteras_routes_to_global_grid(self) -> None:
        grid = wavewatch._select_grid(_HATTERAS_LAT, _HATTERAS_LON)
        assert grid is not None
        assert grid["dataset"] == "ww3_global"

    def test_hawaii_routes_to_global_grid(self) -> None:
        grid = wavewatch._select_grid(_HAWAII_LAT, _HAWAII_LON)
        assert grid is not None
        assert grid["dataset"] == "ww3_global"


class TestWavewatchCapeHatterasForecast:
    """Fetch from PacIOOS ERDDAP -> 25-timestep forecast for Cape Hatteras."""

    @pytest.mark.xfail(
        reason="ERDDAP may be temporarily unavailable, or the most "
        "recent 3-4 model-run cycles may all be unpublished at request time",
        strict=False,
    )
    def test_fetch_returns_25_timesteps_for_cape_hatteras(self) -> None:
        result = wavewatch.fetch(lat=_HATTERAS_LAT, lon=_HATTERAS_LON)
        forecast = result["forecast"]

        assert len(forecast) == _EXPECTED_TIMESTEPS, (
            f"Expected {_EXPECTED_TIMESTEPS} timesteps (72h @ 3h) for Cape Hatteras; "
            f"got {len(forecast)}"
        )
        for point in forecast:
            assert isinstance(point, MarineForecastPoint)
            assert point.time.endswith("Z")

    @pytest.mark.xfail(
        reason="ERDDAP may be temporarily unavailable",
        strict=False,
    )
    def test_fetch_reports_global_grid_name(self) -> None:
        result = wavewatch.fetch(lat=_HATTERAS_LAT, lon=_HATTERAS_LON)
        assert result["grid"] == "Global"
        assert result["model_run"].endswith("Z")


class TestWavewatchHawaiiForecast:
    """Fetch from PacIOOS ERDDAP -> forecast for a Hawaii point."""

    @pytest.mark.xfail(
        reason="ERDDAP may be temporarily unavailable, or the most "
        "recent model-run cycles may all be unpublished at request time",
        strict=False,
    )
    def test_fetch_reports_global_grid_name_for_hawaii(self) -> None:
        result = wavewatch.fetch(lat=_HAWAII_LAT, lon=_HAWAII_LON)
        assert result["grid"] == "Global"
        assert len(result["forecast"]) > 0
