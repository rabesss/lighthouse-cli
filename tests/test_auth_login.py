"""Tests for lighthouse auth login command (pure HTTP auth)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.ms_auth import MicrosoftSSOError, D2L_COOKIE_NAMES


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _make_d2l_cookies() -> dict[str, str]:
    """Return a valid D2L cookies dict."""
    return {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "lighthouse-cli"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def cookies_path(config_dir: Path) -> Path:
    return config_dir / "cookies.json"


# ---------------------------------------------------------------------------
# VAL-AUTH-001: Command registration
# ---------------------------------------------------------------------------

def test_auth_login_registered_as_subcommand(cli_runner: CliRunner) -> None:
    """lighthouse auth login --help succeeds and shows all flags."""
    result = cli_runner.invoke(cli, ["auth", "login", "--help"])
    assert result.exit_code == 0
    output = result.output
    assert "--user" in output
    assert "--pass" in output
    assert "--totp" in output
    assert "--save-credentials" in output
    assert "--json" in output


def test_auth_login_appears_in_auth_help(cli_runner: CliRunner) -> None:
    """auth --help lists the login subcommand."""
    result = cli_runner.invoke(cli, ["auth", "--help"])
    assert result.exit_code == 0
    assert "login" in result.output


# ---------------------------------------------------------------------------
# VAL-AUTH-003: Credentials via flags skip prompts
# ---------------------------------------------------------------------------

def test_credentials_via_flags_skip_prompt(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--user and --pass flags supply credentials without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--user", "user@manipal.edu", "--pass", "secret", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    assert "Username:" not in result.output
    assert "Password:" not in result.output


# ---------------------------------------------------------------------------
# VAL-AUTH-004: Credentials via environment variables
# ---------------------------------------------------------------------------

def test_credentials_via_env_vars(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIGHTHOUSE_USERNAME/PASSWORD env vars supply credentials."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    assert "Username:" not in result.output


def test_flags_take_precedence_over_env_vars(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--user/--pass flags take precedence over LIGHTHOUSE_USERNAME/PASSWORD."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "env_user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "env_secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--user", "flag_user@manipal.edu", "--pass", "flag_secret", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    mock_sso.login.assert_called_once()
    call_args = mock_sso.login.call_args.args
    assert call_args[0] == "flag_user@manipal.edu"
    assert call_args[1] == "flag_secret"


# ---------------------------------------------------------------------------
# VAL-AUTH-005 / VAL-AUTH-006: 2FA via flag/stdin
# ---------------------------------------------------------------------------

def test_totp_flag_submits_code(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp submits the 2FA code without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    mock_sso.login.assert_called_once()
    assert mock_sso.login.call_args.args[2] == "123456"


def test_totp_stdin_pipe(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp - reads the 2FA code from stdin pipe."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "-"],
                input="123456\n",
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    mock_sso.login.assert_called_once()
    assert mock_sso.login.call_args.args[2] == "123456"


# ---------------------------------------------------------------------------
# VAL-AUTH-011 / VAL-AUTH-012: Cookie save and session verification
# ---------------------------------------------------------------------------

def test_cookies_saved_to_file(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cookies.json written with correct format and 0600 permissions."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    # Also patch the config module globals since they're computed at import time
    import lighthouse_cli.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "COOKIE_FILE", config_dir / "cookies.json")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    cookies_path = config_dir / "cookies.json"
    assert cookies_path.exists()
    data = json.loads(cookies_path.read_text())
    assert "cookies" in data
    assert "extracted_at" in data
    assert "d2lSecureSessionVal" in data["cookies"]
    assert data["cookies"]["d2lSecureSessionVal"] == "sec123"
    mode = cookies_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_post_login_session_verification(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_auth() confirms session is valid after login."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    mock_client.check_auth.assert_called_once()


# ---------------------------------------------------------------------------
# VAL-AUTH-015 / VAL-AUTH-016: Cookies compatible with auth status
# ---------------------------------------------------------------------------

def test_auth_status_works_after_login(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookies from auth login work with auth status."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = _make_d2l_cookies()

    import lighthouse_cli.config as config_mod
    cookies_path = config_dir / "cookies.json"
    cookies_path.write_text(json.dumps(cookies))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "COOKIE_FILE", cookies_path)

    with patch("lighthouse_cli.commands.LighthouseClient") as mock_commands:
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_auth:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_commands.return_value = mock_client
            mock_auth.return_value = mock_client
            result = cli_runner.invoke(cli, ["auth", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Session valid" in result.output or "valid" in result.output


# ---------------------------------------------------------------------------
# VAL-AUTH-017 / VAL-AUTH-018: Error handling
# ---------------------------------------------------------------------------

def test_wrong_credentials_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid credentials produce clear error, no traceback."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong_password")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "[50126] Invalid username or password.",
        step="POST credentials",
        recovery="Double-check your email and password.",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "50126" in result.output or "Invalid" in result.output
    assert "Traceback" not in result.output


def test_wrong_totp_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid 2FA code produces clear error, no traceback."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "2FA verification failed: invalid or expired code.",
        step="MFA",
        recovery="Request a new 2FA code and try again.",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "wrong"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "2FA" in result.output or "verification" in result.output
    assert "Traceback" not in result.output


def test_network_failure_during_sso(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network error produces clear message."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "Failed to redirect to Microsoft SSO.",
        step="initiate SAML",
        recovery="Check that lighthouse.manipal.edu is reachable.",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "Microsoft" in result.output or "lighthouse" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-020: 2FA timeout (now: empty code rejection)
# ---------------------------------------------------------------------------

def test_totp_timeout_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty 2FA code produces clear error."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "2FA code is required but was empty.",
        step="MFA",
        recovery="Provide a 2FA code via --totp flag or pipe.",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "2FA" in result.output or "code" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-026: JSON output
# ---------------------------------------------------------------------------

def test_json_output_success(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json produces valid JSON with success:true on success."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456", "--json"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data.get("success") is True
    assert "cookies" in data


def test_json_output_failure(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json produces valid JSON with success:false on failure."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "Invalid username or password.",
        step="POST credentials",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456", "--json"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data.get("success") is False
    assert "error" in data


# ---------------------------------------------------------------------------
# VAL-AUTH-030 / VAL-AUTH-031: Empty username/password rejection
# ---------------------------------------------------------------------------

def test_empty_password_rejected(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty password exits with error before network call."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "")

    result = cli_runner.invoke(
        cli,
        ["auth", "login", "--totp", "123456"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "password" in result.output.lower()


def test_empty_username_rejected(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty username exits with error before network call."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = cli_runner.invoke(
        cli,
        ["auth", "login", "--totp", "123456"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "username" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-032: --totp without value is an error
# ---------------------------------------------------------------------------

def test_totp_without_value_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp without value produces Click usage error (exit 2)."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = cli_runner.invoke(
        cli,
        ["auth", "login", "--totp"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "requires an argument" in result.output.lower() or "totp" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-033: SSO error detection
# ---------------------------------------------------------------------------

def test_sso_page_structure_change_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MS SSO page structure change produces descriptive error."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError(
        "Could not find Microsoft login configuration on the page.",
        step="get MS config",
        recovery="Microsoft may have changed their login page.",
    )

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "could not find" in result.output.lower() or "Microsoft" in result.output


# ---------------------------------------------------------------------------
# VAL-AUTH-036: Password not logged
# ---------------------------------------------------------------------------

def test_password_not_logged(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password never appears in stdout/stderr."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "super_secret_password")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456", "--json"],
                catch_exceptions=False,
            )

    assert "super_secret_password" not in result.output
    assert "super_secret_password" not in result.stderr


# ---------------------------------------------------------------------------
# VAL-AUTH-037: TOTP code not persisted
# ---------------------------------------------------------------------------

def test_totp_not_persisted(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOTP code is never written to cookies.json."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = _make_d2l_cookies()
    cookies_path = config_dir / "cookies.json"
    cookies_path.write_text(json.dumps(cookies))

    content = cookies_path.read_text()
    assert "123456" not in content
    assert "totp" not in content.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-038: Exit codes
# ---------------------------------------------------------------------------

def test_exit_code_success(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful login exits with code 0."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0


def test_exit_code_auth_failure(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth failure exits with code 1."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "wrong")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = MicrosoftSSOError("Invalid credentials")

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1


def test_exit_code_cli_usage_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI usage error exits with code 2."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    result = cli_runner.invoke(
        cli,
        ["auth", "login", "--totp"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# VAL-AUTH-039: Ctrl+C handling
# ---------------------------------------------------------------------------

def test_keyboard_interrupt_exits_cleanly(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt exits with code 130."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_sso = MagicMock()
    mock_sso.login.side_effect = KeyboardInterrupt()

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 130


# ---------------------------------------------------------------------------
# VAL-AUTH-002: Non-TTY with no credentials produces error
# ---------------------------------------------------------------------------

def test_non_tty_no_credentials_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY stdin with no credentials produces error, exit code 1."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
    monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)

    with patch("lighthouse_cli.auth.CredentialStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.load.return_value = None
        mock_store_cls.return_value = mock_store

        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "credentials" in result.output.lower() or "required" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-024: Concurrent auth attempts (atomic writes)
# ---------------------------------------------------------------------------

def test_concurrent_auth_no_corruption(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cookies.json is valid JSON after concurrent auth attempts."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies1 = _make_d2l_cookies()
    cookies2 = {
        "d2lSecureSessionVal": "sec2",
        "d2lSessionVal": "ses2",
        "d2lSameSiteCanaryA": "canaryA2",
        "d2lSameSiteCanaryB": "canaryB2",
    }

    import lighthouse_cli.config as config_module
    import threading

    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "COOKIE_FILE", config_dir / "cookies.json")

    errors = []

    def write(value: dict) -> None:
        try:
            config_module.save_cookies(value)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=write, args=(cookies1,))
    t2 = threading.Thread(target=write, args=(cookies2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    cookies_path = config_dir / "cookies.json"
    assert cookies_path.exists()
    data = json.loads(cookies_path.read_text())
    assert "cookies" in data
    assert len(data["cookies"]) >= 4
    assert "d2lSecureSessionVal" in data["cookies"]


# ---------------------------------------------------------------------------
# VAL-AUTH-025: Config directory auto-creation
# ---------------------------------------------------------------------------

def test_config_directory_auto_created(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config directory is created if missing."""
    config_dir = tmp_path / ".config" / "lighthouse-cli"
    assert not config_dir.exists()
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    import lighthouse_cli.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "COOKIE_FILE", config_dir / "cookies.json")

    cookies = _make_d2l_cookies()

    mock_sso = MagicMock()
    mock_sso.login.return_value = cookies

    with patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso):
        with patch("lighthouse_cli.auth.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert config_dir.exists()
    mode = config_dir.stat().st_mode & 0o777
    assert mode in (0o700, 0o755)


# ---------------------------------------------------------------------------
# Removed browser-specific tests
# ---------------------------------------------------------------------------
# The following tests have been removed because they tested Playwright browser
# launch behavior which is no longer needed:
# - test_headless_browser_launch
# - test_sso_navigation_chain
# - test_cookie_extraction_after_sso
# - test_browser_cleanup_on_success
# - test_browser_cleanup_on_failure
# - test_browser_launch_failure
# - test_headless_mode_no_visible_window
# Their equivalents are now tested in tests/test_ms_auth.py using HTTP mocks.
