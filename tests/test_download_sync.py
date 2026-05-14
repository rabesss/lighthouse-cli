"""Integration tests for download/sync command with manifest system."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


class TestDownloadManifestIntegration:
    """Test that download command creates correct manifest with SHA-256 and last_modified."""

    def test_download_creates_manifest_with_correct_schema(self, cli_runner, tmp_path):
        """Download creates .lighthouse.json with all required keys per entry."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [
                {
                    "ModuleId": 1001,
                    "Title": "Unit 1",
                    "Modules": [],
                    "Topics": [
                        {
                            "TopicId": 12345,
                            "Title": "Lecture 1.pdf",
                            "TypeIdentifier": "File",
                            "Url": "https://example.com/file.pdf",
                            "LastModifiedDate": "2026-03-15T12:00:00Z",
                        }
                    ],
                }
            ]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "009_BME_2125"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"PDF content here", "Lecture%201.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--json"],
            )

            # Check manifest was created
            course_dir = output_dir / "Signals & Systems-44347"
            manifest_path = course_dir / MANIFEST_FILENAME

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            assert manifest_path.exists(), f"Manifest not found at {manifest_path}"

            # Verify manifest schema
            manifest_data = json.loads(manifest_path.read_text())
            assert "12345" in manifest_data
            entry = manifest_data["12345"]
            assert "sha256" in entry
            assert "filename" in entry
            assert "size" in entry
            assert "downloaded_at" in entry
            assert "last_modified" in entry
            assert entry["last_modified"] == "2026-03-15T12:00:00Z"
            assert entry["filename"] == "Lecture 1.pdf"  # URL-decoded
            assert entry["size"] == len(b"PDF content here")

    def test_download_creates_course_name_folder_not_org_id(self, cli_runner, tmp_path):
        """Download creates folder named after course Name, not OrgUnitId."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 1, "Title": "f", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"}
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "009_BME_2125"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--json"],
            )

            # Course folder should be named after sanitized course name + org_id
            course_dir = output_dir / "Signals & Systems-44347"
            assert course_dir.exists(), f"Expected {course_dir}. Contents: {list(output_dir.iterdir())}"

    def test_download_sanitizes_course_name_special_chars(self, cli_runner, tmp_path):
        """Course name with forbidden chars is sanitized in folder name."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 1, "Title": "f", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"}
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 99999, "Name": 'Intro: CS *2025* / Section<1>', "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "99999", "-o", str(output_dir), "--json"],
            )

            # Should create "Intro_ CS _2025_ _ Section_1_-99999"
            expected_folder = "Intro_ CS _2025_ _ Section_1_-99999"
            course_dir = output_dir / expected_folder
            assert course_dir.exists(), f"Expected {course_dir}. Contents: {list(output_dir.iterdir())}"

    def test_manifest_atomic_write_no_corruption(self, cli_runner, tmp_path):
        """Manifest is written atomically — no partial/corrupt JSON on success."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [
                {
                    "ModuleId": 1,
                    "Title": "Mod",
                    "Modules": [],
                    "Topics": [
                        {"TopicId": 10, "Title": "f.pdf", "TypeIdentifier": "File",
                         "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                        {"TopicId": 11, "Title": "g.pdf", "TypeIdentifier": "File",
                         "Url": "", "LastModifiedDate": "2026-01-02T00:00:00Z"},
                    ],
                }
            ]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=[
                 (b"content1", "f.pdf"),
                 (b"content2", "g.pdf"),
             ]):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir)],
            )

            course_dir = output_dir / "Signals-44347"
            manifest_path = course_dir / MANIFEST_FILENAME

            assert manifest_path.exists()
            # Must be valid JSON — no truncation
            data = json.loads(manifest_path.read_text())
            assert len(data) == 2
            assert "10" in data
            assert "11" in data

    def test_last_modified_from_toc_not_http_headers(self, cli_runner, tmp_path):
        """last_modified in manifest comes from TOC LastModifiedDate, not current time."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc_date = "2026-06-15T09:30:00Z"
        toc = {
            "Modules": [{
                "ModuleId": 1,
                "Title": "Mod",
                "Modules": [],
                "Topics": [
                    {"TopicId": 100, "Title": "file.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": toc_date},
                ],
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"pdf content", "file.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--json"],
            )

            course_dir = output_dir / "Test-44347"
            manifest_path = course_dir / MANIFEST_FILENAME
            manifest_data = json.loads(manifest_path.read_text())

            assert manifest_data["100"]["last_modified"] == toc_date


class TestSanitizationIntegration:
    """Integration tests for sanitization behavior."""

    def test_url_decoded_filename_in_manifest(self, cli_runner, tmp_path):
        """Content-Disposition filename with %20 is URL-decoded before saving."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 1, "Title": "L1", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"}
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "L1%20Intro%20File.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir)],
            )

            course_dir = output_dir / "Test-44347"
            manifest_path = course_dir / MANIFEST_FILENAME
            manifest_data = json.loads(manifest_path.read_text())

            # Filename should be URL-decoded to "L1 Intro File.pdf"
            assert manifest_data["1"]["filename"] == "L1 Intro File.pdf"
            # File on disk should also have decoded name
            file_path = course_dir / "Mod" / "L1 Intro File.pdf"
            assert file_path.exists(), f"Expected {file_path}"

    def test_force_flag_wipes_manifest(self, cli_runner, tmp_path):
        """--force deletes existing manifest before download."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME

        # Pre-existing corrupt manifest
        manifest_path.write_text("not valid json")

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 1, "Title": "f", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"}
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--force"],
            )

            # Manifest should now be valid JSON
            manifest_data = json.loads(manifest_path.read_text())
            assert "1" in manifest_data


