"""Tests for multi-course scope resolution (VAL-SYNC-011, 012, 013, 014, 032, 035, 036, 037, 038, 039, 057, VAL-CROSS-008)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, CourseNotFoundError
from lighthouse_cli.cli import cli


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _config_for(semesters, enrollments):
    """Build a course-config.json dict mapping course IDs to semester labels.

    Uses the semester name as the label for each enrollment (matched by code prefix).
    """
    tracked = {}
    for e in enrollments:
        oid = str(e["OrgUnit"]["Id"])
        name = e["OrgUnit"]["Name"]
        code = e["OrgUnit"].get("Code", "")
        # Assign semester label based on which semester's code prefix matches
        sem_label = ""
        for s in semesters:
            sname = s.get("Name", "")
            if sname:
                sem_label = sname
                break
        tracked[oid] = {"name": name, "semester": sem_label}
    return {"tracked_courses": tracked}


# ---------------------------------------------------------------------------
# VAL-SYNC-011 & VAL-SYNC-032: Default to latest semester (highest OrgUnitId)
# ---------------------------------------------------------------------------

class TestLatestSemesterResolution:
    """Test that default (no args) downloads all courses from latest semester."""

    def test_default_downloads_all_courses_from_latest_semester_by_highest_orgunitid(self, cli_runner, tmp_path):
        """VAL-SYNC-011 & VAL-SYNC-032: Latest semester = highest OrgUnitId, not by date or name."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Old Sem", "Code": "OLD"},
            {"OrgUnitId": 200, "Name": "Newer Sem", "Code": "NEW"},
            {"OrgUnitId": 300, "Name": "Newest Sem", "Code": "NEWEST"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 101, "Name": "Old Course", "Code": "OLD"}},
            {"OrgUnit": {"Id": 201, "Name": "Newer Course", "Code": "NEW"}},
            {"OrgUnit": {"Id": 202, "Name": "Newer Course 2", "Code": "NEW"}},
            {"OrgUnit": {"Id": 301, "Name": "Newest Course", "Code": "NEWEST"}},
            {"OrgUnit": {"Id": 302, "Name": "Newest Course 2", "Code": "NEWEST"}},
        ]

        # Map courses to their semester names
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "101": {"name": "Old Course", "semester": "Old Sem"},
                "201": {"name": "Newer Course", "semester": "Newer Sem"},
                "202": {"name": "Newer Course 2", "semester": "Newer Sem"},
                "301": {"name": "Newest Course", "semester": "Newest Sem"},
                "302": {"name": "Newest Course 2", "semester": "Newest Sem"},
            }
        }))

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                         "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download_topic_file(cid, tid):
            return f"content for {cid}".encode(), "f.pdf"

        def get_courses():
            return [
                {"OrgUnitId": 101, "Name": "Old Course", "Code": "OLD"},
                {"OrgUnitId": 201, "Name": "Newer Course", "Code": "NEW"},
                {"OrgUnitId": 202, "Name": "Newer Course 2", "Code": "NEW"},
                {"OrgUnitId": 301, "Name": "Newest Course", "Code": "NEWEST"},
                {"OrgUnitId": 302, "Name": "Newest Course 2", "Code": "NEWEST"},
            ]

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", side_effect=get_courses), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = list(output_dir.iterdir())
            course_names = {d.name for d in course_dirs}
            assert "Newest Course-301" in course_names
            assert "Newest Course 2-302" in course_names
            assert "Old Course-101" not in course_names
            assert "Newer Course-201" not in course_names

    def test_semester_with_highest_orgunitid_selected_not_by_date(self, cli_runner, tmp_path):
        """VAL-SYNC-032: Sem II (highest OrgUnitId) should be selected even if other has later date."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2025-2026", "StartDate": "2026-09-01T00:00:00Z"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2025-2026", "StartDate": "2026-01-01T00:00:00Z"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2025-2026"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2025-2026"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid, "Title": "f", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2025-2026"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2025-2026"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course B-222" in course_dirs
            assert "Course A-111" not in course_dirs


# ---------------------------------------------------------------------------
# VAL-SYNC-012: --semester filter
# ---------------------------------------------------------------------------

class TestSemesterFilter:
    """Test --semester filter by name substring or exact ID."""

    def test_semester_filter_by_name_substring(self, cli_runner, tmp_path):
        """--semester 'Sem III' downloads courses mapped to Sem III."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
            {"OrgUnitId": 300, "Name": "Sem III", "Code": "0902_III_2025-2026"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
            {"OrgUnit": {"Id": 333, "Name": "Course C", "Code": "009_CourseC_0902_III_2025-2026"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
                "333": {"name": "Course C", "semester": "Sem III"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"},
                 {"OrgUnitId": 333, "Name": "Course C", "Code": "009_CourseC_0902_III_2025-2026"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "Sem III", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course C-333" in course_dirs
            assert "Course A-111" not in course_dirs
            assert "Course B-222" not in course_dirs

    def test_semester_filter_by_exact_orgunitid(self, cli_runner, tmp_path):
        """--semester with numeric ID matches exact semester OrgUnitId, filters courses by config."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "100", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course A-111" in course_dirs
            assert "Course B-222" not in course_dirs


# ---------------------------------------------------------------------------
# VAL-SYNC-037: Semester not found produces clear error
# ---------------------------------------------------------------------------

class TestSemesterNotFound:
    """Test error when --semester doesn't match any semester."""

    def test_semester_not_found_raises_error(self, cli_runner, tmp_path):
        """VAL-SYNC-037: No semester matching 'Sem X' produces clear error with remediation hint."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "A"}},
        ]

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
             ]):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "Sem X", "-o", str(output_dir)],
            )

            assert result.exit_code == 1
            assert "No semester matching" in result.output or "Sem X" in result.output
            assert "lighthouse semesters" in result.output


# ---------------------------------------------------------------------------
# VAL-SYNC-014: Single course by name or ID
# ---------------------------------------------------------------------------

class TestSingleCourse:
    """Test single course download by name substring or numeric ID."""

    def test_single_course_by_name_substring(self, cli_runner, tmp_path):
        """lighthouse download 'signals' downloads one course by name substring."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 10, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")):

            result = cli_runner.invoke(
                cli,
                ["download", "signals", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert len(course_dirs) == 1
            assert "Signals & Systems-44347" in course_dirs


# ---------------------------------------------------------------------------
# VAL-SYNC-035: Ambiguous course name match raises error
# ---------------------------------------------------------------------------

class TestAmbiguousCourseName:
    """Test that ambiguous name matching raises error listing all matches."""

    def test_ambiguous_course_name_raises_error_listing_all_matches(self, cli_runner, tmp_path):
        """VAL-SYNC-035: 'math' matches multiple courses → error listing both with OrgUnitIds."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 111, "Name": "Mathematics I", "Code": "M1"},
            {"OrgUnitId": 222, "Name": "Mathematics II", "Code": "M2"},
        ]):

            result = cli_runner.invoke(
                cli,
                ["download", "math", "-o", str(output_dir)],
            )

            assert result.exit_code == 1
            assert "Ambiguous" in result.output or "Multiple courses found" in result.output
            assert "111" in result.output
            assert "222" in result.output
            # Should suggest using numeric OrgUnitId
            assert "OrgUnitId" in result.output or "numeric" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-SYNC-036: Course not found raises error
# ---------------------------------------------------------------------------

class TestCourseNotFound:
    """Test non-existent course produces clear error."""

    def test_course_not_found_raises_error(self, cli_runner, tmp_path):
        """VAL-SYNC-036: Non-existent course produces clear error with remediation hint."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
        ]):

            result = cli_runner.invoke(
                cli,
                ["download", "nonexistent", "-o", str(output_dir)],
            )

            assert result.exit_code == 1
            assert "not found" in result.output or "nonexistent" in result.output
            assert "lighthouse courses" in result.output


# ---------------------------------------------------------------------------
# VAL-SYNC-013 & VAL-SYNC-038 & VAL-SYNC-039 & VAL-SYNC-057: --also flag
# ---------------------------------------------------------------------------

class TestAlsoFlag:
    """Test --also flag for ad-hoc courses outside semester scope."""

    def test_also_adds_courses_outside_semester_scope(self, cli_runner, tmp_path):
        """VAL-SYNC-013: --also adds ad-hoc courses by name/ID alongside semester scope."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
            {"OrgUnit": {"Id": 333, "Name": "Signals", "Code": "S1"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
                "333": {"name": "Signals", "semester": "Sem I"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
                 {"OrgUnitId": 333, "Name": "Signals", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "333", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            # Sem 200's course (Course B-222) + --also course (Signals-333)
            assert "Course B-222" in course_dirs
            assert "Signals-333" in course_dirs
            # Sem 100 course should NOT be included
            assert "Course A-111" not in course_dirs

    def test_also_with_invalid_course_produces_per_course_error(self, cli_runner, tmp_path):
        """VAL-SYNC-038: --also referencing non-existent course produces error for that course only."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            if cid == 99999:
                raise CourseNotFoundError(f"Course 99999 not found")
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "99999", "-o", str(output_dir)],
            )

            # Should exit with error (partial failure)
            assert result.exit_code == 1
            # Course B-222 should still be downloaded
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course B-222" in course_dirs

    def test_multiple_also_flags_accumulate(self, cli_runner, tmp_path):
        """VAL-SYNC-039: Multiple --also flags are additive, not overriding."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Signals", "Code": "S1"}},
            {"OrgUnit": {"Id": 333, "Name": "Physics", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Signals", "Code": "S1"},
                 {"OrgUnitId": 333, "Name": "Physics", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--also", "Signals", "--also", "Physics", "--also", "333", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            # All three --also courses should be present
            assert "Signals-222" in course_dirs
            assert "Physics-333" in course_dirs

    def test_also_course_already_in_semester_scope_not_double_downloaded(self, cli_runner, tmp_path):
        """VAL-SYNC-057: --also for course already in semester scope downloads it once."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 222, "Name": "Signals", "Code": "S2"}},
            {"OrgUnit": {"Id": 333, "Name": "Physics", "Code": "S2"}},
        ]

        download_calls = []

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            download_calls.append(cid)
            return f"content{cid}".encode(), "f.pdf"

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 222, "Name": "Signals", "Code": "S2"},
                 {"OrgUnitId": 333, "Name": "Physics", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "Signals", "-o", str(output_dir)],
            )

            assert result.exit_code == 0
            # Signals (222) is already in Sem II scope, --also should not download it twice
            signals_downloads = [c for c in download_calls if c == 222]
            assert len(signals_downloads) == 1, f"Signals should be downloaded once, got {len(signals_downloads)}"
            # Physics (333) should be downloaded once
            physics_downloads = [c for c in download_calls if c == 333]
            assert len(physics_downloads) == 1


