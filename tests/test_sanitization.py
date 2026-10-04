"""Tests for filename and path sanitization (cross-platform filesystem safety)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lighthouse_cli.utils import _sanitize_filename

FORBIDDEN_CHARS = '\\/:*?"<>|'


class TestSanitizeFilename:
    """Tests for _sanitize_filename — used for course names, module paths, filenames."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(
                "Intro: CS *2025* / Section<1>", "Intro_ CS _2025_ _ Section_1_",
                id="forbidden-chars",
            ),
            pytest.param("L1%20Intro%20to%20CS", "L1 Intro to CS", id="percent-decoded"),
            pytest.param("..Secret", "Secret", id="leading-dots"),
            pytest.param(".Hidden", "Hidden", id="leading-dot"),
            pytest.param("Secret..", "Secret", id="trailing-dots"),
            pytest.param("Hidden.", "Hidden", id="trailing-dot"),
            pytest.param("  Physics", "Physics", id="leading-spaces"),
            pytest.param(" Physics ", "Physics", id="spaces-both-ends"),
            pytest.param("Physics  ", "Physics", id="trailing-spaces"),
            # URL-decode first, then replace forbidden, then strip dots/spaces.
            pytest.param("  ..L1%20Intro%20to%20CS..  ", "L1 Intro to CS", id="combined"),
            # Ampersand is NOT a forbidden char on most filesystems.
            pytest.param("Signals & Systems", "Signals & Systems", id="ampersand-kept"),
            pytest.param("", "", id="empty"),
            # A course name needing no change keeps its folder name; collisions
            # then append -{OrgUnitId} (e.g. "Physics-67890").
            pytest.param("Physics", "Physics", id="clean-course-name"),
        ],
    )
    def test_sanitizes_to_exact_name(self, raw: str, expected: str) -> None:
        result = _sanitize_filename(raw)
        assert result == expected
        assert "%20" not in result
        for ch in FORBIDDEN_CHARS:
            assert ch not in result

    def test_replaces_each_forbidden_char(self):
        for ch in FORBIDDEN_CHARS:
            result = _sanitize_filename(f"file{ch}name")
            assert ch not in result
            assert result == "file_name"

    def test_url_decodes_mixed_encoding(self):
        result = _sanitize_filename("Lecture%201.pdf")
        assert " " in result
        assert "%20" not in result

    def test_preserves_valid_characters(self):
        """Alphanumeric, spaces, hyphens, underscores, brackets are preserved."""
        result = _sanitize_filename("Lecture-1 (Chapter 2) [Extra].pdf")
        assert "Lecture-1" in result
        assert "(Chapter 2)" in result
        assert "[Extra]" in result

    @pytest.mark.parametrize(
        ("raw", "banned"),
        [
            # All replaced with _; strip(". ") removes dots and spaces but not underscores.
            pytest.param("///:**", "\\/:", id="only-forbidden-chars"),
            pytest.param("file\x00name\x1ftest", "\x00\x1f", id="control-chars"),
            pytest.param("Unit 1: Introduction <File>", "<>:", id="module-title"),
        ],
    )
    def test_removes_unsafe_chars(self, raw: str, banned: str) -> None:
        result = _sanitize_filename(raw)
        for ch in banned:
            assert ch not in result

    def test_nested_module_paths(self):
        """Nested modules produce nested sanitized paths."""
        path = Path(_sanitize_filename("Module A")) / _sanitize_filename("Sub: Module B")
        assert ":" not in str(path)
        assert "<" not in str(path)


class TestPathResolution:
    """Tests for -o / --output-dir path handling."""

    def test_relative_path_resolved_from_cwd(self):
        assert Path(os.getcwd()) / "test-output" == Path("test-output").resolve()

    def test_path_expanduser_called(self):
        """Path with ~ (as in -o ~/Downloads/test) is expanded to the home directory."""
        expanded = Path("~/test-lighthouse").expanduser()
        assert expanded.is_absolute()
        assert "~" not in str(expanded)