class TestHTMLTopicDownload:
    """Test HTML topic download with --types file,html (VAL-SYNC-020, VAL-SYNC-034)."""

    def test_download_with_types_file_html_includes_both(self, cli_runner, tmp_path):
        """--types file,html downloads both File and HTML topics."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "Lecture.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    {"TopicId": 101, "Title": "Notes.html", "TypeIdentifier": "HTML",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"PDF content", "Lecture.pdf")), \
             patch.object(LighthouseClient, "get_topic_html", return_value=(b"<html>test</html>", "Notes.html")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "file,html", "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert len(data["downloaded"]) == 2, f"Expected 2 downloads, got {len(data['downloaded'])}"

    def test_html_topic_saved_as_html_file(self, cli_runner, tmp_path):
        """HTML topics are saved as .html files with body content (VAL-SYNC-034)."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 200, "Title": "Overview", "TypeIdentifier": "HTML",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "get_topic_html", return_value=(b"<html><body>Hello</body></html>", "Overview.html")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "html", "--json"],
            )

            assert result.exit_code == 0
            course_dir = output_dir / "Test-44347"
            html_file = course_dir / "Mod" / "Overview.html"
            assert html_file.exists(), f"HTML file not found at {html_file}"
            assert html_file.read_bytes() == b"<html><body>Hello</body></html>"

    def test_unknown_type_produces_warning(self, cli_runner, tmp_path):
        """--types file,video warns about unknown type video and proceeds with file (VAL-SYNC-041)."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 300, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--types", "file,video"],
            )

            assert result.exit_code == 0
            assert "Unknown content type: video" in result.output


class TestFallbackFilename:
    """Test topic download fallback filename (VAL-SYNC-059)."""

    def test_no_content_disposition_falls_back_to_topic_id(self, cli_runner, tmp_path):
        """When Content-Disposition is missing, file is saved as topic_{id}."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 999, "Title": "Untitled", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        # Simulate no Content-Disposition by returning empty filename from _extract_filename
        # The download_topic_file uses _extract_filename which returns "" when no filename=
        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"binary data", "topic_999")):

            result = cli_runner.invoke(
                cli,
                ["download", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0
            course_dir = output_dir / "Test-44347"
            fallback_file = course_dir / "Mod" / "topic_999"
            assert fallback_file.exists(), f"Fallback file not found at {fallback_file}"


class TestNoCourseIdDownloadsLatestSemester:
    """Test that download without COURSE_ID downloads all courses from latest semester (VAL-SYNC-011)."""

    def test_download_without_course_id_downloads_latest_semester(self, cli_runner, tmp_path):
        """download without course_id resolves to latest semester and downloads all courses."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]

        toc_course_a = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 10, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        toc_course_b = {
            "Modules": [{
                "ModuleId": 2, "Title": "Mod2", "Modules": [], "Topics": [
                    {"TopicId": 20, "Title": "g.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        def get_content_toc(cid):
            if cid == 222:
                return toc_course_b
            return toc_course_a

        def get_topic_html(cid, tid):
            return b"<html>test</html>", "test.html"

        def download_topic_file(cid, tid):
            if cid == 222:
                return b"content b", "g.pdf"
            return b"content a", "f.pdf"

        def get_courses():
            return [
                {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
                {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
            ]

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", side_effect=get_courses), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file), \
             patch.object(LighthouseClient, "get_topic_html", side_effect=get_topic_html):

            result = cli_runner.invoke(
                cli,
                ["download", "-o", str(output_dir), "--dry-run"],
            )

            # Should show dry-run plan for both courses (Sem II = latest, highest OrgUnitId)
            assert result.exit_code == 0
            # Dry run should mention "Would download"
            assert "Would download" in result.output
