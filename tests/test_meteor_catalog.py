"""Unit tests for meteor shower catalog loading — T1.10.

Tests load_catalog() from weewx_clearskies_api/data/meteor_showers.py.

Coverage:
  - load_catalog() with a valid JSON file returns correct number of
    MeteorShowerData instances.
  - load_catalog() with a missing file falls back to the embedded
    METEOR_SHOWERS list (12 showers).
  - load_catalog() with malformed entries skips them and returns valid ones.
  - load_catalog() with completely invalid JSON falls back to the embedded list.
  - New extended fields (id, description, velocity_kms, solar_longitude_max,
    image) are populated from JSON when present.

No DB, no network. Uses tmp_path (pytest built-in) for temporary JSON files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_entry(**overrides: object) -> dict:
    """Return a complete valid MeteorShowerData JSON entry dict."""
    base: dict = {
        "name": "Perseids",
        "peak_month": 8,
        "peak_day": 12,
        "duration_days": 14,
        "radiant_ra_deg": 48.0,
        "radiant_dec_deg": 58.0,
        "zhr": 100,
        "parent_body": "109P/Swift-Tuttle",
        "id": "perseids",
        "description": "The most reliable major shower.",
        "velocity_kms": 59.0,
        "solar_longitude_max": 140.0,
        "image": "perseids.jpg",
    }
    base.update(overrides)
    return base


def _write_catalog(tmp_path: Path, entries: list) -> str:
    """Write entries as JSON to a temp file and return the path string."""
    catalog_file = tmp_path / "test_meteor_showers.json"
    catalog_file.write_text(json.dumps(entries), encoding="utf-8")
    return str(catalog_file)


# ===========================================================================
# 1. Valid JSON file
# ===========================================================================


class TestLoadCatalogValidFile:
    """load_catalog() with a valid JSON file returns the correct instances."""

    def test_returns_list_of_meteor_shower_data_instances(self, tmp_path: Path) -> None:
        """load_catalog() returns a list of MeteorShowerData objects."""
        from weewx_clearskies_api.data.meteor_showers import MeteorShowerData, load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry()])
        results = load_catalog(catalog_path)

        assert isinstance(results, list)
        assert all(isinstance(r, MeteorShowerData) for r in results), (
            "All returned items must be MeteorShowerData instances"
        )

    def test_returns_correct_count_for_one_entry(self, tmp_path: Path) -> None:
        """A catalog with one valid entry returns exactly 1 result."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry()])
        results = load_catalog(catalog_path)
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"

    def test_returns_correct_count_for_multiple_entries(self, tmp_path: Path) -> None:
        """A catalog with 3 entries returns exactly 3 results."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        entries = [
            _valid_entry(name="Alpha", peak_month=1, peak_day=3),
            _valid_entry(name="Beta", peak_month=4, peak_day=22),
            _valid_entry(name="Gamma", peak_month=8, peak_day=12),
        ]
        catalog_path = _write_catalog(tmp_path, entries)
        results = load_catalog(catalog_path)
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    def test_name_field_populated(self, tmp_path: Path) -> None:
        """Parsed MeteorShowerData has the correct 'name' value."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(name="Leonids")])
        results = load_catalog(catalog_path)
        assert results[0].name == "Leonids"

    def test_zhr_field_populated(self, tmp_path: Path) -> None:
        """Parsed MeteorShowerData has the correct 'zhr' value."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(zhr=150)])
        results = load_catalog(catalog_path)
        assert results[0].zhr == 150

    def test_parent_body_field_populated(self, tmp_path: Path) -> None:
        """Parsed MeteorShowerData has the correct 'parent_body' value."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(parent_body="1P/Halley")])
        results = load_catalog(catalog_path)
        assert results[0].parent_body == "1P/Halley"


# ===========================================================================
# 2. Extended fields (id, description, velocity_kms, solar_longitude_max, image)
# ===========================================================================


