"""Focused command JSON, error-safety, help, and semester contract tests."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests
import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, NetworkError
from lighthouse_cli.cli import cli
from lighthouse_cli.commands import _resolve_course_scope
from lighthouse_cli.display import _has_json_option, format_user_error, output_json, safe_display_text
from lighthouse_cli.utils import get_course_name, get_enrolled_course_catalog


def test_content_json_failure_has_one_command_shaped_document() -> None:
    with patch.object(
        LighthouseClient,
        "get_content_toc",
        side_effect=RuntimeError(
            "HTTP 503 Server Error for url: "
            "https://lighthouse.example/content?token=CONTENT_SENTINEL"
        ),
    ):
        result = CliRunner().invoke(cli, ["content", "123", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "course_id": 123,
        "modules": [],
        "error": "Remote server error (HTTP 503).",
    }
    assert "CONTENT_SENTINEL" not in result.stdout + result.stderr
    assert "https://" not in result.stdout + result.stderr
    assert "Error:" in result.stderr


def test_content_projection_suppresses_quoted_secret_labels() -> None:
    toc = {
        "Modules": [{
            "ModuleId": 1,
            "Title": 'headers={"Cookie":"abcd1234"}',
            "Modules": [],
            "Topics": [{
                "TopicId": 2,
                "Title": 'data={"password":"abcd1234"}',
                "TypeIdentifier": "File",
                "Url": "https://example.invalid/content?page=2",
            }],
        }],
    }
    with patch.object(LighthouseClient, "get_content_toc", return_value=toc):
        result = CliRunner().invoke(cli, ["content", "123", "--json"])

    assert result.exit_code == 0
    assert "abcd1234" not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["modules"][0]["Title"] == ""
    assert payload["modules"][0]["Topics"][0]["Title"] == ""


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "https://user:CONTENT_SECRET@example.invalid/path",
        "https://example.invalid/path?%74oken=CONTENT_SECRET",
        "https://example.invalid/path?%2574oken=CONTENT_SECRET",
        "https://example.invalid/path?ctx=CONTENT_SECRET",
        "https://example.invalid/path?sFT=CONTENT_SECRET",
        "https://example.invalid/path?apiCanary=CONTENT_SECRET",
        "https://example.invalid/path?d2lSameSiteCanaryA=CONTENT_SECRET",
        "https://example.invalid/path#%74oken=CONTENT_SECRET",
        "//example.invalid/path",
    ],
)
def test_content_projection_omits_unsafe_or_secret_bearing_urls(
    unsafe_url: str,
) -> None:
    toc = {
        "Modules": [{
            "ModuleId": 1,
            "Title": "Module",
            "Modules": [],
            "Topics": [{
                "TopicId": 2,
                "Title": "Topic",
                "TypeIdentifier": "File",
                "Url": unsafe_url,
            }],
        }],
    }
    with patch.object(LighthouseClient, "get_content_toc", return_value=toc):
        result = CliRunner().invoke(cli, ["content", "123", "--json"])

    assert result.exit_code == 0
    assert "CONTENT_SECRET" not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["modules"][0]["Topics"][0]["Url"] is None


def test_quiz_projection_suppresses_quoted_secret_labels_in_human_and_json() -> None:
    quiz = {
        "QuizId": 7,
        "Name": "token SECRET",
        "Description": '{"password":"abcd1234"}',
        "Instructions": "Normal instructions",
    }
    with patch.object(LighthouseClient, "get_quiz_detail", return_value=quiz):
        human = CliRunner().invoke(cli, ["quiz", "123", "7"])
        structured = CliRunner().invoke(cli, ["quiz", "123", "7", "--json"])

    assert human.exit_code == 0
    assert structured.exit_code == 0
    assert "abcd1234" not in human.stdout + human.stderr
    assert "abcd1234" not in structured.stdout + structured.stderr
    assert json.loads(structured.stdout)["quiz"]["Name"] == "Quiz"


def test_submit_projection_suppresses_quoted_secret_labels(tmp_path: Path) -> None:
    file_path = tmp_path / "answer.pdf"
    file_path.write_bytes(b"answer")
    client = MagicMock()
    client.get_courses.return_value = [{"OrgUnitId": 123, "Name": "Course"}]
    client.get_enrolled_courses.return_value = [{"OrgUnitId": 123, "Name": "Course"}]
    client.get_dropbox_folders.return_value = [{"Id": 7, "Name": "Folder"}]
    client.get_dropbox_folder_detail.return_value = {
        "Name": "passphrase SECRET",
    }
    client.submit_file.return_value = {
        "submissionId": 'data={"password":"abcd1234"}',
        "submittedAt": 'responseBody=abcd1234',
    }
    with patch("lighthouse_cli.submit.LighthouseClient", return_value=client):
        result = CliRunner().invoke(
            cli,
            ["submit", "123", "7", "--file", str(file_path), "--yes", "--json"],
        )

    assert result.exit_code == 0
    assert "abcd1234" not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["folder_name"] == "Unknown folder"
    assert payload["submission_id"] is None


def test_show_projections_suppress_quoted_secret_labels() -> None:
    from lighthouse_cli import show

    announcement = show._normalise_announcements([
        {
            "Id": 1,
            "Title": 'headers={"Cookie":"abcd1234"}',
            "Body": {"Text": 'data={"password":"abcd1234"}'},
        }
    ])[0]
    calendar = show._normalise_calendar_events([
        {"Id": 2, "Title": 'responseBody=abcd1234', "OrgUnitName": "Normal"}
    ])[0]
    quiz = show._normalise_quizzes([
        {"QuizId": 3, "Name": 'client_secret: abcdef123'}
    ])[0]
    grade = show._normalise_grade_schema([
        {"Id": 4, "Name": 'headers={"Cookie":"abcd1234"}', "MaxPoints": 10}
    ])[0]

    assert announcement["Title"] == ""
    assert announcement["Body"] == ""
    assert calendar["Title"] == ""
    assert quiz["Name"] == ""
    assert grade["name"] == ""


def test_failed_content_identifier_is_not_echoed_in_json_or_stderr() -> None:
    sentinel = "d2lSecureSessionVal=COOKIE_SENTINEL"
    with patch.object(LighthouseClient, "get_enrolled_courses", return_value=[]):
        result = CliRunner().invoke(cli, ["content", sentinel, "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["course_id"] is None
    assert payload["modules"] == []
    assert sentinel not in result.stdout + result.stderr


def test_name_substring_is_kept_for_internal_course_resolution() -> None:
    with patch.object(
        LighthouseClient,
        "get_enrolled_courses",
        return_value=[{"OrgUnitId": 123, "Name": "Signals & Systems"}],
    ), patch.object(
        LighthouseClient,
        "get_courses",
        side_effect=AssertionError("name lookup should use enrollments"),
    ), patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):
        result = CliRunner().invoke(cli, ["content", "signals", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"course_id": 123, "modules": []}


def _deep_content_toc(depth: int = 1100) -> dict[str, object]:
    """Create a hostile/deep TOC with valid siblings for renderer tests."""
    leaf: dict[str, object] = {
        "ModuleId": depth,
        "Title": {"password": "CONTENT_SECRET_SENTINEL"},
        "Modules": [],
        "Topics": [
            {
                "TopicId": 9001,
                "Title": "deep valid topic",
                "TypeIdentifier": "File",
                "Url": "https://example.invalid/deep",
            }
        ],
    }
    for module_id in range(depth - 1, -1, -1):
        leaf = {
            "ModuleId": module_id,
            "Title": f"deep module {module_id}",
            "Modules": [leaf],
            "Topics": [],
        }
    leaf["Modules"].append(None)  # type: ignore[union-attr]
    return {
        "Modules": [
            leaf,
            None,
            {
                "ModuleId": {"token": "CONTENT_SECRET_SENTINEL"},
                "Title": "Visible sibling",
                "Modules": [None, "malformed module"],
                "Topics": [
                    None,
                    {
                        "TopicId": {"token": "CONTENT_SECRET_SENTINEL"},
                        "Title": "\x1b[31mCONTROL_SENTINEL",
                        "TypeIdentifier": {"secret": "CONTENT_SECRET_SENTINEL"},
                        "Url": {"password": "CONTENT_SECRET_SENTINEL"},
                    },
                    {
                        "TopicId": 9002,
                        "Title": "Visible topic sibling",
                        "TypeIdentifier": "Link",
                        "Url": "https://example.invalid/sibling",
                    },
                ],
            },
        ]
    }


def test_content_human_renderer_bounds_deep_and_malformed_toc() -> None:
    toc = _deep_content_toc()
    with patch.object(LighthouseClient, "get_content_toc", return_value=toc):
        result = CliRunner().invoke(cli, ["content", "123"])

    assert result.exit_code == 0
    assert "Visible sibling" in result.stdout
    assert "Visible topic sibling" in result.stdout
    assert "[content truncated]" in result.stdout
    assert "CONTENT_SECRET_SENTINEL" not in result.stdout + result.stderr
    assert "CONTROL_SENTINEL" not in result.stdout + result.stderr
    assert "\x1b" not in result.stdout + result.stderr


def test_content_json_renderer_bounds_deep_and_malformed_toc() -> None:
    toc = _deep_content_toc()
    with patch.object(LighthouseClient, "get_content_toc", return_value=toc):
        result = CliRunner().invoke(cli, ["content", "123", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["course_id"] == 123
    assert isinstance(payload["modules"], list)
    assert "Visible sibling" in result.stdout
    assert "[content truncated]" in result.stdout
    assert "CONTENT_SECRET_SENTINEL" not in result.stdout + result.stderr
    assert "CONTROL_SENTINEL" not in result.stdout + result.stderr
    assert "\x1b" not in result.stdout + result.stderr


def test_quiz_human_renderer_projects_malformed_deep_fields() -> None:
    rich_text: dict[str, object] = {"Text": "fallback description"}
    for _ in range(1100):
        rich_text = {"Html": rich_text}
    quiz = {
        "QuizId": {"token": "QUIZ_SECRET_SENTINEL"},
        "Name": {"password": "QUIZ_SECRET_SENTINEL"},
        "Description": {"Html": rich_text, "Text": "fallback description"},
        "Instructions": "\x1b[31mQUIZ_CONTROL_SENTINEL",
        "AttemptsAllowed": {"NumberOfAttemptsAllowed": {"token": "QUIZ_SECRET_SENTINEL"}},
        "SubmissionTimeLimit": {
            "IsEnforced": True,
            "TimeLimitValue": {"secret": "QUIZ_SECRET_SENTINEL"},
        },
        "IsActive": "true",
    }
    with patch.object(LighthouseClient, "get_quiz_detail", return_value=quiz):
        result = CliRunner().invoke(cli, ["quiz", "123", "7"])

    assert result.exit_code == 0
    assert "📝 Quiz" in result.stdout
    assert "fallback description" in result.stdout
    assert "Attempts: ?" in result.stdout
    assert "Time Limit: ? min" in result.stdout
    assert "QUIZ_SECRET_SENTINEL" not in result.stdout + result.stderr
    assert "QUIZ_CONTROL_SENTINEL" not in result.stdout + result.stderr
    assert "\x1b" not in result.stdout + result.stderr


def test_quiz_json_renderer_projects_malformed_deep_fields() -> None:
    rich_text: dict[str, object] = {"Html": "safe instructions"}
    for _ in range(1100):
        rich_text = {"Text": rich_text}
    quiz = {
        "QuizId": 7,
        "Name": "Safe quiz",
        "Description": rich_text,
        "Instructions": {"password": "QUIZ_SECRET_SENTINEL"},
        "AttemptsAllowed": {"NumberOfAttemptsAllowed": {"token": "QUIZ_SECRET_SENTINEL"}},
        "SubmissionTimeLimit": {"TimeLimitValue": {"secret": "QUIZ_SECRET_SENTINEL"}},
    }
    with patch.object(LighthouseClient, "get_quiz_detail", return_value=quiz):
        result = CliRunner().invoke(cli, ["quiz", "123", "7", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["quiz"]["Name"] == "Safe quiz"
    assert payload["quiz"]["Description"] == ""
    assert payload["quiz"]["AttemptsAllowed"] == {"IsUnlimited": False}
    assert payload["quiz"]["SubmissionTimeLimit"] == {"IsEnforced": False}
    assert "QUIZ_SECRET_SENTINEL" not in result.stdout + result.stderr


def test_quiz_rich_text_normalizes_benign_multiline_content() -> None:
    quiz = {
        "QuizId": 7,
        "Name": "Safe quiz",
        "Description": "Line one\nLine two",
        "Instructions": "Read this\r\nthen answer",
    }
    with patch.object(LighthouseClient, "get_quiz_detail", return_value=quiz):
        result = CliRunner().invoke(cli, ["quiz", "123", "7", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["quiz"]["Description"] == "Line one Line two"
    assert payload["quiz"]["Instructions"] == "Read this then answer"


def test_multi_course_scope_uses_fixed_name_for_malformed_semester_with_also() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_semesters.return_value = [
        {"OrgUnitId": 200, "Name": {"token": "SEMESTER_SECRET_SENTINEL"}}
    ]
    client.get_course_enrollments.return_value = []
    client.get_enrolled_courses.return_value = [
        {"OrgUnitId": 999, "Name": "Extra course", "Code": "EXTRA"}
    ]

    with patch(
        "lighthouse_cli.commands._load_course_config",
        return_value={"999": {"name": "Extra course", "semester": "Sem II"}},
    ):
        scope = _resolve_course_scope(client, None, ["999"])

    assert scope == ([999], "Unknown Semester", 200, [])
    assert "SEMESTER_SECRET_SENTINEL" not in repr(scope)


def test_json_detection_stops_at_bare_option_terminator() -> None:
    assert not _has_json_option(["--", "--json"])
    assert _has_json_option(["--json", "--", "--json"])


def test_post_terminator_json_value_does_not_trigger_json_usage_output() -> None:
    result = CliRunner().invoke(cli, ["content", "--", "--json", "extra"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid command arguments. See --help." in result.stderr


def test_auth_status_invalid_json_has_safe_error_and_failing_exit_code() -> None:
    sentinel = "COOKIE_VALUE_SENTINEL"
    client = Mock()
    client.cookies = {"d2lSecureSessionVal": sentinel}
    client.check_auth.return_value = False

    with patch("lighthouse_cli.commands.LighthouseClient", return_value=client):
        result = CliRunner().invoke(cli, ["auth", "status", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "valid": False,
        "cookies": ["d2lSecureSessionVal"],
        "error": "Session expired. Run: lighthouse auth login",
    }
    assert sentinel not in result.stdout + result.stderr


def test_auth_status_invalid_human_path_matches_json_failure() -> None:
    client = Mock()
    client.cookies = {"d2lSecureSessionVal": "COOKIE_VALUE_SENTINEL"}
    client.check_auth.return_value = False

    with patch("lighthouse_cli.commands.LighthouseClient", return_value=client):
        result = CliRunner().invoke(cli, ["auth", "status"])

    assert result.exit_code == 1
    assert "Error: Session expired. Run: lighthouse auth login" in result.stderr


def test_semesters_normalize_malformed_records_for_human_and_json() -> None:
    sentinel = "SEMESTER_SECRET_SENTINEL"
    records = [
        {"OrgUnitId": 100, "Name": "Good", "Code": "G", "extra": sentinel},
        {"OrgUnitId": "bad", "Name": "Drop", "Code": "D"},
        None,
        {"OrgUnitId": 200, "Name": None, "Code": {"raw": sentinel}},
        {"OrgUnitId": 300, "Name": {"raw": sentinel}, "Code": "C"},
    ]

    with patch.object(LighthouseClient, "get_semesters", return_value=records):
        structured = CliRunner().invoke(cli, ["semesters", "--json"])
        human = CliRunner().invoke(cli, ["semesters"])

    assert structured.exit_code == 0
    assert json.loads(structured.stdout) == [
        {"OrgUnitId": 100, "Name": "Good", "Code": "G"},
        {"OrgUnitId": 200, "Name": "", "Code": ""},
        {"OrgUnitId": 300, "Name": "", "Code": "C"},
    ]
    assert sentinel not in structured.stdout + structured.stderr
    assert human.exit_code == 0
    assert "Good" in human.stdout
    assert sentinel not in human.stdout + human.stderr


def test_quiz_json_failure_has_one_command_shaped_document() -> None:
    with patch.object(
        LighthouseClient,
        "get_quiz_detail",
        side_effect=RuntimeError(
            "HTTP 404 Client Error for url: "
            "https://lighthouse.example/quizzes/7?cookie=QUIZ_SENTINEL"
        ),
    ):
        result = CliRunner().invoke(cli, ["quiz", "123", "7", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "course_id": 123,
        "quiz": {},
        "error": "Not found (HTTP 404).",
    }
    assert "QUIZ_SENTINEL" not in result.stdout + result.stderr
    assert "https://" not in result.stdout + result.stderr
    assert "Error:" in result.stderr


def test_json_usage_error_is_parseable_but_human_usage_stays_on_stderr() -> None:
    result = CliRunner().invoke(cli, ["content", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": "Invalid command arguments. See --help."
    }
    assert "Invalid command arguments. See --help." in result.stderr

    human = CliRunner().invoke(cli, ["content"])
    assert human.exit_code == 2
    assert human.stdout == ""
    assert "Invalid command arguments. See --help." in human.stderr


def test_json_usage_errors_cover_nested_leaf_commands() -> None:
    result = CliRunner().invoke(cli, ["auth", "verify", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]
    assert "Invalid command arguments. See --help." in result.stderr


@pytest.mark.parametrize(
    "argv_and_sentinel",
    [
        (["auth", "login", "--mfa-method", "PASSWORD_SENTINEL", "--json"], "PASSWORD_SENTINEL"),
        (["download", "44347", "--assignment", "TOKEN_SENTINEL", "--json"], "TOKEN_SENTINEL"),
        (["quiz", "44347", "QUIZ_SENTINEL", "--json"], "QUIZ_SENTINEL"),
    ],
)
def test_json_usage_errors_never_echo_invalid_secret_like_values(
    argv_and_sentinel: tuple[list[str], str],
) -> None:
    argv, sentinel = argv_and_sentinel

    result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": "Invalid command arguments. See --help."
    }
    assert "Invalid command arguments. See --help." in result.stderr
    assert sentinel not in result.stdout + result.stderr


def test_human_usage_errors_never_echo_invalid_secret_like_values() -> None:
    sentinel = "PASSWORD_SENTINEL"

    result = CliRunner().invoke(
        cli,
        ["auth", "login", "--mfa-method", sentinel],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid command arguments. See --help." in result.stderr
    assert sentinel not in result.stdout + result.stderr


def test_format_user_error_keeps_status_and_category_without_transport_details() -> None:
    response = requests.Response()
    response.status_code = 500
    error = requests.HTTPError(
        "500 Server Error for url: "
        "https://lighthouse.example/api?password=PASSWORD_SENTINEL;"
        " response body: BODY_SENTINEL"
    )
    error.response = response

    message = format_user_error(error)

    assert message == "Remote server error (HTTP 500)."
    assert "PASSWORD_SENTINEL" not in message
    assert "BODY_SENTINEL" not in message
    assert "https://" not in message


def test_format_user_error_strips_relative_query_and_body() -> None:
    message = format_user_error(
        "Request failed: /d2l/api?session=SESSION_SENTINEL; "
        "response body: BODY_SENTINEL"
    )

    assert message == "Network error. Check your connection and try again."
    assert "SESSION_SENTINEL" not in message
    assert "BODY_SENTINEL" not in message


@pytest.mark.parametrize(
    "raw",
    [
        "responseBody=BODY_SENTINEL",
        "response_body: BODY_SENTINEL",
        "flowToken=FLOW_SENTINEL",
        'oPostParams={"password":"P_SENTINEL"}',
        "cookieValue=COOKIE_SENTINEL",
        "d2lSecureSessionVal=SESSION_SENTINEL",
        "access_token=ACCESS_SENTINEL",
        "client_secret=CLIENT_SENTINEL",
        'headers={"Cookie": "COOKIE_SENTINEL"}',
        "password hunter2",
        "password SECRET",
        "secret SECRET",
        "foo secret SECRET",
        "token SECRET",
        "passphrase SECRET",
        "Run: lighthouse auth login --pass PASSWORD_SENTINEL",
        'headers={"Cookie":"abcd1234"}',
        'data={"password":"abcd1234"}',
        'headers={"X-Api-Key":"abcdef123"}',
    ],
)
def test_format_user_error_never_echoes_secret_shaped_transport_text(raw: str) -> None:
    message = format_user_error(raw)

    assert "SENTINEL" not in message
    assert "hunter2" not in message
    assert "PASSWORD_SENTINEL" not in message
    assert "abcd1234" not in message
    assert "abcdef123" not in message


def test_format_user_error_uses_generic_fallback_for_unknown_upstream_text() -> None:
    assert format_user_error("opaque upstream implementation detail") == "Command failed."


@pytest.mark.parametrize(
    "raw",
    [
        '{"password":"abcd1234"}',
        'headers={"Cookie":"abcd1234"}',
        'headers={"X-Api-Key":"abcdef123"}',
        "responseBody=abcd1234",
        "flowToken abcd1234",
        "oPostParams={x}",
        "cookieValue=abcd1234",
        "sessionVal=abcd1234",
        "access_token=abcd1234",
        "client-secret: abcdef123",
        "password hunter2",
        "--pass hunter2",
    ],
)
def test_safe_display_text_rejects_nested_and_cli_secret_shapes(raw: str) -> None:
    assert safe_display_text(raw, "[omitted]") == "[omitted]"


def test_safe_display_text_preserves_normal_prose_and_rejects_controls() -> None:
    assert safe_display_text("  Signals & Systems  ") == "Signals & Systems"
    assert safe_display_text("Session 1") == "Session 1"
    assert safe_display_text("Token Ring Networks") == "Token Ring Networks"
    assert safe_display_text("Password Security") == "Password Security"
    assert safe_display_text("Signals\nSystems", "[omitted]") == "[omitted]"
    assert safe_display_text("a" * 513, "[omitted]") == "[omitted]"


def test_semester_rich_cells_render_remote_markup_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rich_console = pytest.importorskip("rich.console")
    rich_table = pytest.importorskip("rich.table")
    rich_text = pytest.importorskip("rich.text")
    from lighthouse_cli import display

    stream = io.StringIO()
    console = rich_console.Console(
        file=stream,
        force_terminal=True,
        width=200,
        height=25,
    )
    monkeypatch.setattr(
        display,
        "_RICH_CACHE",
        (rich_table.Table, rich_text.Text, console),
    )
    monkeypatch.setattr(display, "_RICH_CHECKED", True)
    payload = "Course [link=//attacker.invalid]trusted.example[/link]"
    style = "Course [bold red]FAILED[/bold red]"

    with patch.object(
        LighthouseClient,
        "get_semesters",
        return_value=[{"OrgUnitId": 1, "Name": payload, "Code": style}],
    ):
        result = CliRunner().invoke(cli, ["semesters"])

    rendered = stream.getvalue()
    assert result.exit_code == 0
    assert payload in rendered
    assert style in rendered
    assert "\x1b]8;" not in rendered
    assert "\x1b[1mFAILED\x1b[0m" not in rendered


@pytest.mark.parametrize(
    "raw",
    [
        "password=SECRET_VALUE",
        "pass=TOPSECRET",
        "passwordValue=TOPSECRET",
        "tokenValue=TOPSECRET",
        'headers={"Cookie":"abcd1234"}',
        'data={"password":"abcd1234"}',
        'headers={"X-Api-Key":"abcdef123"}',
        "--pass hunter2",
        "token is TOKEN_VALUE",
        "password SECRET",
        "secret SECRET",
        "foo secret SECRET",
        "token SECRET",
        "passphrase SECRET",
    ],
)
def test_label_wrappers_share_safe_display_text_precision(raw: str) -> None:
    from lighthouse_cli import commands, course_config, show, submit, sync_engine

    wrappers = (
        lambda value: commands._safe_server_text(value, fallback="[omitted]"),
        lambda value: show._safe_announcement_text(value) or "[omitted]",
        lambda value: submit._safe_display_name(value, "[omitted]"),
        lambda value: sync_engine._safe_label(value, "[omitted]"),
        lambda value: course_config._safe_catalog_text(value, "[omitted]"),
    )
    # All production label wrappers must reject the same adversarial shapes.
    for wrapper in wrappers:
        assert wrapper(raw) == "[omitted]"


@pytest.mark.parametrize("label", ["Session 1", "Token Ring Networks", "Password Security"])
def test_label_wrappers_preserve_legitimate_keyword_labels(label: str) -> None:
    from lighthouse_cli import commands, course_config, show, submit, sync_engine

    assert commands._safe_server_text(label, fallback="[omitted]") == label
    assert show._safe_announcement_text(label) == label
    assert submit._safe_display_name(label, "[omitted]") == label
    assert sync_engine._safe_label(label, "[omitted]") == label
    assert course_config._safe_catalog_text(label, "[omitted]") == label


def test_output_json_never_emits_nonstandard_nan_tokens(capsys) -> None:
    output_json({"value": math.nan, "nested": [math.inf, -math.inf]})

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"value": None, "nested": [None, None]}


def test_course_catalog_always_attempts_enrollment_projection_first() -> None:
    class Client:
        def __init__(self) -> None:
            self.enrolled_calls = 0
            self.legacy_calls = 0

        def get_enrolled_courses(self):
            self.enrolled_calls += 1
            return [
                {"OrgUnitId": "35", "Name": "Enrollment course"},
                {"OrgUnitId": "bad", "Name": "Ignore"},
                {"OrgUnitId": True, "Name": "Boolean"},
                {"OrgUnitId": 0, "Name": "Zero"},
                {"OrgUnitId": -4, "Name": "Negative"},
                {"OrgUnitId": 3.5, "Name": "Fractional"},
                {"OrgUnitId": 35, "Name": "Duplicate"},
            ]

        def get_courses(self):
            self.legacy_calls += 1
            return [{"OrgUnitId": 999, "Name": "Legacy"}]

    client = Client()
    assert get_enrolled_course_catalog(client) == [
        {"OrgUnitId": 35, "Name": "Enrollment course", "Code": ""}
    ]
    assert client.enrolled_calls == 1
    assert client.legacy_calls == 0


def test_course_catalog_does_not_fallback_after_projection_failure() -> None:
    class Client:
        def __init__(self) -> None:
            self.legacy_calls = 0

        def get_enrolled_courses(self):
            raise RuntimeError("projection unavailable")

        def get_courses(self):
            self.legacy_calls += 1
            return [{"OrgUnitId": "7", "Name": "Legacy", "Code": "L"}]

    client = Client()
    with pytest.raises(RuntimeError, match="projection unavailable"):
        get_enrolled_course_catalog(client)
    assert client.legacy_calls == 0


def test_production_catalog_does_not_fallback_after_native_failure() -> None:
    client = LighthouseClient()
    with patch.object(
        LighthouseClient,
        "get_enrolled_courses",
        side_effect=RuntimeError("native enrollment failed"),
    ), patch.object(
        LighthouseClient,
        "get_courses",
        return_value=[{"OrgUnitId": 7, "Name": "Legacy"}],
    ) as legacy:
        with pytest.raises(RuntimeError, match="native enrollment failed"):
            get_enrolled_course_catalog(client)

    legacy.assert_not_called()


def test_course_catalog_falls_back_for_legacy_client_without_projection() -> None:
    class Client:
        def get_courses(self):
            return [{"OrgUnitId": "7", "Name": "Legacy", "Code": "L"}]

    assert get_enrolled_course_catalog(Client()) == [
        {"OrgUnitId": 7, "Name": "Legacy", "Code": "L"}
    ]


def test_course_catalog_accepts_subclass_legacy_getter_override() -> None:
    class LegacySubclass(LighthouseClient):
        def get_courses(self) -> list[dict[str, object]]:
            return [{"OrgUnitId": 7, "Name": "Legacy", "Code": "L"}]

    client = LegacySubclass()
    assert get_enrolled_course_catalog(client) == [
        {"OrgUnitId": 7, "Name": "Legacy", "Code": "L"}
    ]


def test_course_catalog_uses_configured_legacy_mock_when_projection_is_empty() -> None:
    client = Mock(spec=LighthouseClient)
    client.get_enrolled_courses.return_value = None
    client.get_courses.return_value = [
        {"OrgUnitId": "7", "Name": "Legacy", "Code": "L"},
    ]

    assert get_enrolled_course_catalog(client) == [
        {"OrgUnitId": 7, "Name": "Legacy", "Code": "L"}
    ]
    assert get_course_name(client, 7) == "Legacy"
    assert client.get_courses.call_count == 2


def test_courses_expose_explicit_semester_state_without_writing_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "course-config.json"
    original = json.dumps(
        {
            "tracked_courses": {
                "123": {"name": "Mapped", "semester": "Sem V"},
            }
        }
    )
    config_path.write_text(original, encoding="utf-8")
    enrollments = [
        {"OrgUnit": {"Id": 123, "Name": "Mapped", "Code": "M"}},
        {"OrgUnit": {"Id": 456, "Name": "Unmapped", "Code": "U"}},
    ]

    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", config_path), patch.object(
        LighthouseClient,
        "get_course_enrollments",
        return_value=enrollments,
    ):
        result = CliRunner().invoke(cli, ["courses", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {
            "OrgUnitId": 123,
            "Name": "Mapped",
            "Code": "M",
            "IsActive": True,
            "semester": "Sem V",
            "semester_source": "config",
        },
        {
            "OrgUnitId": 456,
            "Name": "Unmapped",
            "Code": "U",
            "IsActive": True,
            "semester": "",
            "semester_source": "unmapped",
        },
    ]
    assert config_path.read_text(encoding="utf-8") == original

    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", config_path), patch.object(
        LighthouseClient,
        "get_course_enrollments",
        return_value=enrollments,
    ):
        human = CliRunner().invoke(cli, ["courses"])
    assert human.exit_code == 0
    assert "Unmapped" in human.stdout


def test_courses_project_malformed_enrollment_labels_for_human_and_json() -> None:
    sentinel = "COURSE_LABEL_SECRET_SENTINEL"
    enrollments = [
        {
            "OrgUnit": {
                "Id": 123,
                "Name": f"password={sentinel}",
                "Code": "\x1b[31mCODE_CONTROL_SENTINEL",
            },
            "Access": {"IsActive": True},
        },
        {
            "OrgUnit": {
                "Id": 456,
                "Name": {"token": sentinel},
                "Code": {"secret": sentinel},
            },
            "Access": {"IsActive": True},
        },
        None,
    ]

    with patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments):
        structured = CliRunner().invoke(cli, ["courses", "--json"])
        human = CliRunner().invoke(cli, ["courses"])

    assert structured.exit_code == 0
    assert json.loads(structured.stdout) == [
        {
            "OrgUnitId": 123,
            "Name": "",
            "Code": "",
            "IsActive": True,
            "semester": "",
            "semester_source": "unmapped",
        },
        {
            "OrgUnitId": 456,
            "Name": "",
            "Code": "",
            "IsActive": True,
            "semester": "",
            "semester_source": "unmapped",
        },
    ]
    assert human.exit_code == 0
    assert sentinel not in structured.stdout + structured.stderr + human.stdout + human.stderr
    assert "CODE_CONTROL_SENTINEL" not in structured.stdout + structured.stderr + human.stdout + human.stderr
    assert "\x1b" not in structured.stdout + structured.stderr + human.stdout + human.stderr


def test_help_explains_write_scope_and_json_controls() -> None:
    root = CliRunner().invoke(cli, ["--help"])
    assert root.exit_code == 0
    assert "submit" in root.stdout.lower()
    assert "remote" in root.stdout.lower()
    assert "local" in root.stdout.lower()
    assert "command-specific JSON" in root.stdout

    download = CliRunner().invoke(cli, ["download", "--help"])
    assert download.exit_code == 0
    assert "local" in download.stdout.lower()
    assert "manifest" in download.stdout.lower()
    assert "dry-run" in download.stdout
    assert "force" in download.stdout.lower()

    reset = CliRunner().invoke(cli, ["config", "courses", "--help"])
    assert reset.exit_code == 0
    assert "local course tracking only" in reset.stdout


@pytest.mark.parametrize(
    ("argv", "patch_target", "payload_key"),
    [
        (["semesters", "--json"], "lighthouse_cli.commands.LighthouseClient", "semesters"),
        (["courses", "--json"], "lighthouse_cli.commands.LighthouseClient", "courses"),
        (["download", "--json"], "lighthouse_cli.commands.LighthouseClient", "courses"),
        (["sync", "--json"], "lighthouse_cli.commands.LighthouseClient", "courses"),
        (["config", "courses", "--add", "123", "--json"], "lighthouse_cli.course_config.LighthouseClient", "courses"),
    ],
)
def test_leaf_constructor_failure_is_one_json_document(
    argv: list[str], patch_target: str, payload_key: str,
) -> None:
    with patch(patch_target, side_effect=RuntimeError("constructor failed")):
        result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload[payload_key] == []
    assert payload["error"] == "Command failed."
    assert result.stdout.count("\"error\"") == 1
    assert "Error: Command failed." in result.stderr


@pytest.mark.parametrize("command", ["download", "sync"])
def test_omitted_course_without_trustworthy_config_fails_closed_as_json(
    command: str, tmp_path: Path,
) -> None:
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", tmp_path / "missing.json"), \
        patch.object(LighthouseClient, "get_semesters", side_effect=AssertionError("scope must fail before API")), \
        patch.object(LighthouseClient, "get_course_enrollments", side_effect=AssertionError("scope must fail before API")):
        result = CliRunner().invoke(cli, [command, "--json", "-o", str(tmp_path / "out")])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["courses"] == []
    assert "COURSE_ID" in payload["error"]
    assert "config courses" in payload["error"]
    assert result.stderr.startswith("Error:")


def test_attachment_validation_failure_is_one_json_document() -> None:
    result = CliRunner().invoke(cli, ["download", "--attachment", "1", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "--attachment requires --assignment"
    assert result.stdout.count("\"error\"") == 1


@pytest.mark.parametrize("command", ["download", "sync"])
def test_single_course_engine_failure_is_one_json_document(command: str, tmp_path: Path) -> None:
    with patch("lighthouse_cli.commands.run_course", side_effect=RuntimeError("opaque upstream detail")):
        result = CliRunner().invoke(
            cli,
            [command, "123", "--json", "-o", str(tmp_path / command)],
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["course_id"] == 123
    assert payload["error"] == "Command failed."
    assert result.stdout.count("\"error\"") == 1


def test_dry_run_preserves_local_path_validation_errors_as_json(tmp_path: Path) -> None:
    output_root = tmp_path / "out"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output_root / "Course-123").symlink_to(outside, target_is_directory=True)
    toc = {
        "Modules": [{
            "ModuleId": 1,
            "Title": "Module",
            "Modules": [],
            "Topics": [{
                "TopicId": 7,
                "Title": "notes.pdf",
                "TypeIdentifier": "File",
                "LastModifiedDate": "2026-01-01T00:00:00Z",
            }],
        }],
    }
    with patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
        patch.object(LighthouseClient, "get_courses", return_value=[{"OrgUnitId": 123, "Name": "Course"}]):
        result = CliRunner().invoke(
            cli,
            ["download", "123", "--dry-run", "--json", "-o", str(output_root)],
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["planned"] == []
    assert payload["errors"]
    assert any(word in payload["errors"][0]["error"].lower() for word in ("symlink", "escapes"))


@pytest.mark.parametrize(
    ("command", "mode_args"),
    [("download", []), ("download", ["--dry-run"]), ("sync", [])],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_symlinked_output_root_is_rejected_before_client_or_body_work(
    command: str, mode_args: list[str], json_output: bool, tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(outside, target_is_directory=True)
    args = [command, "123", "-o", str(output_link), *mode_args]
    if json_output:
        args.append("--json")

    with patch("lighthouse_cli.commands.LighthouseClient", side_effect=AssertionError("client must not initialize")):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert "symlink" in (result.stdout + result.stderr).lower()
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["error"]
    assert list(outside.iterdir()) == []


def test_run_course_rejects_symlinked_output_root_before_api_or_writes(tmp_path: Path) -> None:
    from lighthouse_cli.sync_engine import Mode, run_course

    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(outside, target_is_directory=True)
    client = Mock(spec=LighthouseClient)

    result = run_course(client, 123, output_link, mode=Mode.PLAN)

    assert result["errors"] and result["errors"][0]["type"] == "path"
    assert "symlink" in result["errors"][0]["error"].lower()
    client.get_content_toc.assert_not_called()
    client.get_courses.assert_not_called()
    assert list(outside.iterdir()) == []


def test_single_sync_empty_toc_preserves_orphaned_entries_and_exit_code(
    tmp_path: Path,
) -> None:
    sentinel = "password=SECRET_SENTINEL"
    output_root = tmp_path / "out"
    output_root.mkdir()
    course_root = output_root / "Course-44347"
    course_root.mkdir()
    (course_root / ".lighthouse.json").write_text(
        json.dumps(
            {
                "99": {
                    "sha256": "a" * 64,
                    "filename": sentinel,
                    "size": 3,
                    "downloaded_at": "2026-01-01T00:00:00Z",
                    "last_modified": "2026-01-01T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        LighthouseClient,
        "get_enrolled_courses",
        return_value=[{"OrgUnitId": 44347, "Name": "Course"}],
    ), patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):
        structured = CliRunner().invoke(
            cli,
            ["sync", "44347", "--json", "-o", str(output_root)],
        )
        human = CliRunner().invoke(
            cli,
            ["sync", "44347", "-o", str(output_root)],
        )

    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert payload["downloaded"] == []
    assert payload["orphaned"] == [
        {
            "topic_id": "99",
            "size": 3,
            "size_kb": 0.0,
            "sha256": "a" * 64,
        }
    ]
    assert sentinel not in structured.stdout + structured.stderr
    assert human.exit_code == 0
    assert "1 orphaned" in human.stdout


def test_multi_course_errors_drop_server_provided_filename_and_title(
    tmp_path: Path,
) -> None:
    sentinel = "TOKEN_SENTINEL"
    config_path = tmp_path / "course-config.json"
    config_path.write_text(
        json.dumps(
            {
                "tracked_courses": {
                    "111": {"name": "Course", "semester": "Sem I"},
                }
            }
        ),
        encoding="utf-8",
    )
    toc = {
        "Modules": [
            {
                "ModuleId": 1,
                "Title": "Module",
                "Modules": [],
                "Topics": [
                    {
                        "TopicId": 7,
                        "Title": sentinel,
                        "TypeIdentifier": "File",
                        "LastModifiedDate": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ]
    }

    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", config_path), \
        patch.object(LighthouseClient, "get_semesters", return_value=[{"OrgUnitId": 100, "Name": "Sem I"}]), \
        patch.object(
            LighthouseClient,
            "get_course_enrollments",
            return_value=[{"OrgUnit": {"Id": 111, "Name": "Course"}}],
        ), patch.object(
            LighthouseClient,
            "get_enrolled_courses",
            return_value=[{"OrgUnitId": 111, "Name": "Course"}],
        ), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
        patch.object(LighthouseClient, "download_topic_file", side_effect=RuntimeError("download failed")):
        result = CliRunner().invoke(
            cli,
            ["download", "--semester", "100", "--json", "-o", str(tmp_path / "out")],
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["courses"][0]["errors"] == [
        {"topic_id": "7", "error": "Command failed."}
    ]
    assert sentinel not in result.stdout + result.stderr


def test_semesters_runtime_failure_is_one_json_document() -> None:
    with patch.object(LighthouseClient, "get_semesters", side_effect=RuntimeError("opaque")):
        result = CliRunner().invoke(cli, ["semesters", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"semesters": [], "error": "Command failed."}


def test_courses_runtime_failure_is_one_json_document() -> None:
    with patch.object(LighthouseClient, "get_enrolled_courses", side_effect=RuntimeError("opaque")), \
        patch.object(LighthouseClient, "get_courses", side_effect=RuntimeError("opaque fallback")):
        result = CliRunner().invoke(cli, ["courses", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"courses": [], "error": "Command failed."}


def test_config_runtime_failure_is_one_json_document() -> None:
    with patch("lighthouse_cli.course_config.load", side_effect=RuntimeError("opaque")):
        result = CliRunner().invoke(cli, ["config", "courses", "--list", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"courses": [], "error": "Command failed."}


def test_config_add_normalizes_positive_ids_and_keeps_first_duplicate(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "course-config.json"
    enrollments = [
        None,
        {"OrgUnit": {"Id": 0, "Name": "Zero"}},
        {"OrgUnit": {"Id": "bad", "Name": "Bad"}},
        {"OrgUnit": {"Id": -2, "Name": "Negative"}},
        {"OrgUnit": {"Id": "7", "Name": "First", "Code": "A"}},
        {"OrgUnit": {"Id": 7, "Name": "Duplicate", "Code": "B"}},
    ]
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", config_path), \
        patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments):
        result = CliRunner().invoke(
            cli,
            ["config", "courses", "--add", "7", "--semester", "Sem V", "--json"],
        )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {"id": "7", "name": "First", "semester": "Sem V"}
    ]


def test_courses_skips_bad_ids_and_normalizes_boolean_like_active_values() -> None:
    enrollments = [
        None,
        {"OrgUnitId": "bad", "Name": "Ignored"},
        {"OrgUnitId": "11", "Name": None, "Code": None, "IsActive": "false"},
        {"OrgUnitId": 12, "Name": "Active", "IsActive": "true"},
    ]
    with patch.object(LighthouseClient, "get_enrolled_courses", return_value=enrollments):
        result = CliRunner().invoke(cli, ["courses", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {
            "OrgUnitId": 11,
            "Name": "",
            "Code": "",
            "IsActive": False,
            "semester": "",
            "semester_source": "unmapped",
        },
        {
            "OrgUnitId": 12,
            "Name": "Active",
            "Code": "",
            "IsActive": True,
            "semester": "",
            "semester_source": "unmapped",
        },
    ]


def test_recovery_hint_discards_arbitrary_credential_arguments() -> None:
    assert format_user_error("Run: lighthouse auth login --pass PASSWORD_SENTINEL") == (
        "Run: lighthouse auth login"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Course 'course-name-with-private-context' not found. "
            "Run: lighthouse courses",
            "Course not found. Run: lighthouse courses",
        ),
        (
            "No tracked courses mapped to semester 'Sem V'.\n"
            "Run: lighthouse config courses --list to see your mappings.",
            "No tracked courses mapped to the requested semester. "
            "Run: lighthouse config courses",
        ),
        (
            "Dropbox folder 999 not found. Run: lighthouse assignments",
            "Dropbox folder not found. Run: lighthouse assignments",
        ),
        (
            "Requested assignment folder was not found.",
            "Assignment folder not found. Run: lighthouse assignments",
        ),
        (
            "Folder 'private assignment' not found in course 44347.\n"
            "Available folders:\n  789 – Private\n\n"
            "Run: lighthouse assignments",
            "Dropbox folder not found. Run: lighthouse assignments",
        ),
        (
            "Permission denied to submit to folder 789. "
            "Check your enrollment and submission rights.",
            "Permission denied to submit. Check your enrollment and submission rights.",
        ),
        (
            "Refusing to submit without --yes in non-interactive mode. "
            "Use --yes flag to confirm.",
            "Refusing to submit without --yes in non-interactive mode. "
            "Use --yes flag to confirm.",
        ),
        (
            "Could not read file: [Errno 13] Permission denied: "
            "'/private/user/submissions/answer.pdf'",
            "Could not read file. Check the path and permissions.",
        ),
        ("Submission cancelled.", "Submission cancelled."),
        (
            "Assignment attachments have an invalid response shape.",
            "Assignment response has an invalid shape.",
        ),
        (
            "Submission outcome is unknown because the API returned an unsupported "
            "result shape. Verify the assignment status before trying again.",
            "Submission outcome is unknown because the API returned an unsupported "
            "result shape. Verify the assignment status before trying again.",
        ),
    ],
)
def test_format_user_error_uses_fixed_templates_for_known_local_errors(
    raw: str, expected: str,
) -> None:
    message = format_user_error(raw)

    assert message == expected
    assert "private" not in message.lower()
    assert "999" not in message
    assert "44347" not in message
    assert "789" not in message
    assert "/private/" not in message
    assert "available folders" not in message.lower()


def test_format_user_error_redacts_nested_cookie_password_and_api_key_values() -> None:
    raw = (
        "opaque upstream response: {"
        '"headers": {"Cookie": "d2lSecureSessionVal=cookie-value-sentinel", '
        '"X-Api-Key": "sk-test-api-key-sentinel"}, '
        '"credentials": {"password": "correct-horse-sentinel"}, '
        '"config": {"apiKey": "sk-live-api-key-sentinel"}}'
    )

    message = format_user_error(raw)

    assert message == "Command failed."
    for secret in (
        "cookie-value-sentinel",
        "sk-test-api-key-sentinel",
        "correct-horse-sentinel",
        "sk-live-api-key-sentinel",
    ):
        assert secret not in message


def test_format_user_error_keeps_typed_submission_rate_limit_actionable() -> None:
    error = NetworkError(
        "Submission request was rate limited; no retry was attempted. "
        "response body: {\"token\": \"TOKEN_SENTINEL\"}"
    )

    assert format_user_error(error) == "Rate limited. No retry was attempted."
