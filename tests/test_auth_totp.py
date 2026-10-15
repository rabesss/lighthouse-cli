"""Tests for ``auth totp-setup`` / ``auth totp-remove`` (the stored authenticator).

Secrets arrive on stdin or a hidden prompt, are bound to the saved username,
and never reach stdout, stderr or an error message.
"""

from __future__ import annotations

import getpass
import io
import json
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lighthouse_cli import auth as auth_mod
from lighthouse_cli import session_renewal
from lighthouse_cli.cli import cli
from lighthouse_cli.config import save_cookies
from lighthouse_cli.credential_store import CredentialStore
from lighthouse_cli.session_renewal import renewal_lock
from lighthouse_cli.totp import SECRET_ERROR_MESSAGES, parse_totp_secret, time_step

USERNAME = "student@example.edu"
PASSWORD = "pw-sentinel-Q7"  # pragma: allowlist secret
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
OTHER_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # pragma: allowlist secret  gitleaks:allow
URI = f"otpauth://totp/Microsoft:{USERNAME}?secret={SECRET}&issuer=Microsoft"
T0 = 1_800_000_015.0  # 15 s into a 30 s step
STEP0 = int(T0 // 30)


class _UnreadableStdin(io.BytesIO):
    """A stdin that fails the test when read (Click probes it with ``read(0)``)."""

    def read(self, size: int | None = -1) -> bytes:
        if size == 0:
            return b""
        raise AssertionError("stdin was read")

    def read1(self, size: int = -1) -> bytes:
        raise AssertionError("stdin was read")

    def readline(self, size: int | None = -1) -> bytes:
        raise AssertionError("stdin was read")

    def readinto(self, buffer: Any) -> int:
        raise AssertionError("stdin was read")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    monkeypatch.setattr(session_renewal, "_clock", lambda: T0)
    return d


@pytest.fixture
def saved(config_dir: Path) -> CredentialStore:
    store = CredentialStore()
    store.save(USERNAME, PASSWORD)
    return store


def _setup(runner: CliRunner, stdin: str | io.BytesIO, *args: str) -> Any:
    return runner.invoke(cli, ["auth", "totp-setup", *args], input=stdin, catch_exceptions=False)


def _assert_no_secret(result: Any) -> None:
    output = result.stdout + result.stderr
    for sentinel in (SECRET, OTHER_SECRET, PASSWORD, "JBSWY3DP"):
        assert sentinel not in output


# ---------------------------------------------------------------------------
# totp-setup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdin", [f"{SECRET}\n", f"{URI}\n", "jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp\n"])
def test_setup_from_stdin_binds_the_secret_to_the_saved_username(
    runner: CliRunner, saved: CredentialStore, stdin: str,
) -> None:
    result = _setup(runner, stdin, "--json")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"success": True, "configured": True}
    stored = saved.load_totp()
    assert stored is not None
    assert stored.username == USERNAME
    assert stored.params == parse_totp_secret(SECRET)
    _assert_no_secret(result)


def test_setup_human_output_names_the_next_step(runner: CliRunner, saved: CredentialStore) -> None:
    result = _setup(runner, f"{SECRET}\n")

    assert result.exit_code == 0
    assert result.stdout == "Authenticator secret stored. Next: lighthouse auth login\n"
    _assert_no_secret(result)


def test_setup_reserves_the_current_step(runner: CliRunner, saved: CredentialStore) -> None:
    """The code typed while registering the app is never sent again."""
    assert _setup(runner, f"{SECRET}\n", "--json").exit_code == 0

    stored = saved.load_totp()
    assert stored is not None
    assert stored.last_step == STEP0 == time_step(T0, 30)


@pytest.mark.parametrize("ahead", [2, 100])
def test_setup_with_the_same_key_keeps_a_newer_step(runner: CliRunner, saved: CredentialStore, ahead: int) -> None:
    """Even a step a fast clock reserved: forgetting it could resend an accepted code."""
    saved.save_totp(USERNAME.upper(), parse_totp_secret(SECRET), last_step=STEP0 + ahead)

    assert _setup(runner, f"{SECRET}\n", "--json").exit_code == 0

    stored = saved.load_totp()
    assert stored is not None
    assert stored.last_step == STEP0 + ahead


def test_setup_with_a_new_key_starts_from_the_current_step(runner: CliRunner, saved: CredentialStore) -> None:
    saved.save_totp(USERNAME, parse_totp_secret(OTHER_SECRET), last_step=STEP0 + 5)

    assert _setup(runner, f"{SECRET}\n", "--json").exit_code == 0

    stored = saved.load_totp()
    assert stored is not None
    assert stored.params == parse_totp_secret(SECRET)
    assert stored.last_step == STEP0


def test_setup_clears_the_breaker(runner: CliRunner, saved: CredentialStore, config_dir: Path) -> None:
    state = config_dir / "relogin_state.json"
    state.write_text(json.dumps({"v": 1, "blocked": True, "category": "code_rejected"}))

    assert _setup(runner, f"{SECRET}\n", "--json").exit_code == 0

    assert not state.exists()


def test_setup_on_a_terminal_reads_a_hidden_prompt(
    runner: CliRunner, saved: CredentialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[tuple[str, bool]] = []

    def hidden(prompt: str, stream: Any = None) -> str:
        prompts.append((prompt, stream is sys.stderr))
        return SECRET

    monkeypatch.setattr(auth_mod, "_is_interactive", lambda: True)
    monkeypatch.setattr(getpass, "getpass", hidden)

    result = _setup(runner, _UnreadableStdin(), "--json")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"success": True, "configured": True}
    assert "input is hidden" in result.stderr
    # The prompt goes to stderr, keeping stdout JSON-only.
    assert prompts == [("Secret: ", True)]
    assert saved.load_totp() is not None


