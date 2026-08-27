"""Secret-absence suite: sentinel-based proof that secrets never leak.

Runs real login/verify flows with sentinel secret values and proves those
sentinels appear in NO on-disk artifact, stdout, stderr, traceback, or
exception message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests

from lighthouse_cli.config import COOKIE_NAMES
from lighthouse_cli.ms_auth import MfaPendingError, MicrosoftSSOClient, MicrosoftSSOError

# ---------------------------------------------------------------------------
# Sentinels — unique strings planted in every secret-bearing position.
# ---------------------------------------------------------------------------

S_PASSWORD = "SENTINEL-PASSWORD-q7x4k"
S_TOTP = "SENTINEL-TOTP-3m9vd"
S_SAML = "SENTINEL-SAMLRESPONSE-z8h2n-BASE64PADDING-BASE64PADDING-BASE64PADDING"
S_COOKIE_PREFIX = "SENTINEL-COOKIE-"
S_FLOWTOKEN = "SENTINEL-FLOWTOKEN-f1j6r"
S_CTX = "SENTINEL-SCTX-c5t8w"

ALL_SENTINELS = [
    S_PASSWORD,
    S_TOTP,
    S_SAML,
    S_FLOWTOKEN,
    S_CTX,
    *[f"{S_COOKIE_PREFIX}{name}" for name in COOKIE_NAMES],
]

BASE = "https://lighthouse.manipal.edu"
MS_BASE = "https://login.microsoftonline.com"
LOGIN_INIT_URL = f"{BASE}/d2l/lp/auth/saml/login"
MS_SSO_URL = f"{MS_BASE}/tenant/saml2?SAMLRequest=z"
CREDS_POST_URL = f"{MS_BASE}/common/login"
BEGIN_URL = f"{MS_BASE}/common/SAS/BeginAuth"
END_URL = f"{MS_BASE}/common/SAS/EndAuth"
PROCESS_URL = f"{MS_BASE}/common/SAS/ProcessAuth"
ACS_URL = f"{BASE}/d2l/lp/auth/saml/consume"


def config_html() -> str:
    fields = [
        '"sFT": "pub-ft"',
        '"sCtx": "pub-ctx"',
        f'"urlPost": "{CREDS_POST_URL}"',
        '"canary": "pub-canary"',
    ]
    return (
        "<html><body><script>\n$Config = {\n"
        + ",\n".join(fields)
        + "\n};\n</script></body></html>"
    )


def mfa_html() -> str:
    fields = [
        '"pgid": "ConvergedTFA"',
        f'"sFT": "{S_FLOWTOKEN}"',
        f'"sCtx": "{S_CTX}"',
        '"canary": "mfa-canary"',
        f'"urlBeginAuth": "{BEGIN_URL}"',
        f'"urlEndAuth": "{END_URL}"',
        f'"urlPost": "{PROCESS_URL}"',
        '"sFTName": "flowToken"',
        '"oPerAuthPollingInterval": {"PhoneAppOTP": 0.5}',
        '"arrUserProofs": [{"authMethodId": "PhoneAppOTP", "display": "Android", "data": "+91 ***1234", "isDefault": true}]',
    ]
    return (
        "<html><body><script>\n$Config = {\n"
        + ",\n".join(fields)
        + "\n};\n</script></body></html>"
    )


SAML_HTML = (
    f'<html><body><form action="{ACS_URL}">'
    f'<input type="hidden" name="SAMLResponse" value="{S_SAML}">'
    "</form></body></html>"
)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        html: str = "",
        url: str = "",
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = html
        self.url = url
        self.headers = headers or {}
        self._json = json_data

    def json(self) -> dict[str, Any]:
        return self._json or {}


class ScriptedSession:
    def __init__(self) -> None:
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}
        self._queue: list[Any] = []

    def enqueue(self, *items: Any) -> None:
        self._queue.extend(items)

    def _next(self, method: str, url: str) -> Any:
        if not self._queue:
            raise AssertionError(f"Script exhausted at {method} {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url)

    def close(self) -> None:
        pass


@pytest.fixture
def sealed_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir(parents=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", "absence-passphrase")
    return d


def assert_no_sentinels_on_disk(config_dir: Path) -> None:
    """Every file under the config dir must be free of every sentinel."""
    blobs: list[tuple[Path, bytes]] = []
    for path in sorted(config_dir.rglob("*")):
        if path.is_file():
            blobs.append((path, path.read_bytes()))
    assert blobs, "expected at least one artifact to scan"
    for path, blob in blobs:
        for sentinel in ALL_SENTINELS:
            assert sentinel.encode() not in blob, f"secret leaked into {path.name}"
            # Cookie values are also embedded URL-encoded inside form posts
            # saved in checkpoints; check the encoded form too.
            import urllib.parse

            assert urllib.parse.quote(sentinel).encode() not in blob


# ---------------------------------------------------------------------------
# Full-flow leak scans
# ---------------------------------------------------------------------------


class TestNoSecretsOnDiskOrOutput:
    def test_full_login_flow_leaves_no_plaintext_secrets(
        self, sealed_dir: Path
    ) -> None:
        """login() with sentinels everywhere → no sentinel on disk."""
        # Pre-existing artifacts (credentials + cookies) must also stay sealed.
        from lighthouse_cli.auth import CredentialStore

        CredentialStore().save("user@manipal.edu", S_PASSWORD)
        from lighthouse_cli.config import save_cookies

        save_cookies({name: f"{S_COOKIE_PREFIX}{name}" for name in COOKIE_NAMES})

        session = ScriptedSession()
        session.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(), url=PROCESS_URL),
            FakeResponse(json_data={"Success": True, "SessionId": "sid"}),
            FakeResponse(json_data={"Success": True}),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>home</html>", url=f"{BASE}/d2l/home"),
        )
        for name in COOKIE_NAMES:
            session.cookies.set(name, f"{S_COOKIE_PREFIX}{name}", domain="lighthouse.manipal.edu")

        with patch("requests.Session", return_value=session):
            client = MicrosoftSSOClient()
            try:
                cookies = client.login("user@manipal.edu", S_PASSWORD, S_TOTP)
            finally:
                client.close()

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert_no_sentinels_on_disk(sealed_dir)

    def test_deferred_checkpoint_and_verify_leave_no_plaintext_secrets(
        self, sealed_dir: Path
    ) -> None:
        """The resumable checkpoint (cookies + flow tokens + URLs) is sealed."""
        # A pre-existing sealed credential also proves sealing survives flows.
        from lighthouse_cli.auth import CredentialStore

        CredentialStore().save("user@manipal.edu", S_PASSWORD)

        session = ScriptedSession()
        session.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(), url=PROCESS_URL),
            FakeResponse(json_data={"Success": True, "SessionId": "sid"}),
        )
        session.cookies.set("esctx", "pub-esctx", domain=".login.microsoftonline.com")

        client = MicrosoftSSOClient()
        with patch("requests.Session", return_value=session):
            try:
                with pytest.raises(MfaPendingError):
                    client.login("user@manipal.edu", S_PASSWORD, defer_mfa_to_pending=True)
            finally:
                client.close()

        assert_no_sentinels_on_disk(sealed_dir)

        # Verify completes from the sealed checkpoint; still nothing on disk.
        verify_session = ScriptedSession()
        verify_session.enqueue(
            FakeResponse(json_data={"Success": True}),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>home</html>", url=f"{BASE}/d2l/home"),
        )
        for name in COOKIE_NAMES:
            verify_session.cookies.set(
                name, f"{S_COOKIE_PREFIX}{name}", domain="lighthouse.manipal.edu"
            )
        with patch("requests.Session", return_value=verify_session):
            client = MicrosoftSSOClient()
            try:
                cookies = client.complete_mfa_pending(S_TOTP)
            finally:
                client.close()
        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert_no_sentinels_on_disk(sealed_dir)


class TestNoSecretsInErrorsAndOutput:
    def test_exception_messages_carry_no_secret_values(self, sealed_dir: Path) -> None:
        """Wrong-code failure messages never contain the code or flow tokens."""
        session = ScriptedSession()
        session.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(), url=PROCESS_URL),
            FakeResponse(json_data={"Success": True, "SessionId": "sid"}),
        )
        with patch("requests.Session", return_value=session):
            client = MicrosoftSSOClient()
            try:
                with pytest.raises(MfaPendingError):
                    client.login("user@manipal.edu", S_PASSWORD, defer_mfa_to_pending=True)
            finally:
                client.close()

        bad = ScriptedSession()
        bad.enqueue(
            FakeResponse(json_data={"Success": False, "Retry": False, "ResultValue": "WrongCode"}),
        )
        with patch("requests.Session", return_value=bad):
            client2 = MicrosoftSSOClient()
            try:
                with pytest.raises(MicrosoftSSOError) as exc_info:
                    client2.complete_mfa_pending(S_TOTP)
            finally:
                client2.close()

        rendered = str(exc_info.value)
        for sentinel in ALL_SENTINELS:
            assert sentinel not in rendered
        assert exc_info.value.recovery is not None
        for sentinel in ALL_SENTINELS:
            assert sentinel not in (exc_info.value.recovery or "")

    def test_cli_error_output_carries_no_secret_values(
        self, sealed_dir: Path, cli_runner: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing auth login prints no sentinel to stdout or stderr.

        Hermetic: requests.Session is patched with an immediately failing
        session so no real network I/O happens with sentinel credentials.
        """
        from lighthouse_cli.cli import cli as root_cli

        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", S_PASSWORD)

        dead = ScriptedSession()
        dead.enqueue(requests.ConnectionError("boom"))

        with patch("requests.Session", return_value=dead):
            result = cli_runner.invoke(
                root_cli,
                ["auth", "login", "--totp", S_TOTP, "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        for sentinel in ALL_SENTINELS:
            assert sentinel not in combined

    def test_login_plan_repr_carries_no_totp_value(self) -> None:
        """LoginPlan marks its secret-bearing field repr=False (P0 redaction)."""
        from lighthouse_cli.auth import LoginPlan

        plan = LoginPlan("fresh", S_TOTP, False, False)
        rendered = repr(plan)
        assert "LoginPlan" in rendered
        for sentinel in ALL_SENTINELS:
            assert sentinel not in rendered
        assert str(plan) == rendered
        # The value itself is still there — only its representation is masked.
        assert plan.totp_code == S_TOTP

    def test_traceback_path_carries_no_secret_values(
        self, sealed_dir: Path
    ) -> None:
        """Even a raw traceback of the failing call shows no sentinel values."""
        # Create a pending checkpoint first (defer login), then break the network.
        session = ScriptedSession()
        session.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(), url=PROCESS_URL),
            FakeResponse(json_data={"Success": True, "SessionId": "sid"}),
        )
        with patch("requests.Session", return_value=session):
            client = MicrosoftSSOClient()
            try:
                with pytest.raises(MfaPendingError):
                    client.login("user@manipal.edu", S_PASSWORD, defer_mfa_to_pending=True)
            finally:
                client.close()

        dead = ScriptedSession()
        dead.enqueue(requests.ConnectionError("boom"))
        with patch("requests.Session", return_value=dead):
            client2 = MicrosoftSSOClient()
            try:
                # exc_info MUST be captured inside the raises block — after
                # it exits sys.exc_info() is cleared and format_exc() is
                # vacuously "NoneType: None".
                with pytest.raises(requests.ConnectionError) as exc_info:
                    client2.complete_mfa_pending(S_TOTP)
            finally:
                client2.close()

        import traceback

        tb = "".join(traceback.format_exception(exc_info.value))
        for sentinel in ALL_SENTINELS:
            assert sentinel not in tb
        assert tb != "NoneType: None\n"
