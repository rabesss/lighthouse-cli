"""Black-box characterization tests for MicrosoftSSOClient.

These tests pin down the CURRENT observable behavior of ``login()`` and
``complete_mfa_pending()`` — scripted HTTP responses, HTML fixtures as plain
strings, no internal method calls — so the state-machine rewrite preserves
behavior.  Secrets are referenced by sentinel values and are never printed,
repr'd, or asserted by value (key-presence assertions only).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
import requests

from lighthouse_cli.config import COOKIE_NAMES
from lighthouse_cli.ms_auth import (
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
)

# ---------------------------------------------------------------------------
# Sentinels (never asserted by value; used only as fixture payloads)
# ---------------------------------------------------------------------------

USERNAME = "sentinel.user@manipal.edu"
PASSWORD = "sentinel-password-value"
TOTP_CODE = "654321"
SAML_TOKEN = "SENTINEL-SAMLRESPONSE-BASE64-VALUE"

BASE = "https://lighthouse.manipal.edu"
MS_BASE = "https://login.microsoftonline.com"
LOGIN_INIT_URL = f"{BASE}/d2l/lp/auth/saml/login"
MS_SSO_URL = f"{MS_BASE}/tenant-id/saml2?SAMLRequest=z"
CREDS_POST_URL = f"{MS_BASE}/common/login"
BEGIN_URL = f"{MS_BASE}/common/SAS/BeginAuth"
END_URL = f"{MS_BASE}/common/SAS/EndAuth"
PROCESS_URL = f"{MS_BASE}/common/SAS/ProcessAuth"
ACS_URL = f"{BASE}/d2l/lp/auth/saml/consume"
MFA_PAGE_URL = f"{MS_BASE}/common/SAS/ProcessAuth?session=1"


# ---------------------------------------------------------------------------
# HTML fixtures (plain strings)
# ---------------------------------------------------------------------------


def config_html(extra_fields: str = "", url_get_credential_type: bool = False) -> str:
    fields = [
        '"sFT": "FLOW-TOKEN-1"',
        '"sCtx": "CTX-TOKEN-1"',
        f'"urlPost": "{CREDS_POST_URL}"',
        '"canary": "PAGE-CANARY-1"',
        '"apiCanary": "API-CANARY-1"',
        '"sessionId": "SESSION-ID-1"',
        '"correlationId": "CORR-ID-1"',
    ]
    if url_get_credential_type:
        fields.append(f'"urlGetCredentialType": "{MS_BASE}/common/GetCredentialType"')
    if extra_fields:
        fields.append(extra_fields)
    return (
        "<html><head><title>Sign in</title></head><body><script>\n"
        "$Config = {\n" + ",\n".join(fields) + "\n};\n</script></body></html>"
    )


def mfa_html(auth_method_id: str = "PhoneAppOTP", display: str = "Android") -> str:
    fields = [
        '"pgid": "ConvergedTFA"',
        '"sFT": "MFA-FLOW-TOKEN"',
        '"sCtx": "MFA-CTX-TOKEN"',
        '"canary": "MFA-CANARY"',
        f'"urlBeginAuth": "{BEGIN_URL}"',
        f'"urlEndAuth": "{END_URL}"',
        f'"urlPost": "{PROCESS_URL}"',
        '"sFTName": "flowToken"',
        f'"sPOST_Username": "{USERNAME}"',
        '"oPerAuthPollingInterval": {"PhoneAppOTP": 0.5, "PhoneAppNotification": 0.5, "OneWaySMS": 0.5}',
        '"arrUserProofs": ['
        + "{"
        + f'"authMethodId": "{auth_method_id}", "display": "{display}", '
        + '"data": "+91 ***1234", "isDefault": true}'
        + "]",
    ]
    return (
        "<html><head><title>Verify</title></head><body><script>\n"
        "$Config = {\n" + ",\n".join(fields) + "\n};\n</script></body></html>"
    )


LEGACY_MFA_HTML = (
    "<html><head><title>Verify</title></head><body>"
    '<div id="idDiv_SAOTCC_Description">Enter code</div>'
    f'<form action="{PROCESS_URL}">'
    '<input type="hidden" name="sFT" value="LEGACY-FLOW-TOKEN">'
    '<input type="hidden" name="sCtx" value="LEGACY-CTX">'
    '<input type="text" name="otc">'
    "</form></body></html>"
)

SAML_HTML = (
    "<html><body>"
    f'<form method="POST" action="{ACS_URL}">'
    f'<input type="hidden" name="SAMLResponse" value="{SAML_TOKEN}">'
    '<input type="hidden" name="RelayState" value="' + BASE + '/d2l/home">'
    "</form></body></html>"
)

ERROR_HTML = (
    "<html><head><title>Sign in error</title></head><body><script>\n"
    "$Config = {\n"
    '"pgid": "ConvergedError",\n'
    '"serverError": "50126",\n'
    '"sErrTxt": "Invalid username or password."\n'
    "};\n</script></body></html>"
)


def kmsi_html(pgid: str = "KmsiInterrupt") -> str:
    return (
        "<html><head><title>Stay signed in?</title></head><body><script>\n"
        "$Config = {\n"
        f'"pgid": "{pgid}",\n'
        '"sFT": "KMSI-FLOW-TOKEN",\n'
        '"sCtx": "KMSI-CTX",\n'
        '"canary": "KMSI-CANARY",\n'
        f'"urlPost": "{MS_BASE}/common/login",\n'
        f'"sPOST_Username": "{USERNAME}"\n'
        "};\n</script></body></html>"
    )


HIDDENFORM_HTML = (
    "<html><body>Working..."
    f'<form name="hiddenform" action="{MS_BASE}/common/final">'
    '<input type="hidden" name="code" value="HF-CODE">'
    '<input type="hidden" name="state" value="HF-STATE">'
    "</form></body></html>"
)

SAML_REQUEST_HTML = (
    "<html><script>"
    f"window.location='{ACS_URL}?SAMLRequest=REQ&RelayState=x';"
    "</script></html>"
)


# Microsoft's Aug-2026 session-pull reload interstitial: HTTP 200, title
# "Redirecting", ZERO forms; $Config echoes the whole credential form in
# oPostParams (including passwd) and points urlPost at ...&sso_reload=True.
# Structure mirrors a sanitized live capture; values are sentinels.
SSO_RELOAD_URL_POST = "/tenant-id/login?ctx=CTX&sso_reload=True"
SSO_RELOAD_FIELDS = {
    "i13": "0",
    "login": USERNAME,
    "loginfmt": USERNAME,
    "type": "11",
    "LoginOptions": "3",
    "passwd": PASSWORD,
    "canary": "PAGE-CANARY-1",
    "ctx": "CTX-TOKEN-1",
    "flowToken": "FLOW-TOKEN-1",
    "hpgrequestid": "SESSION-ID-1",
    "ps": "2",
    "NewUser": "1",
    "fspost": "0",
    "i21": "0",
    "CookieDisclosure": "0",
    "isSignupPost": "0",
    "i19": "9231",
    "IsFidoSupported": "1",
}


def sso_reload_html(
    url_post: str = SSO_RELOAD_URL_POST,
    o_post_params: dict[str, str] | None = None,
) -> str:
    import json as _json

    params = SSO_RELOAD_FIELDS if o_post_params is None else o_post_params
    return (
        "<html><head><title>Redirecting</title></head><body><script>\n"
        "$Config = {\n"
        '"iSessionPullType": 2,\n'
        '"slMaxRetry": 2,\n'
        f'"urlPost": "{url_post}",\n'
        f'"oPostParams": {_json.dumps(params)}\n'
        "};\n</script></body></html>"
    )


# ---------------------------------------------------------------------------
# Scripted transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        html: str = "",
        url: str = "",
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | list[Any] | str | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = html
        self.url = url
        self.headers = headers or {}
        self._json = json_data

    def json(self) -> Any:
        if self._json is None:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return self._json


class ScriptedSession:
    """Stands in for requests.Session: pops scripted items in order."""

    def __init__(self) -> None:
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.request_kwargs: list[tuple[str, str, dict[str, Any]]] = []
        self._queue: list[Any] = []

    def enqueue(self, *items: Any) -> None:
        self._queue.extend(items)

    def _next(self, method: str, url: str) -> Any:
        self.calls.append((method, url))
        if not self._queue:
            raise AssertionError(f"Script exhausted at {method} {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request_kwargs.append(("GET", url, kwargs))
        return self._next("GET", url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request_kwargs.append(("POST", url, kwargs))
        return self._next("POST", url)

    def close(self) -> None:
        pass


def set_d2l_cookies(session: ScriptedSession) -> None:
    """Simulate the D2L ACS redirect chain having set session cookies."""
    for name in COOKIE_NAMES:
        session.cookies.set(name, f"sentinel-{name}", domain="lighthouse.manipal.edu", path="/")


@pytest.fixture
def scripted() -> ScriptedSession:
    return ScriptedSession()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config storage at a temp dir (LIGHTHOUSE_CONFIG_DIR env only)."""
    d = tmp_path / "config"
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    return d


