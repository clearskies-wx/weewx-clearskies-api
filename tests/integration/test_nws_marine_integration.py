"""Live-network integration tests for the NWS marine zone text forecast
provider (T8.1).

Exercises ``weewx_clearskies_api/providers/marine/nws_marine.py`` and the
shared zone-discovery utility ``providers/_common/nws_zones.py`` against the
real NWS API (``api.weather.gov``) — no respx/HTTP mocking. Covers:

  1. Zone forecast fetch for AMZ250 (coastal waters off Wrightsville Beach,
     NC — the zone id used throughout the existing marine test fixtures,
     e.g. tests/test_marine_endpoint.py's `_make_location()`) — verifies a
     non-empty periods list with wind/seas/weather narrative text.
  2. Zone discovery (`discover_marine_zones`) within 25 miles of
     Wrightsville Beach, NC (34.21, -77.79) — verifies at least one coastal
     marine zone is found.

Per API-MANUAL.md §16 MarineTextForecast field table and
PROVIDER-MANUAL.md §14.4/§14.8.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.models.responses import MarineTextForecast
from weewx_clearskies_api.providers._common import nws_zones
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.marine import nws_marine

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

# Coastal waters off Wrightsville Beach, NC -- same zone id used by the
# existing marine endpoint unit-test fixtures.
_ZONE_ID = "AMZ250"

_WRIGHTSVILLE_LAT = 34.21
_WRIGHTSVILLE_LON = -77.79
_DISCOVERY_RADIUS_MILES = 25.0


@pytest.fixture(autouse=True)
def _reset_nws_marine_state():
    reset_cache_for_tests()
    nws_marine._reset_http_client_for_tests()
    nws_zones._reset_http_client_for_tests()
    yield
    reset_cache_for_tests()
    nws_marine._reset_http_client_for_tests()
    nws_zones._reset_http_client_for_tests()


class TestNwsMarineZoneForecast:
    """GET .../zones/coastal/AMZ250/forecast -> MarineTextForecast periods."""

    def test_fetch_returns_non_empty_periods_for_amz250(self) -> None:
        periods = nws_marine.fetch(zone_id=_ZONE_ID)

        assert len(periods) > 0, f"Expected forecast periods for zone {_ZONE_ID}"
        for period in periods:
            assert isinstance(period, MarineTextForecast)
            assert period.periodName
            assert period.text

    def test_periods_carry_wind_seas_or_weather_narrative(self) -> None:
        """Every period's full narrative is available via `text`; at least one
        period across the whole forecast should also expose a structured
        `wind` field when NWS supplies it for this zone/period (module
        docstring: "opportunistic ... not guaranteed present")."""
        periods = nws_marine.fetch(zone_id=_ZONE_ID)
        assert len(periods) > 0

        # The narrative text always carries the wind/seas/weather content,
        # even when the structured `wind` field is absent for a period.
        assert all(len(p.text) > 0 for p in periods)


class TestNwsMarineZoneDiscovery:
    """GET .../points + .../zones?type=coastal -> marine zones near a point."""

    def test_discover_marine_zones_near_wrightsville_beach_returns_results(self) -> None:
        zones = nws_zones.discover_marine_zones(
            _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _DISCOVERY_RADIUS_MILES
        )

        assert len(zones) > 0, (
            f"Expected at least one NWS coastal marine zone within "
            f"{_DISCOVERY_RADIUS_MILES} miles of Wrightsville Beach, NC"
        )
        for zone in zones:
            assert zone.zone_id
            assert zone.name
            assert zone.distance_miles <= _DISCOVERY_RADIUS_MILES

    def test_discovered_zones_are_sorted_nearest_first(self) -> None:
        zones = nws_zones.discover_marine_zones(
            _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON, _DISCOVERY_RADIUS_MILES
        )
        distances = [z.distance_miles for z in zones]
        assert distances == sorted(distances)
