"""Tests for lighthouse assignments command (VAL-ASGN-001 – VAL-ASGN-008)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.cli import cli


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _folder(fid, name, due="2026-05-20T23:59:00Z", attachments=(), **extra) -> dict:
    return {"Id": fid, "Name": name, "DueDate": due, "Attachments": list(attachments), **extra}


def _file(aid, filename, size, kind="File") -> dict:
    return {"Id": aid, "FileName": filename, "Size": size, "Type": kind}


@contextmanager
def _single_course(folders=None, folders_error=None):
    """Course 44347 is enrolled; its dropbox returns ``folders`` or raises."""
    with patch.object(
        LighthouseClient, "get_dropbox_folders", return_value=folders, side_effect=folders_error
    ), patch.object(LighthouseClient, "get_courses", return_value=[
        {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
    ]):
        yield


@contextmanager
def _two_courses():
    courses = [
        {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
        {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
    ]
    folders = {
        111: [_folder(101, "Assign A1")],
        222: [_folder(201, "Assign B1", "2026-05-21T23:59:00Z", [_file(1, "f.pdf", 100)])],
    }
    with patch.object(LighthouseClient, "get_courses", return_value=courses), \
         patch.object(LighthouseClient, "get_dropbox_folders", side_effect=folders.get):
        yield


def _json(cli_runner, *args):
    result = cli_runner.invoke(cli, ["assignments", *args, "--json"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
    return json.loads(result.output)


def _human(cli_runner, *args) -> str:
    result = cli_runner.invoke(cli, ["assignments", *args])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
    return result.output


class TestSingleCourseAssignments:
    def test_assignments_table_columns(self, cli_runner):
        """VAL-ASGN-001: Table has ID, Name, Due Date, Attachments columns."""
        folders = [
            _folder(101, "Assignment 1", attachments=[
                _file(1, "q1.pdf", 1024),
                _file(2, "q2.pdf", 2048),
            ]),
            _folder(102, "Assignment 2", "2026-05-25T23:59:00Z"),
        ]

        with _single_course(folders):
            output = _human(cli_runner, "44347")

        assert "101" in output
        assert "Assignment 1" in output
        assert "Assignment 2" in output
        assert "2" in output  # attachment count
        assert "0" in output  # attachment count for assignment 2
        assert "Due Date" in output
        assert "Attachments" in output

    def test_assignments_json_output_single_course(self, cli_runner):
        """VAL-ASGN-002: --json returns course_id and assignments array."""
        folders = [_folder(
            101,
            "Assignment 1",
            attachments=[_file(1, "q1.pdf", 1024)],
            CustomInstructions="<p>Submit your solutions.</p>",
        )]

        with _single_course(folders):
            data = _json(cli_runner, "44347")

        assert data["course_id"] == 44347
        assert len(data["assignments"]) == 1
        assert data["assignments"][0]["folder_id"] == 101
        assert data["assignments"][0]["attachment_count"] == 1
        assert data["assignments"][0]["custom_instructions"] is not None

    def test_assignment_with_no_attachments(self, cli_runner):
        """VAL-ASGN-006: Folder with zero attachments shows attachment_count: 0."""
        with _single_course([_folder(200, "Empty Assignment", due=None)]):
            data = _json(cli_runner, "44347")

        assert data["assignments"][0]["attachment_count"] == 0
        assert data["assignments"][0]["attachments"] == []

    def test_link_type_attachments_distinguished(self, cli_runner):
        """VAL-ASGN-007: Link attachments have attachment_type: Link vs File."""
        folders = [_folder(300, "Link Assignment", attachments=[
            _file(10, "https://example.com/resource", 0, kind="Link"),
            _file(11, "question.pdf", 4096),
        ])]

        with _single_course(folders):
            atts = _json(cli_runner, "44347")["assignments"][0]["attachments"]

        assert len(atts) == 2
        assert atts[0]["attachment_type"] == "Link"
        assert atts[1]["attachment_type"] == "File"

    def test_custom_instructions_included(self, cli_runner):
        """VAL-ASGN-008: CustomInstructions field present in JSON and previewed in human mode."""
        folders = [_folder(
            400,
            "Instructions Test",
            CustomInstructions="<p>Read the <b>instructions</b> carefully before submitting.</p>",
        )]

        with _single_course(folders):
            data = _json(cli_runner, "44347")
            output = _human(cli_runner, "44347")

        # HTML is preserved in custom_instructions; the preview is stripped.
        assert "<p>" in data["assignments"][0]["custom_instructions"]
        preview = data["assignments"][0]["custom_instructions_preview"]
        assert preview is not None
        assert "<" not in preview
        assert "Instructions:" in output

    def test_html_in_folder_name_stripped(self, cli_runner):
        with _single_course([_folder(500, "<b>Important</b> Assignment &amp; Stuff")]):
            output = _human(cli_runner, "44347")
            name = _json(cli_runner, "44347")["assignments"][0]["name"]

        assert "<b>" not in output
        assert "&amp;" not in output
        assert "<" not in name
        assert "Important" in name

    def test_time_restricted_availability_info(self, cli_runner):
        folders = [_folder(600, "Time Restricted", Availability={
            "StartDate": "2026-05-15T00:00:00Z",
            "EndDate": "2026-05-20T23:59:00Z",
        })]

        with _single_course(folders):
            output = _human(cli_runner, "44347")

        assert "Opens:" in output
        assert "2026-05-15" in output

    def test_course_not_found_error(self, cli_runner):
        with _single_course([]):
            result = cli_runner.invoke(cli, ["assignments", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()
        assert "lighthouse courses" in result.output


class TestAllCoursesAssignments:
    def test_no_course_id_fetches_all_courses(self, cli_runner):
        """VAL-ASGN-003: No COURSE_ID iterates all enrolled courses."""
        with _two_courses():
            output = _human(cli_runner)

        assert "Course A" in output
        assert "Course B" in output
        assert "Assign A1" in output
        assert "Assign B1" in output

    def test_all_courses_json_is_single_array(self, cli_runner):
        """VAL-ASGN-004: --json emits single JSON array, not concatenated objects."""
        with _two_courses():
            data = _json(cli_runner)

        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) == 2
        assert data[0]["course_id"] == 111
        assert data[1]["course_id"] == 222


class TestCourseWithNoAssignments:
    def test_course_with_zero_assignments(self, cli_runner):
        """VAL-ASGN-005: No folders shows 'No assignments found' (human) or [] (JSON), exit 0."""
        with _single_course([]):
            output = _human(cli_runner, "44347")
            data = _json(cli_runner, "44347")

        assert "No assignments found" in output
        assert data["course_id"] == 44347
        assert data["assignments"] == []


class TestSessionExpiry:
    def test_session_expired_error(self, cli_runner):
        expired = SessionExpiredError("Session expired. Run: lighthouse auth login")

        with _single_course(folders_error=expired):
            result = cli_runner.invoke(cli, ["assignments", "44347"])

        assert result.exit_code == 1
        assert "Session expired" in result.output
        assert "auth login" in result.output
