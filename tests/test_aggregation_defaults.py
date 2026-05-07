"""Unit tests for the aggregation service's per-canonical-field aggregator defaults.

Asserts that services/archive.py exposes a DAY_AGGREGATOR constant with the
expected defaults per the brief:
  outTemp  → aggregator for daily temperature (mean/meanmax)
  rain     → sum
  windGust → max

Plus checks the full set of expected canonical fields are covered.

ADR references: brief §2 interval=day spec, brief §test-author-parallel-scope.
"""

from __future__ import annotations

import pytest


class TestDayAggregatorConstant:
    """DAY_AGGREGATOR is published in services/archive.py and contains expected defaults."""

    def test_day_aggregator_is_importable(self) -> None:
        """DAY_AGGREGATOR can be imported from services/archive."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert DAY_AGGREGATOR is not None
        assert isinstance(DAY_AGGREGATOR, dict)

    def test_rain_aggregator_is_sum_variant(self) -> None:
        """rain → sum-type aggregator (accumulated rainfall over the day)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "rain" in DAY_AGGREGATOR, (
            "DAY_AGGREGATOR must contain an entry for 'rain'"
        )
        # rain aggregator must be a sum-type (not mean or max)
        assert "sum" in DAY_AGGREGATOR["rain"].lower(), (
            f"rain aggregator must be sum-type, got {DAY_AGGREGATOR['rain']!r}"
        )

    def test_wind_gust_aggregator_is_max(self) -> None:
        """windGust → max (peak gust for the day is the meaningful metric)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "windGust" in DAY_AGGREGATOR
        assert DAY_AGGREGATOR["windGust"] == "max", (
            f"windGust aggregator must be 'max', got {DAY_AGGREGATOR['windGust']!r}"
        )

    def test_out_temp_aggregator_is_mean_type(self) -> None:
        """outTemp → mean-type aggregator (the day's average temperature)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "outTemp" in DAY_AGGREGATOR
        # meanmax is the actual value (archive_day col avg used)
        # Accept "mean", "avg", "meanmax" — all indicate central tendency
        aggregator = DAY_AGGREGATOR["outTemp"].lower()
        assert any(t in aggregator for t in ("mean", "avg")), (
            f"outTemp aggregator must be mean-type, got {DAY_AGGREGATOR['outTemp']!r}"
        )

    def test_rain_rate_aggregator_is_max(self) -> None:
        """rainRate → max (peak rate within the day)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "rainRate" in DAY_AGGREGATOR
        assert DAY_AGGREGATOR["rainRate"] == "max", (
            f"rainRate aggregator must be 'max', got {DAY_AGGREGATOR['rainRate']!r}"
        )

    def test_out_humidity_aggregator_is_mean_type(self) -> None:
        """outHumidity → mean-type aggregator (average humidity)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "outHumidity" in DAY_AGGREGATOR
        aggregator = DAY_AGGREGATOR["outHumidity"].lower()
        assert any(t in aggregator for t in ("mean", "avg")), (
            f"outHumidity aggregator must be mean-type, got {DAY_AGGREGATOR['outHumidity']!r}"
        )

    def test_barometer_aggregator_is_mean_type(self) -> None:
        """barometer → mean-type aggregator (average pressure)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "barometer" in DAY_AGGREGATOR
        aggregator = DAY_AGGREGATOR["barometer"].lower()
        assert any(t in aggregator for t in ("mean", "avg")), (
            f"barometer aggregator must be mean-type, got {DAY_AGGREGATOR['barometer']!r}"
        )

    def test_wind_speed_aggregator_is_max(self) -> None:
        """windSpeed → max (meaningful daily wind stat)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "windSpeed" in DAY_AGGREGATOR
        assert DAY_AGGREGATOR["windSpeed"] == "max"

    def test_radiation_aggregator_is_present(self) -> None:
        """radiation is in DAY_AGGREGATOR (max or mean — either acceptable)."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "radiation" in DAY_AGGREGATOR

    def test_uv_aggregator_is_present(self) -> None:
        """UV is in DAY_AGGREGATOR."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        assert "UV" in DAY_AGGREGATOR

    def test_all_core_observation_fields_have_aggregator(self) -> None:
        """Every core Observation field from canonical-data-model §3.1 has an entry."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        # Core fields from §3.1 that have numeric values and are aggregation-relevant
        required_fields = {
            "outTemp",
            "outHumidity",
            "windSpeed",
            "windGust",
            "barometer",
            "rain",
            "rainRate",
            "radiation",
            "UV",
            "inTemp",
            "inHumidity",
        }
        missing = required_fields - set(DAY_AGGREGATOR.keys())
        assert not missing, (
            f"DAY_AGGREGATOR is missing entries for: {sorted(missing)}. "
            "All core observation fields must have a default aggregator."
        )

    def test_all_aggregator_values_are_recognized_strings(self) -> None:
        """Every value in DAY_AGGREGATOR is a recognized aggregator string."""
        from weewx_clearskies_api.services.archive import DAY_AGGREGATOR

        valid_prefixes = {"mean", "avg", "max", "min", "sum"}
        for field, aggregator in DAY_AGGREGATOR.items():
            assert any(aggregator.lower().startswith(prefix) for prefix in valid_prefixes), (
                f"DAY_AGGREGATOR[{field!r}] = {aggregator!r} is not a recognized "
                f"aggregator. Valid prefixes: {valid_prefixes}"
            )