def run_login(scripted_session: ScriptedSession, **kwargs: Any) -> dict[str, str]:
    client = MicrosoftSSOClient()
    with patch("requests.Session", return_value=scripted_session):
        try:
            return client.login(USERNAME, PASSWORD, kwargs.pop("totp_code", None), **kwargs)
        finally:
            client.close()


def make_client(scripted_session: ScriptedSession) -> MicrosoftSSOClient:
    with patch("requests.Session", return_value=scripted_session):
        return MicrosoftSSOClient()


def read_pending() -> dict[str, Any] | None:
    """Read the pending checkpoint through its public loader (unseals)."""
    from lighthouse_cli.config import load_mfa_pending

    return load_mfa_pending()


def begin_success() -> FakeResponse:
    return FakeResponse(json_data={"Success": True, "SessionId": "SID-A", "FlowToken": "BEGIN-FT", "Ctx": "BEGIN-CTX"})


def end_success() -> FakeResponse:
    return FakeResponse(json_data={"Success": True, "FlowToken": "END-FT", "Ctx": "END-CTX"})


# ---------------------------------------------------------------------------
# Bootstrap + password flow
# ---------------------------------------------------------------------------


class TestPasswordFlow:
    def test_direct_saml_after_password(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Password POST returns the SAML form directly; ACS sets cookies."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=SAML_HTML, url=CREDS_POST_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        methods_urls = [(m, u) for m, u in scripted.calls]
        assert ("GET", LOGIN_INIT_URL) in methods_urls
        assert ("GET", MS_SSO_URL) in methods_urls
        assert ("POST", CREDS_POST_URL) in methods_urls
        assert ("POST", ACS_URL) in methods_urls

    def test_wrong_password_error_page(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Microsoft error page becomes a clean MicrosoftSSOError."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=ERROR_HTML, url=CREDS_POST_URL),
        )
        with pytest.raises(MicrosoftSSOError, match="50126"):
            run_login(scripted)

    def test_redirect_chain_after_password(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """A 302 after the password POST is followed to the SAML page."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(302, url=CREDS_POST_URL, headers={"Location": "/saml/landing"}),
            FakeResponse(200, html=SAML_HTML, url=f"{MS_BASE}/saml/landing"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("GET", f"{MS_BASE}/saml/landing") in scripted.calls

    def test_acs_without_cookies_falls_back_to_home(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """When ACS sets no cookies the driver probes /d2l/home before failing."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=SAML_HTML, url=CREDS_POST_URL),
            FakeResponse(200, html="<html>acs landing</html>", url=ACS_URL),
            FakeResponse(200, html="<html>no cookies</html>", url=f"{BASE}/d2l/home"),
        )
        with pytest.raises(MicrosoftSSOError, match="cookies"):
            run_login(scripted)
        assert ("GET", f"{BASE}/d2l/home") in scripted.calls

    def test_unexpected_response_error_carries_page_shape(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """The neither-MFA-nor-error-nor-SAML branch enriches its error with the
        sanitized page-shape summary (status/url/pgid/markers)."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            # Genuinely unrecognized page: not MFA, not an error page, no
            # SAMLResponse, and no walk-recognizable markers (a KMSI page
            # would now be submitted inline by the bounded walk).
            FakeResponse(200, html="<html><head><title>Mystery</title></head><body>huh</body></html>", url=CREDS_POST_URL),
        )
        with pytest.raises(MicrosoftSSOError, match=r"Unexpected response — page:"):
            run_login(scripted)

    def test_probe_rejects_unrecognized_post_credentials_page(
        self, scripted: ScriptedSession, isolated_config: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html="<html><head><title>Mystery</title></head><body>huh</body></html>", url=CREDS_POST_URL),
        )
        client = make_client(scripted)
        with pytest.raises(MicrosoftSSOError, match="unrecognized page"):
            client.probe_mfa_methods(USERNAME, PASSWORD)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_probe_reports_converged_proofs(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """A ConvergedTFA page with arrUserProofs probes as 'converged' with
        the parsed proofs — 'legacy_form' is reserved for the no-proofs form."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(), url=CREDS_POST_URL),
        )
        client = make_client(scripted)
        result = client.probe_mfa_methods(USERNAME, PASSWORD)
        assert result.page == "converged"
        assert [p.auth_method_id for p in result.proofs] == ["PhoneAppOTP"]
        assert result.proofs[0].is_default is True


class TestUsernameBootstrap:
    def test_http_bootstrap_when_playwright_missing(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without Playwright the browser pre-password requests are mirrored over HTTP."""
        # Force the ImportError gate in _step_prepare_username — otherwise an
        # environment with playwright installed takes the browser branch.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        scripted.enqueue(
            # Step 1-2
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(url_get_credential_type=True), url=MS_SSO_URL),
            # HTTP bootstrap: Me.htm, ssoprobe, dssostatus, GetCredentialType, ssoprobe, dssostatus
            FakeResponse(200, html="{}", url="https://login.live.com/Me.htm?v=3"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
            FakeResponse(200, json_data={"FlowToken": "FLOW-TOKEN-2", "apiCanary": "API-CANARY-2"},
                         url=f"{MS_BASE}/common/GetCredentialType"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
            # Password POST lands on SAML directly
            FakeResponse(200, html=SAML_HTML, url=CREDS_POST_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        urls = [u for _, u in scripted.calls]
        assert "https://login.live.com/Me.htm?v=3" in urls
        assert any("autologon.microsoftazuread-sso.com" in u and "ssoprobe" in u for u in urls)
        assert any(u.endswith("/dssostatus") for u in urls)
        assert f"{MS_BASE}/common/GetCredentialType" in urls

    def test_playwright_launch_failure_falls_back_to_http(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Playwright importable but Chromium missing: warn on stderr and
        continue via the mirrored HTTP sequence instead of failing login."""
        fake_api = ModuleType("playwright.sync_api")
        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("chromium executable missing")
        fake_api.sync_playwright = boom  # type: ignore[attr-defined]
        fake_root = ModuleType("playwright")
        fake_root.sync_api = fake_api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", fake_root)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)

        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(url_get_credential_type=True), url=MS_SSO_URL),
            # HTTP bootstrap: Me.htm, ssoprobe, dssostatus, GCT, ssoprobe, dssostatus
            FakeResponse(200, html="{}", url="https://login.live.com/Me.htm?v=3"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
            FakeResponse(200, json_data={"FlowToken": "FLOW-TOKEN-2"},
                         url=f"{MS_BASE}/common/GetCredentialType"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
            FakeResponse(200, html=SAML_HTML, url=CREDS_POST_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert f"{MS_BASE}/common/GetCredentialType" in [u for _, u in scripted.calls]
        captured = capsys.readouterr()
        assert "pure-HTTP flow" in captured.err
        assert PASSWORD not in captured.err
        assert "chromium executable missing" not in captured.err
        assert captured.out == ""

    def test_playwright_failure_surfaces_when_http_also_fails(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both paths unusable: the login fails (here via the HTTP path's own
        transport error) rather than swallowing it after the Playwright
        warning; the CLI-level wrapper renders it without a raw traceback."""
        fake_api = ModuleType("playwright.sync_api")
        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("chromium executable missing")
        fake_api.sync_playwright = boom  # type: ignore[attr-defined]
        fake_root = ModuleType("playwright")
        fake_root.sync_api = fake_api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", fake_root)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
        monkeypatch.setattr(
            MicrosoftSSOClient,
            "_step_prepare_username_http",
            lambda self, config, username: (_ for _ in ()).throw(
                requests.ConnectionError("network unreachable")
            ),
        )

        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(url_get_credential_type=True), url=MS_SSO_URL),
        )
        with pytest.raises(requests.ConnectionError):
            run_login(scripted)

    def test_playwright_semantic_failure_does_not_fall_back(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_api = ModuleType("playwright.sync_api")
        fake_api.sync_playwright = lambda: object()  # type: ignore[attr-defined]
        fake_root = ModuleType("playwright")
        fake_root.sync_api = fake_api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", fake_root)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
        monkeypatch.setattr(
            MicrosoftSSOClient,
            "_bootstrap_username_via_playwright",
            lambda self, config, username: (_ for _ in ()).throw(
                MicrosoftSSOError("semantic page failure", step="prepare username")
            ),
        )
        http_fallback = patch.object(
            MicrosoftSSOClient, "_step_prepare_username_http"
        )
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(url_get_credential_type=True), url=MS_SSO_URL),
        )
        with http_fallback as fallback:
            with pytest.raises(MicrosoftSSOError, match="semantic page failure"):
                run_login(scripted)
            fallback.assert_not_called()


# ---------------------------------------------------------------------------
# Converged MFA (SAS API)
# ---------------------------------------------------------------------------


class TestConvergedMfa:
    def _mfa_script_head(self, scripted: ScriptedSession, auth_method_id: str = "PhoneAppOTP") -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(auth_method_id=auth_method_id), url=MFA_PAGE_URL),
        )

    def test_app_otp_one_step(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Offline Authenticator code completes in one login call."""
        self._mfa_script_head(scripted, "PhoneAppOTP")
        scripted.enqueue(
            begin_success(),
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("POST", BEGIN_URL) in scripted.calls
        assert ("POST", END_URL) in scripted.calls
        assert ("POST", PROCESS_URL) in scripted.calls

    def test_app_notification_polls_until_approval(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """PhoneAppNotification polls EndAuth while Retry=true, then finishes."""
        self._mfa_script_head(scripted, "PhoneAppNotification")
        scripted.enqueue(
            begin_success(),
            FakeResponse(json_data={"Retry": True, "Entropy": "42"}),
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        end_posts = [u for m, u in scripted.calls if m == "POST" and u == END_URL]
        assert len(end_posts) == 2

    def test_method_mismatch_rejected(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """--mfa-method app on an SMS-only account fails before BeginAuth."""
        self._mfa_script_head(scripted, "OneWaySMS")
        with pytest.raises(MicrosoftSSOError, match="not available"):
            run_login(scripted, totp_code=TOTP_CODE, mfa_method="app")
        # No SAS API traffic happened.
        assert ("POST", BEGIN_URL) not in scripted.calls

    def test_auto_literal_code_rejected_before_sms_beginauth(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        self._mfa_script_head(scripted, "OneWaySMS")
        with pytest.raises(MicrosoftSSOError, match="valid only for PhoneAppOTP"):
            run_login(scripted, totp_code=TOTP_CODE, mfa_method="auto")
        assert ("POST", BEGIN_URL) not in scripted.calls

    def test_begin_auth_failure_raises(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """BeginAuth rejection surfaces a clean error."""
        self._mfa_script_head(scripted, "PhoneAppOTP")
        scripted.enqueue(
            FakeResponse(json_data={"Success": False, "Message": "code send failed"}),
        )
        with pytest.raises(MicrosoftSSOError, match="MFA setup failed"):
            run_login(scripted, totp_code=TOTP_CODE)

    def test_end_auth_invalid_json_raises_cleanly(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Non-JSON EndAuth response becomes a clean MicrosoftSSOError."""
        self._mfa_script_head(scripted, "PhoneAppOTP")
        scripted.enqueue(
            begin_success(),
            FakeResponse(200, html="<html>garbage</html>", url=END_URL),
        )
        with pytest.raises(MicrosoftSSOError, match="EndAuth"):
            run_login(scripted, totp_code=TOTP_CODE)

    def test_legacy_form_mfa_posts_otc_form(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Older MFA pages without arrUserProofs fall back to the otc form POST."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=LEGACY_MFA_HTML, url=MFA_PAGE_URL),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("POST", PROCESS_URL) in scripted.calls


# ---------------------------------------------------------------------------
# Post-MFA interstitials
# ---------------------------------------------------------------------------


class TestPostMfaInterstitials:
    def _head(self, scripted: ScriptedSession) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(auth_method_id="PhoneAppOTP"), url=MFA_PAGE_URL),
            begin_success(),
            end_success(),
        )

    def test_kmsi_interrupt_submitted(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """KmsiInterrupt page is auto-submitted ('Stay signed in')."""
        self._head(scripted)
        scripted.enqueue(
            FakeResponse(200, html=kmsi_html("KmsiInterrupt"), url=PROCESS_URL),
            FakeResponse(302, url=f"{MS_BASE}/common/login", headers={"Location": "/landing"}),
            FakeResponse(200, html=SAML_HTML, url=f"{MS_BASE}/landing"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("POST", f"{MS_BASE}/common/login") in scripted.calls

    def test_cmsi_interrupt_submitted(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """CmsiInterrupt page is auto-submitted like KMSI."""
        self._head(scripted)
        scripted.enqueue(
            FakeResponse(200, html=kmsi_html("CmsiInterrupt"), url=PROCESS_URL),
            FakeResponse(302, url=f"{MS_BASE}/common/login", headers={"Location": "/landing"}),
            FakeResponse(200, html=SAML_HTML, url=f"{MS_BASE}/landing"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)
        assert set(cookies.keys()) == set(COOKIE_NAMES)

    def test_hiddenform_interstitial_auto_submitted(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Microsoft auto-submit hiddenform pages are POSTed with their fields."""
        self._head(scripted)
        scripted.enqueue(
            FakeResponse(200, html=HIDDENFORM_HTML, url=PROCESS_URL),
            FakeResponse(200, html=SAML_HTML, url=f"{MS_BASE}/common/final"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("POST", f"{MS_BASE}/common/final") in scripted.calls

    def test_saml_request_walker_follows_js_redirect(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """A JS window.location carrying SAMLRequest is fetched (no SAMLResponse yet)."""
        self._head(scripted)
        scripted.enqueue(
            FakeResponse(200, html=SAML_REQUEST_HTML, url=PROCESS_URL),
            FakeResponse(200, html=SAML_HTML, url=f"{ACS_URL}?SAMLRequest=REQ"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("GET", f"{ACS_URL}?SAMLRequest=REQ&RelayState=x") in scripted.calls

    def test_processauth_redirect_chain(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """ProcessAuth 302 chains are followed until SAML appears."""
        self._head(scripted)
        scripted.enqueue(
            FakeResponse(302, url=PROCESS_URL, headers={"Location": "/hop1"}),
            FakeResponse(302, url=f"{MS_BASE}/hop1", headers={"Location": "https://lighthouse.manipal.edu/hop2"}),
            FakeResponse(200, html=SAML_HTML, url=f"{BASE}/hop2"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)

        cookies = run_login(scripted, totp_code=TOTP_CODE)
        assert set(cookies.keys()) == set(COOKIE_NAMES)


# ---------------------------------------------------------------------------
# Deferred MFA + checkpoint resume phases
# ---------------------------------------------------------------------------


class TestDeferAndResume:
    def _defer_login(self, scripted: ScriptedSession) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=mfa_html(auth_method_id="OneWaySMS", display="SMS"), url=MFA_PAGE_URL),
            begin_success(),
        )
        with pytest.raises(MfaPendingError):
            run_login(scripted, defer_mfa_to_pending=True)

    def test_sms_defer_saves_pending_then_verify_completes(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """auth login (defer) checkpoints BeginAuth; auth verify resumes and clears."""
        self._defer_login(scripted)

        pending = read_pending()
        assert pending is not None
        assert pending.get("version") == 2
        assert "mfa_page_url" in pending
        assert "mfa_config" in pending
        assert "begin" in pending
        assert "selected_proof" in pending
        assert "cookies" in pending
        # The checkpoint is sealed: the raw file carries only envelope + metadata.
        raw = (isolated_config / "mfa_pending.json").read_text()
        assert SAML_TOKEN not in raw
        assert TOTP_CODE not in raw

        # --- verify ---
        scripted.enqueue(
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert ("POST", END_URL) in scripted.calls
        assert read_pending() is None

    def test_voice_defer_then_verify_is_codeless(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(
                200,
                html=mfa_html(auth_method_id="TwoWayVoiceMobile", display="Call"),
                url=MFA_PAGE_URL,
            ),
            begin_success(),
        )
        with pytest.raises(MfaPendingError, match="press #"):
            run_login(
                scripted, mfa_method="call", defer_mfa_to_pending=True
            )

        scripted.enqueue(
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending("ok")
        finally:
            client.close()
        assert set(cookies) == set(COOKIE_NAMES)
        end_calls = [
            kwargs
            for method, url, kwargs in scripted.request_kwargs
            if method == "POST" and url == END_URL
        ]
        assert end_calls
        assert "AdditionalAuthData" not in end_calls[-1]["json"]

    def test_push_defer_verify_prints_number_match_on_non_tty_stderr(
        self, scripted: ScriptedSession, isolated_config: Path,
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(
                200,
                html=mfa_html(auth_method_id="PhoneAppNotification", display="Push"),
                url=MFA_PAGE_URL,
            ),
            begin_success(),
        )
        with pytest.raises(MfaPendingError, match="approval requested"):
            run_login(
                scripted, mfa_method="push", defer_mfa_to_pending=True
            )

        scripted.enqueue(
            FakeResponse(json_data={"Retry": True, "Entropy": "42"}),
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending("ok")
        finally:
            client.close()
        captured = capsys.readouterr()
        assert set(cookies) == set(COOKIE_NAMES)
        assert "number shown: 42" in captured.err
        assert captured.out == ""
        end_calls = [
            kwargs
            for method, url, kwargs in scripted.request_kwargs
            if method == "POST" and url == END_URL
        ]
        assert end_calls
        assert all("AdditionalAuthData" not in call["json"] for call in end_calls)

    def test_verify_without_pending_fails_cleanly(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        client = make_client(scripted)
        try:
            with pytest.raises(MicrosoftSSOError, match="No pending MFA session"):
                client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

    def test_endauth_success_checkpoint_resumable(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """After EndAuth success the flow tokens are checkpointed; a second verify
        skips straight to ProcessAuth (resumable phase ms_auth EndAuth-success)."""
        self._defer_login(scripted)

        # Verify attempt #1: EndAuth succeeds, ProcessAuth dies on the network.
        scripted.enqueue(
            end_success(),
            requests.ConnectionError("connection reset mid-flow"),
        )
        client = make_client(scripted)
        try:
            with pytest.raises(requests.ConnectionError):
                client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        pending = read_pending()
        assert pending is not None
        assert "end_auth_flow" in pending
        assert "end_auth_ctx" in pending

        # Verify attempt #2: skips EndAuth entirely, posts ProcessAuth only.
        scripted.enqueue(
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        assert [c for c in scripted.calls[3:] if c == ("POST", END_URL)].count(("POST", END_URL)) == 1
        assert read_pending() is None

    def test_kmsi_checkpoint_resumable(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """A KMSI page reached during verify is checkpointed; a second verify
        resumes by submitting the saved KMSI page directly."""
        self._defer_login(scripted)

        # Verify attempt #1: EndAuth ok, ProcessAuth shows KMSI, KMSI submit dies.
        scripted.enqueue(
            end_success(),
            FakeResponse(200, html=kmsi_html("KmsiInterrupt"), url=PROCESS_URL),
            requests.ConnectionError("connection reset at kmsi"),
        )
        client = make_client(scripted)
        try:
            with pytest.raises(requests.ConnectionError):
                client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        pending = read_pending()
        assert pending is not None
        assert "kmsi_checkpoint" in pending

        # Verify attempt #2: submits the saved KMSI page; no EndAuth/ProcessAuth.
        scripted.enqueue(
            FakeResponse(302, url=f"{MS_BASE}/common/login", headers={"Location": "/landing"}),
            FakeResponse(200, html=SAML_HTML, url=f"{MS_BASE}/landing"),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        assert set(cookies.keys()) == set(COOKIE_NAMES)
        remaining = [u for m, u in scripted.calls[3:] if m == "POST"]
        assert remaining.count(END_URL) == 1  # verify #1 only
        assert remaining.count(PROCESS_URL) == 1  # verify #1 only; verify #2 resumes at KMSI
        assert read_pending() is None

    def test_wrong_code_clears_pending(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """A rejected code clears the checkpoint (must request a fresh one)."""
        self._defer_login(scripted)

        scripted.enqueue(
            FakeResponse(json_data={"Success": False, "Retry": False, "ResultValue": "WrongCode"}),
        )
        client = make_client(scripted)
        try:
            with pytest.raises(MicrosoftSSOError, match="2FA verification failed"):
                client.complete_mfa_pending("000000")
        finally:
            client.close()

        assert read_pending() is None

    def test_network_failure_leaves_pending_retryable(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """A network drop during verify keeps the checkpoint intact for retry."""
        self._defer_login(scripted)

        scripted.enqueue(requests.ConnectionError("dns failure"))
        client = make_client(scripted)
        try:
            with pytest.raises(requests.ConnectionError):
                client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        assert read_pending() is not None

        # Retry with a working network completes the login.
        scripted.enqueue(
            end_success(),
            FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
            FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
        )
        set_d2l_cookies(scripted)
        client = make_client(scripted)
        try:
            cookies = client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()
        assert set(cookies.keys()) == set(COOKIE_NAMES)

    def test_already_completed_code_reports_cleanly(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """AuthenticationPreviouslyCompleted without a saved EndAuth checkpoint
        asks the user to start a new login."""
        self._defer_login(scripted)

        scripted.enqueue(
            FakeResponse(json_data={
                "Success": False, "Retry": False,
                "ResultValue": "AuthenticationPreviouslyCompleted",
            }),
        )
        client = make_client(scripted)
        try:
            with pytest.raises(MicrosoftSSOError, match="already accepted"):
                client.complete_mfa_pending(TOTP_CODE)
        finally:
            client.close()

        assert read_pending() is None


class TestDescribePageShape:
    """Sanitized diagnostics for unrecognized post-credentials pages."""

    def _snap(self, html: str, url: str = "https://login.microsoftonline.com/x", status: int = 200):
        from lighthouse_cli.ms_auth import ResponseSnapshot

        return ResponseSnapshot(url=url, status_code=status, location="", html=html)

    def test_summary_contains_structure_not_tokens(self):
        from lighthouse_cli.ms_auth import describe_page_shape

        html = (
            "<title>Stay signed in?</title>"
            '$Config={"pgid":"ConvergedKmsi","sFT":"SECRET-FLOW-TOKEN",'
            '"sCtx":"SECRET-CTX","urlPost":"/common/SAS"};'
            "<form action='/common/SAS/ProcessAuth'>"
        )
        snap = self._snap(html, url="https://login.microsoftonline.com/kmsi?ctx=SECRET-QUERY")
        out = describe_page_shape(snap)
        assert "ConvergedKmsi" in out
        assert "Stay signed in?" in out
        assert "status=200" in out
        assert "url=login.microsoftonline.com/kmsi" in out
        assert "ProcessAuth-form=1" in out
        # No token material may leak: query string stripped, $Config values absent.
        assert "SECRET-QUERY" not in out
        assert "SECRET-FLOW-TOKEN" not in out
        assert "SECRET-CTX" not in out

    def test_empty_page_is_safe(self):
        from lighthouse_cli.ms_auth import describe_page_shape

        out = describe_page_shape(self._snap("", url="", status=302))
        assert "status=302" in out and "(no url)" in out


class TestFlowRecorder:
    """LIGHTHOUSE_DEBUG_FLOW writes sanitized step records only."""

    def test_records_are_sanitized(self, tmp_path):
        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        log = tmp_path / "flow.jsonl"
        client = MicrosoftSSOClient(flow_log=str(log))
        # GET record via a mocked transport.
        resp = type("R", (), {"status_code": 200, "text": "ok", "url": "https://x.test/a?token=SECRET", "headers": {}})()
        with patch.object(client._session, "get", return_value=resp):
            client._get("https://x.test/a?token=SECRET")
        # POST records via a mocked transport: field NAMES only, never values.
        post_resp = type("R", (), {"status_code": 200, "text": "ok", "url": "https://x.test/login", "headers": {}})()
        with patch.object(client._session, "post", return_value=post_resp):
            client._post("https://x.test/login", data={"passwd": "SECRETVALUE", "login": "user"})
        log_text = log.read_text()
        lines = [json.loads(line) for line in log_text.splitlines()]
        assert lines[0] == {"method": "GET", "url": "x.test/a", "status": 200}
        assert "SECRET" not in log_text
        assert "SECRETVALUE" not in log_text
        # "passwd" appears only as a field NAME inside a form_fields record.
        for record in lines:
            if "passwd" in json.dumps(record):
                assert record["method"] == "POST"
                assert "passwd" in record["form_fields"]

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        monkeypatch.delenv("LIGHTHOUSE_DEBUG_FLOW", raising=False)
        client = MicrosoftSSOClient()
        client._record_flow("GET", "https://x.test/a")
        assert not list(tmp_path.glob("*.jsonl"))  # nothing written anywhere

        # Prove the assertion above is meaningful: the same recorder with the
        # env var set DOES write the record.
        on = tmp_path / "on.jsonl"
        monkeypatch.setenv("LIGHTHOUSE_DEBUG_FLOW", str(on))
        enabled = MicrosoftSSOClient()
        enabled._record_flow("GET", "https://x.test/a")
        assert on.exists()

    def test_username_prepare_records_ssoprobe_gets(self, tmp_path):
        """The direct ssoprobe GETs (not routed through _get) reach the flow log."""
        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        log = tmp_path / "flow.jsonl"
        client = MicrosoftSSOClient(flow_log=str(log))
        client._session = ScriptedSession()
        client._session.enqueue(
            FakeResponse(200, html="", url="https://login.live.com/Me.htm?v=3"),
            FakeResponse(200, html="", url="https://autologon.microsoftazuread-sso.com/common/winauth/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/common/instrumentation/dssostatus"),
            FakeResponse(200, json_data={"FlowToken": "FLOW-TOKEN-2"}, url=f"{MS_BASE}/common/GetCredentialType"),
            FakeResponse(200, html="", url="https://autologon.microsoftazuread-sso.com/common/winauth/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/common/instrumentation/dssostatus"),
        )
        config = {
            "sFT": "tok",
            "sCtx": "ctx",
            "urlGetCredentialType": "/common/GetCredentialType",
            "_ms_url": f"{MS_BASE}/common/",
            "correlationId": "cid",
        }
        client._step_prepare_username_http(config, USERNAME)

        records = [json.loads(line) for line in log.read_text().splitlines()]
        probes = [r for r in records if "/winauth/ssoprobe" in r["url"]]
        assert len(probes) == 2  # pre-GCT probe + post-GCT cache-busted probe
        assert {r["method"] for r in probes} == {"GET"}
        assert {r["status"] for r in probes} == {200}
        # The recorder strips query strings: no client-request-id / cache-buster.
        assert "client-request-id" not in log.read_text()


class TestGctMalformedResponse:
    """A 200/non-JSON GetCredentialType response must not crash the flow."""

    def test_non_json_200_returns_config_unchanged(self, tmp_path):
        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        client = MicrosoftSSOClient(flow_log=str(tmp_path / "flow.jsonl"))
        client._session = ScriptedSession()
        client._session.enqueue(FakeResponse(200, html="<html>not json</html>"))
        config = {"sFT": "tok", "sCtx": "ctx", "urlPost": "/common/login",
                  "urlGetCredentialType": "/common/GetCredentialType",
                  "_ms_url": "https://login.microsoftonline.com/x"}
        out = client._step_get_credential_type(config, "user@example.edu")
        assert out == config  # unchanged, no UnboundLocalError
        records = [json.loads(line) for line in (tmp_path / "flow.jsonl").read_text().splitlines()]
        # Post-response GCT record uses the POST method label (matches the
        # pre-request intent record) and flags the body as unparseable.
        gct = [r for r in records if "GetCredentialType" in r["url"] and r.get("status") == 200]
        assert gct and gct[0]["method"] == "POST"
        assert gct[0]["form_fields"] == ["(unparseable)"]

    @pytest.mark.parametrize("payload", [[1, 2, 3], "ok"])
    def test_non_dict_json_200_returns_config_unchanged(self, tmp_path, payload):
        """Valid-but-non-object JSON (array, string) must not crash key extraction."""
        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        client = MicrosoftSSOClient(flow_log=str(tmp_path / "flow.jsonl"))
        client._session = ScriptedSession()
        client._session.enqueue(FakeResponse(200, json_data=payload))
        config = {"sFT": "tok", "sCtx": "ctx", "urlPost": "/common/login",
                  "urlGetCredentialType": "/common/GetCredentialType",
                  "_ms_url": "https://login.microsoftonline.com/x"}
        out = client._step_get_credential_type(config, "user@example.edu")
        assert out == config  # unchanged, no AttributeError on .keys()
        records = [json.loads(line) for line in (tmp_path / "flow.jsonl").read_text().splitlines()]
        gct = [r for r in records if "GetCredentialType" in r["url"] and r.get("status") == 200]
        assert gct and gct[0]["form_fields"] == []  # no keys to report

    def test_simplejson_style_decode_error_returns_config_unchanged(self, tmp_path):
        """requests may parse JSON with simplejson, whose JSONDecodeError is
        NOT json.JSONDecodeError — both subclass ValueError, which the flow
        catches, so such environments degrade gracefully too."""

        class SimplejsonLikeDecodeError(ValueError):
            pass

        class SimplejsonStyleResponse(FakeResponse):
            def json(self) -> Any:
                raise SimplejsonLikeDecodeError("Expecting value")

        from lighthouse_cli.ms_auth import MicrosoftSSOClient

        client = MicrosoftSSOClient(flow_log=str(tmp_path / "flow.jsonl"))
        client._session = ScriptedSession()
        client._session.enqueue(SimplejsonStyleResponse(200))
        config = {"sFT": "tok", "sCtx": "ctx", "urlPost": "/common/login",
                  "urlGetCredentialType": "/common/GetCredentialType",
                  "_ms_url": "https://login.microsoftonline.com/x"}
        out = client._step_get_credential_type(config, "user@example.edu")
        assert out == config  # unchanged — no raw traceback escapes
        records = [json.loads(line) for line in (tmp_path / "flow.jsonl").read_text().splitlines()]
        gct = [r for r in records if "GetCredentialType" in r["url"] and r.get("status") == 200]
        assert gct and gct[0]["form_fields"] == ["(unparseable)"]


# ---------------------------------------------------------------------------
# Aug-2026 session-pull reload interstitial (sso_reload=True + oPostParams)
# ---------------------------------------------------------------------------


class TestSsoReloadInterstitial:
    """A form-less 200 "Redirecting" page after the password POST must be
    detected and re-POSTed (bounded), not reported as an unexpected response."""

    def _snap(self, html: str, url: str = CREDS_POST_URL) -> Any:
        from lighthouse_cli.ms_auth import ResponseSnapshot

        return ResponseSnapshot(url=url, status_code=200, location="", html=html)

    # -- pure detection ------------------------------------------------------

    def test_detects_interstitial_markers(self) -> None:
        from lighthouse_cli.ms_auth import is_sso_reload_page

        assert is_sso_reload_page(self._snap(sso_reload_html()))

    @pytest.mark.parametrize(
        "html",
        [config_html(), mfa_html(), LEGACY_MFA_HTML, HIDDENFORM_HTML, kmsi_html()],
    )
    def test_normal_pages_are_not_interstitial(self, html: str) -> None:
        from lighthouse_cli.ms_auth import is_sso_reload_page

        assert not is_sso_reload_page(self._snap(html))

    def test_empty_opost_params_is_not_interstitial(self) -> None:
        from lighthouse_cli.ms_auth import is_sso_reload_page

        assert not is_sso_reload_page(
            self._snap(sso_reload_html(o_post_params={}))
        )

    def test_urlpost_without_sso_reload_is_not_interstitial(self) -> None:
        from lighthouse_cli.ms_auth import is_sso_reload_page

        assert not is_sso_reload_page(
            self._snap(sso_reload_html(url_post="/tenant-id/login?ctx=CTX"))
        )

    # -- pure transition -----------------------------------------------------

    def test_transition_reposts_echoed_params_to_absolute_url(self) -> None:
        from lighthouse_cli.ms_auth import sso_reload_transition

        t = sso_reload_transition(self._snap(sso_reload_html()), CREDS_POST_URL)
        assert t.kind == "sso_reload"
        # Tenant-relative urlPost resolves against the response URL.
        assert t.url == f"{MS_BASE}{SSO_RELOAD_URL_POST}"
        # The echoed credential form round-trips verbatim (field names only
        # asserted; values flow straight back to Microsoft, never to logs).
        assert set(t.data or {}) == set(SSO_RELOAD_FIELDS)

    def test_classify_routes_interstitial_as_repost(self) -> None:
        from lighthouse_cli.ms_auth import classify_post_mfa

        t = classify_post_mfa(self._snap(sso_reload_html()), CREDS_POST_URL)
        assert t.kind == "sso_reload"

    def test_classify_converged_tfa_is_mfa_terminal(self) -> None:
        from lighthouse_cli.ms_auth import classify_post_mfa

        t = classify_post_mfa(self._snap(mfa_html()), CREDS_POST_URL)
        assert t.kind == "mfa"

    def test_cross_origin_reload_target_is_rejected(self) -> None:
        from lighthouse_cli.ms_auth import sso_reload_transition

        html = sso_reload_html(
            url_post="https://evil.example/login?sso_reload=True"
        )
        with pytest.raises(MicrosoftSSOError, match="unsafe re-POST target"):
            sso_reload_transition(self._snap(html), CREDS_POST_URL)

    def test_explicit_default_https_port_is_same_origin(self) -> None:
        from lighthouse_cli.ms_auth import sso_reload_transition

        html = sso_reload_html(
            url_post=(
                "https://login.microsoftonline.com:443/tenant-id/login"
                "?sso_reload=True"
            )
        )
        transition = sso_reload_transition(self._snap(html), CREDS_POST_URL)

        assert transition.kind == "sso_reload"
        assert transition.url.startswith("https://login.microsoftonline.com:443/")

    @pytest.mark.parametrize("port", [0, 8443])
    def test_non_default_https_port_is_rejected(self, port: int) -> None:
        from lighthouse_cli.ms_auth import sso_reload_transition

        html = sso_reload_html(
            url_post=(
                f"https://login.microsoftonline.com:{port}/tenant-id/login"
                "?sso_reload=True"
            )
        )

        with pytest.raises(MicrosoftSSOError, match="unsafe re-POST target"):
            sso_reload_transition(self._snap(html), CREDS_POST_URL)

    def test_nested_reload_value_is_rejected_without_value_echo(self) -> None:
        from lighthouse_cli.ms_auth import sso_reload_transition

        html = sso_reload_html(o_post_params={"passwd": ["nested-secret"]})
        with pytest.raises(MicrosoftSSOError, match="unsupported value type") as exc:
            sso_reload_transition(self._snap(html), CREDS_POST_URL)
        assert "nested-secret" not in str(exc.value)

    # -- walk integration ----------------------------------------------------

    def test_login_walks_interstitial_to_error_page(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """Wrong-password flow: password POST -> interstitial -> re-POST ->
        real ConvergedSignIn error page (the pre-Aug-2026 behavior)."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL),
            FakeResponse(200, html=ERROR_HTML, url=CREDS_POST_URL),
        )
        with pytest.raises(MicrosoftSSOError) as ei:
            run_login(scripted)
        assert "50126" in str(ei.value)

    def test_probe_walks_interstitial_to_converged_page(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """MFA-method probe traverses the same hop and reports the proofs."""
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL),
            FakeResponse(200, html=mfa_html(), url=CREDS_POST_URL),
        )
        client = make_client(scripted)
        result = client.probe_mfa_methods(USERNAME, PASSWORD)
        assert result.page == "converged"
        assert [p.auth_method_id for p in result.proofs] == ["PhoneAppOTP"]

    def test_walk_is_bounded_by_local_reload_budget(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """A looping interstitial stops after the local safety budget."""
        from lighthouse_cli.ms_auth import _MAX_SSO_RELOADS

        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            *[
                FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL)
                for _ in range(_MAX_SSO_RELOADS + 2)
            ],
        )
        with pytest.raises(MicrosoftSSOError, match="reload limit exceeded"):
            run_login(scripted)
        # Exactly _MAX_SSO_RELOADS re-POSTs were issued (password POST + the
        # bounded reloads; the third interstitial is returned, never re-POSTed).
        reposts = [c for c in scripted.calls if c[0] == "POST"]
        assert len(reposts) == 1 + _MAX_SSO_RELOADS

    def test_probe_reload_exhaustion_is_error_not_no_mfa(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        from lighthouse_cli.ms_auth import _MAX_SSO_RELOADS

        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            *[
                FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL)
                for _ in range(_MAX_SSO_RELOADS + 1)
            ],
        )
        client = make_client(scripted)
        with pytest.raises(MicrosoftSSOError, match="reload limit exceeded"):
            client.probe_mfa_methods(USERNAME, PASSWORD)

    # -- leak guards ---------------------------------------------------------

    def test_flow_log_records_field_names_only(
        self, scripted: ScriptedSession, isolated_config: Path, tmp_path: Path
    ) -> None:
        """oPostParams echoes the password: the recorder must persist field
        NAMES and marker booleans only, never values."""
        flow_log = tmp_path / "flow.jsonl"
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL),
            FakeResponse(200, html=ERROR_HTML, url=CREDS_POST_URL),
        )
        with patch("requests.Session", return_value=scripted):
            client = MicrosoftSSOClient(flow_log=str(flow_log))
            with pytest.raises(MicrosoftSSOError):
                client.login(USERNAME, PASSWORD)

        raw = flow_log.read_text()
        assert PASSWORD not in raw
        assert "FLOW-TOKEN-1" not in raw
        assert "PAGE-CANARY-1" not in raw
        records = [json.loads(line) for line in raw.splitlines()]
        # The re-POST is recorded by name, like every other form POST.
        repost = [
            r
            for r in records
            if r["method"] == "POST" and "passwd" in (r.get("form_fields") or [])
        ]
        assert repost, "expected the sso_reload re-POST to be recorded"
        # The interstitial page shape flags the new markers.
        page = [r for r in records if r.get("page") and "oPostParams=1" in r["page"]]
        assert page and "sso_reload=1" in page[0]["page"]

    def test_describe_page_shape_flags_interstitial(self) -> None:
        from lighthouse_cli.ms_auth import describe_page_shape

        shape = describe_page_shape(self._snap(sso_reload_html()))
        assert "oPostParams=1" in shape
        assert "sso_reload=1" in shape
        assert "Redirecting" in shape
        # And the normal login page stays all-zeros for the new markers.
        assert "oPostParams=0" in describe_page_shape(self._snap(config_html()))

    def test_signin_error_page_is_not_mfa(self) -> None:
        """The ConvergedSignIn page the sso_reload walk lands on after a
        wrong password carries $Config flags like fAvoidNewOTCGeneration… —
        a bare "otc" substring made is_mfa_page misroute it to MFA handling
        (live regression, Aug 2026). Word-bounded matching keeps it an error."""
        from lighthouse_cli.ms_auth import is_error_page, is_mfa_page

        html = (
            "<html><head><title>Sign in to your account</title></head><body><script>\n"
            "$Config = {\n"
            '"pgid": "ConvergedSignIn",\n'
            '"sErrorCode": "50126",\n'
            '"sErrTxt": "",\n'
            '"fAvoidNewOTCGenerationWhenAlreadySent": true,\n'
            '"urlPost": "/tenant-id/login",\n'
            '"sFT": "FLOW-TOKEN-1",\n'
            '"sPOST_Username": "' + USERNAME + '"\n'
            "};\n</script>\n"
            "Your account has apps like Microsoft Authenticator available.\n"
            "</body></html>"
        )
        snap = self._snap(html)
        assert not is_mfa_page(html)
        assert is_error_page(snap)

    def test_login_reports_wrong_password_through_interstitial(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """Full live-shaped sequence: password POST -> interstitial -> re-POST
        -> ConvergedSignIn error page (with the OTC-flag false-positive bait)
        -> clean 50126 wrong-password error."""
        terminal = (
            "<html><head><title>Sign in to your account</title></head><body><script>\n"
            "$Config = {\n"
            '"pgid": "ConvergedSignIn",\n'
            '"sErrorCode": "50126",\n'
            '"fAvoidNewOTCGenerationWhenAlreadySent": true,\n'
            '"sErrTxt": ""\n'
            "};\n</script>\n"
            "apps like Microsoft Authenticator are available.\n"
            "</body></html>"
        )
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(200, html=sso_reload_html(), url=CREDS_POST_URL),
            FakeResponse(200, html=terminal, url=CREDS_POST_URL),
        )
        with pytest.raises(MicrosoftSSOError) as ei:
            run_login(scripted)
        assert "50126" in str(ei.value)
        assert "2FA" not in str(ei.value)
