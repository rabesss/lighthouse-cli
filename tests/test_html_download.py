"""Tests for HTML topic download — verifying real (non-mocked) get_topic_html works.

These tests verify that the HTML download pipeline end-to-end works correctly
without mocking `get_topic_html`. The real implementation in `api.py` is exercised.

The key fix verified here: `api.py:get_topic_html` now calls `_sanitize_filename`
(from `utils.py`) instead of the previously undefined `_sanitize_api_filename`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


class TestHtmlDownloadEndToEnd:
    """Verify HTML download works with real (non-mocked) get_topic_html implementation."""

    def test_html_download_uses_real_get_topic_html_not_mocked(self, cli_runner, tmp_path):
        """HTML download pipeline uses real api.py get_topic_html — no mock on that method.

        This test verifies that the actual API call path is exercised for HTML topics.
        Only the HTTP response is mocked (to avoid real network), but get_topic_html
        itself is NOT mocked — it runs the real logic including _sanitize_filename.
        """
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Module 1", "Modules": [], "Topics": [
                    {"TopicId": 500, "Title": "Lecture Notes", "TypeIdentifier": "HTML",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},
                ]
            }]
        }

        # Mock HTTP layer — but NOT get_topic_html itself.
        # The real get_topic_html() will be called and must not raise NameError.
        def mock_get_json(path):
            if "/content/topics/500" in str(path):
                return {
                    "Title": "Lecture Notes",
                    "Body": {"Text": "<html><body><h1>Hello World</h1></body></html>"},
                    "Html": "",
                }
            raise AssertionError(f"Unexpected get_json call: {path}")

        def mock_get_raw(path):
            raise AssertionError(f"Unexpected get_raw call: {path}")

        def mock_cookies():
            return {"d2lSecureSessionVal": "test", "d2lSessionVal": "test"}

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test Course", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "get_json", side_effect=mock_get_json), \
             patch.object(LighthouseClient, "get_raw", side_effect=mock_get_raw), \
             patch.object(LighthouseClient, "cookies", property(lambda self: mock_cookies())):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "html", "--json"],
            )

            # MUST succeed — if _sanitize_api_filename was still referenced,
            # we'd get NameError: name '_sanitize_api_filename' is not defined
            assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"

            # Verify the HTML file was created
            course_dir = output_dir / "Test Course-44347"
            html_file = course_dir / "Module 1" / "Lecture Notes.html"
            assert html_file.exists(), f"HTML file not found at {html_file}"
            content = html_file.read_bytes()
            assert b"<h1>Hello World</h1>" in content

            # Verify manifest entry exists
            manifest_path = course_dir / MANIFEST_FILENAME
            assert manifest_path.exists()
            manifest_data = json.loads(manifest_path.read_text())
            assert "500" in manifest_data, f"Topic ID 500 not in manifest: {manifest_data}"
            assert manifest_data["500"]["filename"] == "Lecture Notes.html"

    def test_html_download_sanitizes_filename_with_special_chars(self, cli_runner, tmp_path):
        """HTML topic title with special characters is sanitized via _sanitize_filename."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 600, "Title": "Unit 1: Intro <Test>", "TypeIdentifier": "HTML",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},
                ]
            }]
        }

        def mock_get_json(path):
            if "/content/topics/600" in str(path):
                return {
                    "Title": "Unit 1: Intro <Test>",
                    "Body": {"Text": "<p>Content</p>"},
                    "Html": "",
                }
            raise AssertionError(f"Unexpected get_json call: {path}")

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Course", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "get_json", side_effect=mock_get_json), \
             patch.object(LighthouseClient, "get_raw", side_effect=lambda p: (b"", {})), \
             patch.object(LighthouseClient, "cookies", property(lambda self: {})):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "html", "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"

            course_dir = output_dir / "Course-44347"
            # Filename should have <> colons replaced with _
            html_file = course_dir / "Mod" / "Unit 1_ Intro _Test_.html"
            assert html_file.exists(), f"Expected sanitized filename, file not found at {html_file}"

    def test_html_download_appends_html_extension(self, cli_runner, tmp_path):
        """HTML topic title without .html extension gets .html appended."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 700, "Title": "Overview", "TypeIdentifier": "HTML",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},
                ]
            }]
        }

        def mock_get_json(path):
            if "/content/topics/700" in str(path):
                return {
                    "Title": "Overview",
                    "Body": {"Text": "<p>Overview content</p>"},
                    "Html": "",
                }
            raise AssertionError(f"Unexpected get_json: {path}")

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Course", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "get_json", side_effect=mock_get_json), \
             patch.object(LighthouseClient, "get_raw", side_effect=lambda p: (b"", {})), \
             patch.object(LighthouseClient, "cookies", property(lambda self: {})):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "html", "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"

            course_dir = output_dir / "Course-44347"
            html_file = course_dir / "Mod" / "Overview.html"
            assert html_file.exists(), f"HTML file not found at {html_file}"


class TestSanitizeFilenameShared:
    """Verify _sanitize_filename is available from utils and used correctly."""

    def test_sanitize_filename_from_utils(self):
        """_sanitize_filename from utils.py handles forbidden chars."""
        from lighthouse_cli.utils import _sanitize_filename
        result = _sanitize_filename("file:name<>test.pdf")
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result

    def test_sanitize_filename_url_decodes(self):
        """_sanitize_filename URL-decodes before sanitizing."""
        from lighthouse_cli.utils import _sanitize_filename
        result = _sanitize_filename("Lecture%201.pdf")
        assert result == "Lecture 1.pdf"
        assert "%20" not in result

    def test_sanitize_filename_strips_leading_trailing_spaces_dots(self):
        """_sanitize_filename strips leading/trailing dots and spaces."""
        from lighthouse_cli.utils import _sanitize_filename
        result = _sanitize_filename("  ..Lecture 1.pdf..  ")
        assert result == "Lecture 1.pdf"
