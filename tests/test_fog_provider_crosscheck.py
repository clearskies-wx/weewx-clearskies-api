"""Tests for the fog/mist provider cross-check in compose_weather_text().

When local sensors detect fog (T-Td ≤ 2°F, calm winds), the system checks the
provider weather text for corroboration before reporting fog.  If the provider
has fresh data that does NOT mention fog/mist, the local fog label is suppressed.
Absence of fresh provider data (stale or unavailable) is not evidence of absence
— the local label passes through.

These tests exercise the cross-check integration in
`weewx_clearskies_api.sse.enrichment.weather_text.compose_weather_text()`.

Mocking strategy
----------------
- `detect_fog_mist`  → patched at the name used inside weather_text.py
  (`weewx_clearskies_api.sse.enrichment.weather_text.detect_fog_mist`)
  to return a controlled fog/mist state without requiring real sensor buffers.
- `get_provider_weather_text` → patched at the module that owns the state
  (`weewx_clearskies_api.sse.enrichment.provider_weather_feed.get_provider_weather_text`)
  because weather_text.py imports it lazily (inside the function body) using
  `from ... import get_provider_weather_text`.  Patching the canonical module
  location intercepts both the eager and lazy import paths.
- `get_smoothed` → returns None for all fields (no real ring buffers in unit tests).
- `sky_classify`, `_sky_module.is_daytime`, `_sky_module.get_current_kcs`,
  `_sky_module.get_solar_elevation` → deterministic nighttime/no-Kcs defaults
  so haze/sky paths don't fire unexpectedly.
- `detect_haze` → patched to return None (haze path is not under test here).
- `build_observation`, `generate_standard`, `generate_verbose` → patched to
  avoid ObservationModel dependency in pure cross-check tests.

Autouse fixture `_reset_fog_condition` mirrors the pattern from
test_fog_condition.py to clear fog_condition module state between tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from weewx_clearskies_api.sse import fog_condition


# ---------------------------------------------------------------------------
# Autouse reset — mirrors test_fog_condition.py pattern
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fog_condition():
    """Reset fog_condition module state before and after each test."""
    fog_condition.reset()
    yield
    fog_condition.reset()


# ---------------------------------------------------------------------------
# Patch targets — centralised so a rename only needs one edit here
# ---------------------------------------------------------------------------

_DETECT_FOG_MIST = "weewx_clearskies_api.sse.enrichment.weather_text.detect_fog_mist"
_GET_PROVIDER_TEXT = (
    "weewx_clearskies_api.sse.enrichment.provider_weather_feed.get_provider_weather_text"
)
_GET_SMOOTHED = "weewx_clearskies_api.sse.enrichment.weather_text.get_smoothed"
_SKY_CLASSIFY = "weewx_clearskies_api.sse.enrichment.weather_text.sky_classify"
_DETECT_HAZE = "weewx_clearskies_api.sse.enrichment.weather_text.detect_haze"
_BUILD_OBS = "weewx_clearskies_api.sse.enrichment.weather_text.build_observation"
_GEN_STANDARD = "weewx_clearskies_api.sse.enrichment.weather_text.generate_standard"
_GEN_VERBOSE = "weewx_clearskies_api.sse.enrichment.weather_text.generate_verbose"

# sky_module functions used directly as `_sky_module.<fn>` in weather_text.py
_SKY_IS_DAYTIME = "weewx_clearskies_api.sse.sky_condition.is_daytime"
_SKY_GET_KCS = "weewx_clearskies_api.sse.sky_condition.get_current_kcs"
_SKY_GET_ELEV = "weewx_clearskies_api.sse.sky_condition.get_solar_elevation"


# ---------------------------------------------------------------------------
# Helper — run compose_weather_text() with all non-cross-check paths silenced
# ---------------------------------------------------------------------------


def _smoothed_with_dewpoint(field: str) -> float | None:
    """Return a non-None dewpoint so the missing-hygrometer deferral doesn't fire."""
    if field == "dewpoint":
        return 60.0
    return None


