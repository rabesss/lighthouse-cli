"""Tests for lighthouse auth login: policy helpers + CLI smokes.

The pure decision layer (``resolve_credentials``, ``normalize_totp``,
``plan_login``, ``_persist_check_report``) is tested with plain args and no
I/O; end-to-end behaviour runs through CliRunner smokes.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli import auth as auth_mod
from lighthouse_cli.auth import (
    normalize_totp,
    plan_login,
    resolve_credentials,
    validate_totp_usage,
)
from lighthouse_cli.cli import cli
from lighthouse_cli.config import load_cookies, save_cookies, save_mfa_pending
from lighthouse_cli.credential_store import CredentialStoreError
from lighthouse_cli.ms_auth import MicrosoftSSOError
from lighthouse_cli.ms_mfa import MfaProbeResult, UserProof

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# A hostile proof display: none of its parts may ever reach the output.
LEAKY_DISPLAY = "FULL-DISPLAY-SENTINEL user@example.com +919876541234"
DISPLAY_LEAKS = ("FULL-DISPLAY-SENTINEL", "user@example.com", "+919876541234")


def _make_d2l_cookies() -> dict[str, str]:
    """Return a valid D2L cookies dict."""
    return {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("2FA verification timed out waiting for approval.", "2FA verification timed out waiting for approval."),
        ("D2L ACS redirect limit exceeded.", "D2L ACS redirect limit exceeded."),
        ("D2L home redirect limit exceeded.", "D2L home redirect limit exceeded."),
        ("Microsoft session-pull requested an unsafe re-POST target.", "Microsoft session-pull requested an unsafe re-POST target."),
        ("2FA code required after verification was sent.", "2FA code required after verification was sent."),
        ("A pre-provided --totp code is valid only for PhoneAppOTP.", "A pre-provided --totp code is valid only for PhoneAppOTP."),
        (
            "A pre-provided --totp code cannot be validated for a legacy MFA form.",
            "A pre-provided --totp code cannot be used with a legacy MFA form.",
        ),
        ("Pending MFA session is incomplete (missing state).", "Pending MFA session is incomplete."),
    ],
)
def test_first_party_auth_failures_keep_safe_actionable_categories(
    message: str,
    expected: str,
) -> None:
    assert auth_mod._safe_auth_error_message(message) == expected


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LIGHTHOUSE_CONFIG_DIR at a per-test directory."""
    config_dir = tmp_path / ".config" / "lighthouse-cli"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def creds_env(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config plus env-supplied credentials; returns the config dir."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    return isolated_config


@contextmanager
def _mock_sso(
    check_auth: bool = True,
    login_side_effect: BaseException | None = None,
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Mock MicrosoftSSOClient and LighthouseClient inside lighthouse_cli.auth.

    Yields ``(sso_mock, client_mock)``; ``sso.login`` returns valid cookies
    unless ``login_side_effect`` is given.
    """
    sso = MagicMock()
    sso.login.return_value = _make_d2l_cookies()
    if login_side_effect is not None:
        sso.login.side_effect = login_side_effect
    with patch.object(auth_mod, "MicrosoftSSOClient", return_value=sso):
        with patch.object(auth_mod, "LighthouseClient") as client_cls:
            client_cls.return_value.check_auth.return_value = check_auth
            yield sso, client_cls.return_value


def _invoke_login(
    runner: CliRunner,
    args: list[str],
    **kwargs: Any,
) -> Any:
    return runner.invoke(cli, ["auth", "login", *args], catch_exceptions=False, **kwargs)


def _interactive(value: bool) -> Any:
    return patch.object(auth_mod, "_is_interactive", return_value=value)


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def test_auth_login_registered_as_subcommand(cli_runner: CliRunner) -> None:
    """lighthouse auth login --help succeeds and shows all flags."""
    result = cli_runner.invoke(cli, ["auth", "login", "--help"])
    assert result.exit_code == 0
    output = result.output
    assert "--user" in output
    assert "--pass" not in output
    assert "--totp" in output
    assert "--save-credentials" in output
    assert "--json" in output


def test_auth_help_lists_login_and_mfa_methods(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli, ["auth", "--help"])
    assert result.exit_code == 0
    assert "login" in result.output
    assert "mfa-methods" in result.output


# ---------------------------------------------------------------------------
# resolve_credentials — pure precedence: flags > env > store > prompt
# ---------------------------------------------------------------------------

STORED = ("stored@manipal.edu", "stored_secret")


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        pytest.param(
            ("flag@manipal.edu", "flag_secret", "env@manipal.edu", "env_secret", STORED),
            ("flag@manipal.edu", "flag_secret"),
            id="flags-beat-env-and-store",
        ),
        pytest.param(
            ("flag@manipal.edu", None, "env@manipal.edu", "env_secret", None),
            ("flag@manipal.edu", "env_secret"),
            id="env-fills-missing-flag-per-field",
        ),
        # Empty env values count as absent (the caller strips before passing).
        pytest.param((None, None, "", "", STORED), STORED, id="store-when-flags-and-env-absent"),
        pytest.param(
            ("flag@manipal.edu", None, None, None, STORED),
            ("flag@manipal.edu", "stored_secret"),
            id="flag-user-with-stored-password",
        ),
        pytest.param((None, None, "", "", None), (None, None), id="unresolved-fields-are-none"),
    ],
)
def test_resolve_precedence(sources: tuple[Any, ...], expected: tuple[Any, Any]) -> None:
    assert resolve_credentials(*sources, prompt=None) == expected


@pytest.mark.parametrize(
    ("sources", "expected", "asked"),
    [
        pytest.param(
            ("flag@manipal.edu", None, None, None, None),
            ("flag@manipal.edu", "typed_password"),
            ["password"],
            id="only-missing-field",
        ),
        pytest.param(
            ("u@manipal.edu", "p", None, None, None), ("u@manipal.edu", "p"), [],
            id="never-when-resolved",
        ),
        # ``--user ''`` skips the environment yet still prompts (legacy behaviour).
        pytest.param(
            ("", "p", "env@manipal.edu", "env_p", None), ("typed_username", "p"), ["username"],
            id="empty-flag-skips-env",
        ),
    ],
)
def test_resolve_prompts_once_per_missing_field(
    sources: tuple[Any, ...], expected: tuple[Any, Any], asked: list[str],
) -> None:
    prompted: list[str] = []

    def prompt(field: str) -> str:
        prompted.append(field)
        return f"typed_{field}"

    assert resolve_credentials(*sources, prompt=prompt) == expected
    assert prompted == asked


# ---------------------------------------------------------------------------
# normalize_totp — literal codes vs the challenge BeginAuth sends
# ---------------------------------------------------------------------------

def test_normalize_stdin_defers_reading() -> None:
    """--totp - reads from stdin after BeginAuth, not at parse time."""
    assert normalize_totp("ignored", totp_stdin=True) == (None, True)


def test_normalize_whitespace_code_rejected() -> None:
    """A whitespace-only surviving literal code is a usage error."""
    with pytest.raises(ValueError, match="2FA code cannot be empty"):
        normalize_totp("   ", totp_stdin=False)


# ---------------------------------------------------------------------------
# plan_login — resume | fresh | defer
# ---------------------------------------------------------------------------

def _pending(method: str, proof: str) -> dict[str, Any]:
    return {"mfa_method": method, "selected_proof": {"auth_method_id": proof}}


@pytest.mark.parametrize(
    ("totp_code", "read_after", "mfa_method", "pending", "interactive", "mode"),
    [
        pytest.param(
            "123456", False, "app", _pending("app", "PhoneAppOTP"), True, "resume",
            id="resume-with-matching-pending-method",
        ),
        pytest.param(
            "123456", False, "auto", _pending("auto", "OneWaySMS"), True, "fresh",
            id="auto-never-guesses-pending-method",
        ),
        # An explicit method differing from the pending session never resumes.
        pytest.param(
            "123456", False, "app", _pending("sms", "OneWaySMS"), True, "fresh",
            id="method-mismatch",
        ),
        pytest.param(
            None, False, "app", _pending("app", "PhoneAppOTP"), True, "fresh",
            id="never-resumes-without-code",
        ),
        pytest.param(
            "123456", True, "app", _pending("app", "PhoneAppOTP"), True, "fresh",
            id="never-resumes-stdin-code",
        ),
        # Non-TTY with no code and no stdin read defers to auth verify.
        pytest.param(None, False, "sms", None, False, "defer", id="defer-non-interactive"),
        pytest.param(None, False, "auto", None, True, "fresh", id="fresh-interactive"),
        pytest.param(None, True, "auto", None, False, "fresh", id="fresh-piped"),
        pytest.param("123456", False, "auto", None, False, "fresh", id="fresh-with-code"),
    ],
)
def test_plan_login(
    totp_code: str | None,
    read_after: bool,
    mfa_method: str,
    pending: dict[str, Any] | None,
    interactive: bool,
    mode: str,
) -> None:
    plan = plan_login(
        totp_code=totp_code, read_totp_after_challenge=read_after, mfa_method=mfa_method,
        pending=pending, interactive=interactive,
    )
    assert plan.mode == mode
    assert plan.totp_code == totp_code
    assert plan.defer_mfa_to_pending is (mode == "defer")


# ---------------------------------------------------------------------------
# _persist_check_report — shared tail ordering
# ---------------------------------------------------------------------------

def _patch_tail(
    monkeypatch: pytest.MonkeyPatch, check_ok: bool = True,
) -> tuple[list[str], MagicMock, MagicMock]:
    """Stub the tail's collaborators; return (call order, client factory, store)."""
    order: list[str] = []
    monkeypatch.setattr(auth_mod, "save_cookies", lambda cookies: order.append("cookies"))
    client = MagicMock()
    client.check_auth.side_effect = lambda: order.append("check") or check_ok
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(auth_mod, "LighthouseClient", client_factory)
    store = MagicMock()
    store.save.side_effect = lambda u, p: order.append("creds")
    monkeypatch.setattr(auth_mod, "CredentialStore", lambda: store)
    return order, client_factory, store


@pytest.mark.parametrize(
    ("pair", "expected_order"),
    [
        pytest.param(("u", "p"), ["cookies", "check", "creds"], id="login-pair"),
        # The verify shape (no pair) structurally cannot store secrets.
        pytest.param(None, ["cookies", "check"], id="verify-without-pair"),
    ],
)
def test_tail_orders_cookies_before_check_before_credential_save(
    monkeypatch: pytest.MonkeyPatch, pair: tuple[str, str] | None, expected_order: list[str],
) -> None:
    """Security ordering: seal cookies → validate session → save credentials."""
    order, client_factory, _store = _patch_tail(monkeypatch)

    rc = auth_mod._persist_check_report(
        _make_d2l_cookies(), json_output=True,
        failure_hint="Try: lighthouse auth login", save_credentials_pair=pair,
    )

    assert rc == 0
    assert order == expected_order
    client_factory.assert_called_once_with(read_only_auth=True)


def test_tail_failed_session_check_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """check_auth failure reports an error and never stores credentials."""
    _order, _factory, store = _patch_tail(monkeypatch, check_ok=False)

    rc = auth_mod._persist_check_report(
        _make_d2l_cookies(), json_output=True,
        failure_hint="Try: lighthouse auth login", save_credentials_pair=("u", "p"),
    )

    assert rc == 1
    store.save.assert_not_called()
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is False
    assert "verification failed" in data["error"]
    assert "Try: lighthouse auth login" in data["error"]


def test_tail_reports_only_allowlisted_cookie_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_tail(monkeypatch)
    cookies = _make_d2l_cookies()
    cookies["d2lPassword=COOKIE_NAME_SECRET"] = "value"

    rc = auth_mod._persist_check_report(cookies, json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["cookies"] == list(auth_mod.COOKIE_NAMES)
    assert "COOKIE_NAME_SECRET" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Credentials via flags / env / store (CliRunner smokes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("json_args", [[], ["--json"]])
def test_removed_password_flag_never_echoes_its_value(
    cli_runner: CliRunner,
    isolated_config: Path,
    json_args: list[str],
) -> None:
    """The removed argv password interface fails without reflecting the secret."""
    sentinel = "ARGV_PASSWORD_SENTINEL"

    result = _invoke_login(
        cli_runner,
        ["--user", "user@manipal.edu", "--pass", sentinel, *json_args],
    )

    assert result.exit_code == (1 if json_args else 2)
    assert sentinel not in result.stdout + result.stderr
    assert "Invalid command arguments" in result.output


def test_mfa_methods_has_no_password_flag_and_never_echoes_removed_value(
    cli_runner: CliRunner,
    isolated_config: Path,
) -> None:
    help_result = cli_runner.invoke(cli, ["auth", "mfa-methods", "--help"])
    sentinel = "ARGV_PASSWORD_SENTINEL"
    rejected = cli_runner.invoke(
        cli,
        ["auth", "mfa-methods", "--user", "user@manipal.edu", "--pass", sentinel],
    )

    assert help_result.exit_code == 0
    assert "--pass" not in help_result.output
    assert rejected.exit_code == 2
    assert sentinel not in rejected.stdout + rejected.stderr
    assert "Invalid command arguments" in rejected.output


@pytest.mark.parametrize(
    ("env_user", "env_pass", "stored", "flags", "expected"),
    [
        pytest.param(
            "user@manipal.edu", "secret", None, [], ("user@manipal.edu", "secret"),
            id="env-vars",
        ),
        # The username flag combines with the environment-only password channel.
        pytest.param(
            "env_user@manipal.edu", "env_secret", None, ["--user", "flag_user@manipal.edu"],
            ("flag_user@manipal.edu", "env_secret"),
            id="flag-beats-env",
        ),
        pytest.param(
            None, "env_secret", None, ["--user", "flag_user@manipal.edu"],
            ("flag_user@manipal.edu", "env_secret"),
            id="flag-user-env-password",
        ),
        # Sealed stored credentials are the third source.
        pytest.param(None, None, STORED, [], STORED, id="store-fallback"),
    ],
)
def test_login_credential_sources(
    cli_runner: CliRunner,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_user: str | None,
    env_pass: str | None,
    stored: tuple[str, str] | None,
    flags: list[str],
    expected: tuple[str, str],
) -> None:
    for name, value in (("LIGHTHOUSE_USERNAME", env_user), ("LIGHTHOUSE_PASSWORD", env_pass)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    if stored is not None:
        auth_mod.CredentialStore().save(*stored)

    with _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, [*flags, "--totp", "123456"])

    assert result.exit_code == 0
    assert "Username:" not in result.output
    sso.login.assert_called_once()
    assert sso.login.call_args.args[:2] == expected


# ---------------------------------------------------------------------------
# TOTP via flag/stdin
# ---------------------------------------------------------------------------

def test_totp_flag_submits_code_and_verifies_session(cli_runner: CliRunner, creds_env: Path) -> None:
    """--totp submits the 2FA code without prompting; check_auth() then confirms
    the session is valid."""
    with _mock_sso() as (sso, client):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    sso.login.assert_called_once()
    assert sso.login.call_args.args[2] == "123456"
    client.check_auth.assert_called_once()


def test_explicit_app_method_ignores_stale_sms_pending(cli_runner: CliRunner, creds_env: Path) -> None:
    """Explicit --mfa-method app with a literal code starts a fresh flow rather than
    resuming a leftover SMS pending session (offline app TOTP belongs to no SMS session)."""
    with patch.object(auth_mod, "load_mfa_pending", return_value={"mfa_method": "sms"}):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner, ["--mfa-method", "app", "--totp", "123456"],
            )

    assert result.exit_code == 0
    sso.login.assert_called_once()
    assert sso.login.call_args.args[2] == "123456"
    sso.complete_mfa_pending.assert_not_called()


def test_successful_inline_login_clears_stale_pending_for_next_default_login(
    cli_runner: CliRunner, creds_env: Path,
) -> None:
    """A completed fresh flow cannot be resumed by the next default login."""
    save_mfa_pending({"mfa_method": "sms", "created_at": "2026-08-27T00:00:00Z"})

    with _mock_sso() as (sso, _client):
        first = _invoke_login(
            cli_runner,
            ["--mfa-method", "app", "--totp", "123456"],
        )
        second = _invoke_login(cli_runner, ["--totp", "654321"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert sso.login.call_count == 2
    sso.complete_mfa_pending.assert_not_called()
    assert not (creds_env / "mfa_pending.json").exists()


def test_deferred_mfa_does_not_clear_pending_checkpoint(cli_runner: CliRunner, creds_env: Path) -> None:
    """The deferred MFA result remains eligible for ``auth verify``."""
    pending_error = auth_mod.MfaPendingError(
        "Verification code sent.",
        step="MFA",
        recovery="Run: lighthouse auth verify <code>",
    )

    with patch.object(auth_mod, "clear_mfa_pending") as clear_pending:
        with _mock_sso(login_side_effect=pending_error):
            result = _invoke_login(cli_runner, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mfa_pending"] is True
    assert payload["message"] == "Verification code sent."
    assert payload["recovery"] == "lighthouse auth verify <code>"
    clear_pending.assert_not_called()


def test_totp_stdin_pipe(cli_runner: CliRunner, creds_env: Path) -> None:
    """--totp - reads the 2FA code from stdin pipe."""
    with _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, ["--totp", "-"], input="123456\n")

    assert result.exit_code == 0
    sso.login.assert_called_once()
    # SMS reads stdin after BeginAuth, not at CLI parse time.
    assert sso.login.call_args.args[2] is None
    assert sso.login.call_args.kwargs.get("read_totp_after_challenge") is True


# ---------------------------------------------------------------------------
# Cookie persistence and session verification
# ---------------------------------------------------------------------------

def test_cookies_saved_sealed_with_owner_only_permissions(cli_runner: CliRunner, creds_env: Path) -> None:
    """cookies.json written sealed (v2 envelope) with 0600 permissions."""
    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    cookies_path = creds_env / "cookies.json"
    assert cookies_path.exists()
    raw = cookies_path.read_text()
    data = json.loads(raw)
    # Sealed v2 envelope: only metadata in the clear, payload encrypted.
    assert data["v"] == 2
    assert data["key_source"] == "passphrase"
    assert "kdf_salt" in data
    assert "ciphertext" in data
    assert "extracted_at" in data
    assert "cookies" not in data
    assert "sec123" not in raw
    # Round-trips through the public loader.
    assert load_cookies() == _make_d2l_cookies()
    mode = cookies_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_auth_status_works_after_login(cli_runner: CliRunner, isolated_config: Path) -> None:
    """Cookies from auth login work with auth status."""
    cookies = _make_d2l_cookies()
    (isolated_config / "cookies.json").write_text(json.dumps(cookies))

    with patch("lighthouse_cli.commands.LighthouseClient") as mock_commands:
        with patch.object(auth_mod, "LighthouseClient") as mock_auth:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_commands.return_value = mock_client
            mock_auth.return_value = mock_client
            result = cli_runner.invoke(cli, ["auth", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Session valid" in result.output or "valid" in result.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("error", "exit_code", "needle"),
    [
        pytest.param(
            MicrosoftSSOError(
                "[50126] Invalid username or password.",
                step="POST credentials",
                recovery="Double-check your email and password.",
            ),
            1, "50126", id="wrong-credentials",
        ),
        pytest.param(
            MicrosoftSSOError("Invalid credentials"), 1, "Authentication failed",
            id="bare-sso-error",
        ),
        pytest.param(
            MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            ),
            1, "2FA", id="wrong-totp",
        ),
        pytest.param(
            MicrosoftSSOError(
                "Failed to redirect to Microsoft SSO.",
                step="initiate SAML",
                recovery="Check that lighthouse.manipal.edu is reachable.",
            ),
            1, "Microsoft", id="network-failure",
        ),
        pytest.param(
            MicrosoftSSOError(
                "2FA code is required but was empty.",
                step="MFA",
                recovery="Provide a 2FA code via --totp flag or pipe.",
            ),
            1, "2FA", id="empty-totp",
        ),
        pytest.param(
            MicrosoftSSOError(
                "Could not find Microsoft login configuration on the page.",
                step="get MS config",
                recovery="Microsoft may have changed their login page.",
            ),
            1, "Microsoft", id="sso-page-structure-change",
        ),
        pytest.param(KeyboardInterrupt(), 130, "Interrupted.", id="keyboard-interrupt"),
    ],
)
def test_login_failure_exits_with_clear_message(
    cli_runner: CliRunner, creds_env: Path, error: BaseException, exit_code: int, needle: str,
) -> None:
    """SSO failures exit with a clear message and never a traceback."""
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == exit_code
    assert needle in result.output
    assert "Traceback" not in result.output


def test_unexpected_error_never_leaks_exception_text(cli_runner: CliRunner, creds_env: Path) -> None:
    """A third-party exception renders as `Unexpected error (<Type>)` + guidance —
    never raw str(exc), which may embed URLs or tokens."""
    leaky = RuntimeError("https://login.microsoftonline.com/token?code=SECRET")
    with _mock_sso(login_side_effect=leaky):
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 1
    assert "Unexpected error (RuntimeError)" in result.output
    assert "SECRET" not in result.output
    assert "microsoftonline" not in result.output


def test_unexpected_failure_wrapped_cleanly(cli_runner: CliRunner, creds_env: Path) -> None:
    """An unexpected exception exits cleanly under --json — never a traceback."""
    with _mock_sso() as (_sso, client):
        client.check_auth.side_effect = RuntimeError("kaboom")
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["success"] is False
    # F18: only the exception TYPE is surfaced — raw str(exc) may embed
    # URLs/tokens, so the message text must not appear.
    assert "Unexpected error (RuntimeError)" in data["error"]
    assert "kaboom" not in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# JSON output contract
# ---------------------------------------------------------------------------

def test_json_output_success_never_logs_password(
    cli_runner: CliRunner, creds_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json produces valid JSON with success:true; the password never appears
    in stdout/stderr."""
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "super_secret_password")

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data.get("success") is True
    assert "cookies" in data
    assert "super_secret_password" not in result.output
    assert "super_secret_password" not in result.stderr


def test_json_output_failure(cli_runner: CliRunner, creds_env: Path) -> None:
    """--json produces valid JSON with success:false on failure."""
    error = MicrosoftSSOError("Invalid username or password.", step="POST credentials")
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data.get("success") is False
    assert "error" in data


def test_auth_json_error_has_one_stdout_document_and_stderr_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = auth_mod._auth_error(
        "Invalid username or password.", json_output=True
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "success": False,
        "error": "Invalid username or password.",
    }
    assert captured.err == "Error: Invalid username or password.\n"


@pytest.mark.parametrize(
    "raw",
    [
        'headers={"Cookie":"COOKIE_SENTINEL"}',
        "{'password':'PASSWORD_SENTINEL'}",
        'error={"apiKey":"REAL_KEY"}',
        "Run: lighthouse auth login --pass SECRET",
        "foo token SECRET",
    ],
)
def test_auth_json_error_uses_opaque_fallback_for_secret_shaped_text(
    raw: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = auth_mod._auth_error(raw, json_output=True)

    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert payload["error"] == "Authentication failed. Check your credentials and try again."
    assert "SENTINEL" not in captured.out + captured.err
    assert "SECRET" not in captured.out + captured.err
    assert "REAL_KEY" not in captured.out + captured.err
    assert captured.err.startswith("Error: Authentication failed.")


def test_interrupted_json_error_has_one_stdout_document_and_stderr_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = auth_mod._interrupted(json_output=True)

    captured = capsys.readouterr()
    assert rc == 130
    assert json.loads(captured.out) == {
        "success": False,
        "error": "Interrupted by user",
    }
    assert captured.err == "Error: Interrupted by user\n"


def test_mfa_pending_outputs_opaque_message_and_allowlisted_recovery(
    cli_runner: CliRunner, creds_env: Path,
) -> None:
    pending = auth_mod.MfaPendingError(
        LEAKY_DISPLAY,
        step="MFA",
        recovery="Run: lighthouse auth verify --totp SECRET",
    )
    with _mock_sso(login_side_effect=pending):
        json_result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])
    with _mock_sso(login_side_effect=pending):
        human_result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload == {
        "success": False,
        "mfa_pending": True,
        "message": "Authentication failed. Check your credentials and try again.",
        "recovery": None,
    }
    assert human_result.exit_code == 0
    assert "Authentication failed. Check your credentials" in human_result.output
    for leaked in (*DISPLAY_LEAKS, "SECRET"):
        assert leaked not in json_result.stdout + json_result.stderr
        assert leaked not in human_result.output


def test_keyring_failure_is_clean_under_json(
    cli_runner: CliRunner, creds_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key source: stdout stays one JSON object; stderr is a safe diagnostic."""
    monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        "lighthouse_cli.credential_store._load_keyring_module", lambda: None,
    )

    result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["success"] is False
    assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in data["error"]
    assert "Error: No encryption key source" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


# ---------------------------------------------------------------------------
# Credential-save guarantees
# ---------------------------------------------------------------------------

def test_failed_validation_never_saves_credentials(cli_runner: CliRunner, creds_env: Path) -> None:
    """--save-credentials with a failed session check stores nothing."""
    with _mock_sso(check_auth=False):
        result = _invoke_login(
            cli_runner, ["--totp", "123456", "--save-credentials", "--json"],
        )

    assert result.exit_code == 1
    # Cookies were sealed first (fixed ordering), but credentials were not saved.
    assert (creds_env / "cookies.json").exists()
    assert not (creds_env / "credentials.json").exists()


def test_verify_never_saves_credentials(
    cli_runner: CliRunner, isolated_config: Path,
) -> None:
    """auth verify completes the session but NEVER stores username/password."""
    store_cls = MagicMock()
    store_cls.return_value.preflight.return_value = "passphrase"

    with patch.object(auth_mod, "load_mfa_pending", return_value={"mfa_method": "sms"}):
        with patch.object(auth_mod, "MicrosoftSSOClient") as sso_cls:
            sso_cls.return_value.complete_mfa_pending.return_value = _make_d2l_cookies()
            with patch.object(auth_mod, "CredentialStore", store_cls):
                with patch.object(auth_mod, "LighthouseClient") as client_cls:
                    client_cls.return_value.check_auth.return_value = True
                    result = cli_runner.invoke(
                        cli, ["auth", "verify", "123456", "--json"],
                        catch_exceptions=False,
                    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["success"] is True
    # The tail ran (cookies sealed by the real store), but no credential save
    # was ever attempted through any CredentialStore reference.
    assert (isolated_config / "cookies.json").exists()
    assert not (isolated_config / "credentials.json").exists()
    store_cls.return_value.save.assert_not_called()


def test_verify_without_pending_reports_usage_before_key_preflight(
    cli_runner: CliRunner, isolated_config: Path,
) -> None:
    """A missing checkpoint must not create or probe an encryption key."""
    store = MagicMock()
    store.mfa_pending_file = isolated_config / "mfa_pending.json"

    with patch.object(auth_mod, "CredentialStore", return_value=store), \
         patch.object(auth_mod, "MicrosoftSSOClient") as sso_cls:
        result = cli_runner.invoke(cli, ["auth", "verify", "123456", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"].startswith("No pending MFA session")
    store.preflight.assert_not_called()
    sso_cls.assert_not_called()


def test_verify_with_encrypted_pending_without_key_reports_key_source(
    cli_runner: CliRunner,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing sealed checkpoint still requires its encryption key."""
    save_mfa_pending({"mfa_method": "sms", "created_at": "2026-08-27T00:00:00Z"})
    monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        "lighthouse_cli.credential_store._load_keyring_module", lambda: None,
    )

    with patch.object(auth_mod, "MicrosoftSSOClient") as sso_cls:
        result = cli_runner.invoke(
            cli, ["auth", "verify", "123456", "--json"], catch_exceptions=False,
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "No encryption key source" in payload["error"]
    sso_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Empty credential rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["username", "password"])
def test_empty_credential_rejected(
    cli_runner: CliRunner, creds_env: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    """An empty username or password exits with an error before any network call."""
    monkeypatch.setenv(f"LIGHTHOUSE_{field.upper()}", "")

    result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert field in result.output.lower()


def test_totp_without_value_error(cli_runner: CliRunner, creds_env: Path) -> None:
    """--totp without value produces Click usage error (exit 2)."""
    result = _invoke_login(cli_runner, ["--totp"])

    assert result.exit_code == 2
    assert "invalid command arguments" in result.output.lower()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_username_prompt_stream_follows_output_mode(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under --json the username banner lands on stderr, keeping stdout pure JSON;
    without --json it stays on stdout as before."""
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)

    with _interactive(True), _mock_sso():
        json_result = _invoke_login(
            cli_runner, ["--totp", "123456", "--json"], input="prompted@manipal.edu\n",
        )
        human_result = _invoke_login(
            cli_runner, ["--totp", "123456"], input="prompted@manipal.edu\n",
        )

    assert json_result.exit_code == 0
    assert "Username (email):" in json_result.stderr
    assert "Username (email):" not in json_result.stdout
    data = json.loads(json_result.stdout)
    assert data["success"] is True
    assert "prompted@manipal.edu" not in json_result.stdout
    assert human_result.exit_code == 0
    assert "Username (email):" in human_result.output


@pytest.mark.parametrize(
    ("interactive", "env_method", "args", "method", "defer"),
    [
        # A plain TTY login asks the user to choose from Microsoft's proof list.
        pytest.param(True, None, [], "choose", False, id="tty-default-picker"),
        # An explicit automation-style selector is never replaced by the picker.
        pytest.param(True, None, ["--mfa-method", "auto"], "auto", False, id="tty-explicit-auto"),
        # A pre-supplied app code keeps legacy auto selection, not ambiguous choose.
        pytest.param(True, None, ["--totp", "123456"], "auto", False, id="tty-literal-totp"),
        pytest.param(False, " app ", ["--totp", "123456"], "app", False, id="env-method-whitespace"),
        # Scripts keep tenant-default selection and the resumable verify flow.
        pytest.param(False, None, [], "auto", True, id="script-default-deferred"),
    ],
)
def test_login_mfa_method_selection(
    cli_runner: CliRunner,
    creds_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
    env_method: str | None,
    args: list[str],
    method: str,
    defer: bool,
) -> None:
    if env_method is None:
        monkeypatch.delenv("LIGHTHOUSE_MFA_METHOD", raising=False)
    else:
        monkeypatch.setenv("LIGHTHOUSE_MFA_METHOD", env_method)

    with _interactive(interactive), _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, args, input="n\n")

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == method
    assert sso.login.call_args.kwargs["defer_mfa_to_pending"] is defer
    # Only the picker announces itself, and only a TTY login offers the guide.
    picker_banner = "You will be asked to pick a verification method."
    assert (picker_banner in result.output) is (method == "choose")
    assert ("Show the full command guide?" in result.output) is interactive


@pytest.mark.parametrize(
    ("answer", "guide_shown"),
    [pytest.param("\n", True, id="accept-guide"), pytest.param("n\n", False, id="skip-guide")],
)
def test_interactive_login_shows_next_steps_and_optional_guide(
    cli_runner: CliRunner, creds_env: Path, answer: str, guide_shown: bool,
) -> None:
    """A completed TTY login leads into useful commands without running them;
    declining the optional guide still leaves the compact next steps visible."""
    with _interactive(True), _mock_sso():
        result = _invoke_login(cli_runner, [], input=answer)

    assert result.exit_code == 0
    assert "Login complete. Session saved and verified." in result.output
    assert "Try next:" in result.output
    assert "lighthouse courses" in result.output
    assert "lighthouse download <course> --dry-run" in result.output
    assert "Show the full command guide? [Y/n]:" in result.stderr
    for guide_line in ("Command guide:", "Read only:", "Remote change:"):
        assert (guide_line in result.output) is guide_shown
    assert "Cookies:" not in result.output


def test_login_guide_prompt_eof_is_clean(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed stdin after successful login never turns success into a traceback."""
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    auth_mod._print_login_next_steps()

    captured = capsys.readouterr()
    assert "Try next:" in captured.out
    assert "Show the full command guide? [Y/n]:" in captured.err
    assert "Command guide:" not in captured.out


def test_non_tty_no_credentials_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY stdin with no credentials produces error, exit code 1."""
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
    monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)

    with patch.object(auth_mod, "CredentialStore") as mock_store_cls:
        mock_store_cls.return_value.load.return_value = None
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "credentials" in result.output.lower() or "required" in result.output.lower()


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_totp_not_persisted(cli_runner: CliRunner, creds_env: Path) -> None:
    """A real (mocked-SSO) login never leaks the TOTP into the sealed artifact."""
    # quote_plus matches application/x-www-form-urlencoded encoding, so
    # this checks a genuinely different byte sequence from the raw sentinel.
    totp = "654+21/SEN="

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", totp])

    assert result.exit_code == 0
    cookies_path = creds_env / "cookies.json"
    assert cookies_path.exists()
    raw = cookies_path.read_bytes()
    # The artifact on disk is a sealed envelope; the code must appear in it
    # neither as plaintext nor URL-encoded.
    assert totp.encode() not in raw
    assert urllib.parse.quote_plus(totp).encode() not in raw


# ---------------------------------------------------------------------------
# Concurrency and config directory
# ---------------------------------------------------------------------------

def test_concurrent_auth_no_corruption(isolated_config: Path) -> None:
    """cookies.json is valid JSON after concurrent auth attempts."""
    cookies2 = {
        "d2lSecureSessionVal": "sec2",
        "d2lSessionVal": "ses2",
        "d2lSameSiteCanaryA": "canaryA2",
        "d2lSameSiteCanaryB": "canaryB2",
    }
    errors: list[Exception] = []

    def write(value: dict[str, str]) -> None:
        try:
            save_cookies(value)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=write, args=(c,)) for c in (_make_d2l_cookies(), cookies2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    cookies_path = isolated_config / "cookies.json"
    assert cookies_path.exists()
    data = json.loads(cookies_path.read_text())
    assert data["v"] == 2
    assert "ciphertext" in data
    # Atomic replace means the file always holds one complete sealed write.
    loaded = load_cookies()
    assert len(loaded) >= 4
    assert "d2lSecureSessionVal" in loaded


def test_config_directory_auto_created(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config directory is created if missing."""
    config_dir = tmp_path / ".config" / "lighthouse-cli"
    assert not config_dir.exists()
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    assert config_dir.exists()
    mode = config_dir.stat().st_mode & 0o777
    assert mode in (0o700, 0o755)


# ---------------------------------------------------------------------------
# Review-round regressions: unreadable pending checkpoint + first-party errors
# ---------------------------------------------------------------------------

class TestUnreadablePendingCheckpoint:
    """A pending checkpoint sealed under a different key source must not
    abort a fresh --totp login that would never resume it (PR review)."""

    def test_fresh_totp_login_proceeds_past_unopenable_pending(
        self,
        cli_runner: CliRunner,
        creds_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Day 1: checkpoint sealed under one passphrase...
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", "day-one-passphrase")
        save_mfa_pending({"mfa_method": "sms", "created_at": "2026-08-01T00:00:00Z"})
        # Day 2: passphrase removed/replaced — the sealed file can't open.
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", "day-two-passphrase")

        with _mock_sso() as (sso, _client):
            result = _invoke_login(cli_runner, ["--totp", "123456", "--mfa-method", "app"])

        assert result.exit_code == 0, result.output
        # The SSO client was invoked (fresh flow), not aborted by the load.
        assert sso.login.called
        assert "unreadable MFA pending session" in result.output
        assert "Unexpected error" not in result.output

    def test_credential_store_error_text_is_opaque(
        self,
        cli_runner: CliRunner,
        creds_env: Path,
    ) -> None:
        """CredentialStoreError text is not copied into auth output."""

        def _boom(_cookies: dict[str, str]) -> None:
            raise CredentialStoreError("sealed-hint-sentinel")

        with patch.object(auth_mod, "save_cookies", side_effect=_boom):
            with _mock_sso() as (sso, _client):
                result = _invoke_login(cli_runner, ["--totp", "123456"])

        assert result.exit_code == 1, result.output
        assert sso.login.called  # the flow itself completed
        assert "sealed-hint-sentinel" not in result.output
        assert "Authentication failed" in result.output


# ---------------------------------------------------------------------------
# auth mfa-methods: discover registered 2FA methods without sending a code
# ---------------------------------------------------------------------------

def _probe_result(page: str = "converged", proofs: list[Any] | None = None) -> MfaProbeResult:
    return MfaProbeResult(
        page=page,
        proofs=proofs
        if proofs is not None
        else [
            UserProof("OneWaySMS", "Text +91 ***1234", "+919876541234", False),
            UserProof("TwoWayVoiceMobile", "Call +91 ***1234", "+919876541234", True),
            UserProof("PhoneAppOTP", "Authenticator app", "", False),
        ],
    )


def _stub_probe(**mock_kwargs: Any) -> Any:
    """Patch MicrosoftSSOClient.probe_mfa_methods with a MagicMock(**mock_kwargs)."""
    return patch.object(
        auth_mod.MicrosoftSSOClient, "probe_mfa_methods", MagicMock(**mock_kwargs),
    )


class TestAuthMfaMethodsCommand:
    def _invoke(self, runner: CliRunner, args: list[str]) -> Any:
        return runner.invoke(cli, ["auth", "mfa-methods", *args], catch_exceptions=False)

    def test_json_output_lists_methods_and_keeps_stdout_pure(
        self, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        with _stub_probe(return_value=_probe_result()):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["page"] == "converged"
        ids = [m["id"] for m in payload["methods"]]
        assert ids == ["OneWaySMS", "TwoWayVoiceMobile", "PhoneAppOTP"]
        assert [m["method"] for m in payload["methods"]] == ["sms", "call", "app"]
        assert payload["methods"][1]["is_default"] is True
        # The raw phone number (proof.data) must never reach the output.
        assert "+919876541234" not in result.stdout

    def test_human_output_lists_methods(self, cli_runner: CliRunner, creds_env: Path) -> None:
        with _stub_probe(return_value=_probe_result()):
            result = self._invoke(cli_runner, [])

        assert result.exit_code == 0
        assert "TwoWayVoiceMobile" in result.output
        assert "--mfa-method call" in result.output
        assert "Microsoft default" in result.output
        assert "+919876541234" not in result.output

    def test_malicious_display_is_masked_in_json_and_human_output(
        self, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        proofs = [
            UserProof("OneWaySMS", LEAKY_DISPLAY, "+919876541234", True),
            UserProof("TwoWayVoiceMobile", LEAKY_DISPLAY, "+919876541234", True),
        ]
        with _stub_probe(return_value=_probe_result(proofs=proofs)):
            json_result = self._invoke(cli_runner, ["--json"])
            human_result = self._invoke(cli_runner, [])

        assert json_result.exit_code == 0
        payload = json.loads(json_result.stdout)
        assert payload["methods"][0]["method"] == "sms"
        assert payload["methods"][0]["display"] == (
            "Text code (SMS or WhatsApp): ***1234"
        )
        assert human_result.exit_code == 0
        assert "Voice call to mobile: ***1234" in human_result.output
        for leaked in DISPLAY_LEAKS:
            assert leaked not in json_result.stdout
            assert leaked not in human_result.output

    def test_unrecognized_method_id_is_rendered_as_other(
        self, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        proof = UserProof(
            "FutureMethod\x1b[31mPASSWORD_SENTINEL",
            "FULL-DISPLAY-SENTINEL",
            "",
            True,
        )
        with _stub_probe(return_value=_probe_result(proofs=[proof])):
            json_result = self._invoke(cli_runner, ["--json"])
            human_result = self._invoke(cli_runner, [])

        payload = json.loads(json_result.stdout)
        assert payload["methods"][0]["id"] == "other"
        assert payload["methods"][0]["method"] is None
        combined = json_result.stdout + json_result.stderr + human_result.output
        assert "FutureMethod" not in combined
        assert "PASSWORD_SENTINEL" not in combined
        assert "FULL-DISPLAY-SENTINEL" not in combined
        assert "Other verification method" in human_result.output

    def test_unknown_method_has_no_fake_cli_selector(
        self, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        proof = UserProof("FutureProof", "Future method", "", False)
        with _stub_probe(return_value=_probe_result(proofs=[proof])):
            json_result = self._invoke(cli_runner, ["--json"])
            human_result = self._invoke(cli_runner, [])

        assert json.loads(json_result.stdout)["methods"][0]["method"] is None
        assert "no supported --mfa-method selector" in human_result.output
        assert "--mfa-method unknown" not in human_result.output

    def test_no_mfa_account_reports_cleanly(self, cli_runner: CliRunner, creds_env: Path) -> None:
        with _stub_probe(return_value=_probe_result(page="no_mfa", proofs=[])):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "success": True, "page": "no_mfa", "methods": [],
        }

    def test_sso_error_becomes_clean_json_error(self, cli_runner: CliRunner, creds_env: Path) -> None:
        with _stub_probe(side_effect=MicrosoftSSOError("[50126] wrong password")):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["success"] is False

    def test_unexpected_probe_error_is_clean_json_without_details(
        self, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        with _stub_probe(side_effect=RuntimeError("PROBE_SECRET https://x.test/?token=abc")):
            result = cli_runner.invoke(cli, ["auth", "mfa-methods", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout) == {
            "success": False, "error": "Unexpected error (RuntimeError).",
        }
        assert "Traceback" not in result.output
        assert "PROBE_SECRET" not in result.output
        assert "token=abc" not in result.output

    def test_missing_credentials_error(
        self, cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
        monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)
        with patch.object(auth_mod, "CredentialStore") as store:
            store.return_value.load.return_value = None
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 1
        assert "Credentials required" in json.loads(result.stdout)["error"]


class TestMfaMethodVocabulary:
    @pytest.mark.parametrize(
        ("method", "message"),
        [
            ("sms", "fresh code"),
            ("call", "codeless"),
            ("push", "codeless"),
            ("choose", "ambiguous"),
        ],
    )
    def test_incompatible_literal_totp_is_rejected(
        self, method: str, message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            validate_totp_usage("123456", totp_stdin=False, mfa_method=method)

    @pytest.mark.parametrize("method", ["call", "push"])
    def test_codeless_method_rejects_stdin_totp(self, method: str) -> None:
        with pytest.raises(ValueError, match="codeless"):
            validate_totp_usage(None, totp_stdin=True, mfa_method=method)

    def test_app_and_auto_accept_literal_totp(self) -> None:
        """Normalization is transport-only; incompatible methods fail in validation."""
        for method in ("app", "auto"):
            validate_totp_usage("123456", totp_stdin=False, mfa_method=method)
        assert normalize_totp("123456", totp_stdin=False) == ("123456", False)

    @pytest.mark.parametrize("method", ["sms", "call", "push"])
    def test_login_rejects_incompatible_totp_before_sso(
        self, method: str, cli_runner: CliRunner, creds_env: Path,
    ) -> None:
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner,
                ["--mfa-method", method, "--totp", "123456", "--json"],
            )
        assert result.exit_code == 1
        assert "--totp" in json.loads(result.stdout)["error"]
        sso.login.assert_not_called()

    @pytest.mark.parametrize("method", ["call", "push"])
    def test_login_accepts_call_and_push_choices(self, cli_runner: CliRunner, method: str) -> None:
        """--mfa-method call/push parse at the CLI layer."""
        result = cli_runner.invoke(
            cli, ["auth", "login", "--mfa-method", method, "--help"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
