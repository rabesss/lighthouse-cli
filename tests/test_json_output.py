"""Tests for multi-course JSON output format and synced_at timestamp."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


class TestMultiCourseJsonOutput:
    """Tests for structured JSON output in download/sync multi-course operations."""

    def test_download_multi_course_json_includes_semester_synced_at_summary(
        self, cli_runner, tmp_path
    ):
        """Download --semester with --json outputs semester, synced_at, and summary."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}, "222": {"name": "Course B", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            # Must have semester, synced_at, summary, courses
            assert "semester" in data
            assert "synced_at" in data
            assert "summary" in data
            assert "courses" in data
            assert isinstance(data["courses"], list)
            assert len(data["courses"]) == 2

            # Semester has id and name
            assert "id" in data["semester"]
            assert "name" in data["semester"]
            assert data["semester"]["id"] == 100
            assert data["semester"]["name"] == "Sem I"

            # synced_at is valid UTC ISO 8601 timestamp
            ts = data["synced_at"]
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", ts), \
                f"synced_at '{ts}' is not valid UTC ISO 8601"

    def test_download_multi_course_summary_counts_consistent(
        self, cli_runner, tmp_path
    ):
        """Summary counts match sum of per-course arrays."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10 + 1, "Title": "a.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                        {"TopicId": cid * 10 + 2, "Title": "b.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{tid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            summary = data["summary"]
            courses = data["courses"]

            # Summary.courses_checked matches len(courses)
            assert summary["courses_checked"] == len(courses)

            # Summary.downloaded matches sum of per-course downloaded
            assert summary["downloaded"] == sum(len(c["downloaded"]) for c in courses)

            # Summary.errors matches sum of per-course errors
            assert summary["errors"] == sum(len(c["errors"]) for c in courses)

            # All per-course arrays are present
            for c in courses:
                assert "downloaded" in c
                assert "skipped" in c
                assert "updated" in c
                assert "duplicates" in c
                assert "errors" in c

    def test_sync_multi_course_json_includes_synced_at_and_summary(
        self, cli_runner, tmp_path
    ):
        """Sync multi-course JSON output includes synced_at and summary."""
        output_dir = tmp_path / "sync"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["sync", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            assert "synced_at" in data
            assert "summary" in data
            assert "courses" in data
            assert isinstance(data["courses"], list)

            # synced_at is valid UTC ISO 8601
            ts = data["synced_at"]
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", ts), \
                f"synced_at '{ts}' is not valid UTC ISO 8601"

    def test_download_multi_course_per_course_has_sha256_extension_size_kb(
        self, cli_runner, tmp_path
    ):
        """Per-course downloaded entries include sha256, extension, size_kb."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "Lecture.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            # Use enough content so size_kb > 0 (at least 1024 bytes = 1 KB)
            return b"X" * 2048, "Lecture.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            # Check first course's first downloaded entry
            course = data["courses"][0]
            assert len(course["downloaded"]) == 1
            entry = course["downloaded"][0]

            assert "sha256" in entry
            assert entry["sha256"]  # non-empty string
            assert len(entry["sha256"]) == 64  # SHA-256 hex length

            assert "extension" in entry
            assert entry["extension"] == ".pdf"

            assert "size_kb" in entry
            assert entry["size_kb"] > 0  # numeric

    def test_download_multi_course_sha256_dedup_per_course(
        self, cli_runner, tmp_path
    ):
        """SHA-256 dedup detects same file in different topics within a course."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        # Same content for two topics → same SHA-256
        same_content = b"IDENTICAL FILE CONTENT"

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10 + 1, "Title": "Assignment.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                        {"TopicId": cid * 10 + 2, "Title": "Assignment-Dup.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return same_content, "file.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            course = data["courses"][0]

            # Both topics were downloaded
            assert len(course["downloaded"]) == 2

            # Both have the same SHA-256
            hashes = [e["sha256"] for e in course["downloaded"]]
            assert hashes[0] == hashes[1], "Same content should produce same SHA-256"

            # Duplicates array is populated
            assert len(course["duplicates"]) == 2  # both entries are duplicates
            for dup in course["duplicates"]:
                assert "topic_id" in dup
                assert "filename" in dup
                assert "sha256" in dup
                assert dup["sha256"] == hashes[0]

    def test_download_multi_course_json_empty_course_exit_0(
        self, cli_runner, tmp_path
    ):
        """Download with empty course (no downloadable files) exits 0."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            # No topics → empty course
            return {"Modules": []}

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            assert data["summary"]["downloaded"] == 0
            assert data["summary"]["errors"] == 0

    def test_download_multi_course_error_on_one_course_exit_1(
        self, cli_runner, tmp_path
    ):
        """Download where one course fails should exit 1."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            if cid == 222:
                raise Exception("Network error for course B")
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}, "222": {"name": "Course B", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            # Partial failure → exit code 1
            assert result.exit_code == 1, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            # Course A succeeded, Course B had errors
            course_a = next((c for c in data["courses"] if c["course_id"] == 111), None)
            course_b = next((c for c in data["courses"] if c["course_id"] == 222), None)

            assert course_a is not None
            assert course_b is not None
            assert len(course_a["downloaded"]) == 1
            assert len(course_a["errors"]) == 0
            assert len(course_b["downloaded"]) == 0
            assert len(course_b["errors"]) == 1
            assert "Network error" in course_b["errors"][0]["error"]

    def test_download_multi_course_also_errors_in_json_output(
        self, cli_runner, tmp_path
    ):
        """--also with invalid course includes also_errors in JSON output."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "--also", "99999", "-o", str(output_dir), "--json"],
            )

            # Should succeed even with invalid --also (partial success)
            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            assert "also_errors" in data
            assert len(data["also_errors"]) == 1
            assert "99999" in data["also_errors"][0]

    def test_download_all_courses_json_includes_semester_and_summary(
        self, cli_runner, tmp_path
    ):
        """Download without course_id (all courses) includes semester and summary in JSON."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}, "222": {"name": "Course B", "semester": "Sem II"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["download", "-o", str(output_dir), "--json"],
            )

            # Should download only Sem II courses (highest OrgUnitId)
            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            assert "semester" in data
            assert "synced_at" in data
            assert "summary" in data
            assert "courses" in data
            assert data["semester"]["id"] == 200
            assert data["semester"]["name"] == "Sem II"

            # Only Sem II course (222) should be in results
            assert len(data["courses"]) == 1
            assert data["courses"][0]["course_id"] == 222

    def test_sync_all_courses_json_includes_semester_and_summary(
        self, cli_runner, tmp_path
    ):
        """Sync without course_id (all courses) includes semester and summary in JSON."""
        output_dir = tmp_path / "sync"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}, "222": {"name": "Course B", "semester": "Sem II"}}}))

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download):

            result = cli_runner.invoke(
                cli,
                ["sync", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            assert "semester" in data
            assert "synced_at" in data
            assert "summary" in data
            assert "courses" in data
            assert data["semester"]["id"] == 200
            assert len(data["courses"]) == 1

    def test_sync_multi_course_skipped_and_updated_in_per_course(
        self, cli_runner, tmp_path
    ):
        """Sync multi-course JSON includes skipped and updated entries per course."""
        output_dir = tmp_path / "sync"
        output_dir.mkdir()

        semesters = [{"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
        ]
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))

        # Pre-seed manifest with one file that hasn't changed (skipped)
        # and one file that has been updated (different last_modified)
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / ".lighthouse.json"
        # Write simpler, valid JSON
        manifest_path.write_text(json.dumps({
            "1110": {
                "sha256": "abc123def456",
                "filename": "f.pdf",
                "size": 100,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",
            },
            "1111": {
                "sha256": "old_hash_val",
                "filename": "g.pdf",
                "size": 50,
                "downloaded_at": "2025-12-01T00:00:00Z",
                "last_modified": "2025-12-01T00:00:00Z",
            },
        }))

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        # Same timestamp → skipped
                        {"TopicId": 1110, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                        # Different (older) timestamp in manifest → newer in TOC → updated
                        {"TopicId": 1111, "Title": "g.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-02-01T00:00:00Z"},
                    ]
                }]
            }

        def download_file(cid, tid):
            return f"content{tid}".encode(), f"file{tid}.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_file), \
             patch.object(LighthouseClient, "get_topic_html", return_value=(b"", "empty.html")):

            result = cli_runner.invoke(
                cli,
                ["sync", "--semester", "100", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)

            course = data["courses"][0]

            # Check error field
            assert len(course["errors"]) == 0, f"Unexpected errors: {course['errors']}"

            # One skipped, one updated
            assert len(course["skipped"]) == 1, f"Expected 1 skipped, got {course['skipped']}"
            assert course["skipped"][0]["topic_id"] == "1110"
            assert len(course["updated"]) == 1, f"Expected 1 updated, got {course['updated']}"
