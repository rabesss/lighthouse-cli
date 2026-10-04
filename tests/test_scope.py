"""Tests for multi-course scope resolution (VAL-SYNC-011, 012, 013, 014, 032, 035, 036, 037, 038, 039, 057, VAL-CROSS-008)."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from lighthouse_cli.api import CourseNotFoundError, LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.commands import _resolve_also_course, _resolve_course_scope

SEM_I = {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}
SEM_II = {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"}
AB_SEMESTERS = [
    {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
    {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
]
AB_COURSES = [
    (111, "Course A", "009_CourseA_0902_I_2024-2025"),
    (222, "Course B", "009_CourseB_0902_II_2024-2025"),
]
AB_TRACKED = {111: "Sem I", 222: "Sem II"}


def _one_file_toc(cid):
    return {"Modules": [{"ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
        {"TopicId": cid * 10, "Title": "f.pdf", "TypeIdentifier": "File",
         "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
    ]}]}


def _no_modules(cid):
    return {"Modules": []}


def _run(cli_runner, tmp_path, args, courses, semesters=(), tracked=None, toc=_one_file_toc):
    """Invoke ``lighthouse ARGS -o DIR`` against a patched client.

    ``courses`` holds (OrgUnitId, Name, Code) rows served as both the enrollments
    and the course catalog; ``tracked`` maps an OrgUnitId to its semester label in
    course-config.json. Returns the result, the course folder names written, and
    the OrgUnitIds passed to download_topic_file in call order.
    """
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    cfg_path = tmp_path / "course-config.json"
    names = {oid: name for oid, name, _code in courses}
    cfg_path.write_text(json.dumps({"tracked_courses": {
        str(oid): {"name": names[oid], "semester": label} for oid, label in (tracked or {}).items()
    }}))
    download_calls = []

    def download_topic_file(cid, tid):
        download_calls.append(cid)
        return f"content{cid}".encode(), "f.pdf"

    enrollments = [
        {"OrgUnit": {"Id": oid, "Name": name, "Code": code}} for oid, name, code in courses
    ]
    catalog = [{"OrgUnitId": oid, "Name": name, "Code": code} for oid, name, code in courses]
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg_path), \
         patch.object(LighthouseClient, "get_semesters", return_value=list(semesters)), \
         patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
         patch.object(LighthouseClient, "get_courses", return_value=catalog), \
         patch.object(LighthouseClient, "get_content_toc", side_effect=toc), \
         patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic_file):
        result = cli_runner.invoke(cli, [*args, "-o", str(output_dir)])
    return result, {d.name for d in output_dir.iterdir()}, download_calls


def test_blank_also_selector_never_matches_every_course() -> None:
    client = MagicMock()
    client.get_enrolled_courses.return_value = [
        {"OrgUnitId": 123, "Name": "Course"},
    ]

    with pytest.raises(CourseNotFoundError, match="cannot be empty"):
        _resolve_also_course(client, "   ")


def test_course_scope_order_is_deterministic_and_does_not_mutate_inputs() -> None:
    """Semester IDs sort numerically while extras retain input order and dedupe."""
    semesters = [{"OrgUnitId": 200, "Name": "Sem II"}]
    enrollments = [
        {"OrgUnit": {"Id": 302, "Name": "Course C"}},
        {"OrgUnit": {"Id": 300, "Name": "Course A"}},
        {"OrgUnit": {"Id": 301, "Name": "Course B"}},
        {"OrgUnit": {"Id": 300, "Name": "Course A"}},
    ]
    courses = [
        {"OrgUnitId": 304, "Name": "Course D"},
        {"OrgUnitId": 305, "Name": "Course E"},
        {"OrgUnitId": 300, "Name": "Course A"},
        {"OrgUnitId": 301, "Name": "Course B"},
        {"OrgUnitId": 302, "Name": "Course C"},
    ]
    config = {
        "300": {"semester": "Sem II"},
        "301": {"semester": "Sem II"},
        "302": {"semester": "Sem II"},
    }
    also_courses = ["305", "304", "305", "Course A"]
    original_semesters = deepcopy(semesters)
    original_enrollments = deepcopy(enrollments)
    original_also_courses = list(also_courses)

    client = MagicMock(spec=LighthouseClient)
    client.get_semesters.return_value = semesters
    client.get_course_enrollments.return_value = enrollments
    client.get_enrolled_courses.return_value = courses
    client.get_courses.return_value = courses

    with patch("lighthouse_cli.commands._load_course_config", return_value=config):
        result = _resolve_course_scope(client, "200", also_courses)

    assert result == ([300, 301, 302, 305, 304], "Sem II", 200, [])
    assert semesters == original_semesters
    assert enrollments == original_enrollments
    assert also_courses == original_also_courses


# ---------------------------------------------------------------------------
# VAL-SYNC-011 & VAL-SYNC-032: Default to latest semester (highest OrgUnitId)
# ---------------------------------------------------------------------------

class TestLatestSemesterResolution:

    def test_default_downloads_all_courses_from_latest_semester_by_highest_orgunitid(self, cli_runner, tmp_path):
        """VAL-SYNC-011 & VAL-SYNC-032: Latest semester = highest OrgUnitId, not by date or name."""
        semesters = [
            {"OrgUnitId": 100, "Name": "Old Sem", "Code": "OLD"},
            {"OrgUnitId": 200, "Name": "Newer Sem", "Code": "NEW"},
            {"OrgUnitId": 300, "Name": "Newest Sem", "Code": "NEWEST"},
        ]
        courses = [
            (101, "Old Course", "OLD"),
            (201, "Newer Course", "NEW"),
            (202, "Newer Course 2", "NEW"),
            (301, "Newest Course", "NEWEST"),
            (302, "Newest Course 2", "NEWEST"),
        ]
        tracked = {
            101: "Old Sem", 201: "Newer Sem", 202: "Newer Sem", 301: "Newest Sem", 302: "Newest Sem",
        }

        result, course_names, _ = _run(cli_runner, tmp_path, ["download"], courses, semesters, tracked)

        assert result.exit_code == 0, result.output
        assert "Newest Course-301" in course_names
        assert "Newest Course 2-302" in course_names
        assert "Old Course-101" not in course_names
        assert "Newer Course-201" not in course_names

    def test_semester_with_highest_orgunitid_selected_not_by_date(self, cli_runner, tmp_path):
        """VAL-SYNC-032: Sem II (highest OrgUnitId) should be selected even if other has later date."""
        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2025-2026", "StartDate": "2026-09-01T00:00:00Z"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2025-2026", "StartDate": "2026-01-01T00:00:00Z"},
        ]
        courses = [
            (111, "Course A", "009_CourseA_0902_I_2025-2026"),
            (222, "Course B", "009_CourseB_0902_II_2025-2026"),
        ]

        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download"], courses, semesters, AB_TRACKED,
        )

        assert result.exit_code == 0, result.output
        assert "Course B-222" in course_dirs
        assert "Course A-111" not in course_dirs


# ---------------------------------------------------------------------------
# VAL-SYNC-012: --semester filter
# ---------------------------------------------------------------------------

class TestSemesterFilter:

    def test_semester_filter_by_name_substring(self, cli_runner, tmp_path):
        """--semester 'Sem III' downloads courses mapped to Sem III."""
        semesters = [*AB_SEMESTERS, {"OrgUnitId": 300, "Name": "Sem III", "Code": "0902_III_2025-2026"}]
        courses = [*AB_COURSES, (333, "Course C", "009_CourseC_0902_III_2025-2026")]
        tracked = {**AB_TRACKED, 333: "Sem III"}

        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "Sem III"], courses, semesters, tracked,
        )

        assert result.exit_code == 0, result.output
        assert "Course C-333" in course_dirs
        assert "Course A-111" not in course_dirs
        assert "Course B-222" not in course_dirs

    def test_semester_filter_by_exact_orgunitid(self, cli_runner, tmp_path):
        """--semester with numeric ID matches exact semester OrgUnitId, filters courses by config."""
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "100"],
            AB_COURSES, AB_SEMESTERS, AB_TRACKED,
        )

        assert result.exit_code == 0, result.output
        assert "Course A-111" in course_dirs
        assert "Course B-222" not in course_dirs


# ---------------------------------------------------------------------------
# VAL-SYNC-037: Semester not found produces clear error
# ---------------------------------------------------------------------------

class TestSemesterNotFound:

    def test_semester_not_found_raises_error(self, cli_runner, tmp_path):
        """VAL-SYNC-037: No semester matching 'Sem X' produces clear error with remediation hint."""
        result, _, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "Sem X"],
            [(111, "Course A", "A")], [SEM_I, SEM_II], {111: "Sem I"},
        )

        assert result.exit_code == 1
        assert "No matching semester" in result.output
        assert "Sem X" not in result.output
        assert "lighthouse semesters" in result.output


# ---------------------------------------------------------------------------
# VAL-SYNC-014: Single course by name or ID
# ---------------------------------------------------------------------------

class TestSingleCourse:

    def test_single_course_by_name_substring(self, cli_runner, tmp_path):
        """lighthouse download 'signals' downloads one course by name substring."""
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download", "signals"], [(44347, "Signals & Systems", "X")],
        )

        assert result.exit_code == 0, result.output
        assert course_dirs == {"Signals & Systems-44347"}


# ---------------------------------------------------------------------------
# VAL-SYNC-035: Ambiguous course name match raises error
# ---------------------------------------------------------------------------

class TestAmbiguousCourseName:

    def test_ambiguous_course_name_raises_error_listing_all_matches(self, cli_runner, tmp_path):
        """VAL-SYNC-035: 'math' matches multiple courses → error listing both with OrgUnitIds."""
        result, _, _ = _run(
            cli_runner, tmp_path, ["download", "math"],
            [(111, "Mathematics I", "M1"), (222, "Mathematics II", "M2")],
        )

        assert result.exit_code == 1
        assert "Ambiguous course match" in result.output
        # The candidate IDs came from an untrusted upstream response and
        # must not be copied into human-facing error output.
        assert "111" not in result.output
        assert "222" not in result.output
        assert "numeric OrgUnitId" in result.output


# ---------------------------------------------------------------------------
# VAL-SYNC-036: Course not found raises error
# ---------------------------------------------------------------------------

class TestCourseNotFound:

    def test_course_not_found_raises_error(self, cli_runner, tmp_path):
        """VAL-SYNC-036: Non-existent course produces clear error with remediation hint."""
        result, _, _ = _run(
            cli_runner, tmp_path, ["download", "nonexistent"], [(44347, "Signals & Systems", "X")],
        )

        assert result.exit_code == 1
        assert "not found" in result.output or "nonexistent" in result.output
        assert "lighthouse courses" in result.output


# ---------------------------------------------------------------------------
# VAL-SYNC-013 & VAL-SYNC-038 & VAL-SYNC-039 & VAL-SYNC-057: --also flag
# ---------------------------------------------------------------------------

class TestAlsoFlag:

    def test_also_adds_courses_outside_semester_scope(self, cli_runner, tmp_path):
        """VAL-SYNC-013: --also adds ad-hoc courses by name/ID alongside semester scope."""
        courses = [(111, "Course A", "S1"), (222, "Course B", "S2"), (333, "Signals", "S1")]
        tracked = {111: "Sem I", 222: "Sem II", 333: "Sem I"}

        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "200", "--also", "333"],
            courses, [SEM_I, SEM_II], tracked,
        )

        assert result.exit_code == 0, result.output
        # Sem 200's course (Course B-222) + --also course (Signals-333)
        assert "Course B-222" in course_dirs
        assert "Signals-333" in course_dirs
        assert "Course A-111" not in course_dirs

    def test_also_with_invalid_course_produces_per_course_error(self, cli_runner, tmp_path):
        """VAL-SYNC-038: --also referencing non-existent course produces error for that course only."""
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "200", "--also", "99999"],
            [(111, "Course A", "S1"), (222, "Course B", "S2")], [SEM_I, SEM_II], AB_TRACKED,
        )

        # Invalid --also is scope leniency: warning on stderr, exit unaffected
        # (uniform exit matrix — also_errors never affect exit codes).
        assert result.exit_code == 0
        assert "Course not found. Run: lighthouse courses" in result.output
        assert "99999" not in result.output
        assert "Course B-222" in course_dirs

    def test_multiple_also_flags_accumulate(self, cli_runner, tmp_path):
        """VAL-SYNC-039: Multiple --also flags are additive, not overriding."""
        courses = [(111, "Course A", "S1"), (222, "Signals", "S1"), (333, "Physics", "S1")]

        result, course_dirs, _ = _run(
            cli_runner, tmp_path,
            ["download", "--also", "Signals", "--also", "Physics", "--also", "333"],
            courses, [SEM_I], {111: "Sem I", 222: "Sem I", 333: "Sem I"},
        )

        assert result.exit_code == 0, result.output
        assert "Signals-222" in course_dirs
        assert "Physics-333" in course_dirs

    def test_also_course_already_in_semester_scope_not_double_downloaded(self, cli_runner, tmp_path):
        """VAL-SYNC-057: --also for course already in semester scope downloads it once."""
        result, _, download_calls = _run(
            cli_runner, tmp_path, ["download", "--semester", "200", "--also", "Signals"],
            [(222, "Signals", "S2"), (333, "Physics", "S2")], [SEM_II], {222: "Sem II", 333: "Sem II"},
        )

        assert result.exit_code == 0
        assert download_calls.count(222) == 1, download_calls
        assert download_calls.count(333) == 1, download_calls


# ---------------------------------------------------------------------------
# VAL-CROSS-008: Sync command also supports multi-course scope
# ---------------------------------------------------------------------------

class TestSyncMultiCourseScope:

    def test_sync_without_course_id_syncs_latest_semester(self, cli_runner, tmp_path):
        """Sync without COURSE_ID syncs all courses from latest semester."""
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["sync"], AB_COURSES, AB_SEMESTERS, AB_TRACKED,
        )

        assert result.exit_code == 0, result.output
        assert "Course B-222" in course_dirs
        assert "Course A-111" not in course_dirs

    def test_sync_with_semester_filter(self, cli_runner, tmp_path):
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["sync", "--semester", "Sem I"], AB_COURSES, AB_SEMESTERS, AB_TRACKED,
        )

        assert result.exit_code == 0, result.output
        assert "Course A-111" in course_dirs
        assert "Course B-222" not in course_dirs

    def test_sync_with_also_flag(self, cli_runner, tmp_path):
        result, course_dirs, _ = _run(
            cli_runner, tmp_path, ["sync", "--also", "Signals"],
            [(111, "Course A", "S1"), (222, "Signals", "S1")], [SEM_I], {111: "Sem I", 222: "Sem I"},
        )

        assert result.exit_code == 0, result.output
        assert "Course A-111" in course_dirs
        assert "Signals-222" in course_dirs


# ---------------------------------------------------------------------------
# BLOCKING FIX: Ambiguous --also match raises error listing all matches
# ---------------------------------------------------------------------------

class TestAlsoAmbiguousMatch:

    def test_also_ambiguous_match_raises_error_listing_all_matches(self, cli_runner, tmp_path):
        result, _, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "200", "--also", "math"],
            [(111, "Mathematics I", "M1"), (222, "Mathematics II", "M2")], [SEM_II], {222: "Sem II"},
            toc=_no_modules,
        )

        assert result.exit_code == 0
        assert "Ambiguous course match" in result.output
        assert "111" not in result.output
        assert "222" not in result.output
        assert "numeric OrgUnitId" in result.output

    def test_also_not_found_raises_error_with_remediation_hint(self, cli_runner, tmp_path):
        result, _, _ = _run(
            cli_runner, tmp_path, ["download", "--semester", "200", "--also", "nonexistent"],
            [(222, "Course B", "S2")], [SEM_II], {222: "Sem II"}, toc=_no_modules,
        )

        assert result.exit_code == 0
        assert "Course not found. Run: lighthouse courses" in result.output
        assert "nonexistent" not in result.output


class TestAlsoDuplicateDedup:

    @pytest.mark.parametrize("selector", ["Course B", "222"], ids=["name", "numeric-id"])
    def test_duplicate_also_entries_deduplicated_no_double_download(
        self, cli_runner, tmp_path, selector,
    ):
        """Duplicate --also entries for the same course are deduplicated before download."""
        result, _, download_calls = _run(
            cli_runner, tmp_path,
            ["download", "--semester", "200", "--also", selector, "--also", selector],
            [(222, "Course B", "S2")], [SEM_II], {222: "Sem II"},
        )

        assert result.exit_code == 0, result.output
        assert download_calls.count(222) == 1, download_calls
