"""Charts service — built-in chart groups per ADR-024 + brief call #5 (3a-2).

The 4 built-in groups and their default member sets are baked as constants.
At request time, members are intersected with the ColumnRegistry's mapped set
to prune fields the operator's archive doesn't have.  Groups with zero members
after pruning are omitted from the response (parallel to /records self-hide).

Custom chart groups are out of scope for 3a-2 (Phase 4 config UI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from weewx_clearskies_api.db.reflection import ColumnRegistry


# ---------------------------------------------------------------------------
# Built-in group definitions (per lead-confirmed brief call #5)
# ---------------------------------------------------------------------------


@dataclass
class ChartGroupEntry:
    """One chart group."""

    group_id: str
    name: str
    built_in: bool
    members: list[str]
    default_range: str | None  # e.g. "1d", or None for groups with own selector


_BUILTIN_GROUPS: Final[tuple[ChartGroupEntry, ...]] = (
    ChartGroupEntry(
        group_id="homepage",
        name="Homepage",
        built_in=True,
        members=[
            "outTemp",
            "dewpoint",
            "outHumidity",
            "windSpeed",
            "windGust",
            "windDir",
            "barometer",
            "rain",
            "rainRate",
            "radiation",
            "UV",
            "lightning_strike_count",
            "pollutantPM25",
        ],
        default_range="1d",
    ),
    ChartGroupEntry(
        group_id="monthly",
        name="Monthly",
        built_in=True,
        members=["outTemp", "rain", "windSpeed", "barometer"],
        default_range=None,
    ),
    ChartGroupEntry(
        group_id="ANNUAL",
        name="Annual",
        built_in=True,
        members=["outTemp", "rain"],
        default_range=None,
    ),
    ChartGroupEntry(
        group_id="averageclimate",
        name="Average climate",
        built_in=True,
        members=["outTemp", "rain"],
        default_range=None,
    ),
)


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_chart_groups(registry: ColumnRegistry) -> list[ChartGroupEntry]:
    """Return built-in chart groups with members pruned against the registry.

    Groups with zero members after pruning are omitted (self-hide rule).

    Args:
        registry: The ColumnRegistry from startup schema reflection.

    Returns:
        List of ChartGroupEntry objects with pruned members.
    """
    # Build the set of mapped canonical names from stock columns.
    mapped: set[str] = {
        info.canonical_name
        for info in registry.stock.values()
        if info.canonical_name is not None
    }

    result: list[ChartGroupEntry] = []
    for group in _BUILTIN_GROUPS:
        pruned = [m for m in group.members if m in mapped]
        if not pruned:
            continue  # self-hide
        result.append(
            ChartGroupEntry(
                group_id=group.group_id,
                name=group.name,
                built_in=group.built_in,
                members=pruned,
                default_range=group.default_range,
            )
        )

    return result
