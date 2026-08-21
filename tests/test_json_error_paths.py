"""Regression tests: --json commands must emit parseable JSON on stdout even on error paths."""

from __future__ import annotations

import json

from click.testing import CliRunner

from lighthouse_cli.cli import cli


def test_auth_status_json_no_cookies_emits_json_error(tmp_path, monkeypatch) -> None:
    """With --json, even the no-cookies error path must print a JSON object to
    stdout (never an empty stdout / stderr-only human message)."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
    result = CliRunner().invoke(cli, ["auth", "status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert "error" in payload
