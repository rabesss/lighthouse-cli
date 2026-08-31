"""Focused coverage for read-command JSON output and error handling."""

from __future__ import annotations

import json
from threading import Lock, get_ident
from unittest.mock import Mock, patch

import pytest
import requests
from click.testing import CliRunner

from lighthouse_cli import show
from lighthouse_cli.api import LighthouseClient, SessionExpiredError, resolve_course_id
from lighthouse_cli.cli import cli
from lighthouse_cli.utils import get_course_name


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

    assert payload == {
        "course_id": 42,
        "items": [],
        "error": "Session expired. Run: lighthouse auth login",
    }
    assert "Error: Session expired. Run: lighthouse auth login" in capsys.readouterr().err


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


def test_fetch_error_preserves_permission_category_in_json(capsys) -> None:
    payload = show._fetch_error_result(42, "items", True, PermissionError("private"))

    assert payload == {"course_id": 42, "items": [], "error": "Permission denied."}
    assert "Permission denied." in capsys.readouterr().err


def test_command_error_preserves_permission_category_in_human_mode(capsys) -> None:
    rc = show._emit_command_error(
        42,
        "items",
        False,
        PermissionError("private"),
    )

    assert rc == 1
    assert "Permission denied." in capsys.readouterr().err


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


def test_failed_show_identifier_is_not_echoed_in_json_or_stderr() -> None:
    sentinel = "d2lSecureSessionVal=COOKIE_SENTINEL"
    with patch.object(LighthouseClient, "get_enrolled_courses", return_value=[]):
        result = CliRunner().invoke(cli, ["grades", sentinel, "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["course_id"] is None
    assert payload["grades"] == []
    assert sentinel not in result.stdout + result.stderr


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
        "error": "Session expired. Run: lighthouse auth login",
    }
    assert "Session expired. Run: lighthouse auth login" in captured.err


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


def test_all_course_fanout_supports_a_35_course_roster(monkeypatch, capsys) -> None:
    """The bounded fan-out still handles the verified 35-course catalog."""
    clients = []

    class FakeClient:
        def __init__(self) -> None:
            clients.append(self)

        def get_enrolled_courses(self):
            # Exercise the production catalog path, including its malformed
            # record filtering and first-record-wins de-duplication.
            return [
                *courses,
                {"OrgUnitId": 1, "Name": "Duplicate"},
                {"OrgUnitId": True, "Name": "Malformed"},
                {"OrgUnitId": "../../bad", "Name": "Malformed"},
            ]

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)
    courses = [
        {"OrgUnitId": course_id, "Name": f"Course {course_id}"}
        for course_id in range(1, 36)
    ]
    endpoint_calls = []

    def single(client, org_id: int, _json_output: bool, title: str | None = None):
        assert client in clients
        endpoint_calls.append(org_id)
        return {"course_id": org_id, "items": [org_id]}

    assert show._for_course_or_all(None, single, True, "items") == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["course_id"] for item in payload] == list(range(1, 36))
    assert sorted(endpoint_calls) == list(range(1, 36))
    assert len(clients) <= 1 + show.MAX_WORKERS


@pytest.mark.parametrize("json_output", [True, False])
def test_all_course_budget_rejects_before_per_course_requests(
    monkeypatch,
    capsys,
    json_output: bool,
) -> None:
    """An over-budget catalog emits one stable error and no course requests."""
    clients = []

    class FakeClient:
        def __init__(self) -> None:
            clients.append(self)

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)
    course_count = show.MAX_ALL_COURSES + 1
    courses = [
        {"OrgUnitId": course_id, "Name": f"Course {course_id}"}
        for course_id in range(1, course_count + 1)
    ]
    monkeypatch.setattr(show, "get_enrolled_course_catalog", lambda _client: courses)
    endpoint_calls = []

    def single(_client, org_id: int, _json_output: bool, title: str | None = None):
        endpoint_calls.append(org_id)
        raise AssertionError("per-course work must not start over the budget")

    assert show._for_course_or_all(None, single, json_output, "items") == 1

    captured = capsys.readouterr()
    message = (
        f"All-course reads are limited to {show.MAX_ALL_COURSES} courses; "
        f"found {course_count}. Specify COURSE_ID to narrow the request."
    )
    assert captured.err == f"Error: {message}\n"
    assert endpoint_calls == []
    assert len(clients) == 1
    if json_output:
        assert json.loads(captured.out) == [{
            "course_id": None,
            "items": [],
            "error": message,
        }]
    else:
        assert captured.out == ""


