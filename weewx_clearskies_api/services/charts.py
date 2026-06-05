"""Charts service — operator-configured chart groups per ADR-024.

Groups are derived entirely from the operator's charts.conf (loaded and pruned
at startup via charts_config.py).  If the config has not been wired before the
first request, get_charts_config() raises RuntimeError — that is a startup
misconfiguration and is intentionally allowed to propagate.
"""

from __future__ import annotations

from dataclasses import dataclass

from weewx_clearskies_api.db.reflection import ColumnRegistry


@dataclass
class ChartGroupEntry:
    """One chart group."""

    group_id: str
    name: str
    built_in: bool
    members: list[str]
    default_range: str | None  # e.g. "1d", or None for groups with own selector


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_chart_groups(registry: ColumnRegistry) -> list[ChartGroupEntry]:
    """Return chart groups derived from the operator-configured charts.conf.

    Groups are built from the pruned ChartsConfig that was loaded and wired at
    startup.  Pruning already ran at startup, so all surviving series are
    guaranteed to have a corresponding archive column.

    If the charts config was not wired before this call (startup
    misconfiguration), get_charts_config() raises RuntimeError and the
    exception propagates — it is not caught here.

    Args:
        registry: The ColumnRegistry from startup schema reflection.
            Accepted for API compatibility; not used directly because
            pruning already ran at startup.

    Returns:
        List of ChartGroupEntry objects, one per surviving group.
    """
    from weewx_clearskies_api.services.charts_config import get_charts_config  # noqa: PLC0415

    config = get_charts_config()

    # Derive from the pruned charts config (pruning already ran at startup).
    derived: list[ChartGroupEntry] = []
    for cfg_group in config.groups:
        cfg_members: list[str] = []
        for cfg_chart in cfg_group.charts:
            for cfg_series in cfg_chart.series:
                effective = cfg_series.observation_type or cfg_series.series_id
                if effective not in cfg_members:
                    cfg_members.append(effective)
        if not cfg_members:
            continue
        derived.append(
            ChartGroupEntry(
                group_id=cfg_group.group_id,
                name=cfg_group.title or cfg_group.group_id,
                built_in=True,
                members=cfg_members,
                default_range=cfg_group.rolling_ranges[0] if cfg_group.rolling_ranges else None,
            )
        )
    return derived
