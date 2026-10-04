"""Tests for lighthouse config courses command and config-based filtering."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.course_config import (
    load as _load_course_config,
)
from lighthouse_cli.course_config import (
    save as _save_course_config,
)
from lighthouse_cli.credential_store import CredentialStoreError

INTRO_CS_ENROLLMENT = {
    "OrgUnit": {"Id": 1001, "Name": "Intro to CS", "Code": "CS101_2025"},
    "Access": {"IsActive": True},
}
VALID_ENROLLMENT = {"OrgUnit": {"Id": 1001, "Name": "Valid", "Code": "C101"}}
UNSAFE_NAME = "password=NAME_SECRET\x1b[31m"
UNSAFE_CODE = "token=CODE_SECRET\n"
UNSAFE_ENROLLMENT = {"OrgUnit": {"Id": 1001, "Name": UNSAFE_NAME, "Code": UNSAFE_CODE}}


@pytest.fixture
def cfg_path(tmp_path: Path):
    """The course config file the CLI uses; it does not exist until a test writes it."""
    path = tmp_path / "course-config.json"
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", path):
        yield path


def _write_tracked(cfg_path: Path, tracked) -> None:
    cfg_path.write_text(json.dumps({"tracked_courses": tracked}))


def _enrollments(*records):
    return patch.object(LighthouseClient, "get_course_enrollments", return_value=list(records))

# ---------------------------------------------------------------------------
# Config helper tests
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    """Tests for _load_course_config / _save_course_config."""

    @pytest.mark.parametrize(
        "raw",
        [None, "NOT JSON{{{", "[]", json.dumps({"tracked_courses": []})],
        ids=["no-file", "corrupt-json", "non-object-json", "non-object-tracked-courses"],
    )
    def test_load_returns_empty_for_missing_or_malformed_file(self, cfg_path: Path, raw) -> None:
        if raw is not None:
            cfg_path.write_text(raw)
        assert _load_course_config() == {}

    def test_save_and_load_roundtrip(self, cfg_path: Path) -> None:
        config = {
            "1001": {"name": "Intro to CS", "semester": "Sem IV"},
            "1002": {"name": "Linear Algebra", "semester": "Sem IV"},
        }
        _save_course_config(config)
        assert _load_course_config() == config

    def test_load_skips_malformed_entries_and_normalizes_fields(self, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {
            "1001": None,
            "1002": {"name": "Valid", "semester": "Sem V"},
            "1003": {"name": 7, "semester": []},
        })
        assert _load_course_config() == {
            "1002": {"name": "Valid", "semester": "Sem V"},
            "1003": {"name": "", "semester": ""},
        }

    def test_load_canonicalizes_positive_course_id_keys(self, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {
            " 001002 ": {"name": "Valid", "semester": "Sem V"},
            "not-an-id": {"name": "Ignored", "semester": "Sem V"},
        })
        assert _load_course_config() == {"1002": {"name": "Valid", "semester": "Sem V"}}

    def test_save_rejects_symlinked_course_config_path(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        linked_parent = tmp_path / "linked-config"
        linked_parent.symlink_to(outside, target_is_directory=True)

        with patch(
            "lighthouse_cli.course_config.COURSE_CONFIG_FILE",
            linked_parent / "course-config.json",
        ):
            with pytest.raises(CredentialStoreError, match="symlink"):
                _save_course_config({"1001": {"name": "Course", "semester": "Sem V"}})

        assert list(outside.iterdir()) == []

    def test_save_uses_private_directory_and_file_modes(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "private-config" / "course-config.json"

        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg_path):
            _save_course_config({"1001": {"name": "Course", "semester": "Sem V"}})

        assert stat.S_IMODE(cfg_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# config courses --list / --json / --reset tests
# ---------------------------------------------------------------------------

class TestConfigCoursesList:
    """Tests for lighthouse config courses --list / --json / --reset."""

    def test_list_shows_tracked_courses(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {
            "1001": {"name": "Intro to CS", "semester": "Sem IV"},
            "1002": {"name": "Linear Algebra", "semester": "Sem III"},
        })
        result = cli_runner.invoke(cli, ["config", "courses", "--list"])
        assert result.exit_code == 0
        assert "1001" in result.output
        assert "Intro to CS" in result.output

    def test_list_empty_when_no_config(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        result = cli_runner.invoke(cli, ["config", "courses", "--list"])
        assert result.exit_code == 0
        assert "No courses tracked" in result.output

    def test_json_output(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {"1001": {"name": "Intro to CS", "semester": "Sem IV"}})
        result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == "1001"

    @pytest.mark.parametrize("tracked", [{}, []], ids=["no-courses", "malformed-config"])
    def test_json_output_is_empty_array(
        self, cli_runner: CliRunner, cfg_path: Path, tracked
    ) -> None:
        _write_tracked(cfg_path, tracked)
        result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        assert result.stderr == ""

    def test_json_output_skips_malformed_course_entry(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        _write_tracked(cfg_path, {"1001": None, "1002": {"name": "Valid", "semester": "Sem V"}})
        result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == [
            {"id": "1002", "name": "Valid", "semester": "Sem V"}
        ]

    def test_list_skips_oversized_id_and_preserves_valid_sibling(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        oversized_id = "9" * 5000
        _write_tracked(cfg_path, {
            oversized_id: {"name": "must-not-be-echoed", "semester": "bad"},
            "1002": {"name": "Valid", "semester": "Sem V"},
        })
        json_result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        human_result = cli_runner.invoke(cli, ["config", "courses", "--list"])

        assert json_result.exit_code == 0
        assert json.loads(json_result.stdout) == [
            {"id": "1002", "name": "Valid", "semester": "Sem V"}
        ]
        assert human_result.exit_code == 0
        assert "1002" in human_result.stdout
        assert "Valid" in human_result.stdout
        for result in (json_result, human_result):
            assert oversized_id not in result.stdout
            assert oversized_id not in result.stderr

    def test_list_sanitizes_stored_labels_in_json_and_human_output(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        unsafe_name = "password=LOCAL_SECRET"
        unsafe_semester = "Sem V\x1b[31m"
        _write_tracked(cfg_path, {
            "1001": {"name": unsafe_name, "semester": unsafe_semester},
            "1002": {"name": "Valid", "semester": "Sem IV"},
        })
        json_result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        human_result = cli_runner.invoke(cli, ["config", "courses", "--list"])

        assert json_result.exit_code == 0
        assert json.loads(json_result.stdout) == [
            {"id": "1001", "name": "Unknown course", "semester": ""},
            {"id": "1002", "name": "Valid", "semester": "Sem IV"},
        ]
        assert human_result.exit_code == 0
        assert "Unknown course" in human_result.stdout
        assert "Unmapped" in human_result.stdout
        for result in (json_result, human_result):
            for stream in (result.stdout, result.stderr):
                assert unsafe_name not in stream
                assert unsafe_semester not in stream

    def test_json_reset_clears_config_and_outputs_json(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        _write_tracked(cfg_path, {"1001": {"name": "Intro to CS", "semester": "Sem IV"}})
        result = cli_runner.invoke(cli, ["config", "courses", "--reset", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        assert result.stderr == ""
        assert json.loads(cfg_path.read_text())["tracked_courses"] == {}

    def test_reset_clears_config(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {"1001": {"name": "Intro to CS", "semester": "Sem IV"}})
        result = cli_runner.invoke(cli, ["config", "courses", "--reset"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert json.loads(cfg_path.read_text())["tracked_courses"] == {}


# ---------------------------------------------------------------------------
# config courses --add / --remove tests
# ---------------------------------------------------------------------------

class TestConfigCoursesAddRemove:
    """Tests for lighthouse config courses --add / --remove."""

    def test_add_by_id(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        with _enrollments(INTRO_CS_ENROLLMENT):
            result = cli_runner.invoke(cli, ["config", "courses", "--add", "1001", "--semester", "Sem IV"])
        assert result.exit_code == 0
        assert "Tracking" in result.output
        data = json.loads(cfg_path.read_text())
        assert "1001" in data["tracked_courses"]
        assert data["tracked_courses"]["1001"]["semester"] == "Sem IV"

    def test_add_by_name_ignores_surrounding_whitespace(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        with _enrollments({"OrgUnit": {"Id": 1001, "Name": "Intro to CS", "Code": "CS101"}}):
            result = cli_runner.invoke(
                cli,
                ["config", "courses", "--add", "  Intro to CS  ", "--json"],
            )

        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["id"] == "1001"

    def test_json_add_mutates_config_and_outputs_json(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        with _enrollments(INTRO_CS_ENROLLMENT):
            result = cli_runner.invoke(
                cli,
                ["config", "courses", "--add", "1001", "--semester", "Sem IV", "--json"],
            )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data == [{"id": "1001", "name": "Intro to CS", "semester": "Sem IV"}]
        assert result.stderr == ""
        saved = json.loads(cfg_path.read_text())
        assert saved["tracked_courses"]["1001"]["semester"] == "Sem IV"

    def test_add_uses_fixed_fallbacks_for_unsafe_name_and_code(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        with _enrollments(UNSAFE_ENROLLMENT):
            result = cli_runner.invoke(cli, ["config", "courses", "--add", "1001", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == [
            {"id": "1001", "name": "Unknown course", "semester": ""}
        ]
        assert result.stderr == ""
        saved = json.loads(cfg_path.read_text())
        assert saved["tracked_courses"]["1001"]["name"] == "Unknown course"
        for stream in (result.stdout, result.stderr):
            assert UNSAFE_NAME not in stream
            assert UNSAFE_CODE not in stream

    def test_interactive_table_uses_fixed_fallbacks_for_unsafe_name_and_code(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        with _enrollments(UNSAFE_ENROLLMENT):
            result = cli_runner.invoke(cli, ["config", "courses"], input="1001\nSem V\n")
        assert result.exit_code == 0
        assert "Unknown course" in result.stdout
        assert "Unknown code" in result.stdout
        for stream in (result.stdout, result.stderr):
            assert UNSAFE_NAME not in stream
            assert UNSAFE_CODE not in stream
        saved = json.loads(cfg_path.read_text())
        assert saved["tracked_courses"]["1001"] == {
            "name": "Unknown course",
            "semester": "Sem V",
        }

    def test_interactive_table_and_prompt_sanitize_stored_labels(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        unsafe_name = "password=LOCAL_SECRET"
        unsafe_semester = "Sem V\x1b[31m"
        _write_tracked(cfg_path, {"1001": {"name": unsafe_name, "semester": unsafe_semester}})
        with _enrollments(VALID_ENROLLMENT):
            result = cli_runner.invoke(cli, ["config", "courses"], input="1001\nSem VI\n")
        assert result.exit_code == 0
        assert "tracked" in result.stdout
        assert "Semester for Valid (1001):" in result.stdout
        for stream in (result.stdout, result.stderr):
            assert unsafe_name not in stream
            assert unsafe_semester not in stream
        saved = json.loads(cfg_path.read_text())
        assert saved["tracked_courses"]["1001"] == {
            "name": "Valid",
            "semester": "Sem VI",
        }

    def test_interactive_unmatched_selection_does_not_echo_input(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        unsafe_selection = "token=SELECTION_SECRET\x1b[31m"
        with _enrollments(VALID_ENROLLMENT):
            result = cli_runner.invoke(cli, ["config", "courses"], input=f"{unsafe_selection}\n")
        assert result.exit_code == 1
        assert "Warning: selected course was not found, skipping." in result.stdout
        assert unsafe_selection not in result.stdout
        assert unsafe_selection not in result.stderr

    def test_add_not_found(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        with _enrollments():
            result = cli_runner.invoke(cli, ["config", "courses", "--add", "9999"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remove(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {"1001": {"name": "Intro to CS", "semester": "Sem IV"}})
        result = cli_runner.invoke(cli, ["config", "courses", "--remove", "1001"])
        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "1001" not in json.loads(cfg_path.read_text())["tracked_courses"]

    def test_json_remove_mutates_config_and_outputs_json(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        _write_tracked(cfg_path, {
            "1001": {"name": "Intro to CS", "semester": "Sem IV"},
            "1002": {"name": "Linear Algebra", "semester": "Sem III"},
        })
        result = cli_runner.invoke(cli, ["config", "courses", "--remove", "1001", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == [
            {"id": "1002", "name": "Linear Algebra", "semester": "Sem III"},
        ]
        assert result.stderr == ""
        assert "1001" not in json.loads(cfg_path.read_text())["tracked_courses"]

    def test_remove_uses_canonical_id_and_sanitizes_human_name(
        self, cli_runner: CliRunner, cfg_path: Path
    ) -> None:
        unsafe_name = "token=LOCAL_TOKEN"
        _write_tracked(cfg_path, {
            "0001001": {"name": unsafe_name, "semester": "Sem IV"},
            "1002": {"name": "Valid", "semester": "Sem V"},
        })
        result = cli_runner.invoke(cli, ["config", "courses", "--remove", "1001"])

        assert result.exit_code == 0
        assert "Stopped tracking Unknown course (1001)" in result.stdout
        assert unsafe_name not in result.stdout
        assert unsafe_name not in result.stderr
        saved = json.loads(cfg_path.read_text())
        assert "0001001" not in saved["tracked_courses"]
        assert "1002" in saved["tracked_courses"]

    def test_remove_not_tracked(self, cli_runner: CliRunner, cfg_path: Path) -> None:
        _write_tracked(cfg_path, {})
        result = cli_runner.invoke(cli, ["config", "courses", "--remove", "9999"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# courses --semester / --tracked tests (config-based filtering)
# ---------------------------------------------------------------------------

class TestCoursesWithConfig:
    """Tests for courses command using config-based semester filtering."""

    def test_courses_semester_filter_with_config(
        self, cli_runner: CliRunner, sample_courses: list, cfg_path: Path
    ) -> None:
        _write_tracked(cfg_path, {
            "44347": {"name": "Signals & Systems", "semester": "Sem IV"},
            "44348": {"name": "Eng Math III", "semester": "Sem III"},
        })
        with _enrollments(*sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--semester", "Sem IV", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert all(c["semester"] == "Sem IV" for c in data)

    def test_courses_projection_sanitizes_local_semester_json_and_human(
        self, cli_runner: CliRunner, sample_courses: list, cfg_path: Path
    ) -> None:
        unsafe_semester = "password=LOCAL_SECRET\x1b[31m"
        _write_tracked(cfg_path, {
            "44347": {"name": "Signals & Systems", "semester": unsafe_semester},
        })
        with _enrollments(*sample_courses):
            json_result = cli_runner.invoke(cli, ["courses", "--json"])
            human_result = cli_runner.invoke(cli, ["courses"])

        assert json_result.exit_code == 0
        json_courses = json.loads(json_result.stdout)
        configured = next(course for course in json_courses if course["OrgUnitId"] == 44347)
        assert configured["semester"] == ""
        assert configured["semester_source"] == "unmapped"
        assert human_result.exit_code == 0
        assert "Unmapped" in human_result.stdout
        for result in (json_result, human_result):
            assert unsafe_semester not in result.stdout
            assert unsafe_semester not in result.stderr

    @pytest.mark.parametrize(
        "flags", [["--semester", "Sem IV"], ["--tracked"]], ids=["semester", "tracked"]
    )
    def test_courses_filter_requires_config(
        self, cli_runner: CliRunner, sample_courses: list, cfg_path: Path, flags
    ) -> None:
        with _enrollments(*sample_courses):
            result = cli_runner.invoke(cli, ["courses", *flags])

        assert result.exit_code == 1
        assert "No course config" in result.output

    def test_courses_tracked_flag(
        self, cli_runner: CliRunner, sample_courses: list, cfg_path: Path
    ) -> None:
        _write_tracked(cfg_path, {"44347": {"name": "Signals & Systems", "semester": "Sem IV"}})
        with _enrollments(*sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--tracked", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["OrgUnitId"] == 44347

    def test_courses_no_filter_shows_all(
        self, cli_runner: CliRunner, sample_courses: list, cfg_path: Path
    ) -> None:
        """Without --semester or --tracked, all courses are shown."""
        with _enrollments(*sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 3