def test_all_course_workers_reuse_one_client_per_thread(monkeypatch, capsys) -> None:
    """Worker sessions are thread-local and construction stays bounded."""
    clients = []

    class FakeClient:
        def __init__(self) -> None:
            clients.append(self)

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)
    courses = [
        {"OrgUnitId": course_id, "Name": f"Course {course_id}"}
        for course_id in range(1, 36)
    ]
    monkeypatch.setattr(show, "get_enrolled_course_catalog", lambda _client: courses)
    thread_clients = {}
    lock = Lock()

    def single(client, org_id: int, _json_output: bool, title: str | None = None):
        thread_id = get_ident()
        with lock:
            prior = thread_clients.setdefault(thread_id, client)
        assert prior is client
        return {"course_id": org_id, "items": [org_id]}

    assert show._for_course_or_all(None, single, True, "items") == 0
    capsys.readouterr()

    assert len(clients) <= 1 + show.MAX_WORKERS
    assert len(clients) - 1 == len(thread_clients)


@pytest.mark.parametrize(
    ("collection_key", "single_fn"),
    [
        ("announcements", show._show_announcements),
        ("events", show._show_calendar),
        ("assignments", show._show_course_assignments),
        ("quizzes", show._show_course_quizzes),
        ("grades", show._show_course_grades),
    ],
)
def test_all_course_titles_use_safe_bounded_labels(
    monkeypatch,
    capsys,
    collection_key: str,
    single_fn,
) -> None:
    """Every read command protects human headers from roster labels."""
    sentinel = "SECRET"
    courses = [
        {"OrgUnitId": 101, "Name": {"password": sentinel}},
        {"OrgUnitId": 102, "Name": "\x1b[31munsafe"},
        {"OrgUnitId": 103, "Name": f"password={sentinel}"},
        {"OrgUnitId": 104, "Name": "Valid Course"},
    ]

    class FakeClient:
        def get_announcements(self, _org_id):
            return [{"Title": "Announcement"}]

        def get_calendar(self, _org_id):
            return [{"Title": "Event"}]

        def get_dropbox_folders(self, _org_id):
            return [{"Id": 1, "Name": "Folder", "Attachments": []}]

        def get_quizzes(self, _org_id):
            return [{"QuizId": 1, "Name": "Quiz"}]

        def get_grade_schema(self, _org_id):
            return [{"Id": 1, "Name": "Grade", "MaxPoints": 10}]

        def get_my_grades(self, _org_id):
            return []

    monkeypatch.setattr(show, "LighthouseClient", FakeClient)
    monkeypatch.setattr(show, "get_enrolled_course_catalog", lambda _client: courses)

    assert show._for_course_or_all(None, single_fn, False, collection_key) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert sentinel not in output
    assert "\x1b" not in output
    assert "Course-101" in captured.out
    assert "Course-102" in captured.out
    assert "Course-103" in captured.out
    assert "Valid Course" in captured.out


def test_json_course_label_projection_is_safe_and_bounded() -> None:
    """JSON labels cannot retain nested secrets, controls, or oversized text."""
    sentinel = "SECRET"
    payload, failed = show._normalise_json_payload(
        {
            "course_id": 101,
            "course_name": "C" * (show._MAX_DISPLAY_TEXT_LENGTH + 1),
            "title": {"password": sentinel},
        },
        101,
        "items",
    )

    assert not failed
    assert payload["course_name"] == "Course-101"
    assert payload["title"] == "Course-101"
    assert sentinel not in json.dumps(payload)
    assert "\x1b" not in json.dumps(payload)


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
        "CustomInstructions": {
            "Text": "Read the brief.",
            "Html": "<p>Read the <b>brief</b>.</p>",
        },
        "Attachments": [
            {"Id": 7, "FileName": "brief.pdf", "Size": 42, "Type": "File"}
        ],
    }

    payload = show._show_course_assignments(client, 44347, True)

    assert payload["assignments"][0]["attachment_count"] == 1
    assert payload["assignments"][0]["attachments"][0]["file_id"] == 7
    assert payload["assignments"][0]["custom_instructions"] == "<p>Read the <b>brief</b>.</p>"
    assert payload["assignments"][0]["custom_instructions_preview"] == "Read the brief."
    client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)


