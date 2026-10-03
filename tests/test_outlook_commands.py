"""Network-free tests for opt-in Outlook CLI wiring and output contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.outlook_commands import cmd_outlook_probe
from lighthouse_cli.outlook_web import OutlookWebError


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return {
        "source": "outlook_web",
        "coverage": "rendered_rows_only",
        "complete_mailbox": False,
        "stable_ids": False,
        "scope": "current_view",
        "text_included": False,
        "rows": [
            {"position": 1, "rendered_text": "", "unread": True, "text_omitted": True},
            {"position": 2, "rendered_text": "", "unread": False, "text_omitted": True},
            {"position": 3, "rendered_text": "", "unread": None, "text_omitted": True},
        ],
        "limit_reached": False,
    }


@pytest.fixture(autouse=True)
def collect(snapshot: dict[str, Any]) -> Iterator[Mock]:
    """No wrapper test may launch a browser or perform a real sign-in."""
    with patch("lighthouse_cli.outlook_commands.collect_outlook_rows", return_value=snapshot) as mock:
        yield mock


def test_requires_explicit_interactive_login_before_collecting(collect: Mock) -> None:
    result = CliRunner().invoke(cli, ["outlook", "probe", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "source": "outlook_web",
        "code": "interactive_login_required",
        "error": "Use --interactive-login to open a temporary browser and sign in yourself.",
    }
    assert "Error:" in result.stderr
    assert "Opening" not in result.stderr
    collect.assert_not_called()


def test_human_missing_opt_in_has_no_stdout(collect: Mock) -> None:
    result = CliRunner().invoke(cli, ["outlook", "probe"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "--interactive-login" in result.stderr
    collect.assert_not_called()


def test_json_success_preserves_snapshot_and_sends_prompt_to_stderr(
    collect: Mock, snapshot: dict[str, Any],
) -> None:
    result = CliRunner().invoke(cli, ["outlook", "probe", "--interactive-login", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == snapshot
    assert "Complete sign-in and MFA" in result.stderr
    assert "No sign-in session is saved" in result.stderr
    collect.assert_called_once_with(search=None, limit=25, login_timeout=180)


@pytest.mark.parametrize("query", ["q", "private query", "q" * 512, "résumé"])
@pytest.mark.parametrize("json_output", [False, True])
def test_search_is_rejected_before_collecting_without_echoing_query(
    collect: Mock, query: str, json_output: bool,
) -> None:
    result = CliRunner().invoke(cli, [
        "outlook", "probe", "--interactive-login", "--search", query,
    ] + (["--json"] if json_output else []))

    assert result.exit_code == 1
    if json_output:
        assert json.loads(result.stdout) == {
            "source": "outlook_web", "code": "search_not_supported",
            "error": "Outlook search is not supported yet because result freshness cannot be verified. "
            "No browser was opened.",
        }
    else:
        assert result.stdout == ""
    assert "private query" not in result.stdout + result.stderr
    assert "Opening" not in result.stderr
    collect.assert_not_called()


@pytest.mark.parametrize(("limit", "login_timeout"), [(1, 30), (100, 600)])
def test_boundary_options_are_accepted(collect: Mock, limit: int, login_timeout: int) -> None:
    result = CliRunner().invoke(cli, [
        "outlook", "probe", "--interactive-login",
        "--limit", str(limit), "--login-timeout", str(login_timeout), "--json",
    ])

    assert result.exit_code == 0
    collect.assert_called_once_with(search=None, limit=limit, login_timeout=login_timeout)


@pytest.mark.parametrize(
    ("option", "value", "code"),
    [
        ("--limit", "0", "invalid_limit"),
        ("--limit", "101", "invalid_limit"),
        ("--limit", "-1", "invalid_limit"),
        ("--login-timeout", "29", "invalid_login_timeout"),
        ("--login-timeout", "601", "invalid_login_timeout"),
        ("--search", "", "invalid_search"),
        ("--search", "   ", "invalid_search"),
        ("--search", "q" * 513, "invalid_search"),
        ("--search", "private\nquery", "invalid_search"),
        ("--search", "private\tquery", "invalid_search"),
        ("--search", "private\x1bquery", "invalid_search"),
        ("--search", "private\x00query", "invalid_search"),
    ],
)
def test_invalid_options_fail_before_collecting_without_echo(
    collect: Mock, option: str, value: str, code: str,
) -> None:
    result = CliRunner().invoke(cli, [
        "outlook", "probe", "--interactive-login", option, value, "--json",
    ])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == code
    assert "private" not in result.stdout + result.stderr
    assert "Opening" not in result.stderr
    collect.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [{"limit": True}, {"limit": 1.0}, {"login_timeout": True}, {"search": 123}],
)
def test_direct_wrapper_rejects_invalid_types(
    collect: Mock, capsys: pytest.CaptureFixture[str], options: dict[str, Any],
) -> None:
    assert cmd_outlook_probe(interactive_login=True, json_output=True, **options) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]
    assert "Opening" not in captured.err
    collect.assert_not_called()


@pytest.mark.parametrize(
    "code",
    [
        "dependency_missing", "browser_unavailable", "browser_closed", "browser_error",
        "navigation_timeout", "login_timeout", "view_timeout", "unsupported_layout",
        "view_changed", "empty_or_loading", "admin_approval_required", "consent_required", "consent_incomplete",
        "access_blocked", "registration_blocked",
    ],
)
def test_known_core_failures_have_static_json_and_failing_exit(collect: Mock, code: str) -> None:
    error = OutlookWebError(code)
    expected_message = str(error)
    error.args = ("upstream-private-error",)
    collect.side_effect = error
    result = CliRunner().invoke(cli, ["outlook", "probe", "--interactive-login", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "source": "outlook_web", "code": code, "error": expected_message,
    }
    assert "upstream-private-error" not in result.stdout + result.stderr
    assert expected_message in result.stderr


@pytest.mark.parametrize("unsafe_code", ["private-error-code", ["private-error-code"]])
def test_mutated_error_codes_cannot_escape_the_fixed_map(collect: Mock, unsafe_code: Any) -> None:
    error = OutlookWebError("browser_error")
    error.code = unsafe_code
    collect.side_effect = error
    result = CliRunner().invoke(cli, ["outlook", "probe", "--interactive-login", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "browser_error"
    assert "private-error-code" not in result.stdout + result.stderr


@pytest.mark.parametrize("json_output", [False, True])
def test_unexpected_exception_never_leaks_upstream_details(collect: Mock, json_output: bool) -> None:
    collect.side_effect = RuntimeError("https://example.invalid/?private=unexpected-detail")
    args = ["outlook", "probe", "--interactive-login"] + (["--json"] if json_output else [])
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert "unexpected-detail" not in result.stdout + result.stderr
    assert "https://" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    if json_output:
        assert json.loads(result.stdout) == {
            "source": "outlook_web", "code": "outlook_failed",
            "error": "Outlook row collection failed.",
        }
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("json_output", [False, True])
def test_interrupt_returns_130_and_a_single_json_error_when_requested(
    collect: Mock, json_output: bool,
) -> None:
    collect.side_effect = KeyboardInterrupt
    args = ["outlook", "probe", "--interactive-login"] + (["--json"] if json_output else [])
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 130
    assert "Outlook row collection interrupted." in result.stderr
    if json_output:
        assert json.loads(result.stdout)["code"] == "interrupted"
    else:
        assert result.stdout == ""


def test_human_output_identifies_partial_snapshot_and_unread_states(
    snapshot: dict[str, Any],
) -> None:
    snapshot["limit_reached"] = True
    result = CliRunner().invoke(cli, ["outlook", "probe", "--interactive-login"])

    assert result.exit_code == 0
    assert "3 currently rendered rows" in result.stdout
    assert "Partial view only; row positions are not message IDs." in result.stdout
    assert "Metadata only: all message content is withheld." in result.stdout
    assert "1. [unread] [content withheld]" in result.stdout
    assert "2. [read] [content withheld]" in result.stdout
    assert "3. [unknown] [content withheld]" in result.stdout
    assert "additional rows may exist" in result.stdout
    assert "Opening" not in result.stdout


@pytest.mark.parametrize("json_output", [False, True])
def test_output_boundary_withholds_all_text_even_if_collector_adds_content(
    snapshot: dict[str, Any], json_output: bool,
) -> None:
    sentinel = "PRIVATE_CONTENT_SENTINEL"
    snapshot["text_included"] = True
    snapshot["extra_content"] = sentinel
    for row in snapshot["rows"]:
        row["position"] = sentinel
        row["rendered_text"] = sentinel
        row["text_omitted"] = False
        row["label"] = sentinel
        row["url"] = sentinel
    result = CliRunner().invoke(cli, [
        "outlook", "probe", "--interactive-login",
    ] + (["--json"] if json_output else []))

    assert result.exit_code == 0
    assert sentinel not in result.stdout + result.stderr
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["text_included"] is False
        assert payload["scope"] == "current_view"
        assert "extra_content" not in payload
        assert all(row["rendered_text"] == "" for row in payload["rows"])
        assert all(row["text_omitted"] is True for row in payload["rows"])
        assert all("label" not in row and "url" not in row for row in payload["rows"])


def test_empty_snapshot_does_not_claim_empty_mailbox(snapshot: dict[str, Any]) -> None:
    snapshot["rows"] = []
    result = CliRunner().invoke(cli, ["outlook", "probe", "--interactive-login"])

    assert result.exit_code == 0
    assert "0 currently rendered rows" in result.stdout
    assert "empty mailbox" not in result.stdout.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["outlook", "probe", "--limit", "private-invalid-number", "--json"],
        ["outlook", "probe", "--login-timeout", "private-invalid-number", "--json"],
        ["outlook", "probe", "--private-invalid-option", "--json"],
        ["outlook", "private-invalid-command", "--json"],
    ],
)
def test_json_parse_failures_are_single_safe_document(collect: Mock, args: list[str]) -> None:
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "Invalid command arguments. See --help."}
    assert "private-invalid" not in result.stdout + result.stderr
    collect.assert_not_called()


@pytest.mark.parametrize(
    "args", [["--help"], ["outlook", "--help"], ["outlook", "probe", "--help"], ["--version"]],
)
def test_help_and_version_do_not_load_outlook_implementation(collect: Mock, args: list[str]) -> None:
    with patch("lighthouse_cli.cli.import_module", side_effect=AssertionError("unexpected import")):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    collect.assert_not_called()


def test_help_starts_with_browser_dependency_unavailable() -> None:
    script = (
        "import sys; sys.modules['playwright'] = None; "
        "sys.modules['playwright.sync_api'] = None; "
        "from click.testing import CliRunner; from lighthouse_cli.cli import cli; "
        "result = CliRunner().invoke(cli, ['outlook', 'probe', '--help']); "
        "assert result.exit_code == 0, result.output; "
        "assert '--interactive-login' in result.stdout; "
        "assert 'lighthouse_cli.outlook_commands' not in sys.modules; "
        "assert 'lighthouse_cli.outlook_web' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, timeout=10)

    assert result.returncode == 0, result.stderr.decode()


def test_probe_help_explains_experimental_content_free_scope(collect: Mock) -> None:
    result = CliRunner().invoke(cli, ["outlook", "probe", "--help"])

    assert result.exit_code == 0
    assert "experimental metadata-only probe" in result.stdout
    assert "withholds all row text, labels, and previews" in result.stdout
    assert "Search is unsupported" in result.stdout
    collect.assert_not_called()


def test_outlook_does_not_advertise_a_list_command(collect: Mock) -> None:
    result = CliRunner().invoke(cli, ["outlook", "--help"])

    assert result.exit_code == 0
    assert "probe" in result.stdout
    assert "  list " not in result.stdout
    collect.assert_not_called()
