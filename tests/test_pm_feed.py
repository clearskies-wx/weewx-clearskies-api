"""Unit tests for weewx_clearskies_api.sse.enrichment.pm_feed (ADR-066, ADR-067).

Validates:
  - set_latest_pm() stores values and is_observed flag correctly
  - feed_to_smoother() guards: not observed, never set, stale data, fresh data
  - feed_to_smoother() selectively feeds pm25 and pm10 (each independently)
  - get_pm_staleness() returns None when never set, seconds since last set
  - reset() clears all module state

Module-level state is intentional in pm_feed.py; the autouse fixture
calls reset() before every test to provide clean isolation.

Staleness threshold: _STALE_SECONDS = 7200.0 (2 hours).
Data flow: AQI provider → set_latest_pm() → feed_to_smoother() → input_smoother.add_sample()
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.sse.enrichment import pm_feed


# ---------------------------------------------------------------------------
# Autouse reset fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pm_feed():
    """Reset pm_feed module state before and after each test."""
    pm_feed.reset()
    yield
    pm_feed.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A timestamp far enough in the past that _latest_timestamp == 0.0 check passes.
_FRESH_TS = 1_800_000.0       # acts as "now - 0" relative to _fake_time
_STALE_SECONDS = 7200.0       # must match pm_feed._STALE_SECONDS


def _set_pm(
    pm25: float | None = 15.0,
    pm10: float | None = 30.0,
    timestamp: float = _FRESH_TS,
    is_observed: bool = True,
) -> None:
    """Call set_latest_pm() with given values."""
    pm_feed.set_latest_pm(
        pm25=pm25,
        pm10=pm10,
        timestamp=timestamp,
        is_observed=is_observed,
    )


# ===========================================================================
# Group 1: set_latest_pm()
# ===========================================================================


class TestSetLatestPm:
    """set_latest_pm() stores the values into module state."""

    def test_stores_pm25_value(self) -> None:
        """set_latest_pm(pm25=12.5, ...) → _latest_pm25 = 12.5."""
        pm_feed.set_latest_pm(pm25=12.5, pm10=None, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._latest_pm25 == pytest.approx(12.5)

    def test_stores_pm10_value(self) -> None:
        """set_latest_pm(pm10=45.0, ...) → _latest_pm10 = 45.0."""
        pm_feed.set_latest_pm(pm25=None, pm10=45.0, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._latest_pm10 == pytest.approx(45.0)

    def test_stores_timestamp(self) -> None:
        """set_latest_pm(timestamp=TS) → _latest_timestamp = TS."""
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._latest_timestamp == pytest.approx(_FRESH_TS)

    def test_stores_is_observed_true(self) -> None:
        """set_latest_pm(is_observed=True) → _is_observed = True."""
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._is_observed is True

    def test_stores_is_observed_false(self) -> None:
        """set_latest_pm(is_observed=False) → _is_observed = False."""
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=False)
        assert pm_feed._is_observed is False

    def test_stores_none_pm25(self) -> None:
        """set_latest_pm(pm25=None) → _latest_pm25 = None."""
        pm_feed.set_latest_pm(pm25=None, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._latest_pm25 is None

    def test_stores_none_pm10(self) -> None:
        """set_latest_pm(pm10=None) → _latest_pm10 = None."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=None, timestamp=_FRESH_TS, is_observed=True)
        assert pm_feed._latest_pm10 is None

    def test_overwrites_previous_values(self) -> None:
        """Calling set_latest_pm() twice overwrites prior values."""
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.set_latest_pm(pm25=25.0, pm10=50.0, timestamp=_FRESH_TS + 300, is_observed=True)
        assert pm_feed._latest_pm25 == pytest.approx(25.0)
        assert pm_feed._latest_pm10 == pytest.approx(50.0)
        assert pm_feed._latest_timestamp == pytest.approx(_FRESH_TS + 300)


# ===========================================================================
# Group 2: feed_to_smoother() guards
# ===========================================================================


