"""Unit tests for content endpoint path-traversal and missing-file handling.

Covers per the 3a-2 brief:
  - Path traversal via symlink pointing outside content_directory → returns None (→ 404).
  - Missing file → None returned (endpoint maps to 404).
  - File too large (> 1 MiB) → ValueError raised (endpoint maps to 500).
  - Normal read returns raw markdown content unchanged.
  - updatedAt is UTC ISO-8601 with Z suffix per ADR-020.

The content service uses a module-level `_content_dir` set by wire_content_directory().
Tests call wire_content_directory() to configure the dir, then call read_content_file().

ADR references: security-baseline §3.5 (path traversal defense), §5 (no server-side
markdown sanitization — raw passthrough only).
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helper to wire content dir for each test
# ---------------------------------------------------------------------------


def _wire_dir(directory: Path) -> None:
    from weewx_clearskies_api.services.content import wire_content_directory
    wire_content_directory(str(directory))


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


class TestContentPathTraversalGuard:
    """Path-traversal defense rejects symlinks that point outside content_directory."""

    def test_symlink_outside_content_dir_returns_none(
        self, tmp_path: Path
    ) -> None:
        """A symlink under content_dir pointing outside → read_content_file returns None."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        outside_file = tmp_path / "secret.md"
        outside_file.write_text("# Secret", encoding="utf-8")
        symlink = content_dir / "about.md"
        try:
            symlink.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            pytest.skip("OS does not support symlinks (Windows non-admin)")

        _wire_dir(content_dir)
        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is None, (
            "Symlink pointing outside content_dir must return None (→ 404, not 500)"
        )

    def test_symlink_to_parent_directory_returns_none(
        self, tmp_path: Path
    ) -> None:
        """A symlink pointing to the parent directory returns None."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        outside_file = tmp_path / "parent_secret.md"
        outside_file.write_text("# Parent Secret", encoding="utf-8")
        symlink = content_dir / "legal.md"
        try:
            symlink.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            pytest.skip("OS does not support symlinks (Windows non-admin)")

        _wire_dir(content_dir)
        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="legal.md")
        assert result is None, (
            "Symlink pointing outside content_dir must return None (→ 404)"
        )


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


class TestContentMissingFile:
    """Missing markdown file → None returned (maps to 404 in the endpoint)."""

    def test_missing_about_md_returns_none(self, tmp_path: Path) -> None:
        """about.md absent from content_dir → read_content_file returns None."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        # Do NOT create about.md
        _wire_dir(content_dir)
        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is None, (
            "Missing about.md must return None (maps to 404 in endpoint)"
        )

    def test_missing_legal_md_returns_none(self, tmp_path: Path) -> None:
        """legal.md absent from content_dir → read_content_file returns None."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        _wire_dir(content_dir)
        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="legal.md")
        assert result is None


# ---------------------------------------------------------------------------
# Normal read
# ---------------------------------------------------------------------------


class TestContentNormalRead:
    """Normal content file read returns raw markdown unchanged."""

    def test_about_md_content_returned_unchanged(self, tmp_path: Path) -> None:
        """about.md raw text is returned as-is (no sanitization per security-baseline §5)."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        about_text = "# About\n\nThis is the about page.\n"
        (content_dir / "about.md").write_text(about_text, encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is not None
        assert result.markdown == about_text, (
            "Content must be returned raw (no sanitization)"
        )

    def test_about_md_with_html_returned_unchanged(self, tmp_path: Path) -> None:
        """Markdown with HTML tags passes through unchanged — sanitization is dashboard-side.

        Per security-baseline §5: api returns whatever bytes are in the file.
        The dashboard's react-markdown pipeline does the sanitization.
        """
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        raw_content = "# About\n\n<script>alert(1)</script>\n"
        (content_dir / "about.md").write_text(raw_content, encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is not None
        # The raw content must be returned unchanged — sanitization is NOT the api's job
        assert result.markdown == raw_content, (
            "Content endpoint must NOT sanitize markdown — raw passthrough only "
            "(sanitization is the dashboard's responsibility per security-baseline §5)"
        )

    def test_updated_at_is_non_null_for_existing_file(self, tmp_path: Path) -> None:
        """updated_at is non-null when mtime is readable."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "about.md").write_text("# About\n", encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is not None
        assert result.updated_at is not None, (
            "updated_at must be non-null when mtime is readable"
        )

    def test_updated_at_is_utc_z_formatted(self, tmp_path: Path) -> None:
        """updated_at is UTC ISO-8601 with Z suffix (ADR-020)."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "about.md").write_text("# About\n", encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="about.md")
        assert result is not None
        if result.updated_at is not None:
            assert result.updated_at.endswith("Z"), (
                f"updated_at must end with Z per ADR-020, got {result.updated_at!r}"
            )

    def test_result_is_content_file_dataclass(self, tmp_path: Path) -> None:
        """read_content_file returns a ContentFile dataclass instance."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "about.md").write_text("# About\n", encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import ContentFile, read_content_file
        result = read_content_file(filename="about.md")
        assert isinstance(result, ContentFile), (
            f"Expected ContentFile, got {type(result).__name__}"
        )


# ---------------------------------------------------------------------------
# File size limit
# ---------------------------------------------------------------------------


class TestContentFileSizeLimit:
    """File > 1 MiB → ValueError raised (maps to 500 in the endpoint)."""

    def test_file_exceeding_1_mib_raises_value_error(
        self, tmp_path: Path
    ) -> None:
        """A file larger than 1 MiB raises ValueError."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        large_file = content_dir / "about.md"
        # Write slightly more than 1 MiB (1_048_576 bytes)
        large_file.write_bytes(b"a" * (1_048_576 + 1))
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        with pytest.raises(ValueError):
            read_content_file(filename="about.md")

    def test_file_at_exactly_1_mib_boundary_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """A file at exactly 1 MiB (1_048_576 bytes) is accepted (limit is >1 MiB)."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        boundary_file = content_dir / "about.md"
        boundary_file.write_bytes(b"x" * 1_048_576)
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        # Should NOT raise at exactly the limit
        result = read_content_file(filename="about.md")
        assert result is not None
        assert result.markdown is not None


# ---------------------------------------------------------------------------
# Invalid filename
# ---------------------------------------------------------------------------


class TestContentInvalidFilename:
    """Invalid filename (not about.md or legal.md) returns None."""

    def test_invalid_filename_returns_none(self, tmp_path: Path) -> None:
        """Filename not in _VALID_FILENAMES (about.md, legal.md) → None."""
        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "etc_passwd").write_text("root:x:0:0:", encoding="utf-8")
        _wire_dir(content_dir)

        from weewx_clearskies_api.services.content import read_content_file
        result = read_content_file(filename="etc_passwd")
        assert result is None, (
            "Filename not in _VALID_FILENAMES must return None (→ 404)"
        )
