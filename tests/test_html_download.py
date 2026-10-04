"""Tests for HTML topic download — verifying real (non-mocked) get_topic_html works.

These tests verify that the HTML download pipeline end-to-end works correctly
without mocking `get_topic_html`. The real implementation in `api.py` is exercised.

The key fix verified here: `api.py:get_topic_html` now calls `_sanitize_filename`
(from `utils.py`) instead of the previously undefined `_sanitize_api_filename`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME
from lighthouse_cli.utils import _sanitize_filename


def _download_html(tmp_path: Path, *, course, module, topic_id, title, body, cookies) -> Path:
    """Run ``download --types html`` for one HTML topic; return the course directory.

    Only the HTTP layer (get_raw) is mocked, never get_topic_html itself, so the
    real code path runs, including _sanitize_filename.
    """
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    toc = {
        "Modules": [{
            "ModuleId": 1, "Title": module, "Modules": [], "Topics": [
                {"TopicId": topic_id, "Title": title, "TypeIdentifier": "HTML",
                 "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},
            ]
        }]
    }

    def fake_get_raw(path, **_kwargs):
        if f"/content/topics/{topic_id}" in str(path):
            topic = {"Title": title, "Body": {"Text": body}, "Html": ""}
            return json.dumps(topic).encode("utf-8"), {}
        raise AssertionError(f"Unexpected get_raw call: {path}")

    with patch.object(LighthouseClient, "get_courses", return_value=[
        {"OrgUnitId": 44347, "Name": course, "Code": "X"}
    ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
         patch.object(LighthouseClient, "get_raw", side_effect=fake_get_raw), \
         patch.object(LighthouseClient, "cookies", property(lambda self: cookies)):

        result = CliRunner().invoke(
            cli,
            ["download", "44347", "-o", str(output_dir), "--types", "html", "--json"],
        )

    # If _sanitize_api_filename were still referenced, this would be a NameError.
    assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"
    return output_dir / f"{course}-44347"


class TestHtmlDownloadEndToEnd:
    def test_html_download_uses_real_get_topic_html_not_mocked(self, tmp_path):
        course_dir = _download_html(
            tmp_path,
            course="Test Course",
            module="Module 1",
            topic_id=500,
            title="Lecture Notes",
            body="<html><body><h1>Hello World</h1></body></html>",
            cookies={"d2lSecureSessionVal": "test", "d2lSessionVal": "test"},
        )

        html_file = course_dir / "Module 1" / "Lecture Notes.html"
        assert html_file.exists(), f"HTML file not found at {html_file}"
        assert b"<h1>Hello World</h1>" in html_file.read_bytes()

        manifest_path = course_dir / MANIFEST_FILENAME
        assert manifest_path.exists()
        manifest_data = json.loads(manifest_path.read_text())
        assert "500" in manifest_data, f"Topic ID 500 not in manifest: {manifest_data}"
        assert manifest_data["500"]["filename"] == "Lecture Notes.html"

    @pytest.mark.parametrize(
        ("topic_id", "title", "expected_filename"),
        [
            (600, "Unit 1: Intro <Test>", "Unit 1_ Intro _Test_.html"),
            (700, "Overview", "Overview.html"),
        ],
        ids=["special-chars-replaced", "html-extension-appended"],
    )
    def test_html_download_filename(self, tmp_path, topic_id, title, expected_filename):
        course_dir = _download_html(
            tmp_path,
            course="Course",
            module="Mod",
            topic_id=topic_id,
            title=title,
            body="<p>Content</p>",
            cookies={},
        )

        html_file = course_dir / "Mod" / expected_filename
        assert html_file.exists(), f"Expected {expected_filename}, file not found at {html_file}"


class TestSanitizeFilenameShared:
    def test_sanitize_filename_from_utils(self):
        result = _sanitize_filename("file:name<>test.pdf")
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result

    @pytest.mark.parametrize(
        "raw",
        ["Lecture%201.pdf", "  ..Lecture 1.pdf..  "],
        ids=["url-decodes", "strips-leading-trailing-spaces-dots"],
    )
    def test_sanitize_filename_normalizes(self, raw):
        assert _sanitize_filename(raw) == "Lecture 1.pdf"