class TestLoadCatalogExtendedFields:
    """New optional fields are populated from JSON when present."""

    def test_id_field_populated(self, tmp_path: Path) -> None:
        """'id' field is populated from JSON entry."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(id="perseids")])
        results = load_catalog(catalog_path)
        assert results[0].id == "perseids"

    def test_description_field_populated(self, tmp_path: Path) -> None:
        """'description' field is populated from JSON entry."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(
            tmp_path,
            [_valid_entry(description="Best summer shower.")]
        )
        results = load_catalog(catalog_path)
        assert results[0].description == "Best summer shower."

    def test_velocity_kms_field_populated(self, tmp_path: Path) -> None:
        """'velocity_kms' field is populated from JSON entry."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(velocity_kms=59.0)])
        results = load_catalog(catalog_path)
        assert results[0].velocity_kms == pytest.approx(59.0)

    def test_solar_longitude_max_field_populated(self, tmp_path: Path) -> None:
        """'solar_longitude_max' field is populated from JSON entry."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(solar_longitude_max=140.0)])
        results = load_catalog(catalog_path)
        assert results[0].solar_longitude_max == pytest.approx(140.0)

    def test_image_field_populated(self, tmp_path: Path) -> None:
        """'image' field is populated from JSON entry."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(tmp_path, [_valid_entry(image="perseids.jpg")])
        results = load_catalog(catalog_path)
        assert results[0].image == "perseids.jpg"

    def test_optional_fields_default_when_absent(self, tmp_path: Path) -> None:
        """When optional fields are absent from JSON, they default to empty/zero."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        entry = {
            "name": "MinimalShower",
            "peak_month": 3,
            "peak_day": 15,
            "duration_days": 5,
            "radiant_ra_deg": 100.0,
            "radiant_dec_deg": 20.0,
            "zhr": 10,
            "parent_body": "Unknown",
            # No id, description, velocity_kms, solar_longitude_max, image
        }
        catalog_path = _write_catalog(tmp_path, [entry])
        results = load_catalog(catalog_path)

        assert results[0].id == ""
        assert results[0].description == ""
        assert results[0].velocity_kms == pytest.approx(0.0)
        assert results[0].solar_longitude_max == pytest.approx(0.0)
        assert results[0].image == ""


# ===========================================================================
# 3. Missing file → fallback to embedded list
# ===========================================================================


class TestLoadCatalogMissingFile:
    """load_catalog() with a non-existent file falls back to METEOR_SHOWERS."""

    def test_missing_file_returns_embedded_fallback(self) -> None:
        """load_catalog() with a path that does not exist returns embedded list."""
        from weewx_clearskies_api.data.meteor_showers import METEOR_SHOWERS, load_catalog

        result = load_catalog("/nonexistent/path/that/does/not/exist.json")
        assert result == list(METEOR_SHOWERS), (
            "Missing file must return the embedded METEOR_SHOWERS list"
        )

    def test_missing_file_returns_12_showers(self) -> None:
        """Fallback embedded list contains exactly 12 showers."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        result = load_catalog("/nonexistent/path/that/does/not/exist.json")
        assert len(result) == 12, (
            f"Embedded fallback must have 12 showers, got {len(result)}"
        )

    def test_missing_file_does_not_raise(self) -> None:
        """load_catalog() with a missing file never raises — returns fallback."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        # Must not raise:
        result = load_catalog("/nonexistent/path/that/does/not/exist.json")
        assert isinstance(result, list)


# ===========================================================================
# 4. Malformed entries skipped, valid ones kept
# ===========================================================================


