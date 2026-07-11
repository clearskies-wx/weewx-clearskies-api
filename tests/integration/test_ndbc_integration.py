"""Live-network integration tests for the NDBC buoy provider (T8.1).

Exercises ``weewx_clearskies_api/providers/buoy/ndbc.py`` against the real
NDBC flat-file host (``www.ndbc.noaa.gov``) — no respx/HTTP mocking. Covers:

  1. Standard met observation fetch for station 41025 (Diamond Shoals, NC —
     an active offshore buoy with wave sensors) — non-null wave height,
     period, and direction.
  2. Spectral decomposition — at least one SpectralWaveComponent returned
     from the same station's ``.data_spec`` / ``.swdir`` files.
  3. Station discovery (``activestations.xml``) near Wrightsville Beach, NC
     (34.21, -77.79).

Per API-MANUAL.md §16 MarineObservation field table and the
providers/buoy/ndbc.py module docstring (wire format, spectral
decomposition, station discovery capability guess).

NDBC is a live NOAA flat-file service with no documented uptime SLA and no
API key. Buoys occasionally go offline for maintenance — the wave-sensor
assertions are wrapped in ``xfail(strict=False)`` so a transient NDBC/buoy
outage doesn't block the suite; the discovery test (activestations.xml is a
static reference file, effectively always available) is not.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.models.responses import MarineObservation, SpectralWaveComponent
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.buoy import ndbc

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

# Diamond Shoals, NC — active NDBC offshore buoy with wave sensors.
_STATION_ID = "41025"

# Wrightsville Beach, NC — used for station-discovery radius search.
_WRIGHTSVILLE_LAT = 34.21
_WRIGHTSVILLE_LON = -77.79
_DISCOVERY_RADIUS_KM = 200.0


@pytest.fixture(autouse=True)
def _reset_ndbc_state():
    """Cold cache + HTTP client for every test so each fetch hits the network."""
    reset_cache_for_tests()
    ndbc._reset_http_client_for_tests()
    yield
    reset_cache_for_tests()
    ndbc._reset_http_client_for_tests()


class TestNdbcStandardMetObservation:
    """GET .../data/realtime2/41025.txt -> canonical MarineObservation."""

    @pytest.mark.xfail(
        reason="NDBC station 41025 may be temporarily offline for maintenance "
        "or report a data gap for one or more fields; NOAA gives no uptime SLA",
        strict=False,
    )
    def test_fetch_returns_marine_observation_for_diamond_shoals(self) -> None:
        result = ndbc.fetch(station_id=_STATION_ID, include_spectral=False)
        observation = result["observation"]

        assert observation is not None, "Expected a non-null MarineObservation"
        assert isinstance(observation, MarineObservation)
        assert observation.stationId == _STATION_ID
        assert observation.time.endswith("Z")

    @pytest.mark.xfail(
        reason="NDBC station 41025 may be temporarily offline or missing wave "
        "sensor fields for the current observation window",
        strict=False,
    )
    def test_wave_fields_are_non_null(self) -> None:
        result = ndbc.fetch(station_id=_STATION_ID, include_spectral=False)
        observation = result["observation"]
        assert observation is not None

        assert observation.waveHeight is not None, "waveHeight (WVHT) should be reported"
        assert observation.dominantPeriod is not None, "dominantPeriod (DPD) should be reported"
        assert observation.meanWaveDirection is not None, (
            "meanWaveDirection (MWD) should be reported"
        )

        # Sanity bounds (physical wave heights at this buoy are not near-zero
        # nor absurdly large — this catches a parsing regression, not just a
        # null-vs-non-null check).
        assert 0.0 <= observation.waveHeight <= 20.0
        assert 0.0 <= observation.dominantPeriod <= 30.0
        assert 0.0 <= observation.meanWaveDirection <= 360.0


class TestNdbcSpectralDecomposition:
    """GET .../data/realtime2/41025.data_spec + .swdir -> SpectralWaveComponent list."""

    @pytest.mark.xfail(
        reason="NDBC station 41025 spectral files (.data_spec/.swdir) may be "
        "temporarily unavailable independent of the standard met file",
        strict=False,
    )
    def test_fetch_returns_at_least_one_spectral_component(self) -> None:
        result = ndbc.fetch(station_id=_STATION_ID, include_spectral=True)
        spectral = result["spectral"]

        assert len(spectral) >= 1, (
            "Expected at least one decomposed swell system from live spectral data"
        )
        for component in spectral:
            assert isinstance(component, SpectralWaveComponent)
            assert component.height > 0.0
            assert component.period > 0.0
            assert component.classification in ("groundswell", "swell", "wind_swell")

    @pytest.mark.xfail(
        reason="NDBC station 41025 spectral files may be temporarily unavailable",
        strict=False,
    )
    def test_spectral_components_are_merged_onto_observation(self) -> None:
        """fetch() attaches decomposed spectral components to the observation
        when both the standard-met and spectral fetches succeed."""
        result = ndbc.fetch(station_id=_STATION_ID, include_spectral=True)
        observation = result["observation"]
        spectral = result["spectral"]

        if observation is not None and spectral:
            assert observation.spectralComponents is not None
            assert len(observation.spectralComponents) == len(spectral)


class TestNdbcStationDiscovery:
    """GET .../activestations.xml -> stations near Wrightsville Beach, NC."""

    def test_discover_stations_near_wrightsville_beach_returns_results(self) -> None:
        stations = ndbc.discover_stations(
            lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON, radius_km=_DISCOVERY_RADIUS_KM
        )

        assert len(stations) > 0, (
            f"Expected at least one NDBC station within {_DISCOVERY_RADIUS_KM}km "
            "of Wrightsville Beach, NC"
        )
        for station in stations:
            assert "stationId" in station
            assert "distanceKm" in station
            assert station["distanceKm"] <= _DISCOVERY_RADIUS_KM

    def test_discovered_stations_are_sorted_nearest_first(self) -> None:
        stations = ndbc.discover_stations(
            lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON, radius_km=_DISCOVERY_RADIUS_KM
        )
        distances = [s["distanceKm"] for s in stations]
        assert distances == sorted(distances)
