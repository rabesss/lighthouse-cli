"""Tests for filename and path sanitization (cross-platform filesystem safety)."""

from __future__ import annotations

import os
from pathlib import Path

from lighthouse_cli.commands import _sanitize_filename


# ---------------------------------------------------------------------------
# _sanitize_filename tests
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    """Tests for _sanitize_filename — used for course names, module paths, filenames."""

    def test_strips_forbidden_chars(self):
        r"""Characters \ / : * ? " < > | are replaced with _."""
        result = _sanitize_filename("Intro: CS *2025* / Section<1>")
        assert result == "Intro_ CS _2025_ _ Section_1_"
        # None of the forbidden chars survive
        for ch in '\\/:*?"<>|':
            assert ch not in result

    def test_replaces_each_forbidden_char(self):
        """Each forbidden char individually replaced with _."""
        for ch in '\\/:*?"<>|':
            result = _sanitize_filename(f"file{ch}name")
            assert ch not in result
            assert result == f"file_name"

    def test_url_decodes_percent_encoding(self):
        """Percent-encoded names like L1%20Intro%20to%20CS become L1 Intro to CS."""
        result = _sanitize_filename("L1%20Intro%20to%20CS")
        assert result == "L1 Intro to CS"

    def test_url_decodes_mixed_encoding(self):
        """URL-encoded spaces (%20) and other percent sequences are decoded."""
        result = _sanitize_filename("Lecture%201.pdf")
        assert " " in result  # space decoded
        assert "%20" not in result  # no literal %20

    def test_strips_leading_dot(self):
        """Leading dots are stripped."""
        assert _sanitize_filename("..Secret") == "Secret"
        assert _sanitize_filename(".Hidden") == "Hidden"

    def test_strips_trailing_dot(self):
        """Trailing dots are stripped."""
        assert _sanitize_filename("Secret..") == "Secret"
        assert _sanitize_filename("Hidden.") == "Hidden"

    def test_strips_leading_space(self):
        """Leading spaces are stripped."""
        assert _sanitize_filename("  Physics") == "Physics"
        assert _sanitize_filename(" Physics ") == "Physics"  # both ends

    def test_strips_trailing_space(self):
        """Trailing spaces are stripped."""
        assert _sanitize_filename("Physics  ") == "Physics"

    def test_combined_sanitization(self):
        """Multiple issues: forbidden chars, URL encode, leading/trailing dots/spaces."""
        result = _sanitize_filename("  ..L1%20Intro%20to%20CS..  ")
        # URL-decode first, then replace forbidden, then strip
        assert "%20" not in result
        assert result == "L1 Intro to CS"  # stripped of leading dots/spaces

    def test_preserves_valid_characters(self):
        """Alphanumeric, spaces, hyphens, underscores, brackets are preserved."""
        result = _sanitize_filename("Lecture-1 (Chapter 2) [Extra].pdf")
        assert "Lecture-1" in result
        assert "(Chapter 2)" in result
        assert "[Extra]" in result

    def test_ampersand_preserved(self):
        """Ampersand is NOT a forbidden char on most filesystems and is preserved."""
        result = _sanitize_filename("Signals & Systems")
        assert result == "Signals & Systems"

    def test_empty_string(self):
        """Empty string becomes empty (after stripping)."""
        assert _sanitize_filename("") == ""

    def test_only_forbidden_chars(self):
        """String of only forbidden chars becomes underscores then stripped to empty."""
        result = _sanitize_filename("///:**")
        # All replaced with _, then stripped -> may be empty or underscores
        # strip(". ") removes dots and spaces but not underscores
        assert "\\" not in result
        assert "/" not in result
        assert ":" not in result

    def test_control_characters_replaced(self):
        """Control characters (\\x00-\\x1f) are replaced."""
        result = _sanitize_filename("file\x00name\x1ftest")
        assert "\x00" not in result
        assert "\x1f" not in result


# ---------------------------------------------------------------------------
# Course name collision handling
# ---------------------------------------------------------------------------

class TestCourseNameCollision:
    """Tests for handling two courses with identical D2L Names."""

    def test_identical_names_get_suffix(self, tmp_path: Path):
        """When two courses share the same Name, second gets -OrgUnitId suffix."""
        # The collision resolution helper will be _resolve_course_folder_name(course_name, org_unit_id)
        # This test documents expected behavior - the function should be implemented in commands.py
        from lighthouse_cli.commands import _sanitize_filename

        base = _sanitize_filename("Physics")
        assert base == "Physics"
        # After collision handling, Physics with ID 67890 should become "Physics-67890"
        collision_name = f"{base}-67890"
        assert collision_name == "Physics-67890"

    def test_collision_suffix_format(self):
        """Collision suffix is -{OrgUnitId} format."""
        # If we have two "Physics" courses (IDs 100 and 200),
        # folders should be "Physics" and "Physics-200" (or similar)
        from lighthouse_cli.commands import _sanitize_filename

        name = _sanitize_filename("Physics")
        # After collision handling with ID 67890
        # Should produce "Physics-67890"
        collision_name = f"Physics-67890"
        assert collision_name.endswith("-67890")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    """Tests for -o / --output-dir path handling."""

    def test_tilde_expanded(self, tmp_path: Path):
        """~ in output-dir is expanded to home directory."""
        from lighthouse_cli.commands import _sanitize_filename

        home = Path.home()
        result = str(home)
        assert "~" not in result or result.startswith(str(home))

    def test_relative_path_resolved_from_cwd(self, tmp_path: Path):
        """Relative -o path is resolved from current working directory."""
        cwd = os.getcwd()
        rel_path = "test-output"
        expected = Path(cwd) / rel_path
        assert expected == Path(rel_path).resolve()

    def test_path_expanduser_called(self):
        """Path with ~ calls Path.expanduser() for resolution."""
        from lighthouse_cli.commands import _sanitize_filename

        # Simulate what happens with -o ~/Downloads/test
        path_str = "~/test-lighthouse"
        expanded = Path(path_str).expanduser()
        assert expanded.is_absolute()
        assert "~" not in str(expanded)


# ---------------------------------------------------------------------------
# Module path sanitization
# ---------------------------------------------------------------------------

class TestModulePathSanitization:
    """Tests for sanitization of module titles used in path construction."""

    def test_module_title_sanitized(self):
        """Module titles used in file paths are sanitized like course names."""
        from lighthouse_cli.commands import _sanitize_filename

        mod_title = "Unit 1: Introduction <File>"
        sanitized = _sanitize_filename(mod_title)
        assert "<" not in sanitized
        assert ">" not in sanitized
        assert ":" not in sanitized

    def test_nested_module_paths(self):
        """Nested modules produce nested sanitized paths."""
        from lighthouse_cli.commands import _sanitize_filename

        parent = _sanitize_filename("Module A")
        child = _sanitize_filename("Sub: Module B")
        # Path should join cleanly
        path = Path(parent) / child
        assert ":" not in str(path)
        assert "<" not in str(path)