# ---------------------------------------------------------------------------
# VAL-CROSS-008: Sync command also supports multi-course scope
# ---------------------------------------------------------------------------

class TestSyncMultiCourseScope:
    """Test that sync command also supports multi-course scope options."""

    def test_sync_without_course_id_syncs_latest_semester(self, cli_runner, tmp_path):
        """Sync without COURSE_ID syncs all courses from latest semester."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["sync", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            # Only Sem II (200) courses
            assert "Course B-222" in course_dirs
            assert "Course A-111" not in course_dirs

    def test_sync_with_semester_filter(self, cli_runner, tmp_path):
        """Sync --semester filters to specified semester."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["sync", "--semester", "Sem I", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course A-111" in course_dirs
            assert "Course B-222" not in course_dirs

    def test_sync_with_also_flag(self, cli_runner, tmp_path):
        """Sync --also adds ad-hoc courses."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
            {"OrgUnit": {"Id": 222, "Name": "Signals", "Code": "S1"}},
        ]

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
                 {"OrgUnitId": 222, "Name": "Signals", "Code": "S1"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            result = cli_runner.invoke(
                cli,
                ["sync", "--also", "Signals", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dirs = {d.name for d in output_dir.iterdir()}
            assert "Course A-111" in course_dirs
            assert "Signals-222" in course_dirs


# ---------------------------------------------------------------------------
# BLOCKING FIX: Ambiguous --also match raises error listing all matches
# ---------------------------------------------------------------------------

class TestAlsoAmbiguousMatch:
    """Test that _resolve_also_course raises CourseNotFoundError for ambiguous matches."""

    def test_also_ambiguous_match_raises_error_listing_all_matches(self, cli_runner, tmp_path):
        """BLOCKING FIX: Ambiguous --also name raises CourseNotFoundError listing both courses."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Mathematics I", "Code": "M1"},
                 {"OrgUnitId": 222, "Name": "Mathematics II", "Code": "M2"},
             ]):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "math", "-o", str(output_dir)],
            )

            assert result.exit_code == 1
            assert "Ambiguous" in result.output or "Multiple courses found" in result.output
            assert "111" in result.output
            assert "222" in result.output
            assert "OrgUnitId" in result.output or "numeric" in result.output.lower()

    def test_also_not_found_raises_error_with_remediation_hint(self, cli_runner, tmp_path):
        """BLOCKING FIX: Invalid --also course raises CourseNotFoundError with hint."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]):

            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "nonexistent", "-o", str(output_dir)],
            )

            assert result.exit_code == 1
            assert "not found" in result.output or "nonexistent" in result.output
            assert "lighthouse courses" in result.output


class TestAlsoDuplicateDedup:
    """Test that duplicate --also entries are deduplicated."""

    def test_duplicate_also_entries_deduplicated_no_double_download(self, cli_runner, tmp_path):
        """BLOCKING FIX: Duplicate --also entries (same course) are deduplicated before download."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        download_calls = []

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            download_calls.append(cid)
            return f"content{cid}".encode(), "f.pdf"

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            # Same course specified twice via --also
            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "Course B", "--also", "Course B", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # Course B should only be downloaded once, not twice
            course_b_downloads = [c for c in download_calls if c == 222]
            assert len(course_b_downloads) == 1, f"Course B should be downloaded once, got {len(course_b_downloads)}"

    def test_also_numeric_id_same_course_twice_deduplicated(self, cli_runner, tmp_path):
        """Duplicate --also with same numeric ID is deduplicated."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir()

        semesters = [
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S2"}},
        ]

        download_calls = []

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
                 "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
            ]}]}

        def download_topic_file(cid, tid):
            download_calls.append(cid)
            return f"content{cid}".encode(), "f.pdf"

        with patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "S2"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):

            # Same course by numeric ID specified twice
            result = cli_runner.invoke(
                cli,
                ["download", "--semester", "200", "--also", "222", "--also", "222", "-o", str(output_dir)],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # Course 222 should only be downloaded once
            course_222_downloads = [c for c in download_calls if c == 222]
            assert len(course_222_downloads) == 1, f"Course 222 should be downloaded once, got {len(course_222_downloads)}"