def test_assignments_rejects_mismatched_detail_id_without_exposing_it(capsys) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{"Id": 101, "Name": "Assignment"}]
    client.get_dropbox_folder_detail.return_value = {
        "Id": 202,
        "Name": "Wrong assignment",
        "Attachments": [{"Id": 7, "FileName": "wrong.pdf", "Size": 42, "Type": "File"}],
    }

    payload = show._show_course_assignments(client, 44347, True)
    captured = capsys.readouterr()

    assert payload == {"course_id": 44347, "assignments": []}
    assert "Assignment record has an invalid identifier." in captured.err
    assert "202" not in captured.out + captured.err
    client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)


def test_assignments_deduplicates_folder_ids_first_record_wins() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {"Id": 101, "Name": "First assignment"},
        {
            "Id": 101,
            "Name": "Conflicting duplicate",
            "Attachments": [{"Id": 8, "FileName": "second.pdf", "Size": 8, "Type": "File"}],
        },
    ]
    client.get_dropbox_folder_detail.return_value = {
        "Id": 101,
        "Name": "First assignment detail",
        "Attachments": [{"Id": 7, "FileName": "first.pdf", "Size": 7, "Type": "File"}],
    }

    payload = show._show_course_assignments(client, 44347, True)

    assert [assignment["folder_id"] for assignment in payload["assignments"]] == [101]
    assert payload["assignments"][0]["name"] == "First assignment detail"
    assert [attachment["file_id"] for attachment in payload["assignments"][0]["attachments"]] == [7]
    client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)


def test_assignments_allows_valid_duplicate_after_malformed_first_record(capsys) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {"Id": 101, "Name": "Malformed first"},
        {
            "Id": 101,
            "Name": "Valid second",
            "Attachments": [{"Id": 8, "FileName": "second.pdf", "Size": 8, "Type": "File"}],
        },
    ]
    client.get_dropbox_folder_detail.return_value = {
        "Id": 101,
        "Name": "Malformed detail",
        "Attachments": None,
    }

    payload = show._show_course_assignments(client, 44347, True)
    captured = capsys.readouterr()

    assert [assignment["folder_id"] for assignment in payload["assignments"]] == [101]
    assert payload["assignments"][0]["name"] == "Valid second"
    assert [attachment["file_id"] for attachment in payload["assignments"][0]["attachments"]] == [8]
    assert "Warning: skipped malformed assignment folder." in captured.err
    client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)


def test_assignments_json_redacts_secret_shaped_filename() -> None:
    sentinel = "ATTACHMENT_SECRET_SENTINEL"
    folder_sentinel = "FOLDER_SECRET_SENTINEL"
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{
        "Id": 101,
        "Name": f"password={folder_sentinel}",
        "Attachments": [{"Id": 7, "FileName": f"password={sentinel}.pdf", "Size": 42, "Type": "File"}],
    }]

    payload = show._show_course_assignments(client, 44347, True)

    assert payload["assignments"][0]["attachments"][0]["file_name"] == ""
    assert sentinel not in json.dumps(payload)
    assert payload["assignments"][0]["name"] == ""
    assert folder_sentinel not in json.dumps(payload)


@pytest.mark.parametrize(
    ("instructions", "expected", "preview"),
    [
        (
            {"Text": "Text fallback", "Html": "<p>HTML <b>value</b>.</p>"},
            "<p>HTML <b>value</b>.</p>",
            "HTML value.",
        ),
        ({"Text": "Text only", "Html": ""}, "Text only", "Text only"),
        (
            {"Text": {"Text": "Nested text", "Html": "<p>Nested</p>"}},
            "<p>Nested</p>",
            "Nested",
        ),
        ("<p>String <i>value</i>.</p>", "<p>String <i>value</i>.</p>", "String value."),
        ("", None, None),
        (None, None, None),
        ({"Unexpected": {"raw": "object"}}, None, None),
    ],
)
def test_custom_instructions_rich_text_is_string_and_preview_is_safe(
    instructions, expected, preview
) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {
            "Id": 101,
            "Name": "Assignment",
            "Attachments": [],
            "CustomInstructions": instructions,
        }
    ]

    payload = show._show_course_assignments(client, 44347, True)
    assignment = payload["assignments"][0]

    assert assignment["custom_instructions"] == expected
    assert assignment["custom_instructions_preview"] == preview
    if preview is not None:
        assert "<" not in preview
        assert "Unexpected" not in preview


