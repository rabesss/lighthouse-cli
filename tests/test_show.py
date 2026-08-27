"""Focused coverage for read-command JSON output and error handling."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from click.testing import CliRunner

from lighthouse_cli import show
from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.cli import cli


def test_grades_json_endpoint_failure_is_a_single_json_document() -> None:
    """A failed grades endpoint still returns JSON and a failing exit code."""
    with patch.object(LighthouseClient, "get_grade_schema", side_effect=RuntimeError("schema down")):
        result = CliRunner().invoke(cli, ["grades", "123", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "course_id": 123,
        "grades": [],
        "error": "schema down",
    }
    assert "schema down" in result.stderr


def test_show_with_error_handling_generic_failure_returns_json_payload(capsys) -> None:
    def fetch(_org_id: int) -> list[dict[str, int]]:
        raise RuntimeError("fetch failed")

    payload = show._show_with_error_handling(
        LighthouseClient(),
        42,
        fetch,
        "items",
        True,
        lambda _data, _title: None,
    )

    assert payload == {"course_id": 42, "items": [], "error": "fetch failed"}
    assert "Warning: failed to fetch items: fetch failed" in capsys.readouterr().err


def test_show_with_error_handling_session_expiry_returns_json_payload(capsys) -> None:
    def fetch(_org_id: int) -> list[dict[str, int]]:
        raise SessionExpiredError("session expired")

    payload = show._show_with_error_handling(
        LighthouseClient(),
        42,
        fetch,
        "items",
        True,
        lambda _data, _title: None,
    )

    assert payload == {"course_id": 42, "items": [], "error": "session expired"}
    assert "Error: session expired" in capsys.readouterr().err


def test_show_with_error_handling_human_failure_returns_error(capsys) -> None:
    def fetch(_org_id: int) -> list[dict[str, int]]:
        raise RuntimeError("fetch failed")

    rc = show._show_with_error_handling(
        LighthouseClient(),
        42,
        fetch,
        "items",
        False,
        lambda _data, _title: None,
    )

    assert rc == 1
    assert "Warning: failed to fetch items: fetch failed" in capsys.readouterr().err


def test_single_json_worker_failure_is_normalized(monkeypatch, capsys) -> None:
    class FakeClient:
        pass

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)

    def single(_client, _org_id: int, _json_output: bool, title: str | None = None):
        raise RuntimeError("single failed")

    rc = show._for_course_or_all("123", single, True, "items")

    assert rc == 1
    assert json.loads(capsys.readouterr().out) == {
        "course_id": 123,
        "items": [],
        "error": "single failed",
    }


def test_all_course_json_failures_are_retained_and_fail_command(monkeypatch, capsys) -> None:
    class FakeClient:
        def get_courses(self):
            return [
                {"OrgUnitId": 20, "Name": "Second"},
                {"OrgUnitId": 3, "Name": "First"},
            ]

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)

    def single(_client, org_id: int, _json_output: bool, title: str | None = None):
        if org_id == 20:
            raise SessionExpiredError("session expired")
        return {"course_id": org_id, "items": [{"value": org_id}]}

    rc = show._for_course_or_all(None, single, True, "items")
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 1
    assert [item["course_id"] for item in payload] == [3, 20]
    assert payload[0]["items"] == [{"value": 3}]
    assert payload[1] == {
        "course_id": 20,
        "items": [],
        "error": "session expired",
    }
    assert "session expired" in captured.err


def test_all_course_json_success_is_sorted_deterministically(monkeypatch, capsys) -> None:
    class FakeClient:
        def get_courses(self):
            return [
                {"OrgUnitId": 9, "Name": "Nine"},
                {"OrgUnitId": 1, "Name": "One"},
                {"OrgUnitId": 5, "Name": "Five"},
            ]

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)

    def single(_client, org_id: int, _json_output: bool, title: str | None = None):
        return {"course_id": org_id, "items": [org_id]}

    rc = show._for_course_or_all(None, single, True, "items")

    assert rc == 0
    assert [item["course_id"] for item in json.loads(capsys.readouterr().out)] == [1, 5, 9]


def test_grades_all_course_failure_sets_aggregate_exit_code() -> None:
    courses = [
        {"OrgUnitId": 20, "Name": "Second"},
        {"OrgUnitId": 3, "Name": "First"},
    ]

    def get_schema(org_id: int):
        if org_id == 20:
            raise RuntimeError("grades unavailable")
        return [{"Id": 1, "Name": "Quiz", "MaxPoints": 10}]

    with patch.object(LighthouseClient, "get_courses", return_value=courses), \
        patch.object(LighthouseClient, "get_grade_schema", side_effect=get_schema), \
        patch.object(LighthouseClient, "get_my_grades", return_value=[]):
        result = CliRunner().invoke(cli, ["grades", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert [item["course_id"] for item in payload] == [3, 20]
    assert payload[1]["grades"] == []
    assert payload[1]["error"] == "grades unavailable"


def test_assignments_json_fetches_detail_when_list_omits_attachments() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{"Id": 101, "Name": "Assignment"}]
    client.get_dropbox_folder_detail.return_value = {
        "Id": 101,
        "Name": "Assignment",
        "Attachments": [
            {"Id": 7, "FileName": "brief.pdf", "Size": 42, "Type": "File"}
        ],
    }

    payload = show._show_course_assignments(client, 44347, True)

    assert payload["assignments"][0]["attachment_count"] == 1
    assert payload["assignments"][0]["attachments"][0]["file_id"] == 7
    client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)
