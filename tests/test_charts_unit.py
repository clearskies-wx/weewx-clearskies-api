"""Unit tests for chart-group self-prune logic (services/charts.py).

Covers per the 3a-2 brief:
  - Self-prune: homepage members shrink when ColumnRegistry lacks
    lightning_strike_count + pollutantPM25.
  - All groups self-hide when mapped set is empty → groups=[].
  - Built-in group constants: 4 groups with correct IDs, names, members.
  - Group with zero members after prune is omitted.

ADR references: ADR-024 (built-in chart groups and self-hide rule).
"""

from __future__ import annotations

from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry


# ---------------------------------------------------------------------------
# Helper
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
# Built-in group constants
# ---------------------------------------------------------------------------

_HOMEPAGE_DEFAULTS = [
    "outTemp", "dewpoint", "outHumidity", "windSpeed", "windGust", "windDir",
    "barometer", "rain", "rainRate", "radiation", "UV",
    "lightning_strike_count", "pollutantPM25",
]
_MONTHLY_DEFAULTS = ["outTemp", "rain", "windSpeed", "barometer"]
_ANNUAL_DEFAULTS = ["outTemp", "rain"]
_AVGCLIMATE_DEFAULTS = ["outTemp", "rain"]


class TestBuiltInChartGroupConstants:
    """_BUILTIN_GROUPS constant in services/charts.py matches the brief spec."""

    def test_four_built_in_groups_defined(self) -> None:
        """Exactly 4 built-in groups: homepage, monthly, ANNUAL, averageclimate."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        group_ids = {g.group_id for g in _BUILTIN_GROUPS}
        assert group_ids == {"homepage", "monthly", "ANNUAL", "averageclimate"}, (
            f"Expected 4 built-in group IDs, got {group_ids!r}"
        )

    def test_homepage_group_has_correct_default_members(self) -> None:
        """homepage group default members match brief spec."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        homepage = next(g for g in _BUILTIN_GROUPS if g.group_id == "homepage")
        assert set(homepage.members) == set(_HOMEPAGE_DEFAULTS), (
            f"homepage default members mismatch. "
            f"Extra: {set(homepage.members) - set(_HOMEPAGE_DEFAULTS)}. "
            f"Missing: {set(_HOMEPAGE_DEFAULTS) - set(homepage.members)}."
        )

    def test_homepage_default_range_is_1d(self) -> None:
        """homepage default_range is '1d'."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        homepage = next(g for g in _BUILTIN_GROUPS if g.group_id == "homepage")
        assert homepage.default_range == "1d", (
            f"homepage default_range must be '1d', got {homepage.default_range!r}"
        )

    def test_monthly_default_range_is_none(self) -> None:
        """monthly default_range is None (has its own month-dropdown selector)."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        monthly = next(g for g in _BUILTIN_GROUPS if g.group_id == "monthly")
        assert monthly.default_range is None, (
            f"monthly default_range must be None, got {monthly.default_range!r}"
        )

    def test_annual_default_range_is_none(self) -> None:
        """ANNUAL default_range is None (year dropdown)."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        annual = next(g for g in _BUILTIN_GROUPS if g.group_id == "ANNUAL")
        assert annual.default_range is None

    def test_all_built_in_groups_have_built_in_true(self) -> None:
        """All 4 built-in groups have built_in=True."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        for group in _BUILTIN_GROUPS:
            assert group.built_in is True, (
                f"Group {group.group_id!r} built_in must be True"
            )

    def test_monthly_default_members_match_brief(self) -> None:
        """monthly group default members: outTemp, rain, windSpeed, barometer."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        monthly = next(g for g in _BUILTIN_GROUPS if g.group_id == "monthly")
        assert set(monthly.members) == set(_MONTHLY_DEFAULTS), (
            f"monthly members mismatch: {set(monthly.members)} vs {set(_MONTHLY_DEFAULTS)}"
        )

    def test_annual_default_members_match_brief(self) -> None:
        """ANNUAL group default members: outTemp, rain."""
        from weewx_clearskies_api.services.charts import _BUILTIN_GROUPS  # type: ignore[attr-defined]
        annual = next(g for g in _BUILTIN_GROUPS if g.group_id == "ANNUAL")
        assert set(annual.members) == set(_ANNUAL_DEFAULTS)


# ---------------------------------------------------------------------------
# Self-prune logic
# ---------------------------------------------------------------------------


class TestChartGroupSelfPrune:
    """get_chart_groups prunes members against the mapped registry set."""

    def test_homepage_members_exclude_missing_lightning_and_pm25(self) -> None:
        """homepage.members drops lightning_strike_count + pollutantPM25 when not mapped."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        # Everything except lightning_strike_count and pollutantPM25
        mapped = [
            m for m in _HOMEPAGE_DEFAULTS
            if m not in {"lightning_strike_count", "pollutantPM25"}
        ]
        registry = _make_registry_with_mapped(mapped)
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
        groups = get_chart_groups(registry)

        homepage = next((g for g in groups if g.group_id == "homepage"), None)
        assert homepage is not None

        for field in ("outTemp", "rain", "windSpeed"):
            assert field in homepage.members, (
                f"Field {field!r} must remain in homepage.members after partial prune"
            )

    def test_empty_mapped_set_hides_all_groups(self) -> None:
        """When mapped set is empty, all groups self-hide → groups=[]."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        registry = _make_registry_with_mapped([])  # No columns mapped
        groups = get_chart_groups(registry)
        assert groups == [], (
            f"All groups must self-hide when mapped set is empty, got {groups!r}"
        )

    def test_monthly_annual_avgclimate_hide_when_no_members_mapped(self) -> None:
        """monthly/ANNUAL/averageclimate hide when none of their members are mapped."""
        from weewx_clearskies_api.services.charts import get_chart_groups

        # UV is in homepage but NOT in monthly/ANNUAL/averageclimate
        registry = _make_registry_with_mapped(["UV"])
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
        groups = get_chart_groups(registry)
        for group in groups:
            assert isinstance(group, ChartGroupEntry), (
                f"Expected ChartGroupEntry, got {type(group).__name__}"
            )