@pytest.mark.parametrize(
    ("encoded", "control"),
    [("Normal&#10;forged", "\n"), ("Normal&#13;forged", "\r"), ("Normal&Tab;forged", "\t")],
)
def test_strip_html_rejects_controls_created_by_entity_decoding(
    encoded: str,
    control: str,
) -> None:
    preview = show._strip_html(encoded)

    assert preview == ""
    assert control not in preview


def test_strip_html_preserves_benign_encoded_text() -> None:
    assert show._strip_html("A&nbsp;B") == "A B"
    assert show._strip_html("&lt;tag&gt;") == "<tag>"


def test_strip_html_normalizes_literal_multiline_whitespace() -> None:
    assert show._strip_html("<p>Line one\n\tLine two</p>") == "Line one Line two"


def test_assignment_view_skips_malformed_attachment_and_keeps_valid_siblings() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {
            "Id": 101,
            "Name": "Assignment",
            "Attachments": [
                None,
                "not-an-attachment",
                {"Id": 7, "FileName": "brief.pdf", "Size": 42, "Type": "File"},
            ],
            "Availability": "malformed",
            "CustomInstructions": {"Unexpected": ["shape"]},
        },
        None,
        {
            "Id": 102,
            "Name": "Sibling",
            "Attachments": [{"Id": 8, "FileName": "sibling.pdf", "Size": 10}],
            "Availability": {"StartDate": {"unexpected": True}, "EndDate": None},
        },
    ]

    payload = show._show_course_assignments(client, 44347, True)

    assert [assignment["folder_id"] for assignment in payload["assignments"]] == [101, 102]
    assert payload["assignments"][0]["attachment_count"] == 1
    assert payload["assignments"][0]["attachments"][0]["file_id"] == 7
    assert payload["assignments"][0]["availability"] is None
    assert payload["assignments"][0]["custom_instructions"] is None
    assert payload["assignments"][1]["attachment_count"] == 1
    assert payload["assignments"][1]["availability"] is None


def test_assignment_view_bounds_deep_and_cyclic_rich_text_and_keeps_siblings() -> None:
    """Malformed RichText cannot abort the course or hide valid folders."""
    client = Mock(spec=LighthouseClient)

    deep: dict[str, object] = {"Text": "too deep"}
    for _ in range(show._RICH_TEXT_MAX_DEPTH + 100):
        deep = {"Text": deep}
    cyclic: dict[str, object] = {}
    cyclic["Html"] = cyclic
    client.get_dropbox_folders.return_value = [
        {
            "Id": 101,
            "Name": "Deep instructions",
            "Attachments": [],
            "CustomInstructions": deep,
        },
        {
            "Id": 102,
            "Name": "Cyclic instructions",
            "Attachments": [],
            "CustomInstructions": cyclic,
        },
        {
            "Id": 103,
            "Name": "Valid sibling",
            "Attachments": [],
            "CustomInstructions": {
                "Html": {"Text": "<p>Still valid.</p>"},
            },
        },
    ]

    payload = show._show_course_assignments(client, 44347, True)

    assert [assignment["folder_id"] for assignment in payload["assignments"]] == [
        101,
        102,
        103,
    ]
    assert payload["assignments"][0]["custom_instructions"] is None
    assert payload["assignments"][0]["custom_instructions_preview"] is None
    assert payload["assignments"][1]["custom_instructions"] is None
    assert payload["assignments"][1]["custom_instructions_preview"] is None
    assert payload["assignments"][2]["custom_instructions"] == "<p>Still valid.</p>"
    assert payload["assignments"][2]["custom_instructions_preview"] == "Still valid."


