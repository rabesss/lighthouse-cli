"""Tests for lighthouse config courses command and config-based filtering."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.commands import (
    _load_course_config,
    _save_course_config,
)


# ---------------------------------------------------------------------------
# Config helper tests
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    """Tests for _load_course_config / _save_course_config."""

    def test_load_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", tmp_path / "nocfg.json"):
            assert _load_course_config() == {}

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            config = {
                "1001": {"name": "Intro to CS", "semester": "Sem IV"},
                "1002": {"name": "Linear Algebra", "semester": "Sem IV"},
            }
            _save_course_config(config)
            loaded = _load_course_config()
            assert loaded == config

    def test_load_handles_corrupt_json(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text("NOT JSON{{{")
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            assert _load_course_config() == {}


# ---------------------------------------------------------------------------
# config courses --list / --json / --reset tests
# ---------------------------------------------------------------------------

class TestConfigCoursesList:
    """Tests for lighthouse config courses --list / --json / --reset."""

    def test_list_shows_tracked_courses(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "1001": {"name": "Intro to CS", "semester": "Sem IV"},
                "1002": {"name": "Linear Algebra", "semester": "Sem III"},
            }
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--list"])
        assert result.exit_code == 0
        assert "1001" in result.output
        assert "Intro to CS" in result.output

    def test_list_empty_when_no_config(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "nocfg.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--list"])
        assert result.exit_code == 0
        assert "No courses tracked" in result.output

    def test_json_output(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "1001": {"name": "Intro to CS", "semester": "Sem IV"},
            }
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == "1001"

    def test_reset_clears_config(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {"1001": {"name": "Intro to CS", "semester": "Sem IV"}}
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--reset"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        # Verify file was cleared
        data = json.loads(cfg_path.read_text())
        assert data["tracked_courses"] == {}


# ---------------------------------------------------------------------------
# config courses --add / --remove tests
# ---------------------------------------------------------------------------

class TestConfigCoursesAddRemove:
    """Tests for lighthouse config courses --add / --remove."""

    def test_add_by_id(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        enrollments = [
            {"OrgUnit": {"Id": 1001, "Name": "Intro to CS", "Code": "CS101_2025"}, "Access": {"IsActive": True}},
        ]
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments):
            result = cli_runner.invoke(cli, ["config", "courses", "--add", "1001", "--semester", "Sem IV"])
        assert result.exit_code == 0
        assert "Tracking" in result.output
        data = json.loads(cfg_path.read_text())
        assert "1001" in data["tracked_courses"]
        assert data["tracked_courses"]["1001"]["semester"] == "Sem IV"

    def test_add_not_found(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=[]):
            result = cli_runner.invoke(cli, ["config", "courses", "--add", "9999"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remove(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {"1001": {"name": "Intro to CS", "semester": "Sem IV"}}
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--remove", "1001"])
        assert result.exit_code == 0
        assert "Stopped" in result.output
        data = json.loads(cfg_path.read_text())
        assert "1001" not in data["tracked_courses"]

    def test_remove_not_tracked(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({"tracked_courses": {}}))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path):
            result = cli_runner.invoke(cli, ["config", "courses", "--remove", "9999"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# courses --semester / --tracked tests (config-based filtering)
# ---------------------------------------------------------------------------

class TestCoursesWithConfig:
    """Tests for courses command using config-based semester filtering."""

    def test_courses_semester_filter_with_config(
        self, cli_runner: CliRunner, sample_courses: list, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "44347": {"name": "Signals & Systems", "semester": "Sem IV"},
                "44348": {"name": "Eng Math III", "semester": "Sem III"},
            }
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--semester", "Sem IV", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert all(c["semester"] == "Sem IV" for c in data)

    def test_courses_semester_filter_no_config(
        self, cli_runner: CliRunner, sample_courses: list, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "nocfg.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--semester", "Sem IV"])

        assert result.exit_code == 1
        assert "No course config" in result.output

    def test_courses_tracked_flag(
        self, cli_runner: CliRunner, sample_courses: list, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "44347": {"name": "Signals & Systems", "semester": "Sem IV"},
            }
        }))
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--tracked", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["OrgUnitId"] == 44347

    def test_courses_tracked_no_config(
        self, cli_runner: CliRunner, sample_courses: list, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "nocfg.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--tracked"])

        assert result.exit_code == 1
        assert "No course config" in result.output

    def test_courses_no_filter_shows_all(
        self, cli_runner: CliRunner, sample_courses: list, tmp_path: Path
    ) -> None:
        """Without --semester or --tracked, all courses are shown."""
        cfg_path = tmp_path / "nocfg.json"
        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 3