def test_setup_refuses_echoed_input_before_reading_anything(
    runner: CliRunner, saved: CredentialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``getpass`` warns and then echoes when it cannot hide input; that
    warning becomes the fixed error before stdin is touched."""
    monkeypatch.setattr(auth_mod, "_is_interactive", lambda: True)
    monkeypatch.setattr(getpass, "getpass", getpass.fallback_getpass)

    result = _setup(runner, _UnreadableStdin(), "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"success": False, "error": auth_mod._HIDDEN_INPUT_UNAVAILABLE}
    assert saved.load_totp() is None


def test_setup_terminal_eof_reports_an_empty_secret(
    runner: CliRunner, saved: CredentialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod, "_is_interactive", lambda: True)
    monkeypatch.setattr(getpass, "getpass", MagicMock(side_effect=EOFError))

    result = _setup(runner, _UnreadableStdin(), "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"success": False, "error": "The authenticator secret is empty."}


@pytest.mark.parametrize(
    "stdin",
    [
        "NOT-A-SECRET-SENTINEL!!\n",
        "\n",
        "JBSWY3DP\n",
        f"otpauth://hotp/x?secret={SECRET}&counter=1\n",
        f"otpauth://totp/x?secret={SECRET}&algorithm=SHA256\n",
        f"otpauth://totp/x?secret={SECRET}&secret={OTHER_SECRET}\n",
    ],
)
def test_invalid_secret_gets_a_fixed_error_that_never_echoes_it(
    runner: CliRunner, saved: CredentialStore, stdin: str,
) -> None:
    result = _setup(runner, stdin, "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["error"] in SECRET_ERROR_MESSAGES
    assert "SENTINEL" not in result.stdout + result.stderr
    _assert_no_secret(result)
    assert saved.load_totp() is None


def test_setup_without_saved_credentials_reads_nothing(runner: CliRunner, config_dir: Path) -> None:
    result = _setup(runner, _UnreadableStdin(), "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"success": False, "error": auth_mod._NO_SAVED_CREDENTIALS}
    assert not (config_dir / "authenticator.json").exists()


def test_setup_busy_stores_nothing(
    runner: CliRunner, saved: CredentialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_renewal, "LOCK_TIMEOUT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with renewal_lock():
            held.set()
            release.wait(10)

    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(10)
    try:
        result = _setup(runner, f"{SECRET}\n", "--json")
    finally:
        release.set()
        worker.join(10)

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"success": False, "error": auth_mod._SIGN_IN_BUSY}
    assert saved.load_totp() is None


def test_setup_without_a_key_source_fails_before_reading(
    runner: CliRunner, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
    monkeypatch.setattr("lighthouse_cli.credential_store._load_keyring_module", lambda: None)

    result = _setup(runner, _UnreadableStdin(), "--json")

    assert result.exit_code == 1
    assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in json.loads(result.stdout)["error"]


# ---------------------------------------------------------------------------
# totp-remove
# ---------------------------------------------------------------------------


def test_remove_deletes_only_the_authenticator(
    runner: CliRunner, saved: CredentialStore, config_dir: Path,
) -> None:
    saved.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=STEP0)
    save_cookies({"d2lSessionVal": "s", "d2lSecureSessionVal": "x"})
    (config_dir / "relogin_state.json").write_text(json.dumps({"v": 1, "blocked": True}))

    first = runner.invoke(cli, ["auth", "totp-remove", "--json"], catch_exceptions=False)
    second = runner.invoke(cli, ["auth", "totp-remove", "--json"], catch_exceptions=False)

    assert (first.exit_code, json.loads(first.stdout)) == (0, {"success": True, "removed": True})
    assert (second.exit_code, json.loads(second.stdout)) == (0, {"success": True, "removed": False})
    assert saved.load_totp() is None
    assert saved.load() == (USERNAME, PASSWORD)
    assert saved.cookie_file.exists()
    assert not (config_dir / "relogin_state.json").exists()


def test_remove_human_output(runner: CliRunner, saved: CredentialStore) -> None:
    saved.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=None)

    first = runner.invoke(cli, ["auth", "totp-remove"], catch_exceptions=False)
    second = runner.invoke(cli, ["auth", "totp-remove"], catch_exceptions=False)

    assert first.stdout == "Stored authenticator removed.\n"
    assert second.stdout == "No stored authenticator was configured.\n"


def test_remove_busy_keeps_the_authenticator(
    runner: CliRunner, saved: CredentialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=None)
    monkeypatch.setattr(session_renewal, "LOCK_TIMEOUT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with renewal_lock():
            held.set()
            release.wait(10)

    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(10)
    try:
        result = runner.invoke(cli, ["auth", "totp-remove", "--json"], catch_exceptions=False)
    finally:
        release.set()
        worker.join(10)

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"success": False, "error": auth_mod._SIGN_IN_BUSY}
    assert saved.load_totp() is not None


def test_auth_help_lists_the_authenticator_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["auth", "--help"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "totp-setup" in result.stdout
    assert "totp-remove" in result.stdout


def test_setup_help_discloses_the_trade_off(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["auth", "totp-setup", "--help"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Trade-off" in result.stdout
    assert "never accepted as a command-line argument" in " ".join(result.stdout.split())


def test_login_help_explains_the_stored_authenticator(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["auth", "login", "--help"], catch_exceptions=False)

    assert result.exit_code == 0
    text = " ".join(result.stdout.split())
    assert "unless a stored authenticator is set up" in text
    assert "With a stored authenticator (lighthouse auth totp-setup)" in text