def test_assignment_view_skips_malformed_attachment_elements() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {
            "Id": 101,
            "Name": "Assignment",
            "Attachments": [
                None,
                "not-an-attachment",
                {"Id": 7, "FileName": "brief.pdf", "Size": 42, "Type": "File"},
                {"Id": 8, "FileName": "notes.txt", "Size": "bad", "Type": None},
            ],
        }
    ]

    payload = show._show_course_assignments(client, 44347, True)

    assert payload["assignments"][0]["attachment_count"] == 2
    assert [a["file_id"] for a in payload["assignments"][0]["attachments"]] == [7, 8]
    assert payload["assignments"][0]["attachments"][1]["size"] == 0
    assert payload["assignments"][0]["attachments"][1]["attachment_type"] == "File"


@pytest.mark.parametrize("json_output", [True, False])
def test_assignment_view_rejects_malformed_ids_without_echoing_them(
    capsys,
    json_output: bool,
) -> None:
    """Folder/file IDs are strict while valid siblings remain visible."""
    sentinel = "d2lSecureSessionVal=ID_SENTINEL"
    client = Mock(spec=LighthouseClient)
    malformed_folders = [
        {"Id": True, "Name": "bool", "Attachments": []},
        {"Id": 1.5, "Name": "float", "Attachments": []},
        {"Id": 0, "Name": "zero", "Attachments": []},
        {"Id": -1, "Name": "negative", "Attachments": []},
        {"Id": "../evil", "Name": "traversal", "Attachments": []},
        {"Id": sentinel, "Name": "secret", "Attachments": []},
    ]
    client.get_dropbox_folders.return_value = malformed_folders + [
        {
            "Id": "101",
            "Name": "Valid folder",
            "Attachments": [
                {"Id": True, "FileName": "bool.txt"},
                {"Id": 0, "FileName": "zero.txt"},
                {"Id": -2, "FileName": "negative.txt"},
                {"Id": "../attachment", "FileName": "traversal.txt"},
                {"Id": sentinel, "FileName": "secret.txt"},
                {"Id": "7", "FileName": "canonical.txt"},
                {"Id": 8, "FileName": "integer.txt"},
            ],
        },
        {
            "Id": 102,
            "Name": "Valid sibling",
            "Attachments": [],
        },
    ]

    result = show._show_course_assignments(client, 44347, json_output)
    captured = capsys.readouterr()

    assert client.get_dropbox_folder_detail.call_count == 0
    assert sentinel not in captured.out + captured.err
    if json_output:
        assert [assignment["folder_id"] for assignment in result["assignments"]] == [
            101,
            102,
        ]
        assert [
            attachment["file_id"]
            for attachment in result["assignments"][0]["attachments"]
        ] == [7, 8]
    else:
        assert result == 0
        assert "101" in captured.out
        assert "102" in captured.out


def test_assignment_projection_sanitizes_folder_scalars_and_rich_text(capsys) -> None:
    """Folder output never serializes nested secrets or control characters."""
    sentinel = "SECRET"
    oversized_label = "L" * (show._MAX_DISPLAY_TEXT_LENGTH + 1)
    oversized_body = "B" * (show._MAX_RICH_TEXT_LENGTH + 1)
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [
        {
            "Id": 101,
            "Name": {"password": sentinel},
            "DueDate": "\x1b[31m2025-05-20T23:59:00Z",
            "Due": oversized_label,
            "CategoryName": {"token": sentinel},
            "Availability": {
                "StartDate": {"password": sentinel},
                "EndDate": "\x1b[31m2025-05-21T23:59:00Z",
            },
            "CustomInstructions": {
                "Html": {"token": sentinel},
                "Text": oversized_body,
            },
            "Attachments": [
                {"Id": 1, "FileName": {"token": sentinel}, "Size": 10, "Type": "File"},
                {"Id": 2, "FileName": "\x1b[31munsafe", "Size": 10, "Type": "File"},
                {"Id": 3, "FileName": "huge", "Size": 10**1000, "Type": "File"},
                {"Id": 4, "FileName": "valid.pdf", "Size": 20, "Type": "File"},
            ],
        },
        {
            "Id": 102,
            "Name": "Valid sibling",
            "DueDate": "2025-05-22T23:59:00Z",
            "CategoryName": "Assignment",
            "Availability": {
                "StartDate": "2025-05-20T00:00:00Z",
                "EndDate": "2025-05-22T23:59:00Z",
            },
            "CustomInstructions": "<p>Bring notes.</p>",
            "Attachments": [],
        },
    ]

    structured = show._show_course_assignments(client, 44347, True)
    human_rc = show._show_course_assignments(client, 44347, False)
    captured = capsys.readouterr()

    assert human_rc == 0
    assert structured["assignments"][0] == {
        "folder_id": 101,
        "name": "",
        "due_date": "",
        "attachment_count": 4,
        "attachments": [
            {"file_id": 1, "file_name": "", "size": 10, "attachment_type": "File"},
            {"file_id": 2, "file_name": "", "size": 10, "attachment_type": "File"},
            {"file_id": 3, "file_name": "huge", "size": 0, "attachment_type": "File"},
            {"file_id": 4, "file_name": "valid.pdf", "size": 20, "attachment_type": "File"},
        ],
        "custom_instructions": None,
        "custom_instructions_preview": None,
        "submission_type": "",
        "availability": None,
    }
    assert structured["assignments"][1]["name"] == "Valid sibling"
    assert structured["assignments"][1]["availability"] == {
        "start": "2025-05-20T00:00:00Z",
        "end": "2025-05-22T23:59:00Z",
    }
    assert sentinel not in json.dumps(structured) + captured.out + captured.err
    assert "\x1b" not in json.dumps(structured) + captured.out + captured.err
    assert "Valid sibling" in captured.out


