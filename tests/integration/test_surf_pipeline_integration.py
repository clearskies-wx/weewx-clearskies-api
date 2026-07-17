"""Integration tests for the surf enrichment pipeline (T8.1).

Exercises the full transform-then-score chain documented in API-MANUAL.md
§17 ("SWAN+TruShore supplement processor" -> "Surf quality scorer") for a Wrightsville
Beach, NC surf spot:

  1. Realistic SWAN+TruShore-shaped wave data (height/period/direction) through
     ``enrichment/wave_transform.py``'s ``apply_supplements()`` (breaker
     index correction, sub-grid interpolation, topographic adjustment —
     no coastal structures configured for this spot) and then through
     ``enrichment/surf_scorer.py``'s ``score_surf()`` — verifies a 1-5 star
     rating and non-empty conditions text.
  2. A live-network variant: real WaveWatch III wave data fetched for
     Wrightsville Beach is fed directly into ``score_surf()`` (WaveWatch
     data bypasses ``wave_transform`` — PROVIDER-MANUAL §14.6: "no
     transformation pipeline applied to WaveWatch data").

Per API-MANUAL.md §17 and config/marine_config.py's SurfSpotConfig schema.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.config.marine_config import SurfSpotConfig
from weewx_clearskies_api.enrichment import wave_transform
from weewx_clearskies_api.enrichment.surf_scorer import score_surf
from weewx_clearskies_api.models.responses import SurfForecast
from weewx_clearskies_api.providers._common.cache import reset_cache_for_tests
from weewx_clearskies_api.providers.marine import wavewatch

pytestmark = pytest.mark.integration

# score_surf()'s qualityLabel/windQuality resolve through i18n.t(), whose
# locale keys for surf ("surf.quality.<1-5>", "surf.wind_quality.<value>")
# are documented as authoritative-but-not-yet-wired in locales/*.json
# (surf_scorer.py module docstring). Until they're wired, i18n.t() falls
# back to echoing the key itself (documented, safe v1 behavior) rather than
# raising -- so this suite accepts either the eventual translated label or
# today's raw-key echo, and will keep passing once the locale files catch up.
_QUALITY_LABELS = {"Poor", "Fair", "Good", "Very Good", "Epic"}
_QUALITY_LABELS |= {f"surf.quality.{n}" for n in range(1, 6)}

_WRIGHTSVILLE_LAT = 34.2085
_WRIGHTSVILLE_LON = -77.7964

# Beach faces roughly SE at Wrightsville Beach; open to E/SE/S/SW swells,
# blocked by the barrier-island shape to the N/NE/W/NW.
_WRIGHTSVILLE_SURF_SECTION = {
    "beach_facing_degrees": "135",
    "bottom_type": "sand",
    "topographic_feature": "straight_beach",
    "directional_exposure": {
        "N": "false",
        "NE": "false",
        "E": "true",
        "SE": "true",
        "S": "true",
        "SW": "true",
        "W": "false",
        "NW": "false",
    },
}


def _wrightsville_surf_config() -> SurfSpotConfig:
    config = SurfSpotConfig(_WRIGHTSVILLE_SURF_SECTION)
    config.validate("wrightsville_beach")
    return config


class TestSurfPipelineWithRealisticWaveData:
    """Realistic SWAN+TruShore-shaped input -> wave_transform supplements -> score_surf."""

    def test_moderate_groundswell_produces_a_1_to_5_star_rating(self) -> None:
        spot_config = _wrightsville_surf_config()

        # Realistic mid-period groundswell from the SE (onto the beach face) --
        # SWAN+TruShore-shaped dict as consumed by wave_transform.apply_supplements().
        wave_data = {
            "wave_height": 1.2,  # meters (~4 ft)
            "wave_period": 11.0,  # seconds
            "wave_direction": 140.0,  # degrees true, close to beach-facing (135)
        }
        supplemented = wave_transform.apply_supplements(
            wave_data, spot_config, _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON
        )
        assert supplemented is not None
        assert supplemented["wave_height"] > 0.0

        forecast = score_surf(
            wave_height=supplemented["wave_height"],
            wave_period=supplemented["wave_period"],
            wave_direction=supplemented["wave_direction"],
            wind_speed=3.0,  # m/s, light -- offshore-ish
            wind_direction=315.0,  # NW, roughly offshore for a SE-facing beach
            spectral_components=None,
            spot_config=spot_config,
            time_utc="2026-07-11T15:00:00Z",
        )

        assert isinstance(forecast, SurfForecast)
        assert 1 <= forecast.qualityStars <= 5
        assert forecast.qualityLabel in _QUALITY_LABELS

    def test_conditions_text_is_non_empty_and_describes_the_swell(self) -> None:
        spot_config = _wrightsville_surf_config()
        wave_data = {"wave_height": 1.2, "wave_period": 11.0, "wave_direction": 140.0}
        supplemented = wave_transform.apply_supplements(
            wave_data, spot_config, _WRIGHTSVILLE_LAT, _WRIGHTSVILLE_LON
        )
        assert supplemented is not None

        forecast = score_surf(
            wave_height=supplemented["wave_height"],
            wave_period=supplemented["wave_period"],
            wave_direction=supplemented["wave_direction"],
            wind_speed=3.0,
            wind_direction=315.0,
            spectral_components=None,
            spot_config=spot_config,
        )

        assert forecast.conditionsText
        assert "ft" in forecast.conditionsText
        assert "seconds" in forecast.conditionsText

    def test_blocked_swell_direction_scores_lower_than_open_direction(self) -> None:
        """A swell from a direction the beach's directional_exposure blocks (N)
        should score at or below the same swell from an open direction (SE),
        per the directional-exposure filter in score_surf()."""
        spot_config = _wrightsville_surf_config()

        open_data = wave_transform.apply_supplements(
            {"wave_height": 1.2, "wave_period": 11.0, "wave_direction": 135.0},
            spot_config,
            _WRIGHTSVILLE_LAT,
            _WRIGHTSVILLE_LON,
        )
        blocked_data = wave_transform.apply_supplements(
            {"wave_height": 1.2, "wave_period": 11.0, "wave_direction": 0.0},
            spot_config,
            _WRIGHTSVILLE_LAT,
            _WRIGHTSVILLE_LON,
        )
        assert open_data is not None
        assert blocked_data is not None

        open_forecast = score_surf(
            wave_height=open_data["wave_height"],
            wave_period=open_data["wave_period"],
            wave_direction=open_data["wave_direction"],
            wind_speed=None,
            wind_direction=None,
            spectral_components=None,
            spot_config=spot_config,
        )
        blocked_forecast = score_surf(
            wave_height=blocked_data["wave_height"],
            wave_period=blocked_data["wave_period"],
            wave_direction=blocked_data["wave_direction"],
            wind_speed=None,
            wind_direction=None,
            spectral_components=None,
            spot_config=spot_config,
        )

        assert blocked_forecast.qualityStars <= open_forecast.qualityStars


@pytest.mark.live_network
class TestSurfPipelineWithLiveWaveWatchData:
    """Real WaveWatch III forecast data (live NOAA ERDDAP) fed through score_surf().

    WaveWatch III data bypasses wave_transform (PROVIDER-MANUAL §14.6 — the
    supplement pipeline only applies to SWAN+TruShore nearshore data), so this test
    calls score_surf() directly with the fetched forecast point.
    """

    @pytest.fixture(autouse=True)
    def _reset_wavewatch_cache(self):
        reset_cache_for_tests()
        yield
        reset_cache_for_tests()

    @pytest.mark.xfail(
        reason="ERDDAP griddap may be temporarily unavailable, or the most "
        "recent model-run cycles may all be unpublished at request time",
        strict=False,
    )
    def test_live_wavewatch_forecast_produces_a_valid_surf_score(self) -> None:
        spot_config = _wrightsville_surf_config()

        ww_result = wavewatch.fetch(lat=_WRIGHTSVILLE_LAT, lon=_WRIGHTSVILLE_LON)
        forecast_points = ww_result["forecast"]
        assert len(forecast_points) > 0, "Expected a live WaveWatch III forecast"

        first = forecast_points[0]
        assert first.waveHeight is not None

        forecast = score_surf(
            wave_height=first.waveHeight,
            wave_period=first.wavePeriod or 0.0,
            wave_direction=first.waveDirection or 0.0,
            wind_speed=first.windSpeed,
            wind_direction=first.windDirection,
            spectral_components=None,
            spot_config=spot_config,
            time_utc=first.time,
        )

        assert isinstance(forecast, SurfForecast)
        assert 1 <= forecast.qualityStars <= 5
        assert forecast.conditionsText
