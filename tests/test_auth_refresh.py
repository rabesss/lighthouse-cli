"""CLI and policy coverage for browser-based authentication refresh."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from lighthouse_cli import auth
from lighthouse_cli.cli import cli
from lighthouse_cli.config import COOKIE_NAMES


def test_auth_refresh_cli_forwards_cdp_port_and_json() -> None:
    with patch("lighthouse_cli.cli.cmd_auth_refresh", return_value=0) as command:
        result = CliRunner().invoke(
            cli,
            ["auth", "refresh", "--cdp-port", "9222", "--json"],
        )

    assert result.exit_code == 0
    command.assert_called_once_with(cdp_port="9222", json_output=True)


def test_auth_refresh_rejects_invalid_cdp_port_before_command() -> None:
    result = CliRunner().invoke(
        cli,
        ["auth", "refresh", "--cdp-port", "70000", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "success": False,
        "error": "CDP port must be an integer from 1 to 65535",
    }


def test_auth_refresh_rejects_non_numeric_cdp_port_as_command_error() -> None:
    result = CliRunner().invoke(
        cli,
        ["auth", "refresh", "--cdp-port", "not-a-port", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["success"] is False


def test_auth_refresh_preflights_extracts_and_persists(monkeypatch) -> None:
    calls: list[object] = []
    cookies = {name: f"value-{index}" for index, name in enumerate(COOKIE_NAMES)}

    class FakeStore:
        def preflight(self) -> None:
            calls.append("preflight")

    def fake_extract(port: int | None) -> dict[str, str]:
        calls.append(("extract", port))
        return cookies

    def fake_persist(received: dict[str, str], **kwargs) -> int:
        calls.append(("persist", received, kwargs))
        return 0

    monkeypatch.setattr(auth, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(auth, "CredentialStore", FakeStore)
    monkeypatch.setattr(auth, "refresh_auth_from_browser", fake_extract)
    monkeypatch.setattr(auth, "_persist_check_report", fake_persist)

    assert auth.cmd_auth_refresh(9222, json_output=True) == 0
    assert calls[0:2] == ["preflight", ("extract", 9222)]
    assert calls[2][0:2] == ("persist", cookies)
    assert calls[2][2]["success_message"] == "Auth refreshed and verified."


def test_auth_refresh_missing_cookies_returns_json_without_persisting(
    monkeypatch, capsys
) -> None:
    class FakeStore:
        def preflight(self) -> None:
            return None

    monkeypatch.setattr(auth, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(auth, "CredentialStore", FakeStore)
    monkeypatch.setattr(
        auth,
        "refresh_auth_from_browser",
        lambda _port: {"d2lSessionVal": "present"},
    )
    persist = patch("lighthouse_cli.auth._persist_check_report")
    with persist as persist_mock:
        rc = auth.cmd_auth_refresh(9222, json_output=True)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "missing required D2L cookies" in payload["error"]
    assert "present" not in payload["error"]
    persist_mock.assert_not_called()