def test_enrollment_only_name_resolution_and_folder_lookup() -> None:
    client = Mock(spec=LighthouseClient)
    enrolled = [
        {"OrgUnitId": 7001, "Name": "Enrollment-only Course", "Code": "E"}
    ]
    client.get_enrolled_courses.return_value = enrolled
    client.get_courses.side_effect = AssertionError("legacy catalog should not be used")

    assert resolve_course_id(client, "enrollment-only") == 7001
    assert get_course_name(client, 7001) == "Enrollment-only Course"


@pytest.mark.parametrize(
    ("command", "patches", "json_key", "message"),
    [
        (
            "announcements",
            [("get_announcements",)],
            "announcements",
            "No announcements found",
        ),
        (
            "calendar",
            [("get_calendar",)],
            "events",
            "No calendar events found",
        ),
        ("quizzes", [("get_quizzes",)], "quizzes", "No quizzes found"),
        (
            "assignments",
            [("get_dropbox_folders",)],
            "assignments",
            "No assignments found",
        ),
    ],
)
def test_single_course_empty_read_views_have_human_and_json_paths(
    command, patches, json_key, message
) -> None:
    with patch.object(LighthouseClient, patches[0][0], return_value=[]):
        human = CliRunner().invoke(cli, [command, "123"])
        structured = CliRunner().invoke(cli, [command, "123", "--json"])

    assert human.exit_code == 0
    assert message in human.stdout
    assert structured.exit_code == 0
    assert json.loads(structured.stdout) == {"course_id": 123, json_key: []}
    assert message not in structured.stdout


def test_single_course_empty_grades_have_human_and_json_paths() -> None:
    with patch.object(LighthouseClient, "get_grade_schema", return_value=[]), \
        patch.object(LighthouseClient, "get_my_grades", return_value=[]):
        human = CliRunner().invoke(cli, ["grades", "123"])
        structured = CliRunner().invoke(cli, ["grades", "123", "--json"])

    assert human.exit_code == 0
    assert "No grades found" in human.stdout
    assert structured.exit_code == 0
    assert json.loads(structured.stdout) == {"course_id": 123, "grades": []}


@pytest.mark.parametrize(
    ("command", "method", "collection_key", "record"),
    [
        (
            "announcements",
            "get_announcements",
            "announcements",
            {"Title": "Keep announcement"},
        ),
        (
            "calendar",
            "get_calendar",
            "events",
            {"Title": "Keep event"},
        ),
        (
            "quizzes",
            "get_quizzes",
            "quizzes",
            {"Name": "Keep quiz"},
        ),
    ],
)
def test_read_views_skip_non_dict_siblings(
    command,
    method: str,
    collection_key: str,
    record: dict[str, str],
) -> None:
    """Malformed list members never reach renderers or JSON output."""
    with patch.object(LighthouseClient, method, return_value=[None, {}, record, "bad"]):
        human = CliRunner().invoke(cli, [command, "123"])
        structured = CliRunner().invoke(cli, [command, "123", "--json"])

    assert human.exit_code == 0
    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert payload == {"course_id": 123, collection_key: [record]}
    assert "Keep" in human.stdout