def _compose(
    *,
    fog_mist_result: str | None,
    provider_text: str | None,
    provider_age: float | None,
    obs_data: dict | None = None,
    has_dewpoint: bool = True,
) -> str:
    """Call compose_weather_text() with cross-check inputs under full mock control.

    Everything except the fog/provider cross-check is pinned to neutral values:
    - Smoothed readings return None except dewpoint (prevents the missing-
      hygrometer deferral from adopting provider fog text).  Set *has_dewpoint*
      to False to simulate a station with no hygrometer.
    - Nighttime (no solar haze path).
    - No Kcs (no solar classifier output).
    - sky_classify() returns None (no sky label from solar).
    - detect_haze() returns None (haze path not under test).
    - build_observation / generate_standard / generate_verbose return stubs.

    The only signals that vary between test calls are fog_mist_result (what
    detect_fog_mist returns) and (provider_text, provider_age).
    """
    from weewx_clearskies_api.sse.enrichment.weather_text import compose_weather_text

    mock_obs = MagicMock(return_value=MagicMock())
    mock_std = MagicMock(return_value=None)
    mock_verb = MagicMock(return_value=None)
    smoothed_side_effect = _smoothed_with_dewpoint if has_dewpoint else (lambda _: None)

    with (
        patch(_DETECT_FOG_MIST, return_value=fog_mist_result),
        patch(_GET_PROVIDER_TEXT, return_value=(provider_text, provider_age)),
        patch(_GET_SMOOTHED, side_effect=smoothed_side_effect),
        patch(_SKY_CLASSIFY, return_value=None),
        patch(_DETECT_HAZE, return_value=None),
        patch(_SKY_IS_DAYTIME, return_value=False),
        patch(_SKY_GET_KCS, return_value=None),
        patch(_SKY_GET_ELEV, return_value=None),
        patch(_BUILD_OBS, mock_obs),
        patch(_GEN_STANDARD, mock_std),
        patch(_GEN_VERBOSE, mock_verb),
    ):
        return compose_weather_text(obs_data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_local_foggy_confirmed_by_provider_fog_text_passes_through():
    """Local 'Foggy' + provider says 'Fog' → 'Foggy' appears in composed text.

    Provider corroborates local detection; cross-check must not suppress.
    """
    result = _compose(
        fog_mist_result="Foggy",
        provider_text="Fog",
        provider_age=60.0,
    )
    assert "Foggy" in result, (
        f"Expected 'Foggy' in composed text when provider confirms fog with 'Fog', "
        f"got {result!r}"
    )


def test_local_foggy_suppressed_when_provider_says_partly_cloudy():
    """Local 'Foggy' + fresh provider says 'Partly Cloudy' → fog label suppressed.

    Provider has fresh data that contradicts fog; cross-check must suppress the
    local fog label so the composed text does not contain 'Foggy'.
    """
    result = _compose(
        fog_mist_result="Foggy",
        provider_text="Partly Cloudy",
        provider_age=60.0,
    )
    assert "Foggy" not in result, (
        f"Expected 'Foggy' suppressed when fresh provider says 'Partly Cloudy', "
        f"got {result!r}"
    )


def test_local_foggy_passes_through_when_provider_data_unavailable():
    """Local 'Foggy' + provider returns (None, None) → 'Foggy' passes through.

    Absence of fresh provider data is not evidence of absence.  When the
    provider feed is stale or has never been fetched, the local label survives.
    """
    result = _compose(
        fog_mist_result="Foggy",
        provider_text=None,
        provider_age=None,
    )
    assert "Foggy" in result, (
        f"Expected 'Foggy' to pass through when provider data is unavailable (None, None), "
        f"got {result!r}"
    )


def test_local_misty_confirmed_by_provider_mist_text_passes_through():
    """Local 'Misty' + provider says 'Mist' → 'Misty' appears in composed text.

    Provider corroborates mist; cross-check must not suppress.
    """
    result = _compose(
        fog_mist_result="Misty",
        provider_text="Mist",
        provider_age=120.0,
    )
    assert "Misty" in result, (
        f"Expected 'Misty' in composed text when provider confirms with 'Mist', "
        f"got {result!r}"
    )


def test_local_misty_suppressed_when_provider_says_clear():
    """Local 'Misty' + fresh provider says 'Clear' → mist label suppressed.

    Provider has fresh data with no fog/mist indicator; cross-check suppresses
    the local mist label.
    """
    result = _compose(
        fog_mist_result="Misty",
        provider_text="Clear",
        provider_age=60.0,
    )
    assert "Misty" not in result, (
        f"Expected 'Misty' suppressed when fresh provider says 'Clear', "
        f"got {result!r}"
    )


def test_local_foggy_confirmed_by_provider_dense_fog_substring():
    """Local 'Foggy' + provider says 'Dense Fog' → passes (substring match on 'fog').

    The cross-check uses substring matching; 'Dense Fog' contains 'fog' and
    must be treated as corroboration.
    """
    result = _compose(
        fog_mist_result="Foggy",
        provider_text="Dense Fog",
        provider_age=60.0,
    )
    assert "Foggy" in result, (
        f"Expected 'Foggy' to pass through with provider text 'Dense Fog' "
        f"(substring 'fog' present), got {result!r}"
    )


def test_local_foggy_confirmed_by_provider_fog_slash_mist_substring():
    """Local 'Foggy' + provider says 'Fog/Mist' → passes (substring match on 'fog').

    Compound provider strings like 'Fog/Mist' contain 'fog' and must corroborate.
    """
    result = _compose(
        fog_mist_result="Foggy",
        provider_text="Fog/Mist",
        provider_age=60.0,
    )
    assert "Foggy" in result, (
        f"Expected 'Foggy' to pass through with provider text 'Fog/Mist' "
        f"(substring 'fog' present), got {result!r}"
    )


def test_detect_fog_mist_returns_none_cross_check_does_not_fire():
    """detect_fog_mist returns None → no fog label, cross-check never fires.

    Even when provider says 'Fog', no fog label should appear in composed text
    when local detection returns None.  The cross-check only suppresses a
    locally-detected label — it does not adopt a provider-only fog label when
    dewpoint is available (that is a separate deferral path for missing hygrometers).
    """
    result = _compose(
        fog_mist_result=None,
        provider_text="Fog",
        provider_age=60.0,
    )
    assert "Foggy" not in result, (
        f"Expected no 'Foggy' when detect_fog_mist returned None, "
        f"got {result!r}"
    )
    assert "Misty" not in result, (
        f"Expected no 'Misty' when detect_fog_mist returned None, "
        f"got {result!r}"
    )


def test_detect_fog_mist_returns_hazy_cross_check_does_not_fire():
    """detect_fog_mist returns 'Hazy' → PM disambiguation path, not fog path.

    'Hazy' from fog_condition is routed to the haze_label pathway in
    compose_weather_text(), not the fog_mist_label pathway.  The cross-check
    is specific to 'Foggy' / 'Misty' labels; it must not be consulted for 'Hazy'.
    The composed text should not contain 'Foggy' or 'Misty'.
    """
    result = _compose(
        fog_mist_result="Hazy",
        provider_text="Fog",
        provider_age=60.0,
    )
    assert "Foggy" not in result, (
        f"Expected no 'Foggy' when detect_fog_mist returned 'Hazy' (PM path), "
        f"got {result!r}"
    )
    assert "Misty" not in result, (
        f"Expected no 'Misty' when detect_fog_mist returned 'Hazy' (PM path), "
        f"got {result!r}"
    )
