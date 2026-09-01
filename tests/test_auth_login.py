"""Tests for lighthouse auth login: policy helpers + CLI smokes.

The pure decision layer (``resolve_credentials``, ``normalize_totp``,
``plan_login``, ``_persist_check_report``) is tested with plain args and no
I/O; end-to-end behaviour runs through CliRunner smokes.
"""

from __future__ import annotations

import json
import threading
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
from lighthouse_cli.ms_auth import MicrosoftSSOError

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "lighthouse-cli"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def isolated_config(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LIGHTHOUSE_CONFIG_DIR at a per-test directory."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    return config_dir


@contextmanager
def _mock_sso(
    check_auth: bool = True,
    login_side_effect: Exception | None = None,
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


def test_auth_login_appears_in_auth_help(cli_runner: CliRunner) -> None:
    """auth --help lists the login subcommand."""
    result = cli_runner.invoke(cli, ["auth", "--help"])
    assert result.exit_code == 0
    assert "login" in result.output


# ---------------------------------------------------------------------------
# resolve_credentials — pure precedence: flags > env > store > prompt
# ---------------------------------------------------------------------------

def test_resolve_flags_beat_env_and_store() -> None:
    """Non-empty flags win over every other source."""
    username, password = resolve_credentials(
        "flag@manipal.edu",
        "flag_secret",
        "env@manipal.edu",
        "env_secret",
        ("stored@manipal.edu", "stored_secret"),
        prompt=None,
    )
    assert (username, password) == ("flag@manipal.edu", "flag_secret")


def test_resolve_env_fills_missing_flag_per_field() -> None:
    """Mixed sources combine per field: flag username + env password."""
    username, password = resolve_credentials(
        "flag@manipal.edu", None, "env@manipal.edu", "env_secret", None, prompt=None,
    )
    assert (username, password) == ("flag@manipal.edu", "env_secret")


def test_resolve_store_fills_when_flags_and_env_absent() -> None:
    """Stored credentials are used only when flags and env are missing."""
    username, password = resolve_credentials(
        None, None, "", "", ("stored@manipal.edu", "stored_secret"), prompt=None,
    )
    assert (username, password) == ("stored@manipal.edu", "stored_secret")


def test_resolve_mixed_flag_and_stored_per_field() -> None:
    """Flag username pairs with stored password when env is absent."""
    username, password = resolve_credentials(
        "flag@manipal.edu", None, None, None,
        ("stored@manipal.edu", "stored_secret"), prompt=None,
    )
    assert (username, password) == ("flag@manipal.edu", "stored_secret")


def test_resolve_empty_env_falls_through_to_store() -> None:
    """Empty env values count as absent (the caller strips before passing)."""
    username, password = resolve_credentials(
        None, None, "", "", ("stored@manipal.edu", "stored_secret"), prompt=None,
    )
    assert (username, password) == ("stored@manipal.edu", "stored_secret")


def test_resolve_prompt_called_only_for_missing_fields() -> None:
    """The injected prompt runs once per still-missing field, nothing else."""
    asked: list[str] = []

    def prompt(field: str) -> str:
        asked.append(field)
        return f"typed_{field}"

    username, password = resolve_credentials(
        "flag@manipal.edu", None, None, None, None, prompt=prompt,
    )
    assert (username, password) == ("flag@manipal.edu", "typed_password")
    assert asked == ["password"]


def test_resolve_no_prompt_when_everything_resolved() -> None:
    """Fully resolved credentials never touch the prompt."""

    def prompt(field: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("prompt must not be called")

    username, password = resolve_credentials(
        "u@manipal.edu", "p", None, None, None, prompt=prompt,
    )
    assert (username, password) == ("u@manipal.edu", "p")


def test_resolve_empty_flag_skips_env_but_falls_to_prompt() -> None:
    """``--user ''`` skips the environment yet still prompts (legacy behaviour)."""
    username, password = resolve_credentials(
        "", "p", "env@manipal.edu", "env_p", None,
        prompt=lambda field: "typed_username",
    )
    assert (username, password) == ("typed_username", "p")


def test_resolve_unresolved_fields_return_none() -> None:
    """With no sources at all both fields come back None."""
    assert resolve_credentials(None, None, "", "", None, prompt=None) == (None, None)


# ---------------------------------------------------------------------------
# normalize_totp — literal codes vs the challenge BeginAuth sends
# ---------------------------------------------------------------------------

def test_normalize_preserves_literal_after_policy_validation() -> None:
    """Normalization is transport-only; incompatible methods fail in validation."""
    assert normalize_totp("123456", totp_stdin=False) == ("123456", False)


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

def test_plan_resume_with_matching_pending_method() -> None:
    plan = plan_login(
        totp_code="123456", read_totp_after_challenge=False, mfa_method="app",
        pending={
            "mfa_method": "app",
            "selected_proof": {"auth_method_id": "PhoneAppOTP"},
        },
        interactive=True,
    )
    assert plan.mode == "resume"
    assert plan.totp_code == "123456"


def test_plan_auto_never_guesses_pending_method_for_literal_code() -> None:
    plan = plan_login(
        totp_code="123456", read_totp_after_challenge=False, mfa_method="auto",
        pending={
            "mfa_method": "auto",
            "selected_proof": {"auth_method_id": "OneWaySMS"},
        },
        interactive=True,
    )
    assert plan.mode == "fresh"


def test_plan_method_mismatch_starts_fresh() -> None:
    """An explicit method differing from the pending session never resumes."""
    plan = plan_login(
        totp_code="123456", read_totp_after_challenge=False, mfa_method="app",
        pending={
            "mfa_method": "sms",
            "selected_proof": {"auth_method_id": "OneWaySMS"},
        },
        interactive=True,
    )
    assert plan.mode == "fresh"
    assert plan.defer_mfa_to_pending is False


def test_plan_never_resumes_without_literal_code() -> None:
    for kwargs in (
        {"totp_code": None, "read_totp_after_challenge": False},
        {"totp_code": "123456", "read_totp_after_challenge": True},
    ):
        plan = plan_login(
            mfa_method="app",
            pending={
                "mfa_method": "app",
                "selected_proof": {"auth_method_id": "PhoneAppOTP"},
            },
            interactive=True,
            **kwargs,
        )
        assert plan.mode != "resume"


def test_plan_defer_non_interactive_without_code() -> None:
    """Non-TTY with no code and no stdin read defers to auth verify."""
    plan = plan_login(
        totp_code=None, read_totp_after_challenge=False, mfa_method="sms",
        pending=None, interactive=False,
    )
    assert plan.mode == "defer"
    assert plan.defer_mfa_to_pending is True


def test_plan_fresh_interactive_or_with_code() -> None:
    interactive = plan_login(
        totp_code=None, read_totp_after_challenge=False, mfa_method="auto",
        pending=None, interactive=True,
    )
    piped = plan_login(
        totp_code=None, read_totp_after_challenge=True, mfa_method="auto",
        pending=None, interactive=False,
    )
    coded = plan_login(
        totp_code="123456", read_totp_after_challenge=False, mfa_method="auto",
        pending=None, interactive=False,
    )
    for plan in (interactive, piped, coded):
        assert plan.mode == "fresh"
        assert plan.defer_mfa_to_pending is False


# ---------------------------------------------------------------------------
# _persist_check_report — shared tail ordering
# ---------------------------------------------------------------------------

def test_tail_orders_cookies_before_check_before_credential_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security ordering: seal cookies → validate session → save credentials."""
    order: list[str] = []
    monkeypatch.setattr(auth_mod, "save_cookies", lambda cookies: order.append("cookies"))
    client = MagicMock()
    client.check_auth.side_effect = lambda: order.append("check") or True
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(auth_mod, "LighthouseClient", client_factory)
    store = MagicMock()
    store.save.side_effect = lambda u, p: order.append("creds")
    monkeypatch.setattr(auth_mod, "CredentialStore", lambda: store)

    rc = auth_mod._persist_check_report(
        _make_d2l_cookies(), json_output=True,
        failure_hint="Try: lighthouse auth login", save_credentials_pair=("u", "p"),
    )

    assert rc == 0
    assert order == ["cookies", "check", "creds"]
    client_factory.assert_called_once_with(read_only_auth=True)


def test_tail_failed_session_check_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """check_auth failure reports an error and never stores credentials."""
    monkeypatch.setattr(auth_mod, "save_cookies", lambda cookies: None)
    client = MagicMock()
    client.check_auth.return_value = False
    monkeypatch.setattr(auth_mod, "LighthouseClient", lambda **_kwargs: client)
    store = MagicMock()
    monkeypatch.setattr(auth_mod, "CredentialStore", lambda: store)

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


def test_tail_without_pair_never_saves_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify shape (no pair) structurally cannot store secrets."""
    monkeypatch.setattr(auth_mod, "save_cookies", lambda cookies: None)
    client = MagicMock()
    client.check_auth.return_value = True
    monkeypatch.setattr(auth_mod, "LighthouseClient", lambda **_kwargs: client)
    store = MagicMock()
    monkeypatch.setattr(auth_mod, "CredentialStore", lambda: store)

    rc = auth_mod._persist_check_report(_make_d2l_cookies(), json_output=True)

    assert rc == 0
    store.save.assert_not_called()


def test_tail_reports_only_allowlisted_cookie_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(auth_mod, "save_cookies", lambda cookies: None)
    client = MagicMock()
    client.check_auth.return_value = True
    monkeypatch.setattr(auth_mod, "LighthouseClient", lambda **_kwargs: client)
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


def test_credentials_via_env_vars(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIGHTHOUSE_USERNAME/PASSWORD env vars supply credentials."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    assert "Username:" not in result.output
    assert sso.login.call_args.args[0] == "user@manipal.edu"


def test_flags_take_precedence_over_env_vars(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The username flag combines with the environment-only password channel."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "env_user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "env_secret")

    with _mock_sso() as (sso, _client):
        result = _invoke_login(
            cli_runner,
            ["--user", "flag_user@manipal.edu", "--totp", "123456"],
        )

    assert result.exit_code == 0
    sso.login.assert_called_once()
    call_args = sso.login.call_args.args
    assert call_args[0] == "flag_user@manipal.edu"
    assert call_args[1] == "env_secret"


def test_mixed_per_field_sources_preserve_precedence(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag username combines with env password — precedence is per field."""
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "env_secret")

    with _mock_sso() as (sso, _client):
        result = _invoke_login(
            cli_runner, ["--user", "flag_user@manipal.edu", "--totp", "123456"],
        )

    assert result.exit_code == 0
    call_args = sso.login.call_args.args
    assert call_args[0] == "flag_user@manipal.edu"
    assert call_args[1] == "env_secret"


def test_store_fallback_used_without_flags_or_env(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sealed stored credentials are the third source."""
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
    monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)
    auth_mod.CredentialStore().save("stored@manipal.edu", "stored_secret")

    with _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    call_args = sso.login.call_args.args
    assert call_args[0] == "stored@manipal.edu"
    assert call_args[1] == "stored_secret"


# ---------------------------------------------------------------------------
# TOTP via flag/stdin
# ---------------------------------------------------------------------------

def test_totp_flag_submits_code(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp submits the 2FA code without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso() as (sso, _client):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    sso.login.assert_called_once()
    assert sso.login.call_args.args[2] == "123456"


def test_explicit_app_method_ignores_stale_sms_pending(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit --mfa-method app with a literal code starts a fresh flow rather than
    resuming a leftover SMS pending session (offline app TOTP belongs to no SMS session)."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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
    cli_runner: CliRunner,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed fresh flow cannot be resumed by the next default login."""
    from lighthouse_cli.config import save_mfa_pending

    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
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
    assert not (isolated_config / "mfa_pending.json").exists()


def test_deferred_mfa_does_not_clear_pending_checkpoint(
    cli_runner: CliRunner,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred MFA result remains eligible for ``auth verify``."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
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


def test_totp_stdin_pipe(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp - reads the 2FA code from stdin pipe."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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

def test_cookies_saved_sealed_with_owner_only_permissions(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cookies.json written sealed (v2 envelope) with 0600 permissions."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    cookies_path = isolated_config / "cookies.json"
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
    from lighthouse_cli.config import load_cookies
    assert load_cookies() == cookies
    mode = cookies_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_post_login_session_verification(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_auth() confirms session is valid after login."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso() as (_sso, client):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    client.check_auth.assert_called_once()


def test_auth_status_works_after_login(
    cli_runner: CliRunner, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookies from auth login work with auth status."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = _make_d2l_cookies()

    cookies_path = config_dir / "cookies.json"
    cookies_path.write_text(json.dumps(cookies))

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

def test_wrong_credentials_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid credentials produce clear error, no traceback."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong_password")

    error = MicrosoftSSOError(
        "[50126] Invalid username or password.",
        step="POST credentials",
        recovery="Double-check your email and password.",
    )
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "50126" in result.output or "Invalid" in result.output
    assert "Traceback" not in result.output


def test_unexpected_error_never_leaks_exception_text(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third-party exception renders as `Unexpected error (<Type>)` + guidance —
    never raw str(exc), which may embed URLs or tokens."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    leaky = RuntimeError("https://login.microsoftonline.com/token?code=SECRET")
    with _mock_sso(login_side_effect=leaky):
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 1
    assert "Unexpected error (RuntimeError)" in result.output
    assert "SECRET" not in result.output
    assert "microsoftonline" not in result.output


def test_wrong_totp_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid 2FA code produces clear error, no traceback."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    error = MicrosoftSSOError(
        "2FA verification failed: invalid or expired code.",
        step="MFA",
        recovery="Request a new 2FA code and try again.",
    )
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "wrong"])

    assert result.exit_code == 1
    assert "2FA" in result.output or "verification" in result.output
    assert "Traceback" not in result.output


def test_network_failure_during_sso(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network error produces clear message."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    error = MicrosoftSSOError(
        "Failed to redirect to Microsoft SSO.",
        step="initiate SAML",
        recovery="Check that lighthouse.manipal.edu is reachable.",
    )
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "Microsoft" in result.output or "lighthouse" in result.output.lower()


def test_unexpected_failure_wrapped_cleanly(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception exits cleanly under --json — never a traceback."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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


def test_totp_timeout_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty 2FA code produces clear error."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    error = MicrosoftSSOError(
        "2FA code is required but was empty.",
        step="MFA",
        recovery="Provide a 2FA code via --totp flag or pipe.",
    )
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "2FA" in result.output or "code" in result.output.lower()


# ---------------------------------------------------------------------------
# JSON output contract
# ---------------------------------------------------------------------------

def test_json_output_success(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json produces valid JSON with success:true on success."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data.get("success") is True
    assert "cookies" in data


def test_json_output_failure(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json produces valid JSON with success:false on failure."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong")

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
    cli_runner: CliRunner,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    pending = auth_mod.MfaPendingError(
        "FULL-DISPLAY-SENTINEL user@example.com +919876541234",
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
    assert "FULL-DISPLAY-SENTINEL" not in json_result.stdout + json_result.stderr
    assert "user@example.com" not in json_result.stdout + json_result.stderr
    assert "+919876541234" not in json_result.stdout + json_result.stderr
    assert "SECRET" not in json_result.stdout + json_result.stderr

    assert human_result.exit_code == 0
    assert "Authentication failed. Check your credentials" in human_result.output
    assert "FULL-DISPLAY-SENTINEL" not in human_result.output
    assert "user@example.com" not in human_result.output
    assert "+919876541234" not in human_result.output
    assert "SECRET" not in human_result.output


def test_keyring_failure_is_clean_under_json(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key source: stdout stays one JSON object; stderr is a safe diagnostic."""
    monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        "lighthouse_cli.credential_store._load_keyring_module", lambda: None,
    )
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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

def test_failed_validation_never_saves_credentials(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--save-credentials with a failed session check stores nothing."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso(check_auth=False):
        result = _invoke_login(
            cli_runner, ["--totp", "123456", "--save-credentials", "--json"],
        )

    assert result.exit_code == 1
    # Cookies were sealed first (fixed ordering), but credentials were not saved.
    assert (isolated_config / "cookies.json").exists()
    assert not (isolated_config / "credentials.json").exists()


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
    from lighthouse_cli.config import save_mfa_pending

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

def test_empty_password_rejected(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty password exits with error before network call."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "")

    result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "password" in result.output.lower()


def test_empty_username_rejected(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty username exits with error before network call."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "username" in result.output.lower()


def test_totp_without_value_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp without value produces Click usage error (exit 2)."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = _invoke_login(cli_runner, ["--totp"])

    assert result.exit_code == 2
    assert "invalid command arguments" in result.output.lower()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_username_prompt_goes_to_stderr_under_json(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under --json the username banner lands on stderr, keeping stdout pure JSON."""
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso():
            result = _invoke_login(
                cli_runner, ["--totp", "123456", "--json"],
                input="prompted@manipal.edu\n",
            )

    assert result.exit_code == 0
    assert "Username (email):" in result.stderr
    assert "Username (email):" not in result.stdout
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert "prompted@manipal.edu" not in result.stdout


def test_username_prompt_on_stdout_for_humans(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --json the username banner stays on stdout as before."""
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso():
            result = _invoke_login(
                cli_runner, ["--totp", "123456"], input="prompted@manipal.edu\n",
            )

    assert result.exit_code == 0
    assert "Username (email):" in result.output


def test_interactive_login_defaults_to_registered_method_picker(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain TTY login asks the user to choose from Microsoft's proof list."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_MFA_METHOD", raising=False)

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(cli_runner, [], input="n\n")

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == "choose"
    assert sso.login.call_args.kwargs["defer_mfa_to_pending"] is False
    assert "You will be asked to pick a verification method." in result.output


def test_interactive_login_preserves_explicit_auto_method(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit automation-style selector is never replaced by the picker."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner, ["--mfa-method", "auto"], input="n\n",
            )

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == "auto"


def test_environment_mfa_method_ignores_surrounding_whitespace(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("LIGHTHOUSE_MFA_METHOD", " app ")

    with patch.object(auth_mod, "_is_interactive", return_value=False):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == "app"


def test_interactive_literal_totp_without_method_keeps_auto(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-supplied app code keeps legacy auto selection, not ambiguous choose."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_MFA_METHOD", raising=False)

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner, ["--totp", "123456"], input="n\n",
            )

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == "auto"


def test_noninteractive_login_default_remains_auto_and_deferred(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripts keep tenant-default selection and the resumable verify flow."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    monkeypatch.delenv("LIGHTHOUSE_MFA_METHOD", raising=False)

    with patch.object(auth_mod, "_is_interactive", return_value=False):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(cli_runner, [])

    assert result.exit_code == 0
    assert sso.login.call_args.kwargs["mfa_method"] == "auto"
    assert sso.login.call_args.kwargs["defer_mfa_to_pending"] is True
    assert "Show the full command guide?" not in result.output


def test_interactive_login_shows_next_steps_and_full_guide(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed TTY login leads into useful commands without running them."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso():
            result = _invoke_login(cli_runner, [], input="\n")

    assert result.exit_code == 0
    assert "Login complete. Session saved and verified." in result.output
    assert "Try next:" in result.output
    assert "lighthouse courses" in result.output
    assert "lighthouse download <course> --dry-run" in result.output
    assert "Show the full command guide? [Y/n]:" in result.stderr
    assert "Command guide:" in result.output
    assert "Read only:" in result.output
    assert "Remote change:" in result.output
    assert "Cookies:" not in result.output


def test_interactive_login_can_skip_full_guide(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining the optional guide still leaves the compact next steps visible."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with patch.object(auth_mod, "_is_interactive", return_value=True):
        with _mock_sso():
            result = _invoke_login(cli_runner, [], input="n\n")

    assert result.exit_code == 0
    assert "Try next:" in result.output
    assert "Command guide:" not in result.output


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

def test_password_not_logged(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password never appears in stdout/stderr."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "super_secret_password")

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456", "--json"])

    assert "super_secret_password" not in result.output
    assert "super_secret_password" not in result.stderr


def test_totp_not_persisted(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real (mocked-SSO) login never leaks the TOTP into the sealed artifact."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    # quote_plus matches application/x-www-form-urlencoded encoding, so
    # this checks a genuinely different byte sequence from the raw sentinel.
    totp = "654+21/SEN="

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", totp])

    assert result.exit_code == 0
    cookies_path = isolated_config / "cookies.json"
    assert cookies_path.exists()
    raw = cookies_path.read_bytes()
    # The artifact on disk is a sealed envelope; the code must appear in it
    # neither as plaintext nor URL-encoded.
    assert totp.encode() not in raw
    import urllib.parse
    assert urllib.parse.quote_plus(totp).encode() not in raw


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

def test_exit_code_success(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful login exits with code 0."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso():
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 0


def test_exit_code_auth_failure(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth failure exits with code 1."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong")

    with _mock_sso(login_side_effect=MicrosoftSSOError("Invalid credentials")):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1


def test_exit_code_cli_usage_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI usage error exits with code 2."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = _invoke_login(cli_runner, ["--totp"])

    assert result.exit_code == 2


def test_keyboard_interrupt_exits_cleanly(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt exits with code 130."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    with _mock_sso(login_side_effect=KeyboardInterrupt()):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 130


# ---------------------------------------------------------------------------
# SSO page structure errors
# ---------------------------------------------------------------------------

def test_sso_page_structure_change_error(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MS SSO page structure change produces descriptive error."""
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    error = MicrosoftSSOError(
        "Could not find Microsoft login configuration on the page.",
        step="get MS config",
        recovery="Microsoft may have changed their login page.",
    )
    with _mock_sso(login_side_effect=error):
        result = _invoke_login(cli_runner, ["--totp", "123456"])

    assert result.exit_code == 1
    assert "could not find" in result.output.lower() or "Microsoft" in result.output


# ---------------------------------------------------------------------------
# Concurrency and config directory
# ---------------------------------------------------------------------------

def test_concurrent_auth_no_corruption(isolated_config: Path) -> None:
    """cookies.json is valid JSON after concurrent auth attempts."""
    cookies1 = _make_d2l_cookies()
    cookies2 = {
        "d2lSecureSessionVal": "sec2",
        "d2lSessionVal": "ses2",
        "d2lSameSiteCanaryA": "canaryA2",
        "d2lSameSiteCanaryB": "canaryB2",
    }

    import lighthouse_cli.config as config_module

    errors: list[Exception] = []

    def write(value: dict[str, str]) -> None:
        try:
            config_module.save_cookies(value)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(c,)) for c in (cookies1, cookies2)]
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
    from lighthouse_cli.config import load_cookies
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
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lighthouse_cli.config import save_mfa_pending

        # Day 1: checkpoint sealed under one passphrase...
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", "day-one-passphrase")
        save_mfa_pending({"mfa_method": "sms", "created_at": "2026-08-01T00:00:00Z"})
        # Day 2: passphrase removed/replaced — the sealed file can't open.
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", "day-two-passphrase")
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CredentialStoreError text is not copied into auth output."""
        from lighthouse_cli.credential_store import CredentialStoreError

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

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

def _probe_result(page: str = "converged", proofs: list[Any] | None = None):
    from lighthouse_cli.ms_mfa import MfaProbeResult, UserProof

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


class TestAuthMfaMethodsCommand:
    def _invoke(self, runner: CliRunner, args: list[str]) -> Any:
        return runner.invoke(cli, ["auth", "mfa-methods", *args], catch_exceptions=False)

    def test_registered_in_auth_help(self, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        assert "mfa-methods" in result.output

    def test_json_output_lists_methods_and_keeps_stdout_pure(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        probe = MagicMock(return_value=_probe_result())
        with patch.object(auth_mod.MicrosoftSSOClient, "probe_mfa_methods", probe):
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

    def test_human_output_lists_methods(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        with patch.object(
            auth_mod.MicrosoftSSOClient, "probe_mfa_methods",
            MagicMock(return_value=_probe_result()),
        ):
            result = self._invoke(cli_runner, [])

        assert result.exit_code == 0
        assert "TwoWayVoiceMobile" in result.output
        assert "--mfa-method call" in result.output
        assert "Microsoft default" in result.output
        assert "+919876541234" not in result.output

    def test_malicious_display_is_masked_in_json_output(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lighthouse_cli.ms_mfa import UserProof

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        proof = UserProof(
            "OneWaySMS",
            "FULL-DISPLAY-SENTINEL user@example.com +919876541234",
            "+919876541234",
            True,
        )
        with patch.object(
            auth_mod.MicrosoftSSOClient, "probe_mfa_methods",
            MagicMock(return_value=_probe_result(proofs=[proof])),
        ):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["methods"][0]["method"] == "sms"
        assert payload["methods"][0]["display"] == (
            "Text code (SMS or WhatsApp): ***1234"
        )
        assert "FULL-DISPLAY-SENTINEL" not in result.stdout
        assert "user@example.com" not in result.stdout
        assert "+919876541234" not in result.stdout

    def test_malicious_display_is_masked_in_human_output(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lighthouse_cli.ms_mfa import UserProof

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        proof = UserProof(
            "TwoWayVoiceMobile",
            "FULL-DISPLAY-SENTINEL user@example.com +919876541234",
            "+919876541234",
            True,
        )
        with patch.object(
            auth_mod.MicrosoftSSOClient, "probe_mfa_methods",
            MagicMock(return_value=_probe_result(proofs=[proof])),
        ):
            result = self._invoke(cli_runner, [])

        assert result.exit_code == 0
        assert "Voice call to mobile: ***1234" in result.output
        assert "FULL-DISPLAY-SENTINEL" not in result.output
        assert "user@example.com" not in result.output
        assert "+919876541234" not in result.output

    def test_unrecognized_method_id_is_rendered_as_other(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lighthouse_cli.ms_mfa import UserProof

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        proof = UserProof(
            "FutureMethod\x1b[31mPASSWORD_SENTINEL",
            "FULL-DISPLAY-SENTINEL",
            "",
            True,
        )
        with patch.object(
            auth_mod.MicrosoftSSOClient, "probe_mfa_methods",
            MagicMock(return_value=_probe_result(proofs=[proof])),
        ):
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
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lighthouse_cli.ms_mfa import UserProof

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        proof = UserProof("FutureProof", "Future method", "", False)
        with patch.object(
            auth_mod.MicrosoftSSOClient,
            "probe_mfa_methods",
            MagicMock(return_value=_probe_result(proofs=[proof])),
        ):
            json_result = self._invoke(cli_runner, ["--json"])
            human_result = self._invoke(cli_runner, [])

        assert json.loads(json_result.stdout)["methods"][0]["method"] is None
        assert "no supported --mfa-method selector" in human_result.output
        assert "--mfa-method unknown" not in human_result.output

    def test_no_mfa_account_reports_cleanly(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        with patch.object(
            auth_mod.MicrosoftSSOClient, "probe_mfa_methods",
            MagicMock(return_value=_probe_result(page="no_mfa", proofs=[])),
        ):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "success": True, "page": "no_mfa", "methods": [],
        }

    def test_sso_error_becomes_clean_json_error(
        self, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        boom = MagicMock(side_effect=MicrosoftSSOError("[50126] wrong password"))
        with patch.object(auth_mod.MicrosoftSSOClient, "probe_mfa_methods", boom):
            result = self._invoke(cli_runner, ["--json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["success"] is False

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
        for method in ("app", "auto"):
            validate_totp_usage("123456", totp_stdin=False, mfa_method=method)
        assert normalize_totp("123456", totp_stdin=False) == ("123456", False)

    @pytest.mark.parametrize("method", ["sms", "call", "push"])
    def test_login_rejects_incompatible_totp_before_sso(
        self, method: str, cli_runner: CliRunner, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner,
                ["--mfa-method", method, "--totp", "123456", "--json"],
            )
        assert result.exit_code == 2
        assert "--totp" in json.loads(result.stdout)["error"]
        sso.login.assert_not_called()

    def test_login_accepts_call_and_push_choices(self, cli_runner: CliRunner) -> None:
        """--mfa-method call/push parse at the CLI layer."""
        for method in ("call", "push"):
            result = cli_runner.invoke(
                cli, ["auth", "login", "--mfa-method", method, "--help"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