class TestLoadCatalogMalformedEntries:
    """load_catalog() skips malformed entries and returns the valid ones."""

    def test_entry_missing_required_field_is_skipped(self, tmp_path: Path) -> None:
        """An entry missing a required field is skipped; valid entries are kept."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        bad_entry = {
            # Missing 'zhr' — required
            "name": "BadShower",
            "peak_month": 5,
            "peak_day": 10,
            "duration_days": 3,
            "radiant_ra_deg": 100.0,
            "radiant_dec_deg": 20.0,
            # zhr missing
            "parent_body": "Unknown",
        }
        good_entry = _valid_entry(name="GoodShower")
        catalog_path = _write_catalog(tmp_path, [bad_entry, good_entry])
        results = load_catalog(catalog_path)

        assert len(results) == 1, (
            f"Expected 1 valid result after skipping bad entry, got {len(results)}"
        )
        assert results[0].name == "GoodShower"

    def test_entry_with_wrong_type_for_required_field_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """An entry with non-numeric zhr (can't be converted) is skipped."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        bad_entry = _valid_entry(name="TypeErrorShower", zhr="not_a_number")
        good_entry = _valid_entry(name="GoodShower", peak_month=4, peak_day=22)
        catalog_path = _write_catalog(tmp_path, [bad_entry, good_entry])
        results = load_catalog(catalog_path)

        assert len(results) == 1
        assert results[0].name == "GoodShower"

    def test_non_dict_entry_is_skipped(self, tmp_path: Path) -> None:
        """A non-dict entry (e.g., a string) in the array is skipped."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_path = _write_catalog(
            tmp_path,
            ["not_a_dict", _valid_entry(name="GoodShower")]
        )
        results = load_catalog(catalog_path)

        assert len(results) == 1
        assert results[0].name == "GoodShower"

    def test_all_bad_entries_falls_back_to_embedded_list(self, tmp_path: Path) -> None:
        """When all entries are malformed and no valid entries survive, return embedded fallback."""
        from weewx_clearskies_api.data.meteor_showers import METEOR_SHOWERS, load_catalog

        # All entries missing the required 'name' field
        bad_entries = [
            {"peak_month": 1, "peak_day": 3, "duration_days": 4,
             "radiant_ra_deg": 230.1, "radiant_dec_deg": 48.5,
             "zhr": 120, "parent_body": "2003 EH1"},
        ]
        catalog_path = _write_catalog(tmp_path, bad_entries)
        results = load_catalog(catalog_path)

        # Must fall back to embedded list
        assert results == list(METEOR_SHOWERS), (
            "All-bad-entries catalog must fall back to embedded METEOR_SHOWERS"
        )

    def test_mixed_good_and_bad_entries_returns_only_good(self, tmp_path: Path) -> None:
        """A mix of valid and invalid entries returns only the valid ones."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        entries = [
            _valid_entry(name="First", peak_month=1, peak_day=3),
            {"not": "a shower"},  # missing all required fields
            _valid_entry(name="Second", peak_month=8, peak_day=12),
            "string_entry",  # non-dict
            _valid_entry(name="Third", peak_month=12, peak_day=14),
        ]
        catalog_path = _write_catalog(tmp_path, entries)
        results = load_catalog(catalog_path)

        assert len(results) == 3
        names = {r.name for r in results}
        assert names == {"First", "Second", "Third"}


# ===========================================================================
# 5. Completely invalid JSON
# ===========================================================================


class TestLoadCatalogInvalidJson:
    """load_catalog() with invalid JSON falls back to embedded METEOR_SHOWERS."""

    def test_invalid_json_returns_embedded_fallback(self, tmp_path: Path) -> None:
        """A file with invalid JSON returns the embedded METEOR_SHOWERS list."""
        from weewx_clearskies_api.data.meteor_showers import METEOR_SHOWERS, load_catalog

        catalog_file = tmp_path / "bad.json"
        catalog_file.write_text("{this is not valid json [[[", encoding="utf-8")
        results = load_catalog(str(catalog_file))

        assert results == list(METEOR_SHOWERS), (
            "Invalid JSON must fall back to embedded METEOR_SHOWERS"
        )

    def test_invalid_json_returns_12_showers(self, tmp_path: Path) -> None:
        """Invalid JSON fallback returns exactly 12 showers."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_file = tmp_path / "bad.json"
        catalog_file.write_text("NOT JSON", encoding="utf-8")
        results = load_catalog(str(catalog_file))

        assert len(results) == 12

    def test_invalid_json_does_not_raise(self, tmp_path: Path) -> None:
        """load_catalog() with invalid JSON never raises."""
        from weewx_clearskies_api.data.meteor_showers import load_catalog

        catalog_file = tmp_path / "bad.json"
        catalog_file.write_text("}", encoding="utf-8")
        # Must not raise:
        result = load_catalog(str(catalog_file))
        assert isinstance(result, list)

    def test_json_object_not_array_falls_back_to_embedded(self, tmp_path: Path) -> None:
        """A JSON object (not array) at top level falls back to embedded list."""
        from weewx_clearskies_api.data.meteor_showers import METEOR_SHOWERS, load_catalog

        catalog_file = tmp_path / "object.json"
        catalog_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        results = load_catalog(str(catalog_file))

        assert results == list(METEOR_SHOWERS), (
            "JSON object (not array) must fall back to embedded METEOR_SHOWERS"
        )

    def test_empty_json_array_falls_back_to_embedded(self, tmp_path: Path) -> None:
        """An empty JSON array [] (no entries) falls back to embedded list."""
        from weewx_clearskies_api.data.meteor_showers import METEOR_SHOWERS, load_catalog

        catalog_file = tmp_path / "empty.json"
        catalog_file.write_text("[]", encoding="utf-8")
        results = load_catalog(str(catalog_file))

        # Empty array yields no valid entries → fallback
        assert results == list(METEOR_SHOWERS), (
            "Empty array must fall back to embedded METEOR_SHOWERS (no valid entries)"
        )