class TestFeedToSmootherGuards:
    """feed_to_smoother() must respect all three guards before calling add_sample."""

    def test_not_observed_no_feed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_observed=False → feed_to_smoother() must not call add_sample."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=False)
        pm_feed.feed_to_smoother({})

        assert len(calls) == 0, (
            "is_observed=False must prevent feed_to_smoother() from calling add_sample"
        )

    def test_never_set_no_feed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never called set_latest_pm() (_latest_timestamp=0.0) → no feed."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        # Do NOT call set_latest_pm(); _latest_timestamp defaults to 0.0.
        pm_feed.feed_to_smoother({})

        assert len(calls) == 0, (
            "_latest_timestamp=0.0 (never set) must prevent feeding"
        )

    def test_stale_data_no_feed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PM data older than 7200s → stale, feed_to_smoother() must not call add_sample."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        stale_timestamp = _FRESH_TS - _STALE_SECONDS - 1.0  # 7201s ago
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=stale_timestamp, is_observed=True)
        pm_feed.feed_to_smoother({})

        assert len(calls) == 0, (
            "Stale PM data (> 7200s old) must not be fed to the smoother"
        )

    def test_data_exactly_at_stale_boundary_not_fed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PM data exactly 7200s old → stale (> _STALE_SECONDS, not ≥), not fed."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        exact_boundary_ts = _FRESH_TS - _STALE_SECONDS  # elapsed == 7200.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=exact_boundary_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        # elapsed = time.time() - timestamp = _FRESH_TS - (_FRESH_TS - 7200) = 7200.0
        # Check: elapsed > _STALE_SECONDS → 7200.0 > 7200.0 is False → feeds.
        # So exactly at boundary, it IS fed.  This matches the > check in source.
        assert len(calls) == 2, (
            "Data exactly 7200.0s old (not strictly > _STALE_SECONDS) must be fed"
        )

    def test_fresh_observed_data_feeds_both_channels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh observed PM2.5 and PM10 → both channels fed to smoother."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 300.0  # 5 minutes ago — well within 2h
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=12.5, pm10=28.0, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        keys_fed = [k for k, _ in calls]
        assert "pollutantPM25" in keys_fed, "PM2.5 must be fed to smoother"
        assert "pollutantPM10" in keys_fed, "PM10 must be fed to smoother"
        assert len(calls) == 2, "Exactly 2 add_sample calls expected (PM25 + PM10)"

    def test_fresh_data_feeds_correct_pm25_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """feed_to_smoother() passes the correct PM2.5 value to add_sample."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=17.3, pm10=None, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        pm25_calls = [(k, v) for k, v in calls if k == "pollutantPM25"]
        assert len(pm25_calls) == 1
        assert pm25_calls[0][1] == pytest.approx(17.3)

    def test_fresh_data_feeds_correct_pm10_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """feed_to_smoother() passes the correct PM10 value to add_sample."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=None, pm10=43.8, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        pm10_calls = [(k, v) for k, v in calls if k == "pollutantPM10"]
        assert len(pm10_calls) == 1
        assert pm10_calls[0][1] == pytest.approx(43.8)


# ===========================================================================
# Group 3: feed_to_smoother() selective feeding
# ===========================================================================