def test_announcements_project_untrusted_fields_without_secret_leak(capsys) -> None:
    """Announcement projections keep scalar siblings and drop unsafe fields."""
    sentinel = "SECRET"
    client = Mock(spec=LighthouseClient)
    client.get_announcements.return_value = [
        {
            "Id": 1,
            "Title": {"password": sentinel},
            "Body": {"Text": {"password": sentinel}},
            "CreatedDate": "2025-05-08T14:30:00Z",
            "Attachments": [
                {"Id": 11, "FileName": {"token": sentinel}, "Size": 10, "Type": "File"},
                {"Id": 12, "FileName": "bad-size", "Size": sentinel, "Type": "File"},
                {"Id": 13, "FileName": "bad-type", "Size": 10, "Type": {"token": sentinel}},
                {"Id": 15, "FileName": "\x1b[31munsafe", "Size": 10, "Type": "File"},
                {"Id": 16, "FileName": "too-large", "Size": 10**1000, "Type": "File"},
                {"Id": 14, "FileName": "good.pdf", "Size": 20, "Type": "File"},
            ],
        },
        {
            "Id": 2,
            "Title": "Valid sibling",
            "Body": {"Html": "<p>Visible body.</p>"},
            "Attachments": [
                {"Id": 21, "FileName": "sibling.pdf", "Size": 30, "Type": "File"},
            ],
        },
    ]

    structured = show._show_announcements(client, 44347, True)
    human_rc = show._show_announcements(client, 44347, False)
    captured = capsys.readouterr()

    assert human_rc == 0
    assert structured["announcements"][0]["Title"] == ""
    assert structured["announcements"][0]["Body"] == ""
    assert structured["announcements"][0]["Attachments"] == [{
        "Id": 14,
        "FileName": "good.pdf",
        "Size": 20,
        "Type": "File",
    }]
    assert structured["announcements"][1]["Title"] == "Valid sibling"
    assert structured["announcements"][1]["Attachments"] == [{
        "Id": 21,
        "FileName": "sibling.pdf",
        "Size": 30,
        "Type": "File",
    }]
    assert sentinel not in json.dumps(structured) + captured.out + captured.err
    assert "Valid sibling" in captured.out
    assert "sibling.pdf" in captured.out


def test_calendar_projection_drops_nested_fields_without_secret_leak(capsys) -> None:
    """Calendar output keeps scalar fields and ignores nested upstream data."""
    sentinel = "SECRET"
    client = Mock(spec=LighthouseClient)
    client.get_calendar.return_value = [
        {
            "CalendarEventId": {"password": sentinel},
            "Title": {"password": sentinel},
            "StartDateTime": {"token": sentinel},
            "EndDateTime": "\x1b[31munsafe",
            "OrgUnitName": {"password": sentinel},
            "Unexpected": {"password": sentinel},
        },
        {
            "CalendarEventId": "evt-002",
            "Title": "Valid sibling",
            "StartDateTime": "2025-05-20T23:59:00Z",
            "EndDateTime": "2025-05-20T23:59:00Z",
            "OrgUnitName": "Signals & Systems",
        },
    ]

    structured = show._show_calendar(client, 44347, True)
    human_rc = show._show_calendar(client, 44347, False)
    captured = capsys.readouterr()

    assert human_rc == 0
    assert structured["events"][0] == {
        "Title": "",
        "OrgUnitName": "",
        "StartDateTime": "",
        "EndDateTime": "",
    }
    assert structured["events"][1] == {
        "CalendarEventId": "evt-002",
        "Title": "Valid sibling",
        "StartDateTime": "2025-05-20T23:59:00Z",
        "EndDateTime": "2025-05-20T23:59:00Z",
        "OrgUnitName": "Signals & Systems",
    }
    assert sentinel not in json.dumps(structured) + captured.out + captured.err
    assert "Valid sibling" in captured.out
    assert "Signals & Systems" in captured.out


