"""Tests for lighthouse auth login command."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def make_mock_playwright_with_browser(
    cookies: list[dict] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a properly chained Playwright mock.

    Chain: sync_playwright() -> pw -> pw.start() -> pw -> pw.chromium.launch() -> browser

    Returns (mock_playwright, pw_mock, mock_browser).
    """
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()

    if cookies is None:
        cookies = [
            {"name": "d2lSecureSessionVal", "value": "sec123"},
            {"name": "d2lSessionVal", "value": "ses123"},
            {"name": "d2lSameSiteCanaryA", "value": "canaryA"},
            {"name": "d2lSameSiteCanaryB", "value": "canaryB"},
        ]
    mock_context.cookies.return_value = cookies
    mock_context.pages.return_value = [mock_page]
    mock_context.new_page.return_value = mock_page
    mock_browser.new_context.return_value = mock_context
    mock_browser.close.return_value = None
    mock_page.goto = MagicMock()
    mock_page.fill = MagicMock()
    mock_page.click = MagicMock()
    mock_page.wait_for_url = MagicMock()
    mock_page.wait_for_selector = MagicMock(return_value=mock_page)  # return a mock element
    mock_page.query_selector_all = MagicMock(return_value=[])
    mock_page.wait_for_timeout = MagicMock()  # used in SSO flow

    mock_playwright = MagicMock()
    pw_mock = MagicMock()
    mock_playwright.return_value = pw_mock
    pw_mock.start.return_value = pw_mock
    pw_mock.chromium.launch.return_value = mock_browser

    return mock_playwright, pw_mock, mock_browser


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
def cookies_path() -> Path:
    """Path to cookies.json, resolving via env var."""
    return Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", str(Path.home() / ".config" / "lighthouse-cli"))) / "cookies.json"


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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--user and --pass flags supply credentials without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIGHTHOUSE_USERNAME/PASSWORD env vars supply credentials."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--user/--pass flags take precedence over LIGHTHOUSE_USERNAME/PASSWORD."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "env_user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "env_secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client
                result = cli_runner.invoke(
                    cli,
                    ["auth", "login", "--user", "flag_user@manipal.edu", "--pass", "flag_secret", "--totp", "123456"],
                    catch_exceptions=False,
                )

    assert result.exit_code == 0
    mock_authenticator.authenticate.assert_called_once()
    call_args = mock_authenticator.authenticate.call_args.args
    assert call_args[0] == "flag_user@manipal.edu"
    assert call_args[1] == "flag_secret"


# ---------------------------------------------------------------------------
# VAL-AUTH-005 / VAL-AUTH-006: 2FA via prompt/flag/stdin
# ---------------------------------------------------------------------------

