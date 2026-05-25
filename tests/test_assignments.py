"""Tests for lighthouse assignments command (VAL-ASGN-001 – VAL-ASGN-008)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# VAL-ASGN-001 & VAL-ASGN-002: Single course listing
# ---------------------------------------------------------------------------

class TestSingleCourseAssignments:
    """Test lighthouse assignments COURSE_ID with table and JSON output."""

    def test_assignments_table_columns(self, cli_runner):
        """VAL-ASGN-001: Table has ID, Name, Due Date, Attachments columns."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                    {"Id": 2, "FileName": "q2.pdf", "Size": 2048, "Type": "File"},
                ],
            },
            {
                "Id": 102,
                "Name": "Assignment 2",
                "DueDate": "2026-05-25T23:59:00Z",
                "Attachments": [],
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            assert "101" in result.output
            assert "Assignment 1" in result.output
            assert "Assignment 2" in result.output
            assert "2" in result.output  # attachment count
            assert "0" in result.output  # attachment count for assignment 2
            # Verify table-like alignment is present
            assert "Due Date" in result.output
            assert "Attachments" in result.output

    def test_assignments_json_output_single_course(self, cli_runner):
        """VAL-ASGN-002: --json returns course_id and assignments array."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                ],
                "CustomInstructions": "<p>Submit your solutions.</p>",
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347", "--json"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert data["course_id"] == 44347
            assert "assignments" in data
            assert len(data["assignments"]) == 1
            assert data["assignments"][0]["folder_id"] == 101
            assert data["assignments"][0]["attachment_count"] == 1
            assert data["assignments"][0]["custom_instructions"] is not None

    def test_assignment_with_no_attachments(self, cli_runner):
        """VAL-ASGN-006: Folder with zero attachments shows attachment_count: 0."""
        folders = [
            {
                "Id": 200,
                "Name": "Empty Assignment",
                "DueDate": None,
                "Attachments": [],
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["assignments"][0]["attachment_count"] == 0
            assert data["assignments"][0]["attachments"] == []

    def test_link_type_attachments_distinguished(self, cli_runner):
        """VAL-ASGN-007: Link attachments have attachment_type: Link vs File."""
        folders = [
            {
                "Id": 300,
                "Name": "Link Assignment",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 10, "FileName": "https://example.com/resource", "Size": 0, "Type": "Link"},
                    {"Id": 11, "FileName": "question.pdf", "Size": 4096, "Type": "File"},
                ],
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            atts = data["assignments"][0]["attachments"]
            assert len(atts) == 2
            # Link attachment
            assert atts[0]["attachment_type"] == "Link"
            assert atts[1]["attachment_type"] == "File"

    def test_custom_instructions_included(self, cli_runner):
        """VAL-ASGN-008: CustomInstructions field present in JSON and previewed in human mode."""
        folders = [
            {
                "Id": 400,
                "Name": "Instructions Test",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [],
                "CustomInstructions": "<p>Read the <b>instructions</b> carefully before submitting.</p>",
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            # HTML is preserved in custom_instructions
            assert "<p>" in data["assignments"][0]["custom_instructions"]
            # Preview is stripped
            preview = data["assignments"][0]["custom_instructions_preview"]
            assert preview is not None
            assert "<" not in preview  # HTML tags stripped

            # Human mode: instructions preview shown
            result2 = cli_runner.invoke(cli, ["assignments", "44347"])
            assert result2.exit_code == 0
            assert "Instructions:" in result2.output

    def test_html_in_folder_name_stripped(self, cli_runner):
        """HTML in folder Name is stripped for display."""
        folders = [
            {
                "Id": 500,
                "Name": "<b>Important</b> Assignment &amp; Stuff",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [],
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347"])
            assert result.exit_code == 0
            # HTML tags should be stripped from display
            assert "<b>" not in result.output
            assert "&amp;" not in result.output

            # JSON should have clean name
            result2 = cli_runner.invoke(cli, ["assignments", "44347", "--json"])
            assert result2.exit_code == 0
            data = json.loads(result2.output)
            name = data["assignments"][0]["name"]
            assert "<" not in name
            assert "Important" in name

    def test_time_restricted_availability_info(self, cli_runner):
        """Time-restricted assignments show availability info."""
        folders = [
            {
                "Id": 600,
                "Name": "Time Restricted",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [],
                "Availability": {
                    "StartDate": "2026-05-15T00:00:00Z",
                    "EndDate": "2026-05-20T23:59:00Z",
                },
            },
        ]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347"])
            assert result.exit_code == 0
            assert "Opens:" in result.output
            assert "2026-05-15" in result.output

    def test_course_not_found_error(self, cli_runner):
        """Non-existent course shows error with remediation hint."""
        from lighthouse_cli.api import CourseNotFoundError

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals", "Code": "X"},
        ]):

            result = cli_runner.invoke(cli, ["assignments", "nonexistent"])

            assert result.exit_code == 1
            assert "not found" in result.output.lower()
            assert "lighthouse courses" in result.output


# ---------------------------------------------------------------------------
# VAL-ASGN-003 & VAL-ASGN-004: All-courses listing
# ---------------------------------------------------------------------------

class TestAllCoursesAssignments:
    """Test lighthouse assignments (no course) iterates all courses."""

    def test_no_course_id_fetches_all_courses(self, cli_runner):
        """VAL-ASGN-003: No COURSE_ID iterates all enrolled courses."""
        courses = [
            {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
            {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
        ]
        folders_a = [
            {"Id": 101, "Name": "Assign A1", "DueDate": "2026-05-20T23:59:00Z", "Attachments": []},
        ]
        folders_b = [
            {"Id": 201, "Name": "Assign B1", "DueDate": "2026-05-21T23:59:00Z", "Attachments": [{"Id": 1, "FileName": "f.pdf", "Size": 100, "Type": "File"}]},
        ]

        def get_dropbox_folders(cid):
            if cid == 111:
                return folders_a
            elif cid == 222:
                return folders_b
            return []

        with patch.object(LighthouseClient, "get_courses", return_value=courses), \
             patch.object(LighthouseClient, "get_dropbox_folders", side_effect=get_dropbox_folders):

            result = cli_runner.invoke(cli, ["assignments"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            assert "Course A" in result.output
            assert "Course B" in result.output
            assert "Assign A1" in result.output
            assert "Assign B1" in result.output

    def test_all_courses_json_is_single_array(self, cli_runner):
        """VAL-ASGN-004: --json emits single JSON array, not concatenated objects."""
        courses = [
            {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
            {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
        ]
        folders_a = [{"Id": 101, "Name": "Assign A1", "DueDate": None, "Attachments": []}]
        folders_b = [{"Id": 201, "Name": "Assign B1", "DueDate": None, "Attachments": []}]

        def get_dropbox_folders(cid):
            if cid == 111:
                return folders_a
            elif cid == 222:
                return folders_b
            return []

        with patch.object(LighthouseClient, "get_courses", return_value=courses), \
             patch.object(LighthouseClient, "get_dropbox_folders", side_effect=get_dropbox_folders):

            result = cli_runner.invoke(cli, ["assignments", "--json"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # Must be parseable as a single JSON array
            data = json.loads(result.output)
            assert isinstance(data, list), f"Expected list, got {type(data)}"
            assert len(data) == 2
            # Each element has course_id and assignments
            assert data[0]["course_id"] == 111
            assert data[1]["course_id"] == 222


# ---------------------------------------------------------------------------
# VAL-ASGN-005: Course with no assignments
# ---------------------------------------------------------------------------

class TestCourseWithNoAssignments:
    """Test course with zero dropbox folders completes gracefully."""

    def test_course_with_zero_assignments_human_mode(self, cli_runner):
        """VAL-ASGN-005: Course with no folders shows 'No assignments found' and exits 0."""
        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=[]), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Empty Course", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            assert "No assignments found" in result.output

    def test_course_with_zero_assignments_json_mode(self, cli_runner):
        """VAL-ASGN-005: JSON mode returns empty assignments array with exit 0."""
        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=[]), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Empty Course", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347", "--json"])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert data["course_id"] == 44347
            assert data["assignments"] == []


# ---------------------------------------------------------------------------
# Session expiry handling
# ---------------------------------------------------------------------------

class TestSessionExpiry:
    """Test session expiry produces actionable error."""

    def test_session_expired_error(self, cli_runner):
        """Session expiry shows actionable error with remediation hint."""
        from lighthouse_cli.api import SessionExpiredError

        def raise_expired(cid):
            raise SessionExpiredError("Session expired. Run: lighthouse auth login")

        with patch.object(LighthouseClient, "get_dropbox_folders", side_effect=raise_expired), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Test", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, ["assignments", "44347"])

            assert result.exit_code == 1
            assert "Session expired" in result.output
            assert "auth login" in result.output