def test_quiz_projection_drops_nested_fields_without_secret_leak(capsys) -> None:
    """Quiz output requires positive IDs and printable scalar fields."""
    sentinel = "SECRET"
    client = Mock(spec=LighthouseClient)
    client.get_quizzes.return_value = [
        {
            "QuizId": {"password": sentinel},
            "Name": {"password": sentinel},
            "StartDate": {"token": sentinel},
            "EndDate": "\x1b[31munsafe",
            "IsActive": {"password": sentinel},
            "Unexpected": {"password": sentinel},
        },
        {
            "QuizId": "102",
            "Name": "Valid sibling",
            "StartDate": "2025-05-17T10:00:00Z",
            "EndDate": "2025-05-17T10:30:00Z",
            "IsActive": True,
        },
    ]

    structured = show._show_course_quizzes(client, 44347, True)
    human_rc = show._show_course_quizzes(client, 44347, False)
    captured = capsys.readouterr()

    assert human_rc == 0
    assert structured["quizzes"][0] == {
        "Name": "",
        "StartDate": "",
        "EndDate": "",
    }
    assert structured["quizzes"][1] == {
        "QuizId": 102,
        "Name": "Valid sibling",
        "StartDate": "2025-05-17T10:00:00Z",
        "EndDate": "2025-05-17T10:30:00Z",
        "IsActive": True,
    }
    assert sentinel not in json.dumps(structured) + captured.out + captured.err
    assert "Valid sibling" in captured.out


def test_grades_projection_drops_malformed_records_and_fields(capsys) -> None:
    """Grades merge only validated scalar schema/value fields."""
    sentinel = "SECRET"
    client = Mock(spec=LighthouseClient)
    client.get_grade_schema.return_value = [
        None,
        {"Id": {"password": sentinel}, "Name": "Discarded"},
        {
            "Id": 1,
            "Name": {"password": sentinel},
            "Weight": {"token": sentinel},
            "GradeType": {"password": sentinel},
            "MaxPoints": {"token": sentinel},
        },
        {
            "Id": "2",
            "Name": "Valid item",
            "Weight": "10%",
            "GradeType": "Points",
            "MaxPoints": 10,
        },
    ]
    client.get_my_grades.return_value = [
        None,
        {"GradeObjectIdentifier": {"password": sentinel}, "PointsNumerator": 1},
        {
            "GradeObjectIdentifier": "1",
            "PointsNumerator": {"password": sentinel},
            "PointsDenominator": {"token": sentinel},
        },
        {
            "GradeObjectIdentifier": "2",
            "PointsNumerator": 9,
            "PointsDenominator": 10,
            "Unexpected": {"password": sentinel},
        },
    ]

    structured = show._show_course_grades(client, 44347, True)
    human_rc = show._show_course_grades(client, 44347, False)
    captured = capsys.readouterr()

    assert human_rc == 0
    assert structured == {
        "course_id": 44347,
        "grades": [
            {"name": "", "weight": "", "grade": "–/–", "type": ""},
            {"name": "Valid item", "weight": "10%", "grade": "9/10", "type": "Points"},
        ],
    }
    assert sentinel not in json.dumps(structured) + captured.out + captured.err
    assert "Valid item" in captured.out


def test_all_course_empty_human_output_is_quiet() -> None:
    with patch.object(
        LighthouseClient,
        "get_enrolled_courses",
        return_value=[{"OrgUnitId": 123, "Name": "Empty Course"}],
    ), patch.object(LighthouseClient, "get_announcements", return_value=[]):
        result = CliRunner().invoke(cli, ["announcements"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_all_course_json_error_omits_transport_url_and_secret() -> None:
    response = requests.Response()
    response.status_code = 503
    error = requests.HTTPError(
        "503 Server Error for url: https://example.invalid/news?token=SECRET_SENTINEL"
    )
    error.response = response

    with patch.object(
        LighthouseClient,
        "get_enrolled_courses",
        return_value=[{"OrgUnitId": 123, "Name": "Course"}],
    ), patch.object(LighthouseClient, "get_announcements", side_effect=error):
        result = CliRunner().invoke(cli, ["announcements", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == [
        {
            "course_id": 123,
            "announcements": [],
            "error": "Remote server error (HTTP 503).",
        }
    ]
    assert "SECRET_SENTINEL" not in result.stdout + result.stderr
    assert "https://" not in result.stdout + result.stderr