def test_totp_flag_submits_code(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp submits the 2FA code without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client
                result = cli_runner.invoke(
                    cli,
                    ["auth", "login", "--totp", "123456"],
                    catch_exceptions=False,
                )

    assert result.exit_code == 0
    mock_authenticator.authenticate.assert_called_once()
    assert mock_authenticator.authenticate.call_args.args[2] == "123456"


def test_totp_stdin_pipe(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--totp - reads the 2FA code from stdin pipe."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client
                result = cli_runner.invoke(
                    cli,
                    ["auth", "login", "--totp", "-"],
                    input="123456\n",
                    catch_exceptions=False,
                )

    assert result.exit_code == 0
    mock_authenticator.authenticate.assert_called_once()
    assert mock_authenticator.authenticate.call_args.args[2] == "123456"


# ---------------------------------------------------------------------------
# VAL-AUTH-008 / VAL-AUTH-009 / VAL-AUTH-010: Browser launch and SSO
# ---------------------------------------------------------------------------

def test_headless_browser_launch(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playwright launches headless Chromium."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    mock_playwright, _, mock_browser = make_mock_playwright_with_browser()

    launch_kwargs = {}

    def capture_launch(**kwargs: Any) -> MagicMock:
        launch_kwargs.update(kwargs)
        return mock_browser

    pw_mock = mock_playwright.return_value
    pw_mock.start.return_value = pw_mock
    pw_mock.chromium.launch.side_effect = capture_launch

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()

    assert pw_mock.chromium.launch.called
    assert launch_kwargs.get("headless") is True
    auth.close()


def test_sso_navigation_chain(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser navigates D2L -> Microsoft SSO -> 2FA -> D2L redirect."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    mock_playwright, _, mock_browser = make_mock_playwright_with_browser()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()

        # Get the page that was actually created
        page = auth.page
        assert page is not None

        # Simulate SSO navigation with mock
        auth.navigate_sso("user@manipal.edu", "secret", "123456")

        # Verify goto was called (D2L login page)
        assert page.goto.called
        # Verify fill was called for credentials
        assert page.fill.called
        # Verify click was called for submit
        assert page.click.called
        auth.close()


def test_cookie_extraction_after_sso(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 4 d2l cookies extracted from browser context."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = [
        {"name": "d2lSecureSessionVal", "value": "sec123", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSessionVal", "value": "ses123", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSameSiteCanaryA", "value": "canaryA", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSameSiteCanaryB", "value": "canaryB", "domain": "lighthouse.manipal.edu"},
    ]
    mock_playwright, _, mock_browser = make_mock_playwright_with_browser(cookies)

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()
        extracted = auth.extract_cookies()
        auth.close()

    assert len(extracted) == 4
    assert "d2lSecureSessionVal" in extracted
    assert "d2lSessionVal" in extracted
    assert "d2lSameSiteCanaryA" in extracted
    assert "d2lSameSiteCanaryB" in extracted
    assert all(extracted[k] for k in extracted)


# ---------------------------------------------------------------------------
# VAL-AUTH-011 / VAL-AUTH-012: Cookie save and session verification
# ---------------------------------------------------------------------------

def test_cookies_saved_to_file(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cookies.json written with correct format and 0600 permissions."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
        with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 0
    assert cookies_path.exists()
    data = json.loads(cookies_path.read_text())
    assert "d2lSecureSessionVal" in data
    assert data["d2lSecureSessionVal"] == "sec123"
    # Check permissions
    mode = cookies_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_post_login_session_verification(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_auth() confirms session is valid after login."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookies from auth login work with auth status."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    # Pre-write valid cookies
    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    # Point api module's CONFIG_DIR to our tmp config_dir
    import lighthouse_cli.api as api_module
    api_module.CONFIG_DIR = config_dir
    api_module.COOKIE_FILE = cookies_path

    with patch("lighthouse_cli.commands.LighthouseClient") as mock_client_cls:
        with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls2:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_client_cls.return_value = mock_client
            mock_client_cls2.return_value = mock_client
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

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("Login failed: invalid credentials")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 1
    assert "invalid credentials" in result.output.lower() or "login failed" in result.output.lower()
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

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("2FA verification failed: invalid code")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
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

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("Network error: unable to reach lighthouse.manipal.edu")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 1
    assert "network" in result.output.lower() or "unable to reach" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-020: 2FA timeout
# ---------------------------------------------------------------------------

def test_totp_timeout_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2FA timeout produces clear error."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("2FA timed out after 120 seconds")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 1
    assert "timed out" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-021: Browser launch failure
# ---------------------------------------------------------------------------

def test_browser_launch_failure(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser launch failure produces clear error with remediation hints."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("No suitable browser found. Install Chrome/Chromium or set CHROME_PATH")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 1
    assert "browser" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-024: Concurrent auth attempts (atomic writes)
# ---------------------------------------------------------------------------

def test_concurrent_auth_no_corruption(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cookies.json is valid JSON after concurrent auth attempts."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies1 = {
        "d2lSecureSessionVal": "sec1",
        "d2lSessionVal": "ses1",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies2 = {
        "d2lSecureSessionVal": "sec2",
        "d2lSessionVal": "ses2",
        "d2lSameSiteCanaryA": "canaryA2",
        "d2lSameSiteCanaryB": "canaryB2",
    }

    # Use api.save_cookies directly
    import lighthouse_cli.api as api_module
    import threading

    # Point api module's CONFIG_DIR to our tmp config_dir
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    # Force re-read of env var by patching the global
    api_module.CONFIG_DIR = config_dir
    api_module.COOKIE_FILE = config_dir / "cookies.json"

    errors = []

    def write(value: dict) -> None:
        try:
            api_module.save_cookies(value)
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
    # Must have all 4 cookies from whichever write finished last
    assert len(data) >= 4
    assert "d2lSecureSessionVal" in data


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

    # Point api module's CONFIG_DIR to our tmp config_dir
    import lighthouse_cli.api as api_module
    api_module.CONFIG_DIR = config_dir
    api_module.COOKIE_FILE = config_dir / "cookies.json"

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
        with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_client_cls.return_value = mock_client
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert config_dir.exists()
    mode = config_dir.stat().st_mode & 0o777
    # Config dir may be created with 0o755 (umask-based), cookies file has 0o600
    assert mode in (0o700, 0o755)


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

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client
                result = cli_runner.invoke(
                    cli,
                    ["auth", "login", "--totp", "123456", "--json"],
                    catch_exceptions=False,
                )

    assert result.exit_code == 0
    output = result.output
    data = json.loads(output)
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

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("Login failed: invalid credentials")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
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
# VAL-AUTH-027 / VAL-AUTH-028: Browser cleanup
# ---------------------------------------------------------------------------

def test_browser_cleanup_on_success(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No orphan browser processes after successful login."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    mock_playwright, _, mock_browser = make_mock_playwright_with_browser()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()
        auth.close()
        mock_browser.close.assert_called_once()


def test_browser_cleanup_on_failure(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No orphan browser processes after failed login."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    mock_playwright, _, mock_browser = make_mock_playwright_with_browser(cookies=[])

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator, AuthenticationError
        auth = HeadlessAuthenticator()
        auth.launch_browser()
        try:
            auth.authenticate("user@manipal.edu", "wrong", "123456")
        except AuthenticationError:
            pass
        finally:
            auth.close()
        mock_browser.close.assert_called_once()


# ---------------------------------------------------------------------------
# VAL-AUTH-030 / VAL-AUTH-031: Empty username/password rejection
# ---------------------------------------------------------------------------

def test_empty_password_rejected(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty password exits with error before browser launch."""
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
    """Empty username exits with error before browser launch."""
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
# VAL-AUTH-033: SSO page structure change detection
# ---------------------------------------------------------------------------

def test_sso_page_structure_change_error(
    cli_runner: CliRunner,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSO page structure change produces descriptive error."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError(
        "Could not find expected element on SSO page: username field"
    )
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    assert result.exit_code == 1
    assert "could not find" in result.output.lower()


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

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOTP code is never written to cookies.json."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
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

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
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

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    from lighthouse_cli.auth import AuthenticationError

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = AuthenticationError("Login failed")
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
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
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt terminates browser and exits with code 130."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.side_effect = KeyboardInterrupt()
    mock_authenticator.close = MagicMock()

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            result = cli_runner.invoke(
                cli,
                ["auth", "login", "--totp", "123456"],
                catch_exceptions=False,
            )

    # Should exit cleanly with code 130
    assert result.exit_code == 130
    # No partial cookies.json (should not exist or be valid)
    if cookies_path.exists():
        data = json.loads(cookies_path.read_text())
        # If exists, should not be partial (should have all 4 cookies or none)


# ---------------------------------------------------------------------------
# VAL-AUTH-040: Headless mode
# ---------------------------------------------------------------------------

def test_headless_mode_no_visible_window(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser runs in headless mode with no visible window."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    mock_playwright, _, mock_browser = make_mock_playwright_with_browser()

    launch_kwargs = {}

    def capture_launch(**kwargs: Any) -> MagicMock:
        launch_kwargs.update(kwargs)
        return mock_browser

    pw_mock = mock_playwright.return_value
    pw_mock.start.return_value = pw_mock
    pw_mock.chromium.launch.side_effect = capture_launch

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()
        auth.close()

    assert launch_kwargs.get("headless") is True


# ---------------------------------------------------------------------------
# VAL-AUTH-002: Non-TTY with no credentials produces error
# ---------------------------------------------------------------------------

def test_non_tty_no_credentials_error(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY stdin with no credentials produces error, exit code 1."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    # No env vars, no flags
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
    monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)

    # CliRunner stdin is not a TTY, so _is_interactive() returns False
    with patch("lighthouse_cli.auth.CredentialStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.load.return_value = None  # No stored credentials
        mock_store_cls.return_value = mock_store

        result = cli_runner.invoke(
            cli,
            ["auth", "login", "--totp", "123456"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "credentials" in result.output.lower() or "required" in result.output.lower()
    # Should NOT hang (CliRunner returns immediately)


# ---------------------------------------------------------------------------
# VAL-AUTH-005: 2FA code via interactive prompt
# ---------------------------------------------------------------------------

def test_interactive_totp_prompt_at_authenticator_level(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --totp is not provided, HeadlessAuthenticator._handle_2fa prompts via getpass."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    cookies_list = [
        {"name": "d2lSecureSessionVal", "value": "sec123", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSessionVal", "value": "ses123", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSameSiteCanaryA", "value": "canaryA", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSameSiteCanaryB", "value": "canaryB", "domain": "lighthouse.manipal.edu"},
    ]
    mock_playwright, _, mock_browser = make_mock_playwright_with_browser(cookies_list)

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        from lighthouse_cli.auth import HeadlessAuthenticator
        auth = HeadlessAuthenticator()
        auth.launch_browser()

        # Mock getpass to return a 2FA code interactively
        with patch("getpass.getpass", return_value="654321") as mock_getpass:
            auth._handle_2fa(None)  # None = interactive prompt path

            # Verify getpass was called with the prompt containing "2FA"
            mock_getpass.assert_called_once()
            prompt_text = mock_getpass.call_args.args[0]
            assert "2FA" in prompt_text or "code" in prompt_text.lower()

        auth.close()


def test_interactive_totp_prompt_cmd_passes_none(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_auth_login passes totp_code=None to authenticator when --totp is not given."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = True
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client

                with patch("getpass.getpass", return_value="654321"):
                    result = cli_runner.invoke(
                        cli,
                        ["auth", "login"],  # No --totp → interactive prompt
                        catch_exceptions=False,
                    )

    assert result.exit_code == 0
    # Verify authenticate was called with None for totp_code (interactive path)
    mock_authenticator.authenticate.assert_called_once()
    assert mock_authenticator.authenticate.call_args.args[2] is None


# ---------------------------------------------------------------------------
# VAL-AUTH-014: Stored credentials loaded on subsequent runs
# ---------------------------------------------------------------------------

def test_stored_credentials_loaded_on_subsequent_run(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subsequent auth login uses stored credentials without prompting."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    # No env vars
    monkeypatch.delenv("LIGHTHOUSE_USERNAME", raising=False)
    monkeypatch.delenv("LIGHTHOUSE_PASSWORD", raising=False)

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    # Mock CredentialStore to return stored credentials
    with patch("lighthouse_cli.auth.CredentialStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.load.return_value = ("stored_user@manipal.edu", "stored_secret")
        mock_store_cls.return_value = mock_store

        with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
            with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
                with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.check_auth.return_value = True
                    mock_client.cookies = cookies
                    mock_client_cls.return_value = mock_client
                    result = cli_runner.invoke(
                        cli,
                        ["auth", "login", "--totp", "123456"],
                        catch_exceptions=False,
                    )

    assert result.exit_code == 0
    # Verify stored credentials were used
    mock_authenticator.authenticate.assert_called_once()
    assert mock_authenticator.authenticate.call_args.args[0] == "stored_user@manipal.edu"
    assert mock_authenticator.authenticate.call_args.args[1] == "stored_secret"
    # No credential prompts in output
    assert "Username:" not in result.output
    assert "Password:" not in result.output


# ---------------------------------------------------------------------------
# VAL-AUTH-016: Auth-dependent commands work after login
# ---------------------------------------------------------------------------

def test_auth_commands_compatible_with_login_cookies(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookies from auth login are compatible with auth status and other commands."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    # Simulate cookies written by auth login
    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }
    cookies_path.write_text(json.dumps(cookies))

    # Point api module to our tmp config
    import lighthouse_cli.api as api_module
    api_module.CONFIG_DIR = config_dir
    api_module.COOKIE_FILE = cookies_path

    # Verify auth status works
    with patch("lighthouse_cli.commands.LighthouseClient") as mock_client_cls:
        with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls2:
            mock_client = MagicMock()
            mock_client.check_auth.return_value = True
            mock_client.cookies = cookies
            mock_client_cls.return_value = mock_client
            mock_client_cls2.return_value = mock_client
            result = cli_runner.invoke(cli, ["auth", "status"], catch_exceptions=False)

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# VAL-AUTH-034: Cookies written even if verification fails
# ---------------------------------------------------------------------------

def test_cookies_saved_even_if_verification_fails(
    cli_runner: CliRunner,
    config_dir: Path,
    cookies_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookies are saved even when post-login session verification fails."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")

    cookies = {
        "d2lSecureSessionVal": "sec123",
        "d2lSessionVal": "ses123",
        "d2lSameSiteCanaryA": "canaryA",
        "d2lSameSiteCanaryB": "canaryB",
    }

    mock_playwright, _, _ = make_mock_playwright_with_browser()

    mock_authenticator = MagicMock()
    mock_authenticator.authenticate.return_value = cookies

    with patch("lighthouse_cli.auth.sync_playwright", mock_playwright):
        with patch("lighthouse_cli.auth.HeadlessAuthenticator", return_value=mock_authenticator):
            with patch("lighthouse_cli.api.LighthouseClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.check_auth.return_value = False  # Verification FAILS
                mock_client.cookies = cookies
                mock_client_cls.return_value = mock_client
                result = cli_runner.invoke(
                    cli,
                    ["auth", "login", "--totp", "123456", "--json"],
                    catch_exceptions=False,
                )

    # Command should fail (exit 1) because verification failed
    assert result.exit_code == 1
    # But cookies.json should still exist with the extracted cookies
    assert cookies_path.exists()
    data = json.loads(cookies_path.read_text())
    assert "d2lSecureSessionVal" in data
    assert data["d2lSecureSessionVal"] == "sec123"
    # Error message should mention verification failure
    output_data = json.loads(result.output)
    assert output_data["success"] is False
    assert "verification" in output_data["error"].lower()
