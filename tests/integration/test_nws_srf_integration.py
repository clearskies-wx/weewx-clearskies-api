"""Live-network integration tests for the NWS Surf Zone Forecast (SRF)
provider (T8.1).

Exercises ``weewx_clearskies_api/providers/marine/nws_srf.py`` against the
real NWS API (``api.weather.gov``) — no respx/HTTP mocking. Covers:

  1. SRF fetch for the WFO covering Wrightsville Beach, NC (ILM — Wilmington,
     NC), resolved from coordinates via the module's `get_cwa()` +
     `/points`/`/zones/forecast` lookup chain — verifies `wfo == "ILM"`.
  2. Rip current risk is present and normalized to low/moderate/high.
  3. UV index is present.

Per API-MANUAL.md §16 SurfZoneForecast field table and PROVIDER-MANUAL.md
§14.5 (free-text SRF parsing, WFO/county-zone resolution).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.models.responses import SurfZoneForecast
from weewx_clearskies_api.providers._common import nws_zones
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.marine import nws_srf

pytestmark = [pytest.mark.integration, pytest.mark.live_network]

# Wrightsville Beach, NC -- covered by WFO ILM (Wilmington, NC).
_WRIGHTSVILLE_LAT = 34.2085
_WRIGHTSVILLE_LON = -77.7964
_EXPECTED_WFO = "ILM"


@pytest.fixture(autouse=True)
def _reset_nws_srf_state():
    reset_cache_for_tests()
    nws_srf._reset_http_client_for_tests()
    nws_zones._reset_http_client_for_tests()
    yield
    reset_cache_for_tests()
    nws_srf._reset_http_client_for_tests()
    nws_zones._reset_http_client_for_tests()


class TestNwsSrfWfoResolution:
    """(lat, lon) -> WFO via the shared get_cwa() zone-discovery utility."""

    def test_fetch_resolves_wfo_ilm_for_wrightsville_beach(self) -> None:
        result = nws_srf.fetch(lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON)
        assert result["wfo"] == _EXPECTED_WFO


class TestNwsSrfForecastContent:
    """GET .../products/types/SRF/locations/ILM -> parsed SurfZoneForecast."""

    @pytest.mark.xfail(
        reason="ILM's SRF product may not yet cover the resolved zone on a "
        "given day, or the product may be mid-reissue at request time",
        strict=False,
    )
    def test_fetch_returns_non_empty_forecast(self) -> None:
        result = nws_srf.fetch(lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON)
        forecasts = result["forecasts"]

        assert len(forecasts) > 0, "Expected at least one day of SRF forecast for ILM"
        for forecast in forecasts:
            assert isinstance(forecast, SurfZoneForecast)

    @pytest.mark.xfail(
        reason="ILM's SRF product may not yet cover the resolved zone on a "
        "given day, or the product may be mid-reissue at request time",
        strict=False,
    )
    def test_rip_current_risk_is_present_and_normalized(self) -> None:
        result = nws_srf.fetch(lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON)
        forecasts = result["forecasts"]
        assert len(forecasts) > 0

        for forecast in forecasts:
            assert forecast.ripCurrentRisk in ("low", "moderate", "high"), (
                f"ripCurrentRisk not normalized: {forecast.ripCurrentRisk!r}"
            )

    @pytest.mark.xfail(
        reason="ILM's SRF product may not yet cover the resolved zone on a "
        "given day, or UV INDEX may be absent from a given day's block",
        strict=False,
    )
    def test_uv_index_is_present(self) -> None:
        result = nws_srf.fetch(lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON)
        forecasts = result["forecasts"]
        assert len(forecasts) > 0

        uv_values = [f.uvIndex for f in forecasts if f.uvIndex is not None]
        assert uv_values, "Expected at least one day with a non-null uvIndex"
        for uv in uv_values:
            assert 0 <= uv <= 16  # NWS UV index practical upper bound
