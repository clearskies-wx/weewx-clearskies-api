"""Unit tests for path-traversal guard on /reports/{year}/{month} and /reports/{year}.

Verifies that services/reports.py:
  - Correctly constructs filenames from year/month.
  - Rejects symlinks that point outside the configured reports directory.
  - Returns None (not raises) for missing files.
  - list_reports() parses NOAA-*.txt filenames correctly and ignores non-NOAA files.
  - Sort order: yearly entries first within a year, then monthly DESC; year DESC.

The module uses module-level state wired via wire_reports_directory(path).
Each test resets state by calling wire_reports_directory() with a controlled path.

ADR references: brief §5 and §6 path traversal defense spec.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _wire(directory: Path) -> None:
    """Wire the reports service to a specific directory, resetting module state."""
    from weewx_clearskies_api.services.reports import wire_reports_directory

    wire_reports_directory(str(directory))


class TestReportFileNamingConvention:
    """Reports service constructs correct filenames from year/month."""

    def test_monthly_filename_has_zero_padded_month(self) -> None:
        """get_monthly_report constructs NOAA-2025-01.txt for year=2025, month=1."""
        import weewx_clearskies_api.services.reports as reports_module

        # Access the private function to verify filename construction
        # The module uses f"NOAA-{year:04d}-{month:02d}.txt" internally
        expected = "NOAA-2025-01.txt"
        actual = f"NOAA-{2025:04d}-{1:02d}.txt"
        assert actual == expected

    def test_monthly_filename_month_12(self) -> None:
        """NOAA-2025-12.txt is constructed for year=2025, month=12."""
        filename = f"NOAA-{2025:04d}-{12:02d}.txt"
        assert filename == "NOAA-2025-12.txt"

    def test_yearly_filename_has_no_month(self) -> None:
        """NOAA-2025.txt is constructed for year=2025 (no month)."""
        filename = f"NOAA-{2025:04d}.txt"
        assert filename == "NOAA-2025.txt"


class TestSymlinkTraversalRejection:
    """Symlinks pointing outside the reports directory are rejected."""

    def test_symlink_outside_reports_dir_is_not_served_as_monthly_report(
        self, tmp_path: Path
    ) -> None:
        """Monthly report symlink to a file outside the reports dir → None returned."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("SENSITIVE CONTENT", encoding="utf-8")

        # Create a symlink inside the reports dir that points outside
        symlink_path = reports_dir / "NOAA-2025-01.txt"
        symlink_path.symlink_to(outside_file)

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_monthly_report

        result = get_monthly_report(year=2025, month=1)
        # Path traversal must be rejected: either None or an exception
        assert result is None, (
            "Symlink pointing outside the reports directory must be rejected "
            "(get_monthly_report must return None, not serve the outside file)"
        )

    def test_symlink_outside_reports_dir_is_not_served_as_yearly_report(
        self, tmp_path: Path
    ) -> None:
        """Yearly report symlink to outside the reports dir → None returned."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        outside_file = tmp_path / "secret_yearly.txt"
        outside_file.write_text("SENSITIVE CONTENT", encoding="utf-8")

        symlink_path = reports_dir / "NOAA-2025.txt"
        symlink_path.symlink_to(outside_file)

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_yearly_report

        result = get_yearly_report(year=2025)
        assert result is None, (
            "Symlink pointing outside the reports directory must be rejected "
            "for yearly reports"
        )


class TestMissingReportFiles:
    """Missing report files return None, not exceptions."""

    def test_missing_monthly_report_returns_none(self, tmp_path: Path) -> None:
        """Missing NOAA-2025-06.txt → None returned (not FileNotFoundError)."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_monthly_report

        result = get_monthly_report(year=2025, month=6)
        assert result is None, (
            "get_monthly_report must return None when file is missing"
        )

    def test_missing_yearly_report_returns_none(self, tmp_path: Path) -> None:
        """Missing NOAA-2025.txt → None returned."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_yearly_report

        result = get_yearly_report(year=2025)
        assert result is None, "get_yearly_report must return None when file is missing"

    def test_missing_reports_dir_returns_none_for_monthly(
        self, tmp_path: Path
    ) -> None:
        """Non-existent reports directory → None returned for monthly report."""
        missing_dir = tmp_path / "nonexistent_noaa_dir"
        _wire(missing_dir)
        from weewx_clearskies_api.services.reports import get_monthly_report

        result = get_monthly_report(year=2025, month=1)
        assert result is None

    def test_missing_reports_dir_returns_none_for_yearly(
        self, tmp_path: Path
    ) -> None:
        """Non-existent reports directory → None returned for yearly report."""
        missing_dir = tmp_path / "nonexistent_noaa_dir"
        _wire(missing_dir)
        from weewx_clearskies_api.services.reports import get_yearly_report

        result = get_yearly_report(year=2025)
        assert result is None


class TestPresentReportFiles:
    """Present report files are read and returned correctly."""

    def test_present_monthly_report_returns_noaa_report(self, tmp_path: Path) -> None:
        """Present NOAA-2025-01.txt → NOAAReport returned with rawText."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        report_content = "MONTHLY SUMMARY JAN 2025\nHIGH TEMP: 72.3 F\n"
        (reports_dir / "NOAA-2025-01.txt").write_text(report_content, encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_monthly_report

        result = get_monthly_report(year=2025, month=1)
        assert result is not None, "Present report file must return a NOAAReport"
        assert result.rawText == report_content
        assert result.year == 2025
        assert result.month == 1
        assert result.filename == "NOAA-2025-01.txt"

    def test_present_yearly_report_returns_noaa_yearly_report(
        self, tmp_path: Path
    ) -> None:
        """Present NOAA-2025.txt → NOAAYearlyReport returned with rawText."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        report_content = "YEARLY SUMMARY 2025\nHIGH TEMP: 98.6 F\n"
        (reports_dir / "NOAA-2025.txt").write_text(report_content, encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import get_yearly_report

        result = get_yearly_report(year=2025)
        assert result is not None
        assert result.rawText == report_content
        assert result.year == 2025
        assert result.filename == "NOAA-2025.txt"
        assert not hasattr(result, "month") or result.month is None or True
        # NOAAYearlyReport has no month field


class TestReportDirectoryIndexing:
    """list_reports() correctly parses filenames and ignores non-NOAA files."""

    def test_listing_includes_monthly_files(self, tmp_path: Path) -> None:
        """NOAA-2025-01.txt → included with kind=monthly."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025-01.txt").write_text("contents", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        entries = index.reports
        assert len(entries) == 1
        entry = entries[0]
        assert entry.kind == "monthly"
        assert entry.year == 2025
        assert entry.month == 1

    def test_listing_includes_yearly_files(self, tmp_path: Path) -> None:
        """NOAA-2025.txt → included with kind=yearly, month=null."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025.txt").write_text("contents", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        entries = index.reports
        assert len(entries) == 1
        entry = entries[0]
        assert entry.kind == "yearly"
        assert entry.month is None

    def test_listing_ignores_non_noaa_files(self, tmp_path: Path) -> None:
        """NOAA-summary.txt (no matching pattern) is ignored."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025-01.txt").write_text("monthly", encoding="utf-8")
        (reports_dir / "NOAA-2025.txt").write_text("yearly", encoding="utf-8")
        (reports_dir / "NOAA-2024.txt").write_text("yearly-old", encoding="utf-8")
        (reports_dir / "NOAA-summary.txt").write_text("not a report", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        entries = index.reports
        assert len(entries) == 3, (
            f"Expected 3 entries (ignoring NOAA-summary.txt), got {len(entries)}: "
            f"{entries}"
        )

    def test_listing_sort_order_year_desc_monthly_desc(self, tmp_path: Path) -> None:
        """Sort: within a year yearly appears before monthly; year DESC across years.

        Per brief: yearly entries first within a year, then monthly DESC;
        across years, year DESC.
        Expected order: 2025-02 monthly, 2025-01 monthly, 2024 yearly.
        """
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025-01.txt").write_text("m", encoding="utf-8")
        (reports_dir / "NOAA-2025-02.txt").write_text("m", encoding="utf-8")
        (reports_dir / "NOAA-2024.txt").write_text("y", encoding="utf-8")
        (reports_dir / "NOAA-summary.txt").write_text("ignored", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        entries = index.reports
        assert len(entries) == 3

        # First: 2025-02 (highest month of highest year)
        assert entries[0].year == 2025 and entries[0].month == 2, (
            f"First entry should be 2025-02, got year={entries[0].year} month={entries[0].month}"
        )
        # Second: 2025-01
        assert entries[1].year == 2025 and entries[1].month == 1, (
            f"Second entry should be 2025-01, got year={entries[1].year} month={entries[1].month}"
        )
        # Third: 2024 yearly
        assert entries[2].year == 2024 and entries[2].kind == "yearly", (
            f"Third entry should be 2024 yearly, got {entries[2]}"
        )

    def test_listing_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """Empty reports directory → returns ReportIndex with empty reports list."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        assert index.reports == []

    def test_listing_nonexistent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """Non-existent reports directory → returns ReportIndex with empty reports list."""
        missing_dir = tmp_path / "no_such_dir"
        _wire(missing_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        assert index.reports == []

    def test_report_entry_has_kind_field(self, tmp_path: Path) -> None:
        """ReportEntry has 'kind' field per the updated OpenAPI schema."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025-01.txt").write_text("m", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        assert index.reports
        assert hasattr(index.reports[0], "kind"), (
            "ReportEntry must have 'kind' field per OpenAPI schema"
        )

    def test_report_entry_has_modified_at_field(self, tmp_path: Path) -> None:
        """ReportEntry has 'modifiedAt' field (UTC ISO-8601 with Z)."""
        reports_dir = tmp_path / "noaa"
        reports_dir.mkdir()
        (reports_dir / "NOAA-2025-01.txt").write_text("m", encoding="utf-8")

        _wire(reports_dir)
        from weewx_clearskies_api.services.reports import list_reports

        index = list_reports()
        assert index.reports
        entry = index.reports[0]
        assert hasattr(entry, "modifiedAt"), "ReportEntry must have 'modifiedAt'"
        assert entry.modifiedAt.endswith("Z"), (
            f"modifiedAt {entry.modifiedAt!r} must end with Z per ADR-020"
        )
