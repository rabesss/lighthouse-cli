"""Read-only authentication behavior used by dry-run commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import lighthouse_cli.api as api
from lighthouse_cli.api import BASE_URL, LighthouseClient, SessionExpiredError
from lighthouse_cli.config import load_cookies, save_cookies
from lighthouse_cli.credential_store import is_sealed_document


@pytest.fixture
def cookie_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use an isolated credential directory for each read-only test."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(tmp_path))
    return tmp_path / "cookies.json"


def _cookies() -> dict[str, str]:
    return {
        "d2lSameSiteCanaryA": "canary-a",
        "d2lSameSiteCanaryB": "canary-b",
        "d2lSecureSessionVal": "secure-session",
        "d2lSessionVal": "session",
    }


def _authenticated_client() -> tuple[LighthouseClient, MagicMock]:
    """Create a client with a request double, avoiding any live network."""
    client = LighthouseClient(read_only_auth=True)
    session = MagicMock()
    client._session = session
    return client, session


def test_read_only_load_reads_sealed_cookies_without_writing(
    cookie_path: Path,
) -> None:
    cookies = _cookies()
    save_cookies(cookies)
    before = cookie_path.read_bytes()

    assert load_cookies(read_only=True) == cookies
    assert cookie_path.read_bytes() == before
    assert is_sealed_document(json.loads(before))


def test_read_only_load_ignores_legacy_plaintext_byte_for_byte(
    cookie_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = {
        "cookies": _cookies(),
        "extracted_at": "2026-01-01T00:00:00+00:00",
    }
    original = json.dumps(legacy, separators=(",", ":")).encode("utf-8")
    cookie_path.write_bytes(original)

    assert load_cookies(read_only=True) == {}
    assert cookie_path.read_bytes() == original
    warning = capsys.readouterr().err
    assert "read-only mode" in warning
    for value in _cookies().values():
        assert value not in warning


def test_read_only_client_legacy_cookies_fail_closed_without_request(
    cookie_path: Path,
) -> None:
    original = json.dumps({"cookies": _cookies()}).encode("utf-8")
    cookie_path.write_bytes(original)
    client, session = _authenticated_client()

    with pytest.raises(SessionExpiredError):
        client._request("GET", f"{BASE_URL}/d2l/api/versions/")

    assert cookie_path.read_bytes() == original
    session.request.assert_not_called()


def test_read_only_client_does_not_refresh_or_save_after_get_401() -> None:
    save_cookies(_cookies())
    client, session = _authenticated_client()
    response = MagicMock(status_code=401, headers={})
    session.request.return_value = response

    with patch.object(api, "refresh_auth_from_browser") as refresh, \
            patch.object(api, "save_cookies") as save:
        with pytest.raises(SessionExpiredError):
            client._request("GET", f"{BASE_URL}/d2l/api/versions/")

    session.request.assert_called_once()
    refresh.assert_not_called()
    save.assert_not_called()


def test_read_only_client_does_not_refresh_or_save_after_head_401() -> None:
    save_cookies(_cookies())
    client, session = _authenticated_client()
    response = MagicMock(status_code=401, headers={})
    session.request.return_value = response

    with patch.object(api, "refresh_auth_from_browser") as refresh, \
            patch.object(api, "save_cookies") as save:
        with pytest.raises(SessionExpiredError):
            client._request("HEAD", f"{BASE_URL}/d2l/api/versions/")

    session.request.assert_called_once()
    refresh.assert_not_called()
    save.assert_not_called()


def test_normal_client_still_migrates_legacy_cookies(
    cookie_path: Path,
) -> None:
    legacy = {"cookies": {"d2lSessionVal": "legacy-session"}}
    cookie_path.write_text(json.dumps(legacy), encoding="utf-8")

    client = LighthouseClient()

    assert client.cookies == {"d2lSessionVal": "legacy-session"}
    assert is_sealed_document(json.loads(cookie_path.read_text(encoding="utf-8")))


def test_read_only_client_passes_mode_to_cookie_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = MagicMock(return_value={})
    monkeypatch.setattr(api, "load_cookies", loader)
    client = LighthouseClient(read_only_auth=True)

    assert client.cookies == {}
    loader.assert_called_once_with(read_only=True)
