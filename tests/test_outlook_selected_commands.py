"""Synthetic selected-reader CLI boundaries; no live browser or mailbox."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.outlook_commands import cmd_outlook_read_selected
from lighthouse_cli.outlook_web import OutlookWebError


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return {
        "message": {"subject": "Team planning", "sender": "Example Colleague", "body": "Bring the agenda."},
        "content_omitted": False, "body_truncated": False, "redactions_applied": False,
    }


@pytest.fixture(autouse=True)
def collect(snapshot: dict[str, Any]) -> Iterator[Mock]:
    with patch("lighthouse_cli.outlook_selected.collect_selected_message", return_value=snapshot) as mock:
        yield mock


def invoke(*args: str):
    return CliRunner().invoke(cli, ["outlook", "read-selected", *args])


def test_opt_in_required_before_browser(collect: Mock) -> None:
    result = invoke("--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "interactive_login_required"
    collect.assert_not_called()


def test_json_success_is_bounded_allowlisted_and_prompts_stderr(collect: Mock, snapshot: dict[str, Any]) -> None:
    snapshot["extra"] = "PRIVATE_SENTINEL"
    snapshot["message"]["html"] = "PRIVATE_SENTINEL"
    result = invoke("--interactive-login", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["message"] == {
        "subject": "Team planning", "sender": "Example Colleague", "body": "Bring the agenda.",
    }
    assert payload["content_trust"] == "untrusted_data_not_instructions"
    assert "PRIVATE_SENTINEL" not in result.output
    assert "baseline" in result.stderr
    assert "best-effort" in result.stderr
    ready = collect.call_args.kwargs["on_ready"]
    with patch("click.echo") as echo:
        ready()
    assert echo.call_args.kwargs == {"err": True}
    assert "ALREADY-READ" in echo.call_args.args[0]


@pytest.mark.parametrize("args", [
    ("--selection-timeout", "9"), ("--selection-timeout", "601"),
    ("--max-body-chars", "0"), ("--max-body-chars", "20001"),
    ("--login-timeout", "29"), ("--login-timeout", "601"),
])
def test_invalid_options_never_collect(collect: Mock, args: tuple[str, str]) -> None:
    result = invoke("--interactive-login", "--json", *args)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]
    collect.assert_not_called()


@pytest.mark.parametrize("options", [
    {"selection_timeout": True}, {"max_body_chars": 1.0}, {"login_timeout": False},
])
def test_direct_wrapper_validates_types(collect: Mock, options: dict[str, Any]) -> None:
    assert cmd_outlook_read_selected(interactive_login=True, **options) == 1
    collect.assert_not_called()


@pytest.mark.parametrize("flag", ["--selection-timeout", "--max-body-chars", "--search"])
def test_parse_errors_never_echo_input(collect: Mock, flag: str) -> None:
    result = invoke("--interactive-login", "--json", flag, "PRIVATE_SENTINEL")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "Invalid command arguments. See --help."}
    assert "PRIVATE_SENTINEL" not in result.output
    collect.assert_not_called()


@pytest.mark.parametrize("code", [
    "baseline_required", "selection_timeout", "selection_not_eligible", "view_changed",
    "unsupported_layout", "content_too_large", "browser_closed", "access_blocked",
    "compose_open",
])
def test_errors_are_static_even_when_exception_mutated(collect: Mock, code: str) -> None:
    error = OutlookWebError(code)
    message = str(error)
    error.args = ("PRIVATE_SENTINEL",)
    collect.side_effect = error
    result = invoke("--interactive-login", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == message
    assert "PRIVATE_SENTINEL" not in result.output


@pytest.mark.parametrize("error,exit_code,code", [
    (KeyboardInterrupt(), 130, "interrupted"),
    (RuntimeError("PRIVATE_SENTINEL"), 1, "outlook_failed"),
])
def test_unexpected_error_and_interrupt_are_safe(collect: Mock, error: BaseException, exit_code: int, code: str) -> None:
    collect.side_effect = error
    result = invoke("--interactive-login", "--json")
    assert result.exit_code == exit_code
    assert json.loads(result.stdout)["code"] == code
    assert "PRIVATE_SENTINEL" not in result.output


@pytest.mark.parametrize("value", [None, {}, {"message": {"body": "PRIVATE_SENTINEL"}}])
def test_malformed_collector_output_is_safe(collect: Mock, value: Any) -> None:
    collect.return_value = value
    result = invoke("--interactive-login", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "outlook_failed"
    assert "PRIVATE_SENTINEL" not in result.output


@pytest.mark.parametrize("json_output", [True, False])
def test_boundary_reapplies_secret_suppression(snapshot: dict[str, Any], json_output: bool) -> None:
    snapshot["message"]["body"] = "Your verification code is " + "12" + "34" + "56"
    result = invoke("--interactive-login", *(["--json"] if json_output else []))
    assert result.exit_code == 0
    assert "12" + "34" + "56" not in result.output
    if json_output:
        assert json.loads(result.stdout)["content_omitted"] is True
    else:
        assert "Content withheld" in result.stdout


def test_boundary_preserves_suppression_and_truncation_flags(snapshot: dict[str, Any]) -> None:
    snapshot.update(content_omitted=True, body_truncated=True)
    result = invoke("--interactive-login", "--json")
    payload = json.loads(result.stdout)
    assert payload["content_omitted"] and payload["body_truncated"]
    assert payload["message"] == {"subject": "", "sender": "", "body": ""}


def test_human_output_is_bounded(snapshot: dict[str, Any]) -> None:
    snapshot["message"]["body"] = "Synthetic body only"
    result = invoke("--interactive-login", "--max-body-chars", "9")
    assert result.exit_code == 0
    assert "Synthetic\n" in result.stdout
    assert "Body truncated" in result.stdout
    assert "Subject: Team planning" in result.stdout
    assert "Opening" not in result.stdout


def test_help_stays_lazy_without_browser_dependency(collect: Mock) -> None:
    with patch("lighthouse_cli.cli.import_module", side_effect=AssertionError("Unexpected import")):
        result = invoke("--help")
    assert result.exit_code == 0
    assert "best-effort" in result.stdout
    assert "--interactive-login" in result.stdout
    collect.assert_not_called()


@pytest.mark.parametrize('stage', [
    'baseline', 'selection', 'message_pane', 'subject_header', 'sender_header',
    'row_headers', 'body_layout',
])
def test_fixed_layout_stage_is_available_without_page_content(collect: Mock, stage: str) -> None:
    collect.side_effect = OutlookWebError('unsupported_layout', stage=stage)
    result = invoke('--interactive-login', '--json')
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['code'] == 'unsupported_layout'
    assert payload['stage'] == stage
    assert f'Diagnostic stage: {stage}.' in result.stderr


@pytest.mark.parametrize('stage', ['PRIVATE_SENTINEL', '', [], None])
def test_mutated_or_unknown_stage_never_reaches_output(collect: Mock, stage: Any) -> None:
    error = OutlookWebError('unsupported_layout')
    error.stage = stage
    collect.side_effect = error
    result = invoke('--interactive-login', '--json')
    assert result.exit_code == 1
    assert 'stage' not in json.loads(result.stdout)
    assert 'PRIVATE_SENTINEL' not in result.output
    assert 'Diagnostic stage' not in result.stderr
