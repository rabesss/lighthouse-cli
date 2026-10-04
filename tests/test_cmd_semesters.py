"""CliRunner tests for `lighthouse semesters` and `lighthouse courses` with a patched client."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.cli import cli


class TestCmdSemesters:
    """Tests for `lighthouse semesters` command."""

    def test_semesters_lists_all_semesters(self, cli_runner: CliRunner, sample_semesters: list) -> None:
        with patch.object(LighthouseClient, "get_semesters", return_value=sample_semesters):
            result = cli_runner.invoke(cli, ["semesters", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 4
        assert data[0]["OrgUnitId"] == 58272
        assert any("Sem IV" in str(s.get("Name", "")) for s in data)

    def test_semesters_empty(self, cli_runner: CliRunner) -> None:
        with patch.object(LighthouseClient, "get_semesters", return_value=[]):
            result = cli_runner.invoke(cli, ["semesters"])

        assert result.exit_code == 0
        assert "ID" in result.output  # header still printed

    def test_semesters_api_error(self, cli_runner: CliRunner) -> None:
        with patch.object(LighthouseClient, "get_semesters", side_effect=SessionExpiredError("Session expired")):
            result = cli_runner.invoke(cli, ["semesters"])

        assert result.exit_code == 1
        assert "Session expired" in result.output or "Error" in result.output


class TestCmdCourses:
    """Tests for `lighthouse courses` command."""

    def test_courses_lists_all_courses(self, cli_runner: CliRunner, sample_courses: list) -> None:
        with patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses"])

        assert result.exit_code == 0
        assert "44347" in result.output or "Signals" in result.output

    def test_courses_json_output(self, cli_runner: CliRunner, sample_courses: list) -> None:
        with patch.object(LighthouseClient, "get_course_enrollments", return_value=sample_courses):
            result = cli_runner.invoke(cli, ["courses", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 3
        assert data[0]["OrgUnitId"] == 44347
