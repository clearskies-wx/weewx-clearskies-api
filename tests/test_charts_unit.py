"""Unit tests for chart-group self-prune logic (services/charts.py).

Covers per the 3a-2 brief:
  - Self-prune: homepage members shrink when ColumnRegistry lacks
    lightning_strike_count + pollutantPM25.
  - All groups self-hide when mapped set is empty -> groups=[].
  - Built-in group constants: 4 groups with correct IDs, names, members.
  - Group with zero members after prune is omitted.

ADR references: ADR-024 (built-in chart groups and self-hide rule).

Note (T3.4): _BUILTIN_GROUPS was removed from services/charts.py in T3.4.
  - TestBuiltInChartGroupConstants now verifies the same invariants via the
    config-driven path: load built-in defaults, wire them, call get_chart_groups().
  - TestChartGroupSelfPrune wires a synthetic ChartsConfig representing the
    original four groups (including the optional lightning/PM2.5 members) so
    the prune behaviour under test is fully exercised.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry
from weewx_clearskies_api.models.chart_config import (
    ChartConfig,
    ChartGroupConfig,
    ChartsConfig,
    SeriesConfig,
)

# ---------------------------------------------------------------------------
# Helper: registry builder
# ---------------------------------------------------------------------------


def _make_registry_with_mapped(mapped_canonical_names: list[str]) -> ColumnRegistry:
    """Build a registry where the mapped set = mapped_canonical_names."""
    registry = ColumnRegistry()
    registry.stock = {
        name: ColumnInfo(db_name=name, canonical_name=name, is_stock=True)
        for name in mapped_canonical_names
    }
    return registry


# ---------------------------------------------------------------------------
# Helper: synthetic ChartsConfig with four legacy-equivalent groups
#
# These member lists mirror the original _BUILTIN_GROUPS constant so that the
# TestChartGroupSelfPrune tests remain meaningful.  The config-driven defaults
# (charts.conf.default) use a slightly different set; tests that need the
# real defaults use _load_builtin_default() instead.
# ---------------------------------------------------------------------------

_HOMEPAGE_DEFAULTS = [
    "outTemp", "dewpoint", "outHumidity", "windSpeed", "windGust", "windDir",
    "barometer", "rain", "rainRate", "radiation", "UV",
    "lightning_strike_count", "pollutantPM25",
]
_MONTHLY_DEFAULTS = ["outTemp", "rain", "windSpeed", "barometer"]
_ANNUAL_DEFAULTS = ["outTemp", "rain"]
_AVGCLIMATE_DEFAULTS = ["outTemp", "rain"]


def _build_synthetic_charts_config() -> ChartsConfig:
    """Build a ChartsConfig whose groups cover the four legacy member sets.

    Each group gets one chart, and each member gets its own single-series
    chart entry so that prune_charts_config() can drop individual members.
    """

    def _group(group_id: str, members: list[str]) -> ChartGroupConfig:
        charts = [
            ChartConfig(
                chart_id=f"{group_id}_{m}",
                series=[SeriesConfig(series_id=m)],
            )
            for m in members
        ]
        rolling = ["1d"] if group_id == "homepage" else []
        return ChartGroupConfig(
            group_id=group_id,
            charts=charts,
            rolling_ranges=rolling,
        )

    return ChartsConfig(
        groups=[
            _group("homepage", _HOMEPAGE_DEFAULTS),
            _group("monthly", _MONTHLY_DEFAULTS),
            _group("ANNUAL", _ANNUAL_DEFAULTS),
            _group("averageclimate", _AVGCLIMATE_DEFAULTS),
        ]
    )


# ---------------------------------------------------------------------------
# Fixture: reset the charts-config singleton after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_charts_config_singleton():
    """Ensure the global charts-config singleton is cleared after every test.

    wire_charts_config() sets a module-level variable.  Without teardown,
    one test's wired config leaks into the next.
    """
    from weewx_clearskies_api.services import charts_config as _cc_module

    yield
    _cc_module._charts_config = None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Built-in group constants (config-driven path)
# ---------------------------------------------------------------------------

# Members the charts.conf.default homepage group defines.
_DEFAULT_HOMEPAGE_MEMBERS = {
    "outTemp", "dewpoint", "windDir", "windGust", "windSpeed",
    "rainRate", "rain", "barometer", "radiation", "UV",
    "lightning_strike_count",
}
_DEFAULT_MONTHLY_MEMBERS = {"outTemp", "windSpeed", "rain", "barometer"}
_DEFAULT_ANNUAL_MEMBERS = {"outTemp", "rain"}


class TestBuiltInChartGroupConstants:
    """Built-in defaults (charts.conf.default) produce the expected 4 groups.

    Previously these tests imported _BUILTIN_GROUPS directly.  T3.4 removed
    that constant; the equivalent invariants now hold over the config-driven
    path: load the built-in default config, wire it, and call get_chart_groups()
    with a full registry so no groups are pruned away.
    """

    @pytest.fixture(autouse=True)
    def _wire_builtin_defaults(self):
        """Wire the built-in default config so get_chart_groups() can be called."""
        from weewx_clearskies_api.services.charts_config import (
            _load_builtin_default,
            wire_charts_config,
        )

        wire_charts_config(_load_builtin_default())

    def _full_registry(self) -> ColumnRegistry:
        """Registry that contains every member across all four default groups."""
        all_members = list(
            _DEFAULT_HOMEPAGE_MEMBERS
            | _DEFAULT_MONTHLY_MEMBERS
            | _DEFAULT_ANNUAL_MEMBERS
        )
        return _make_registry_with_mapped(all_members)

    def test_four_built_in_groups_defined(self) -> None:
        """Exactly 4 built-in groups: homepage, monthly, ANNUAL, averageclimate."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        group_ids = {g.group_id for g in groups}
        assert group_ids == {"homepage", "monthly", "ANNUAL", "averageclimate"}, (
            f"Expected 4 built-in group IDs, got {group_ids!r}"
        )

    def test_homepage_group_has_correct_default_members(self) -> None:
        """homepage group default members match charts.conf.default."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        homepage = next(g for g in groups if g.group_id == "homepage")
        assert set(homepage.members) == _DEFAULT_HOMEPAGE_MEMBERS, (
            f"homepage default members mismatch. "
            f"Extra: {set(homepage.members) - _DEFAULT_HOMEPAGE_MEMBERS}. "
            f"Missing: {_DEFAULT_HOMEPAGE_MEMBERS - set(homepage.members)}."
        )

    def test_homepage_default_range_is_1d(self) -> None:
        """homepage default_range is '1d' (first rolling_ranges entry)."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        homepage = next(g for g in groups if g.group_id == "homepage")
        assert homepage.default_range == "1d", (
            f"homepage default_range must be '1d', got {homepage.default_range!r}"
        )

    def test_monthly_default_range_is_none(self) -> None:
        """monthly default_range is None (has its own month-dropdown selector)."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        monthly = next(g for g in groups if g.group_id == "monthly")
        assert monthly.default_range is None, (
            f"monthly default_range must be None, got {monthly.default_range!r}"
        )

    def test_annual_default_range_is_none(self) -> None:
        """ANNUAL default_range is None (year dropdown)."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        annual = next(g for g in groups if g.group_id == "ANNUAL")
        assert annual.default_range is None

    def test_all_built_in_groups_have_built_in_true(self) -> None:
        """All 4 built-in groups have built_in=True."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        for group in groups:
            assert group.built_in is True, (
                f"Group {group.group_id!r} built_in must be True"
            )

    def test_monthly_default_members_match_brief(self) -> None:
        """monthly group default members: outTemp, rain, windSpeed, barometer."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        monthly = next(g for g in groups if g.group_id == "monthly")
        assert set(monthly.members) == _DEFAULT_MONTHLY_MEMBERS, (
            f"monthly members mismatch: {set(monthly.members)} vs {_DEFAULT_MONTHLY_MEMBERS}"
        )

    def test_annual_default_members_match_brief(self) -> None:
        """ANNUAL group default members: outTemp, rain."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        groups = get_chart_groups(self._full_registry())
        annual = next(g for g in groups if g.group_id == "ANNUAL")
        assert set(annual.members) == _DEFAULT_ANNUAL_MEMBERS


# ---------------------------------------------------------------------------
# Self-prune logic
# ---------------------------------------------------------------------------


class TestChartGroupSelfPrune:
    """get_chart_groups prunes members against the mapped registry set.

    Each test wires a synthetic ChartsConfig (pre-pruned against the test
    registry) to simulate the startup-time wire_charts_config() call.
    """

    def _wire_pruned_config(self, registry: ColumnRegistry) -> None:
        """Build synthetic config, prune it against registry, and wire it."""
        from weewx_clearskies_api.services.charts_config import (
            prune_charts_config,
            wire_charts_config,
        )

        raw = _build_synthetic_charts_config()
        pruned = prune_charts_config(raw, registry)
        wire_charts_config(pruned)

    def test_homepage_members_exclude_missing_lightning_and_pm25(self) -> None:
        """homepage.members drops lightning_strike_count + pollutantPM25 when not mapped."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        # Everything except lightning_strike_count and pollutantPM25
        mapped = [
            m for m in _HOMEPAGE_DEFAULTS
            if m not in {"lightning_strike_count", "pollutantPM25"}
        ]
        registry = _make_registry_with_mapped(mapped)
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)

        homepage = next((g for g in groups if g.group_id == "homepage"), None)
        assert homepage is not None, "homepage group must be present when it has members"

        members_set = set(homepage.members)
        assert "lightning_strike_count" not in members_set, (
            "lightning_strike_count must be pruned when not in mapped registry"
        )
        assert "pollutantPM25" not in members_set, (
            "pollutantPM25 must be pruned when not in mapped registry"
        )

    def test_homepage_members_include_remaining_mapped_fields(self) -> None:
        """Homepage retains outTemp, rain, windSpeed etc. after pruning unmapped fields."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        mapped = [
            m for m in _HOMEPAGE_DEFAULTS
            if m not in {"lightning_strike_count", "pollutantPM25"}
        ]
        registry = _make_registry_with_mapped(mapped)
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)

        homepage = next((g for g in groups if g.group_id == "homepage"), None)
        assert homepage is not None

        for field in ("outTemp", "rain", "windSpeed"):
            assert field in homepage.members, (
                f"Field {field!r} must remain in homepage.members after partial prune"
            )

    def test_empty_mapped_set_hides_all_groups(self) -> None:
        """When mapped set is empty, all groups self-hide -> groups=[]."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        registry = _make_registry_with_mapped([])  # No columns mapped
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)
        assert groups == [], (
            f"All groups must self-hide when mapped set is empty, got {groups!r}"
        )

    def test_monthly_annual_avgclimate_hide_when_no_members_mapped(self) -> None:
        """monthly/ANNUAL/averageclimate hide when none of their members are mapped."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        # UV is in homepage but NOT in monthly/ANNUAL/averageclimate
        registry = _make_registry_with_mapped(["UV"])
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)

        group_ids = {g.group_id for g in groups}
        assert "monthly" not in group_ids, (
            "monthly must self-hide when none of its members are mapped"
        )
        assert "ANNUAL" not in group_ids, (
            "ANNUAL must self-hide when none of its members are mapped"
        )
        assert "averageclimate" not in group_ids, (
            "averageclimate must self-hide when none of its members are mapped"
        )

    def test_homepage_excluded_when_all_its_members_unmapped(self) -> None:
        """homepage is excluded when operator's archive has none of its members."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        # Map only fields that are NOT in homepage's member list
        registry = _make_registry_with_mapped(["inTemp", "inHumidity"])
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)

        homepage = next((g for g in groups if g.group_id == "homepage"), None)
        assert homepage is None, (
            "homepage must self-hide when none of its members are in the mapped set"
        )

    def test_full_registry_returns_all_4_groups(self) -> None:
        """When all needed members are mapped, all 4 groups are returned."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        all_needed = list(
            set(_HOMEPAGE_DEFAULTS) | set(_MONTHLY_DEFAULTS) |
            set(_ANNUAL_DEFAULTS) | set(_AVGCLIMATE_DEFAULTS)
        )
        registry = _make_registry_with_mapped(all_needed)
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)
        group_ids = {g.group_id for g in groups}
        assert group_ids == {"homepage", "monthly", "ANNUAL", "averageclimate"}, (
            f"Expected all 4 groups, got {group_ids!r}"
        )

    def test_pruned_members_list_contains_only_mapped_fields(self) -> None:
        """After prune, every member in a group's list is in the mapped registry."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        mapped = ["outTemp", "rain", "barometer"]
        registry = _make_registry_with_mapped(mapped)
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)

        for group in groups:
            for member in group.members:
                assert member in mapped, (
                    f"Group {group.group_id!r} member {member!r} is not in the mapped "
                    f"set {mapped!r} — prune failed"
                )

    def test_get_chart_groups_returns_chart_group_entry_objects(self) -> None:
        """get_chart_groups returns ChartGroupEntry dataclass instances."""
        from weewx_clearskies_api.services.charts import ChartGroupEntry, get_chart_groups

        registry = _make_registry_with_mapped(["outTemp", "rain"])
        self._wire_pruned_config(registry)
        groups = get_chart_groups(registry)
        for group in groups:
            assert isinstance(group, ChartGroupEntry), (
                f"Expected ChartGroupEntry, got {type(group).__name__}"
            )
