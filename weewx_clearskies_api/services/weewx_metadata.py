"""weewx.units metadata reader for unit auto-detection (ADR-056).

Imports weewx.units at startup to cache obs_group_dict (column → unit group)
and the three unit-system dicts (USUnits, MetricUnits, MetricWXUnits).  Enables
auto-detecting units for archive columns during setup.

Graceful degradation: if weewx is not importable, all lookups return None and
is_available() returns False.  No crash, no startup abort.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Common install locations to probe when the initial import fails.
# ---------------------------------------------------------------------------

_COMMON_WEEWX_PATHS: list[str] = [
    "/usr/share/weewx",
    "/usr/lib/python3/dist-packages",
]

# ---------------------------------------------------------------------------
# Module-level cache — populated at startup by load_weewx_metadata().
# ---------------------------------------------------------------------------

_available: bool = False
_obs_group_dict: dict[str, str] = {}
# int (1=US, 16=Metric, 17=MetricWX) → dict[group_name, unit_string]
_unit_systems: dict[int, dict[str, str]] = {}


def _try_import() -> bool:
    """Attempt ``import weewx.units`` and cache metadata on success."""
    global _available, _obs_group_dict, _unit_systems  # noqa: PLW0603

    try:
        import weewx.units  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return False

    _obs_group_dict = dict(weewx.units.obs_group_dict)

    # Cache the three unit-system dicts keyed by their integer constants.
    # weewx.units exposes USUnits/MetricUnits/MetricWXUnits as ListOfDicts
    # (dict-like); the integer constants live on the weewx top-level module
    # but are also available via weewx.units.unit_constants.
    unit_constants: dict[str, int] = weewx.units.unit_constants
    source_map = {
        unit_constants["US"]: weewx.units.USUnits,
        unit_constants["METRIC"]: weewx.units.MetricUnits,
        unit_constants["METRICWX"]: weewx.units.MetricWXUnits,
    }
    _unit_systems = {k: dict(v) for k, v in source_map.items()}

    _available = True
    return True


def load_weewx_metadata(python_path: str | None = None) -> None:
    """Attempt to import weewx.units and cache metadata.

    If *python_path* is set, prepend it to ``sys.path`` before the import
    attempt.  If the initial import fails, probe common install locations.
    If all fail, log a warning and continue (graceful degradation).
    """
    if python_path and python_path not in sys.path:
        sys.path.insert(0, python_path)

    if _try_import():
        logger.info(
            "weewx metadata loaded",
            extra={
                "obs_group_count": len(_obs_group_dict),
                "unit_systems": sorted(_unit_systems.keys()),
            },
        )
        return

    # Probe common paths before giving up.
    for path in _COMMON_WEEWX_PATHS:
        if path in sys.path:
            continue
        sys.path.insert(0, path)
        if _try_import():
            logger.info(
                "weewx metadata loaded (found at %s)",
                path,
                extra={
                    "obs_group_count": len(_obs_group_dict),
                    "unit_systems": sorted(_unit_systems.keys()),
                },
            )
            return
        # Didn't work — remove the path we just added.
        sys.path.remove(path)

    logger.warning(
        "weewx.units not available — unit auto-detection for archive columns "
        "will not be available. Install weewx or set [weewx] python_path in "
        "api.conf to the directory containing the weewx package."
    )


def is_available() -> bool:
    """Whether weewx.units was successfully imported."""
    return _available


def get_obs_group(column_name: str) -> str | None:
    """Look up the unit group for a column name in obs_group_dict."""
    if not _available:
        return None
    return _obs_group_dict.get(column_name)


def get_unit_for_group(group: str, unit_system: int) -> str | None:
    """Look up the unit string for a group in a given unit system.

    Args:
        group: weewx group name (e.g. ``"group_temperature"``).
        unit_system: 1 (US), 16 (Metric), or 17 (MetricWX).

    Returns:
        The weewx internal unit name (e.g. ``"degree_F"``) or None if the
        group/system is unknown or weewx metadata is not available.
    """
    if not _available:
        return None
    system_dict = _unit_systems.get(unit_system)
    if system_dict is None:
        return None
    return system_dict.get(group)


def reset_cache() -> None:
    """Clear cached state.  Test-only."""
    global _available, _obs_group_dict, _unit_systems  # noqa: PLW0603
    _available = False
    _obs_group_dict = {}
    _unit_systems = {}