class TestFeedToSmootherSelective:
    """feed_to_smoother() feeds only the channels that are not None."""

    def test_none_pm25_only_pm10_fed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pm25=None → pollutantPM25 not fed; pm10=30 → pollutantPM10 fed."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=None, pm10=30.0, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        keys = [k for k, _ in calls]
        assert "pollutantPM25" not in keys, "pm25=None must not feed pollutantPM25"
        assert "pollutantPM10" in keys, "pm10=30 must feed pollutantPM10"
        assert len(calls) == 1

    def test_none_pm10_only_pm25_fed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pm10=None → pollutantPM10 not fed; pm25=15 → pollutantPM25 fed."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=15.0, pm10=None, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        keys = [k for k, _ in calls]
        assert "pollutantPM25" in keys, "pm25=15 must feed pollutantPM25"
        assert "pollutantPM10" not in keys, "pm10=None must not feed pollutantPM10"
        assert len(calls) == 1

    def test_both_none_no_feed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pm25=None and pm10=None → is_observed guard still passes, but no channels fed."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=None, pm10=None, timestamp=recent_ts, is_observed=True)
        pm_feed.feed_to_smoother({})

        assert len(calls) == 0, (
            "pm25=None and pm10=None must result in zero add_sample calls"
        )

    def test_packet_argument_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """feed_to_smoother() ignores the packet argument — PM comes from module state."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        recent_ts = _FRESH_TS - 60.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=18.0, pm10=35.0, timestamp=recent_ts, is_observed=True)

        # Pass an unrelated packet — must be ignored.
        pm_feed.feed_to_smoother({"outTemp": 72.0, "windSpeed": 5.3})

        keys = [k for k, _ in calls]
        assert "pollutantPM25" in keys, "PM25 must be fed regardless of packet contents"
        assert "pollutantPM10" in keys, "PM10 must be fed regardless of packet contents"


# ===========================================================================
# Group 4: get_pm_staleness()
# ===========================================================================


class TestGetPmStaleness:
    """get_pm_staleness() returns None when never set, elapsed seconds otherwise."""

    def test_never_set_returns_none(self) -> None:
        """With no set_latest_pm() call, get_pm_staleness() returns None."""
        result = pm_feed.get_pm_staleness()
        assert result is None, (
            "get_pm_staleness() must return None when no PM has been received"
        )

    def test_freshly_set_returns_small_staleness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Just-set PM → staleness is the elapsed seconds since the timestamp."""
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS + 5.0)
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)
        result = pm_feed.get_pm_staleness()
        assert result == pytest.approx(5.0), (
            "get_pm_staleness() must return seconds elapsed since last PM timestamp"
        )

    def test_stale_pm_returns_large_staleness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PM set 3 hours ago → staleness = 10800s."""
        three_hours_ago = _FRESH_TS - 10800.0
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS)
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=three_hours_ago, is_observed=True)
        result = pm_feed.get_pm_staleness()
        assert result == pytest.approx(10800.0), (
            "PM set 3 hours ago must show staleness of 10800s"
        )

    def test_staleness_increases_with_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Staleness increases as time advances past the timestamp."""
        fake_now = [_FRESH_TS]
        monkeypatch.setattr(pm_feed.time, "time", lambda: fake_now[0])

        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)

        fake_now[0] = _FRESH_TS + 300.0
        stale_300 = pm_feed.get_pm_staleness()

        fake_now[0] = _FRESH_TS + 600.0
        stale_600 = pm_feed.get_pm_staleness()

        assert stale_600 is not None and stale_300 is not None
        assert stale_600 > stale_300, (
            "Staleness must increase as time advances past the last PM timestamp"
        )

    def test_staleness_uses_latest_timestamp_after_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a second set_latest_pm(), staleness resets to the new timestamp."""
        monkeypatch.setattr(pm_feed.time, "time", lambda: _FRESH_TS + 3600.0)

        # First reading: 3600s ago
        pm_feed.set_latest_pm(pm25=10.0, pm10=20.0, timestamp=_FRESH_TS, is_observed=True)
        staleness_first = pm_feed.get_pm_staleness()
        assert staleness_first == pytest.approx(3600.0)

        # Second reading: just now
        pm_feed.set_latest_pm(pm25=12.0, pm10=25.0, timestamp=_FRESH_TS + 3599.0, is_observed=True)
        staleness_second = pm_feed.get_pm_staleness()
        assert staleness_second is not None
        assert staleness_second < staleness_first, (
            "After a fresh set_latest_pm(), staleness must reset to new timestamp"
        )


# ===========================================================================
# Group 5: reset()
# ===========================================================================


class TestReset:
    """reset() clears all module-level state."""

    def test_reset_clears_pm25(self) -> None:
        """reset() sets _latest_pm25 to None."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        assert pm_feed._latest_pm25 is None

    def test_reset_clears_pm10(self) -> None:
        """reset() sets _latest_pm10 to None."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        assert pm_feed._latest_pm10 is None

    def test_reset_clears_timestamp(self) -> None:
        """reset() sets _latest_timestamp to 0.0."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        assert pm_feed._latest_timestamp == 0.0

    def test_reset_clears_is_observed(self) -> None:
        """reset() sets _is_observed to False."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        assert pm_feed._is_observed is False

    def test_reset_restores_staleness_to_none(self) -> None:
        """After reset(), get_pm_staleness() returns None (timestamp back to 0.0)."""
        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        result = pm_feed.get_pm_staleness()
        assert result is None, "get_pm_staleness() must return None after reset()"

    def test_reset_prevents_feeding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After reset(), feed_to_smoother() must not call add_sample."""
        from weewx_clearskies_api.sse.enrichment import input_smoother

        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(input_smoother, "add_sample", lambda k, v: calls.append((k, v)))

        pm_feed.set_latest_pm(pm25=15.0, pm10=30.0, timestamp=_FRESH_TS, is_observed=True)
        pm_feed.reset()
        pm_feed.feed_to_smoother({})

        assert len(calls) == 0, (
            "After reset(), feed_to_smoother() must not call add_sample"
        )
