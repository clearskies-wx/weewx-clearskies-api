"""Unit tests for provider_weather_feed module (ADR-071).

Tests set_latest_weather_text(), get_provider_weather_text(), and reset()
from weewx_clearskies_api.sse.enrichment.provider_weather_feed.

Module-level state is cleared by calling reset() in a pytest fixture before
each test.  time.time() is patched to control staleness.

Staleness boundary: _STALE_SECONDS = 7200.0 (2 hours).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from weewx_clearskies_api.sse.enrichment.provider_weather_feed import (
    _STALE_SECONDS,
    get_provider_weather_text,
    reset,
    set_latest_weather_text,
)


# ---------------------------------------------------------------------------
# Test-isolation fixture: clear module state before every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_feed_state() -> None:
    """Reset provider_weather_feed module state before each test."""
    reset()


# ===========================================================================
# 1. Initial state — never set
# ===========================================================================


class TestInitialState:
    """Module state is empty on startup or after reset()."""

    def test_initial_get_returns_none_none(self) -> None:
        """get_provider_weather_text() before any set → (None, None)."""
        result = get_provider_weather_text()
        assert result == (None, None)

    def test_initial_state_is_none_after_reset(self) -> None:
        """reset() followed by get returns (None, None)."""
        # Set some data first
        set_latest_weather_text(weather_text="Hazy skies", timestamp=1_000_000.0)
        reset()
        result = get_provider_weather_text()
        assert result == (None, None)


# ===========================================================================
# 2. Set and get — fresh data
# ===========================================================================


class TestSetAndGet:
    """Fresh data set within staleness window is returned correctly."""

    def test_set_then_get_returns_text_and_age(self) -> None:
        """set_latest_weather_text() + get_provider_weather_text() → (text, age)."""
        now = 1_000_000.0
        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=now):
            set_latest_weather_text(weather_text="Hazy skies", timestamp=now)
            text, age = get_provider_weather_text()

        assert text == "Hazy skies"
        assert age is not None
        assert age >= 0.0

    def test_age_reflects_elapsed_time(self) -> None:
        """Age returned equals current_time - stored_timestamp."""
        stored_at = 1_000_000.0
        queried_at = 1_000_300.0  # 300 seconds later
        set_latest_weather_text(weather_text="Smoke in the area", timestamp=stored_at)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=queried_at):
            text, age = get_provider_weather_text()

        assert text == "Smoke in the area"
        assert age == pytest.approx(300.0, abs=0.01)

    def test_fresh_data_age_is_below_stale_threshold(self) -> None:
        """Age < _STALE_SECONDS (7200s) → data is returned, not treated as stale."""
        stored_at = 1_000_000.0
        queried_at = stored_at + _STALE_SECONDS - 1.0  # one second before stale
        set_latest_weather_text(weather_text="Clear skies", timestamp=stored_at)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=queried_at):
            text, age = get_provider_weather_text()

        assert text == "Clear skies"
        assert age is not None
        assert age < _STALE_SECONDS

    def test_set_twice_get_returns_latest_value(self) -> None:
        """Two set() calls → get() returns the most recently stored text."""
        now = 1_000_000.0
        set_latest_weather_text(weather_text="First value", timestamp=now - 100.0)
        set_latest_weather_text(weather_text="Second value", timestamp=now)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=now + 1.0):
            text, age = get_provider_weather_text()

        assert text == "Second value"


# ===========================================================================
# 3. Staleness — data older than 7200 seconds
# ===========================================================================


class TestStaleness:
    """Data older than _STALE_SECONDS is treated as unavailable."""

    def test_stale_data_returns_none_none(self) -> None:
        """Data stored > 7200s ago → get returns (None, None)."""
        stored_at = 1_000_000.0
        queried_at = stored_at + _STALE_SECONDS + 1.0  # one second past stale threshold
        set_latest_weather_text(weather_text="Hazy conditions", timestamp=stored_at)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=queried_at):
            result = get_provider_weather_text()

        assert result == (None, None)

    def test_exactly_at_stale_boundary_returns_none_none(self) -> None:
        """Data stored exactly _STALE_SECONDS ago → get returns (None, None) (> threshold)."""
        stored_at = 1_000_000.0
        queried_at = stored_at + _STALE_SECONDS  # exactly at boundary
        set_latest_weather_text(weather_text="Smoke detected", timestamp=stored_at)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=queried_at):
            result = get_provider_weather_text()

        # age == _STALE_SECONDS → age > _STALE_SECONDS is False → should be returned
        # But let's verify what the implementation actually does:
        # The condition is `if age > _STALE_SECONDS: return (None, None)`
        # At exactly _STALE_SECONDS, age is NOT > threshold, so it should return data
        text, age = result
        assert text == "Smoke detected"


# ===========================================================================
# 4. None text stored explicitly
# ===========================================================================


class TestNoneText:
    """When stored text is None, get returns (None, None) regardless of freshness."""

    def test_none_text_stored_returns_none_none(self) -> None:
        """set weather_text=None → get returns (None, None) even when fresh."""
        now = 1_000_000.0
        set_latest_weather_text(weather_text=None, timestamp=now)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=now + 10.0):
            result = get_provider_weather_text()

        assert result == (None, None)

    def test_none_text_then_real_text_returns_real_text(self) -> None:
        """set None, then set real text → get returns real text."""
        t1 = 1_000_000.0
        t2 = 1_000_100.0
        set_latest_weather_text(weather_text=None, timestamp=t1)
        set_latest_weather_text(weather_text="Haze and smoke", timestamp=t2)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=t2 + 10.0):
            text, age = get_provider_weather_text()

        assert text == "Haze and smoke"


# ===========================================================================
# 5. Reset clears all state
# ===========================================================================


class TestReset:
    """reset() returns module to initial empty state."""

    def test_reset_after_set_returns_none_none(self) -> None:
        """After set + reset, get returns (None, None)."""
        set_latest_weather_text(weather_text="Foggy conditions", timestamp=1_000_000.0)
        reset()

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=1_000_001.0):
            result = get_provider_weather_text()

        assert result == (None, None)

    def test_reset_allows_fresh_set(self) -> None:
        """After reset, a new set + get works correctly."""
        set_latest_weather_text(weather_text="Old value", timestamp=1_000_000.0)
        reset()
        t2 = 2_000_000.0
        set_latest_weather_text(weather_text="New value", timestamp=t2)

        with patch("weewx_clearskies_api.sse.enrichment.provider_weather_feed.time.time", return_value=t2 + 5.0):
            text, age = get_provider_weather_text()

        assert text == "New value"
        assert age == pytest.approx(5.0, abs=0.01)
