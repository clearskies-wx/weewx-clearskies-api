"""Unit tests for _validate_column_units in __main__.py.

Verifies that the startup validation warns on mismatched confirmed units
and stays silent when weewx metadata is unavailable or units match.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest


MODULE = "weewx_clearskies_api.__main__"
META = "weewx_clearskies_api.services.weewx_metadata"


def _run_validation(column_units: dict[str, str]) -> None:
    from weewx_clearskies_api.__main__ import _validate_column_units

    _validate_column_units(column_units)


def test_validation_skips_when_weewx_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(f"{META}.is_available", return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        _run_validation({"outTemp": "bananas"})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings


def test_validation_skips_when_column_units_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(f"{META}.is_available", return_value=True),
        caplog.at_level(logging.WARNING),
    ):
        _run_validation({})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings


def test_validation_no_warning_for_matching_unit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(f"{META}.is_available", return_value=True),
        patch(f"{META}.get_obs_group", return_value="group_temperature"),
        patch(
            f"{META}.get_unit_for_group",
            side_effect=lambda group, us: {
                (group, 1): "degree_F",
                (group, 16): "degree_C",
                (group, 17): "degree_C",
            }.get((group, us)),
        ),
        caplog.at_level(logging.WARNING),
    ):
        _run_validation({"outTemp": "degree_F"})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings


def test_validation_warns_on_mismatched_unit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(f"{META}.is_available", return_value=True),
        patch(f"{META}.get_obs_group", return_value="group_temperature"),
        patch(
            f"{META}.get_unit_for_group",
            side_effect=lambda group, us: {
                (group, 1): "degree_F",
                (group, 16): "degree_C",
                (group, 17): "degree_C",
            }.get((group, us)),
        ),
        caplog.at_level(logging.WARNING),
    ):
        _run_validation({"outTemp": "mile_per_hour"})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "mile_per_hour" in warnings[0].message
    assert "group_temperature" in warnings[0].message


def test_validation_skips_unknown_columns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(f"{META}.is_available", return_value=True),
        patch(f"{META}.get_obs_group", return_value=None),
        caplog.at_level(logging.WARNING),
    ):
        _run_validation({"customExtensionCol": "widgets"})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings
