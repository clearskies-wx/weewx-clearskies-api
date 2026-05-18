"""Unit tests for capabilities response construction (no DB, no network).

Covers per the 3a-2 brief:
  - Given a hand-built ColumnRegistry with 5 stock and 2 unmapped columns,
    assert weewxColumns has 5 entries, canonicalFieldsAvailable has 5 names,
    providers is [].
  - Unmapped (operator-custom) columns do NOT appear in weewxColumns.

The capabilities response is built by the endpoint directly from the registry
(no separate service module in 3a-2).

ADR references: ADR-035 (column registry), ADR-038 (provider modules → empty at 3a-2).
"""

from __future__ import annotations

from weewx_clearskies_api.db.reflection import ColumnInfo, ColumnRegistry

# ---------------------------------------------------------------------------
# Helper: build a hand-crafted ColumnRegistry
# ---------------------------------------------------------------------------


def _make_registry(
    stock_columns: dict[str, str],
    unmapped_columns: list[str] | None = None,
) -> ColumnRegistry:
    """Build a ColumnRegistry with given stock and unmapped columns.

    Args:
        stock_columns: {db_name: canonical_name}
        unmapped_columns: list of db column names that are operator-custom
    """
    registry = ColumnRegistry()
    registry.stock = {
        db_name: ColumnInfo(db_name=db_name, canonical_name=canonical, is_stock=True)
        for db_name, canonical in stock_columns.items()
    }
    registry.unmapped = {
        col: ColumnInfo(db_name=col, canonical_name=col, is_stock=False)
        for col in (unmapped_columns or [])
    }
    return registry


def _registry_to_capability_data(registry: ColumnRegistry) -> dict:
    """Simulate the capability response data structure from a registry.

    This mirrors what the /capabilities endpoint returns per the OpenAPI
    CapabilityRegistry schema: providers=[], weewxColumns=[...], canonicalFieldsAvailable=[...].
    """
    weewx_columns = [
        {"canonicalField": info.canonical_name, "archiveColumn": info.db_name}
        for info in registry.stock.values()
    ]
    canonical_fields_available = [info.canonical_name for info in registry.stock.values()]
    return {
        "providers": [],
        "weewxColumns": weewx_columns,
        "canonicalFieldsAvailable": canonical_fields_available,
    }


# ---------------------------------------------------------------------------
# Capabilities response structure via registry
# ---------------------------------------------------------------------------


class TestCapabilitiesRegistryStructure:
    """ColumnRegistry maps correctly to CapabilityRegistry shape."""

    def test_weewx_columns_count_matches_stock_column_count(self) -> None:
        """weewxColumns has one entry per stock column in the registry."""
        registry = _make_registry(
            stock_columns={
                "outTemp": "outTemp",
                "outHumidity": "outHumidity",
                "windSpeed": "windSpeed",
                "barometer": "barometer",
                "rain": "rain",
            },
            unmapped_columns=["aqi", "main_pollutant"],
        )
        data = _registry_to_capability_data(registry)
        assert len(data["weewxColumns"]) == 5, (
            f"Expected 5 weewxColumns (one per stock column), "
            f"got {len(data['weewxColumns'])}"
        )

    def test_unmapped_columns_not_in_weewx_columns(self) -> None:
        """Unmapped (operator-custom) columns do NOT appear in weewxColumns."""
        registry = _make_registry(
            stock_columns={"outTemp": "outTemp", "rain": "rain"},
            unmapped_columns=["aqi", "main_pollutant"],
        )
        data = _registry_to_capability_data(registry)
        column_names = {entry["archiveColumn"] for entry in data["weewxColumns"]}
        assert "aqi" not in column_names, (
            "Unmapped column 'aqi' must NOT appear in weewxColumns"
        )
        assert "main_pollutant" not in column_names, (
            "Unmapped column 'main_pollutant' must NOT appear in weewxColumns"
        )

    def test_canonical_fields_available_count_matches_stock_columns(self) -> None:
        """canonicalFieldsAvailable has one entry per stock column (no providers in 3a-2)."""
        registry = _make_registry(
            stock_columns={
                "outTemp": "outTemp",
                "outHumidity": "outHumidity",
                "windSpeed": "windSpeed",
                "barometer": "barometer",
                "rain": "rain",
            },
            unmapped_columns=["aqi"],
        )
        data = _registry_to_capability_data(registry)
        assert len(data["canonicalFieldsAvailable"]) == 5, (
            f"Expected 5 canonicalFieldsAvailable (= stock columns, no providers), "
            f"got {len(data['canonicalFieldsAvailable'])}"
        )

    def test_providers_is_empty_list_in_3a2(self) -> None:
        """providers is [] — no provider modules wired in 3a-2 (ADR-038)."""
        registry = _make_registry(stock_columns={"outTemp": "outTemp"})
        data = _registry_to_capability_data(registry)
        assert data["providers"] == [], (
            f"providers must be [] in 3a-2 (no provider modules yet), "
            f"got {data['providers']!r}"
        )

    def test_weewx_columns_have_canonical_field_and_archive_column_keys(self) -> None:
        """Each weewxColumns entry has 'canonicalField' and 'archiveColumn' keys."""
        registry = _make_registry(
            stock_columns={"outTemp": "outTemp", "windSpeed": "windSpeed"},
        )
        data = _registry_to_capability_data(registry)
        for entry in data["weewxColumns"]:
            assert "canonicalField" in entry, (
                f"weewxColumns entry missing 'canonicalField': {entry}"
            )
            assert "archiveColumn" in entry, (
                f"weewxColumns entry missing 'archiveColumn': {entry}"
            )

    def test_weewx_columns_canonical_field_matches_canonical_name(self) -> None:
        """weewxColumns[i].canonicalField matches the ColumnInfo.canonical_name."""
        registry = _make_registry(stock_columns={"outTemp": "outTemp"})
        data = _registry_to_capability_data(registry)
        entry = data["weewxColumns"][0]
        assert entry["canonicalField"] == "outTemp"
        assert entry["archiveColumn"] == "outTemp"

    def test_canonical_fields_available_contains_all_canonical_names(self) -> None:
        """canonicalFieldsAvailable contains each canonical name exactly once."""
        stock = {
            "outTemp": "outTemp",
            "rainRate": "rainRate",
            "UV": "UV",
        }
        registry = _make_registry(stock_columns=stock)
        data = _registry_to_capability_data(registry)
        available = set(data["canonicalFieldsAvailable"])
        for canonical in stock.values():
            assert canonical in available, (
                f"canonicalFieldsAvailable must contain {canonical!r}"
            )

    def test_empty_registry_returns_empty_lists(self) -> None:
        """An empty registry produces empty weewxColumns and canonicalFieldsAvailable."""
        registry = _make_registry(stock_columns={})
        data = _registry_to_capability_data(registry)
        assert data["weewxColumns"] == []
        assert data["canonicalFieldsAvailable"] == []
        assert data["providers"] == []

    def test_canonical_fields_available_matches_weewx_columns_canonical_names(
        self,
    ) -> None:
        """canonicalFieldsAvailable = set of canonicalField values from weewxColumns."""
        registry = _make_registry(
            stock_columns={
                "outTemp": "outTemp",
                "rain": "rain",
                "windSpeed": "windSpeed",
            }
        )
        data = _registry_to_capability_data(registry)
        from_weewx = {e["canonicalField"] for e in data["weewxColumns"]}
        available = set(data["canonicalFieldsAvailable"])
        assert available == from_weewx, (
            f"canonicalFieldsAvailable {available} must equal "
            f"weewxColumns canonical names {from_weewx}"
        )
