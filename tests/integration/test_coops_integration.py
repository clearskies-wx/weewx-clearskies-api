"""Live-network integration tests for the NOAA CO-OPS tides provider (T8.1).

Exercises ``weewx_clearskies_api/providers/tides/coops.py`` against the real
CO-OPS Data API (``api.tidesandcurrents.noaa.gov``) — no respx/HTTP mocking.
Covers:

  1. Tide predictions for station 8658163 (Wrightsville Beach, NC) — verifies
     at least one high and one low marker are classified.
  2. Water level observations for the same station — verifies quality flags
     are present on returned readings (when the gauge has recent data).
  3. Station discovery (CO-OPS Metadata API) near Wrightsville Beach, NC
     (34.21, -77.79) — verifies station 8658163 itself is discoverable.

Per API-MANUAL.md §16 TidePrediction / WaterLevel field tables and the
providers/tides/coops.py module docstring ("200 OK for errors" quirk,
GMT-naive timestamp handling).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.models.responses import TidePrediction, WaterLevel
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.tides import coops

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

# Wrightsville Beach, NC CO-OPS tide station.
_STATION_ID = "8658163"

_WRIGHTSVILLE_LAT = 34.2085
_WRIGHTSVILLE_LON = -77.7964
_DISCOVERY_RADIUS_KM = 50.0


@pytest.fixture(autouse=True)
def _reset_coops_state():
    reset_cache_for_tests()
    coops._reset_http_client_for_tests()
    yield
    reset_cache_for_tests()
    coops._reset_http_client_for_tests()


class TestCoopsTidePredictions:
    """GET .../datagetter?product=predictions -> classified TidePrediction list."""

    def test_fetch_returns_tide_predictions_for_wrightsville_beach(self) -> None:
        result = coops.fetch(station_id=_STATION_ID, products=("predictions",))
        predictions = result["predictions"]

        assert len(predictions) > 0, "Expected harmonic tide predictions for a 72h window"
        for prediction in predictions:
            assert isinstance(prediction, TidePrediction)
            assert prediction.time.endswith("Z")

    def test_predictions_include_high_and_low_markers(self) -> None:
        """A 72h prediction window at a semi-diurnal station covers 4+ tide cycles —
        both 'high' and 'low' markers must appear."""
        result = coops.fetch(station_id=_STATION_ID, products=("predictions",))
        predictions = result["predictions"]

        types = {p.type for p in predictions if p.type is not None}
        assert "high" in types, f"Expected at least one 'high' tide marker; got types={types}"
        assert "low" in types, f"Expected at least one 'low' tide marker; got types={types}"


class TestCoopsWaterLevelObservations:
    """GET .../datagetter?product=water_level -> observed WaterLevel list with quality flags."""

    @pytest.mark.xfail(
        reason="CO-OPS water_level gauge at this station may have a temporary "
        "data gap; NOAA gives no uptime SLA on real-time gauge feeds",
        strict=False,
    )
    def test_fetch_returns_water_levels_with_datum(self) -> None:
        result = coops.fetch(station_id=_STATION_ID, products=("water_level",))
        water_levels = result["water_levels"]

        assert len(water_levels) > 0, "Expected recent observed water level readings"
        for reading in water_levels:
            assert isinstance(reading, WaterLevel)
            assert reading.datum == "MLLW"
            assert reading.time.endswith("Z")

    @pytest.mark.xfail(
        reason="CO-OPS quality flags ('v' verified / 'p' preliminary) depend on "
        "how recently NOAA has QC'd the reading; may be temporarily absent",
        strict=False,
    )
    def test_water_levels_carry_quality_flags(self) -> None:
        result = coops.fetch(station_id=_STATION_ID, products=("water_level",))
        water_levels = result["water_levels"]
        assert len(water_levels) > 0

        quality_flags = {reading.quality for reading in water_levels if reading.quality}
        assert quality_flags, (
            f"Expected at least one non-null quality flag among {len(water_levels)} readings"
        )
        assert quality_flags <= {"v", "p"}, f"Unexpected quality flag values: {quality_flags}"


class TestCoopsStationDiscovery:
    """GET .../mdapi/prod/webapi/stations.json -> stations near Wrightsville Beach, NC."""

    def test_discover_stations_includes_wrightsville_beach(self) -> None:
        stations = coops.discover_stations(
            lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON, radius_km=_DISCOVERY_RADIUS_KM
        )

        station_ids = {s["id"] for s in stations}
        assert _STATION_ID in station_ids, (
            f"Expected station {_STATION_ID} within {_DISCOVERY_RADIUS_KM}km of "
            f"Wrightsville Beach; got {sorted(station_ids)}"
        )

    def test_discovered_stations_are_sorted_nearest_first(self) -> None:
        stations = coops.discover_stations(
            lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON, radius_km=_DISCOVERY_RADIUS_KM
        )
        distances = [s["distance_km"] for s in stations]
        assert distances == sorted(distances)
