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
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
import requests

from lighthouse_cli.config import COOKIE_NAMES, load_mfa_pending
from lighthouse_cli.ms_auth import (
    _MAX_SSO_RELOADS,
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    ResponseSnapshot,
    classify_post_mfa,
    describe_page_shape,
    is_error_page,
    is_mfa_page,
    is_sso_reload_page,
    sso_reload_transition,
)
from lighthouse_cli.ms_errors import MFA_METHOD_APP

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
HOME_URL = f"{BASE}/d2l/home"


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

# Genuinely unrecognized page: not MFA, not an error page, no SAMLResponse,
# and no walk-recognizable markers (a KMSI page would now be submitted inline
# by the bounded walk).
MYSTERY_HTML = "<html><head><title>Mystery</title></head><body>huh</body></html>"


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
    params = SSO_RELOAD_FIELDS if o_post_params is None else o_post_params
    return (
        "<html><head><title>Redirecting</title></head><body><script>\n"
        "$Config = {\n"
        '"iSessionPullType": 2,\n'
        '"slMaxRetry": 2,\n'
        f'"urlPost": "{url_post}",\n'
        f'"oPostParams": {json.dumps(params)}\n'
        "};\n</script></body></html>"
    )


# Same-host lookalikes origin pinning must refuse: plain http, a suffix-spoofed
# host, userinfo, and a non-default port.
UNSAFE_MS_URLS = [
    "http://login.microsoftonline.com/common/login",
    "https://login.microsoftonline.com.evil.example/common/login",
    "https://user:password@login.microsoftonline.com/common/login",
    "https://login.microsoftonline.com:8443/common/login",
]


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


def verify(scripted_session: ScriptedSession, code: str = TOTP_CODE) -> dict[str, str]:
    """Run ``auth verify`` (complete_mfa_pending) on a fresh client."""
    with closing(make_client(scripted_session)) as client:
        return client.complete_mfa_pending(code)


def read_pending() -> dict[str, Any] | None:
    """Read the pending checkpoint through its public loader (unseals)."""
    return load_mfa_pending()


def begin_success() -> FakeResponse:
    return FakeResponse(json_data={"Success": True, "SessionId": "SID-A", "FlowToken": "BEGIN-FT", "Ctx": "BEGIN-CTX"})


def end_success() -> FakeResponse:
    return FakeResponse(json_data={"Success": True, "FlowToken": "END-FT", "Ctx": "END-CTX"})


def saml_at(url: str) -> FakeResponse:
    return FakeResponse(200, html=SAML_HTML, url=url)


def home() -> FakeResponse:
    return FakeResponse(200, html="<html>D2L home</html>", url=HOME_URL)


def sso_start(*pages: str, config: str | None = None) -> list[FakeResponse]:
    """D2L's SAML redirect and Microsoft's sign-in page, then each page the
    password POST chain returns (all served at CREDS_POST_URL)."""
    return [
        FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
        FakeResponse(200, html=config or config_html(), url=MS_SSO_URL),
        *(FakeResponse(200, html=page, url=CREDS_POST_URL) for page in pages),
    ]


def mfa_start(auth_method_id: str = "PhoneAppOTP", display: str = "Android") -> list[FakeResponse]:
    """Bootstrap whose password POST lands on a ConvergedTFA page."""
    return [
        *sso_start(),
        FakeResponse(200, html=mfa_html(auth_method_id=auth_method_id, display=display), url=MFA_PAGE_URL),
    ]


def kmsi_submit() -> list[FakeResponse]:
    """The KMSI form POST redirects to a landing page carrying the SAML form."""
    return [
        FakeResponse(302, url=f"{MS_BASE}/common/login", headers={"Location": "/landing"}),
        saml_at(f"{MS_BASE}/landing"),
    ]


def login_succeeds(scripted_session: ScriptedSession, *responses: Any, **kwargs: Any) -> None:
    """Script ``responses`` then D2L home, and check login returns the D2L cookies."""
    scripted_session.enqueue(*responses, home())
    set_d2l_cookies(scripted_session)
    assert set(run_login(scripted_session, **kwargs)) == set(COOKIE_NAMES)


def verify_succeeds(scripted_session: ScriptedSession, *responses: Any, code: str = TOTP_CODE) -> None:
    """Script ``responses`` then D2L home, and check verify returns the D2L cookies."""
    scripted_session.enqueue(*responses, home())
    set_d2l_cookies(scripted_session)
    assert set(verify(scripted_session, code)) == set(COOKIE_NAMES)


def end_auth_payloads(scripted_session: ScriptedSession) -> list[dict[str, Any]]:
    return [
        kwargs["json"]
        for method, url, kwargs in scripted_session.request_kwargs
        if method == "POST" and url == END_URL
    ]


def snap(html: str, url: str = CREDS_POST_URL, status: int = 200) -> ResponseSnapshot:
    return ResponseSnapshot(url=url, status_code=status, location="", html=html)


def chromium_missing(*a: Any, **k: Any) -> None:
    raise RuntimeError("chromium executable missing")


def install_fake_playwright(monkeypatch: pytest.MonkeyPatch, sync_playwright: Any) -> None:
    fake_api = ModuleType("playwright.sync_api")
    fake_api.sync_playwright = sync_playwright  # type: ignore[attr-defined]
    fake_root = ModuleType("playwright")
    fake_root.sync_api = fake_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_root)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)


# ---------------------------------------------------------------------------
# Bootstrap + password flow
# ---------------------------------------------------------------------------


class TestPasswordFlow:
    def test_direct_saml_after_password(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Password POST returns the SAML form directly; ACS sets cookies."""
        login_succeeds(scripted, *sso_start(SAML_HTML))
        for call in [("GET", LOGIN_INIT_URL), ("GET", MS_SSO_URL), ("POST", CREDS_POST_URL), ("POST", ACS_URL)]:
            assert call in scripted.calls

    @pytest.mark.parametrize(
        ("pages", "error"),
        [
            pytest.param([ERROR_HTML], "50126", id="wrong-password-error-page"),
            # The neither-MFA-nor-error-nor-SAML branch enriches its error with
            # the sanitized page-shape summary (status/url/pgid/markers).
            pytest.param([MYSTERY_HTML], r"Unexpected response — page:", id="unexpected-page-shape"),
        ],
    )
    def test_post_password_page_errors(
        self, scripted: ScriptedSession, isolated_config: Path, pages: list[str], error: str
    ) -> None:
        scripted.enqueue(*sso_start(*pages))
        with pytest.raises(MicrosoftSSOError, match=error):
            run_login(scripted)

    def test_redirect_chain_after_password(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """A 302 after the password POST is followed to the SAML page."""
        login_succeeds(
            scripted,
            *sso_start(),
            FakeResponse(302, url=CREDS_POST_URL, headers={"Location": "/saml/landing"}),
            saml_at(f"{MS_BASE}/saml/landing"),
        )
        assert ("GET", f"{MS_BASE}/saml/landing") in scripted.calls

    @pytest.mark.parametrize(
        ("home_chain", "expected_get"),
        [
            pytest.param(
                [FakeResponse(200, html="<html>no cookies</html>", url=HOME_URL)],
                HOME_URL,
                id="probes-home",
            ),
            pytest.param(
                [
                    FakeResponse(302, url=f"{HOME_URL}/nested/", headers={"Location": "landing"}),
                    FakeResponse(200, html="<html>no cookies</html>", url=f"{HOME_URL}/nested/landing"),
                ],
                f"{HOME_URL}/nested/landing",
                id="home-redirect-resolves-against-response-url",
            ),
        ],
    )
    def test_acs_without_cookies_falls_back_to_home(
        self,
        scripted: ScriptedSession,
        isolated_config: Path,
        home_chain: list[FakeResponse],
        expected_get: str,
    ) -> None:
        """When ACS sets no cookies the driver probes /d2l/home before failing."""
        scripted.enqueue(
            *sso_start(SAML_HTML),
            FakeResponse(200, html="<html>acs landing</html>", url=ACS_URL),
            *home_chain,
        )
        with pytest.raises(MicrosoftSSOError, match="cookies"):
            run_login(scripted)
        assert ("GET", expected_get) in scripted.calls

    @pytest.mark.parametrize(
        ("pages", "error"),
        [
            pytest.param([MYSTERY_HTML], "unrecognized page", id="unrecognized-page"),
            pytest.param(
                [sso_reload_html()] * (_MAX_SSO_RELOADS + 1),
                "reload limit exceeded",
                id="reload-exhaustion-is-error-not-no-mfa",
            ),
        ],
    )
    def test_probe_rejects_unusable_post_credentials_page(
        self, scripted: ScriptedSession, isolated_config: Path,
        capsys: pytest.CaptureFixture[str], pages: list[str], error: str,
    ) -> None:
        scripted.enqueue(*sso_start(*pages))
        client = make_client(scripted)
        with pytest.raises(MicrosoftSSOError, match=error):
            client.probe_mfa_methods(USERNAME, PASSWORD)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_probe_wraps_unexpected_transport_errors_without_echoing_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MicrosoftSSOClient()
        monkeypatch.setattr(
            client,
            "_probe_mfa_methods_impl",
            lambda _username, _password: (_ for _ in ()).throw(
                requests.ConnectionError(
                    "POST https://login.microsoftonline.com/SAS?token=PROBE_SECRET"
                )
            ),
        )
        with closing(client), pytest.raises(MicrosoftSSOError, match="MFA method discovery failed") as exc:
            client.probe_mfa_methods(USERNAME, PASSWORD)
        assert "PROBE_SECRET" not in str(exc.value)
        assert "login.microsoftonline.com" not in str(exc.value)

    @pytest.mark.parametrize(
        "pages",
        [
            pytest.param([mfa_html()], id="converged-page"),
            # The MFA-method probe traverses the sso_reload hop too.
            pytest.param([sso_reload_html(), mfa_html()], id="through-sso-reload"),
        ],
    )
    def test_probe_reports_converged_proofs(
        self, scripted: ScriptedSession, isolated_config: Path, pages: list[str]
    ) -> None:
        """A ConvergedTFA page with arrUserProofs probes as 'converged' with
        the parsed proofs — 'legacy_form' is reserved for the no-proofs form."""
        scripted.enqueue(*sso_start(*pages))
        result = make_client(scripted).probe_mfa_methods(USERNAME, PASSWORD)
        assert result.page == "converged"
        assert [p.auth_method_id for p in result.proofs] == ["PhoneAppOTP"]
        assert result.proofs[0].is_default is True


class TestUsernameBootstrap:
    @staticmethod
    def _http_bootstrap(gct_json: dict[str, str]) -> list[FakeResponse]:
        """Mirrored pre-password requests: Me.htm, ssoprobe, dssostatus,
        GetCredentialType, ssoprobe, dssostatus."""
        return [
            FakeResponse(200, html="{}", url="https://login.live.com/Me.htm?v=3"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
            FakeResponse(200, json_data=gct_json, url=f"{MS_BASE}/common/GetCredentialType"),
            FakeResponse(200, html="", url=f"{MS_BASE}/ssoprobe"),
            FakeResponse(200, json_data={}, url=f"{MS_BASE}/dssostatus"),
        ]

    def test_http_bootstrap_when_playwright_missing(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without Playwright the browser pre-password requests are mirrored over HTTP."""
        # Force the ImportError gate in _step_prepare_username — otherwise an
        # environment with playwright installed takes the browser branch.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        login_succeeds(
            scripted,
            *sso_start(config=config_html(url_get_credential_type=True)),
            *self._http_bootstrap({"FlowToken": "FLOW-TOKEN-2", "apiCanary": "API-CANARY-2"}),
            # Password POST lands on SAML directly
            saml_at(CREDS_POST_URL),
        )
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
        install_fake_playwright(monkeypatch, chromium_missing)
        login_succeeds(
            scripted,
            *sso_start(config=config_html(url_get_credential_type=True)),
            *self._http_bootstrap({"FlowToken": "FLOW-TOKEN-2"}),
            saml_at(CREDS_POST_URL),
        )
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
        install_fake_playwright(monkeypatch, chromium_missing)
        monkeypatch.setattr(
            MicrosoftSSOClient,
            "_step_prepare_username_http",
            lambda self, config, username: (_ for _ in ()).throw(
                requests.ConnectionError("network unreachable")
            ),
        )
        scripted.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))
        with pytest.raises(requests.ConnectionError):
            run_login(scripted)

    def test_playwright_semantic_failure_does_not_fall_back(
        self, scripted: ScriptedSession, isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_fake_playwright(monkeypatch, lambda: object())
        monkeypatch.setattr(
            MicrosoftSSOClient,
            "_bootstrap_username_via_playwright",
            lambda self, config, username: (_ for _ in ()).throw(
                MicrosoftSSOError("semantic page failure", step="prepare username")
            ),
        )
        scripted.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))
        with patch.object(MicrosoftSSOClient, "_step_prepare_username_http") as fallback:
            with pytest.raises(MicrosoftSSOError, match="semantic page failure"):
                run_login(scripted)
            fallback.assert_not_called()


# ---------------------------------------------------------------------------
# Converged MFA (SAS API)
# ---------------------------------------------------------------------------


class TestConvergedMfa:
    def test_app_otp_one_step(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Offline Authenticator code completes in one login call."""
        login_succeeds(
            scripted, *mfa_start("PhoneAppOTP"), begin_success(), end_success(), saml_at(PROCESS_URL),
            totp_code=TOTP_CODE,
        )
        for call in [("POST", BEGIN_URL), ("POST", END_URL), ("POST", PROCESS_URL)]:
            assert call in scripted.calls
        assert read_pending() is None

    def test_app_notification_polls_until_approval(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """PhoneAppNotification polls EndAuth while Retry=true, then finishes."""
        login_succeeds(
            scripted,
            *mfa_start("PhoneAppNotification"),
            begin_success(),
            FakeResponse(json_data={"Retry": True, "Entropy": "42"}),
            end_success(),
            saml_at(PROCESS_URL),
        )
        end_posts = [u for m, u in scripted.calls if m == "POST" and u == END_URL]
        assert len(end_posts) == 2

    @pytest.mark.parametrize(
        ("mfa_method", "error"),
        [
            pytest.param("app", "not available", id="app-on-sms-only-account"),
            pytest.param("auto", "valid only for PhoneAppOTP", id="auto-literal-code-on-sms"),
        ],
    )
    def test_unusable_code_rejected_before_beginauth(
        self, scripted: ScriptedSession, isolated_config: Path, mfa_method: str, error: str
    ) -> None:
        scripted.enqueue(*mfa_start("OneWaySMS"))
        with pytest.raises(MicrosoftSSOError, match=error):
            run_login(scripted, totp_code=TOTP_CODE, mfa_method=mfa_method)
        # No SAS API traffic happened.
        assert ("POST", BEGIN_URL) not in scripted.calls

    @pytest.mark.parametrize(
        ("sas_responses", "error"),
        [
            pytest.param(
                [FakeResponse(json_data={"Success": False, "Message": "code send failed"})],
                "MFA setup failed",
                id="beginauth-rejected",
            ),
            pytest.param(
                [begin_success(), FakeResponse(200, html="<html>garbage</html>", url=END_URL)],
                "EndAuth",
                id="endauth-not-json",
            ),
        ],
    )
    def test_sas_api_failure_raises_cleanly(
        self, scripted: ScriptedSession, isolated_config: Path,
        sas_responses: list[FakeResponse], error: str,
    ) -> None:
        scripted.enqueue(*mfa_start("PhoneAppOTP"), *sas_responses)
        with pytest.raises(MicrosoftSSOError, match=error):
            run_login(scripted, totp_code=TOTP_CODE)

    def test_legacy_form_mfa_posts_otc_form(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        """Older MFA pages without arrUserProofs fall back to the otc form POST."""
        login_succeeds(
            scripted,
            *sso_start(),
            FakeResponse(200, html=LEGACY_MFA_HTML, url=MFA_PAGE_URL),
            saml_at(PROCESS_URL),
            totp_code=TOTP_CODE,
            mfa_method="app",
        )
        assert ("POST", PROCESS_URL) in scripted.calls

    def test_legacy_form_rejects_preprovided_literal_code(
        self,
        scripted: ScriptedSession,
        isolated_config: Path,
    ) -> None:
        scripted.enqueue(*sso_start(), FakeResponse(200, html=LEGACY_MFA_HTML, url=MFA_PAGE_URL))
        with pytest.raises(MicrosoftSSOError, match="cannot be validated"):
            run_login(scripted, totp_code=TOTP_CODE, mfa_method="auto")
        assert ("POST", PROCESS_URL) not in scripted.calls


# ---------------------------------------------------------------------------
# Post-MFA interstitials
# ---------------------------------------------------------------------------


class TestPostMfaInterstitials:
    @pytest.mark.parametrize(
        ("pages", "expected_call"),
        [
            # KmsiInterrupt page is auto-submitted ('Stay signed in').
            pytest.param(
                [FakeResponse(200, html=kmsi_html("KmsiInterrupt"), url=PROCESS_URL), *kmsi_submit()],
                ("POST", f"{MS_BASE}/common/login"),
                id="kmsi",
            ),
            # CmsiInterrupt page is auto-submitted like KMSI.
            pytest.param(
                [FakeResponse(200, html=kmsi_html("CmsiInterrupt"), url=PROCESS_URL), *kmsi_submit()],
                ("POST", f"{MS_BASE}/common/login"),
                id="cmsi",
            ),
            # Microsoft auto-submit hiddenform pages are POSTed with their fields.
            pytest.param(
                [FakeResponse(200, html=HIDDENFORM_HTML, url=PROCESS_URL), saml_at(f"{MS_BASE}/common/final")],
                ("POST", f"{MS_BASE}/common/final"),
                id="hiddenform",
            ),
            # A JS window.location carrying SAMLRequest is fetched (no SAMLResponse yet).
            pytest.param(
                [FakeResponse(200, html=SAML_REQUEST_HTML, url=PROCESS_URL), saml_at(f"{ACS_URL}?SAMLRequest=REQ")],
                ("GET", f"{ACS_URL}?SAMLRequest=REQ&RelayState=x"),
                id="saml-request-js-redirect",
            ),
            # ProcessAuth 302 chains are followed until SAML appears.
            pytest.param(
                [
                    FakeResponse(302, url=PROCESS_URL, headers={"Location": "/hop1"}),
                    FakeResponse(302, url=f"{MS_BASE}/hop1", headers={"Location": "https://lighthouse.manipal.edu/hop2"}),
                    saml_at(f"{BASE}/hop2"),
                ],
                ("GET", f"{BASE}/hop2"),
                id="processauth-redirect-chain",
            ),
        ],
    )
    def test_interstitial_is_walked_to_saml(
        self, scripted: ScriptedSession, isolated_config: Path,
        pages: list[FakeResponse], expected_call: tuple[str, str],
    ) -> None:
        login_succeeds(
            scripted, *mfa_start("PhoneAppOTP"), begin_success(), end_success(), *pages,
            totp_code=TOTP_CODE,
        )
        assert expected_call in scripted.calls


# ---------------------------------------------------------------------------
# Deferred MFA + checkpoint resume phases
# ---------------------------------------------------------------------------


class TestDeferAndResume:
    def _defer_login(
        self,
        scripted: ScriptedSession,
        auth_method_id: str = "OneWaySMS",
        display: str = "SMS",
        match: str | None = None,
        **kwargs: Any,
    ) -> None:
        scripted.enqueue(*mfa_start(auth_method_id, display), begin_success())
        with pytest.raises(MfaPendingError, match=match):
            run_login(scripted, defer_mfa_to_pending=True, **kwargs)

    def test_sms_defer_saves_pending_then_verify_completes(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """auth login (defer) checkpoints BeginAuth; auth verify resumes and clears."""
        self._defer_login(scripted)

        pending = read_pending()
        assert pending is not None
        assert pending.get("version") == 2
        for key in ("mfa_page_url", "mfa_config", "begin", "selected_proof", "cookies"):
            assert key in pending
        # The checkpoint is sealed: the raw file carries only envelope + metadata.
        raw = (isolated_config / "mfa_pending.json").read_text()
        assert SAML_TOKEN not in raw
        assert TOTP_CODE not in raw

        verify_succeeds(scripted, end_success(), saml_at(PROCESS_URL))
        assert ("POST", END_URL) in scripted.calls
        assert read_pending() is None

    def test_voice_defer_then_verify_is_codeless(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        self._defer_login(scripted, "TwoWayVoiceMobile", "Call", match="press #", mfa_method="call")

        verify_succeeds(scripted, end_success(), saml_at(PROCESS_URL), code="ok")
        payloads = end_auth_payloads(scripted)
        assert payloads
        assert "AdditionalAuthData" not in payloads[-1]

    def test_push_defer_verify_prints_number_match_on_non_tty_stderr(
        self, scripted: ScriptedSession, isolated_config: Path,
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._defer_login(scripted, "PhoneAppNotification", "Push", match="approval requested", mfa_method="push")

        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        verify_succeeds(
            scripted,
            FakeResponse(json_data={"Retry": True, "Entropy": "42"}),
            end_success(),
            saml_at(PROCESS_URL),
            code="ok",
        )
        captured = capsys.readouterr()
        assert "number shown: 42" in captured.err
        assert captured.out == ""
        payloads = end_auth_payloads(scripted)
        assert payloads
        assert all("AdditionalAuthData" not in payload for payload in payloads)

    def test_verify_without_pending_fails_cleanly(self, scripted: ScriptedSession, isolated_config: Path) -> None:
        with pytest.raises(MicrosoftSSOError, match="No pending MFA session"):
            verify(scripted)

    @pytest.mark.parametrize(
        ("failure", "error", "match"),
        [
            # ProcessAuth dies on the network after EndAuth succeeded.
            pytest.param(
                requests.ConnectionError("connection reset mid-flow"), requests.ConnectionError, None,
                id="transport-error",
            ),
            # ProcessAuth returns a terminal Microsoft page rather than a
            # transport exception.
            pytest.param(
                FakeResponse(200, html=ERROR_HTML, url=PROCESS_URL), MicrosoftSSOError, "50126",
                id="microsoft-error-page",
            ),
        ],
    )
    def test_post_endauth_failure_leaves_checkpoint_resumable(
        self, scripted: ScriptedSession, isolated_config: Path,
        failure: Any, error: type[Exception], match: str | None,
    ) -> None:
        """After EndAuth success the flow tokens are checkpointed, so a
        ProcessAuth failure can be retried safely (resumable phase ms_auth
        EndAuth-success)."""
        self._defer_login(scripted)

        scripted.enqueue(end_success(), failure)
        with pytest.raises(error, match=match):
            verify(scripted)

        pending = read_pending()
        assert pending is not None
        assert pending.get("end_auth_flow")
        assert pending.get("end_auth_ctx")

        # Verify attempt #2 resumes at ProcessAuth and does not send EndAuth
        # again, preserving the one-challenge semantics.
        verify_succeeds(scripted, saml_at(PROCESS_URL))
        assert scripted.calls[3:].count(("POST", END_URL)) == 1
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
        with pytest.raises(requests.ConnectionError):
            verify(scripted)

        pending = read_pending()
        assert pending is not None
        assert "kmsi_checkpoint" in pending

        # Verify attempt #2: submits the saved KMSI page; no EndAuth/ProcessAuth.
        verify_succeeds(scripted, *kmsi_submit())
        remaining = [u for m, u in scripted.calls[3:] if m == "POST"]
        assert remaining.count(END_URL) == 1  # verify #1 only
        assert remaining.count(PROCESS_URL) == 1  # verify #1 only; verify #2 resumes at KMSI
        assert read_pending() is None

    @pytest.mark.parametrize(
        ("result_value", "code", "error"),
        [
            # A rejected code clears the checkpoint (must request a fresh one).
            pytest.param("WrongCode", "000000", "2FA verification failed", id="wrong-code"),
            # Without a saved EndAuth checkpoint, the user must start a new login.
            pytest.param("AuthenticationPreviouslyCompleted", TOTP_CODE, "already accepted", id="already-completed"),
        ],
    )
    def test_terminal_endauth_result_clears_pending(
        self, scripted: ScriptedSession, isolated_config: Path, result_value: str, code: str, error: str
    ) -> None:
        self._defer_login(scripted)

        scripted.enqueue(FakeResponse(json_data={"Success": False, "Retry": False, "ResultValue": result_value}))
        with pytest.raises(MicrosoftSSOError, match=error):
            verify(scripted, code)

        assert read_pending() is None

    def test_network_failure_leaves_pending_retryable(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """A network drop during verify keeps the checkpoint intact for retry."""
        self._defer_login(scripted)

        scripted.enqueue(requests.ConnectionError("dns failure"))
        with pytest.raises(requests.ConnectionError):
            verify(scripted)

        assert read_pending() is not None

        # Retry with a working network completes the login.
        verify_succeeds(scripted, end_success(), saml_at(PROCESS_URL))


class TestDescribePageShape:
    """Sanitized diagnostics for unrecognized post-credentials pages."""

    def test_summary_contains_structure_not_tokens(self):
        html = (
            "<title>Stay signed in?</title>"
            '$Config={"pgid":"ConvergedKmsi","sFT":"SECRET-FLOW-TOKEN",'
            '"sCtx":"SECRET-CTX","urlPost":"/common/SAS"};'
            "<form action='/common/SAS/ProcessAuth'>"
        )
        out = describe_page_shape(snap(html, url="https://login.microsoftonline.com/kmsi?ctx=SECRET-QUERY"))
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
        out = describe_page_shape(snap("", url="", status=302))
        assert "status=302" in out and "(no url)" in out

    def test_summary_masks_email_and_phone_pii(self):
        out = describe_page_shape(snap(
            "<title>ravish.mitmpl2024@learner.manipal.edu +91 9876543210</title>",
            url="https://login.microsoftonline.com/user/ravish%40learner.manipal.edu",
        ))

        assert "ravish.mitmpl2024@learner.manipal.edu" not in out
        assert "9876543210" not in out
        assert "url=login.microsoftonline.com" in out


def recorded(tmp_path: Path, *args: Any, **kwargs: Any) -> str:
    """Write one record through the flow recorder and return the raw log."""
    log = tmp_path / "flow.jsonl"
    with closing(MicrosoftSSOClient(flow_log=str(log))) as client:
        client._record_flow(*args, **kwargs)
    return log.read_text()


class TestFlowRecorder:
    """LIGHTHOUSE_DEBUG_FLOW writes sanitized step records only."""

    def test_direct_record_call_strips_userinfo_query_fragment_and_controls(
        self, tmp_path
    ) -> None:
        lines = recorded(
            tmp_path,
            "GET",
            "https://user:PASSWORD_SENTINEL@login.microsoftonline.com/"
            "path\x00\nwith-control?token=TOKEN_SENTINEL#fragment",
            200,
        ).splitlines()

        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["url"] == "login.microsoftonline.com"
        assert "PASSWORD_SENTINEL" not in lines[0]
        assert "TOKEN_SENTINEL" not in lines[0]
        assert "fragment" not in lines[0]

    def test_direct_record_call_bounds_printable_path(self, tmp_path) -> None:
        record = json.loads(recorded(tmp_path, "GET", "https://login.microsoftonline.com/" + ("x" * 1000), 200))
        assert len(record["url"].split("/", 1)[1]) == 255

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/foo%3Ftoken=FLOW_SECRET", "login.microsoftonline.com/foo"),
            ("/foo%3Fpassword=PASSWORD_SECRET", "login.microsoftonline.com/foo"),
            (
                "/foo%25253Ftoken%25253DFLOW_SECRET",
                "login.microsoftonline.com/foo",
            ),
            (
                "/foo%26flow_token%3DFLOW_SECRET",
                "login.microsoftonline.com/foo",
            ),
            (
                "/foo%3Fheaders%3D%7B%22Cookie%22%3A%22COOKIE_SECRET%22%7D",
                "login.microsoftonline.com/foo",
            ),
        ],
    )
    def test_direct_record_call_removes_nested_encoded_secret_paths(
        self, tmp_path, path: str, expected: str
    ) -> None:
        raw = recorded(tmp_path, "GET", f"https://login.microsoftonline.com{path}", 200)

        assert json.loads(raw)["url"] == expected
        assert "FLOW_SECRET" not in raw
        assert "PASSWORD_SECRET" not in raw
        assert "COOKIE_SECRET" not in raw

    @pytest.mark.parametrize(
        "path",
        [
            "/user/ravish%40learner.manipal.edu",
            "/phone/%2B919876543210",
            "/user/ravish%40learner.manipal.edu%3Ftoken=SECRET",
        ],
    )
    def test_direct_record_call_does_not_log_email_or_phone_pii(
        self, tmp_path, path: str
    ) -> None:
        raw = recorded(tmp_path, "GET", f"https://login.microsoftonline.com{path}", 200)

        assert json.loads(raw)["url"] == "login.microsoftonline.com"
        assert "ravish" not in raw
        assert "manipal.edu" not in raw
        assert "9876543210" not in raw
        assert "SECRET" not in raw

    def test_direct_record_call_redacts_untrusted_field_names(self, tmp_path) -> None:
        raw = recorded(
            tmp_path,
            "POST",
            "https://login.microsoftonline.com/common/login",
            field_names=[
                "password=SECRET_SENTINEL",
                "password:OTHER_SECRET",
                "foo secret SECRET",
                "ravish@example.com",
                "passwd",
                "field\nwith-control",
            ],
        )

        assert json.loads(raw)["form_fields"] == ["passwd", "(redacted)"]
        assert "SECRET_SENTINEL" not in raw
        assert "OTHER_SECRET" not in raw
        assert "ravish@example.com" not in raw
        assert "with-control" not in raw

    def test_records_are_sanitized(self, tmp_path):
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
        """The ssoprobe GETs reach the flow log."""
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


class SimplejsonLikeDecodeError(ValueError):
    pass


class SimplejsonStyleResponse(FakeResponse):
    def json(self) -> Any:
        raise SimplejsonLikeDecodeError("Expecting value")


class TestGctMalformedResponse:
    """A 200/non-JSON GetCredentialType response must not crash the flow."""

    @pytest.mark.parametrize(
        ("response", "form_fields"),
        [
            # No UnboundLocalError; the body is flagged as unparseable.
            pytest.param(FakeResponse(200, html="<html>not json</html>"), ["(unparseable)"], id="non-json"),
            # Valid-but-non-object JSON must not crash key extraction
            # (no AttributeError on .keys()); there are no keys to report.
            pytest.param(FakeResponse(200, json_data=[1, 2, 3]), [], id="json-array"),
            pytest.param(FakeResponse(200, json_data="ok"), [], id="json-string"),
            # requests may parse JSON with simplejson, whose JSONDecodeError is
            # NOT json.JSONDecodeError — both subclass ValueError, which the flow
            # catches, so such environments degrade gracefully too.
            pytest.param(SimplejsonStyleResponse(200), ["(unparseable)"], id="simplejson-decode-error"),
        ],
    )
    def test_malformed_200_returns_config_unchanged(
        self, tmp_path, response: FakeResponse, form_fields: list[str]
    ) -> None:
        client = MicrosoftSSOClient(flow_log=str(tmp_path / "flow.jsonl"))
        client._session = ScriptedSession()
        client._session.enqueue(response)
        config = {"sFT": "tok", "sCtx": "ctx", "urlPost": "/common/login",
                  "urlGetCredentialType": "/common/GetCredentialType",
                  "_ms_url": "https://login.microsoftonline.com/x"}
        out = client._step_get_credential_type(config, "user@example.edu")
        assert out == config  # unchanged — no raw traceback escapes
        records = [json.loads(line) for line in (tmp_path / "flow.jsonl").read_text().splitlines()]
        # Post-response GCT record uses the POST method label (matches the
        # pre-request intent record).
        gct = [r for r in records if "GetCredentialType" in r["url"] and r.get("status") == 200]
        assert gct and gct[0]["method"] == "POST"
        assert gct[0]["form_fields"] == form_fields


# ---------------------------------------------------------------------------
# Aug-2026 session-pull reload interstitial (sso_reload=True + oPostParams)
# ---------------------------------------------------------------------------


class TestSsoReloadInterstitial:
    """A form-less 200 "Redirecting" page after the password POST must be
    detected and re-POSTed (bounded), not reported as an unexpected response."""

    # -- pure detection ------------------------------------------------------

    def test_detects_interstitial_markers(self) -> None:
        assert is_sso_reload_page(snap(sso_reload_html()))

    @pytest.mark.parametrize(
        "html",
        [
            config_html(),
            mfa_html(),
            LEGACY_MFA_HTML,
            HIDDENFORM_HTML,
            kmsi_html(),
            sso_reload_html(o_post_params={}),
            sso_reload_html(url_post="/tenant-id/login?ctx=CTX"),
        ],
        ids=["config", "converged-mfa", "legacy-mfa", "hiddenform", "kmsi",
             "empty-opostparams", "urlpost-without-sso-reload"],
    )
    def test_normal_pages_are_not_interstitial(self, html: str) -> None:
        assert not is_sso_reload_page(snap(html))

    # -- pure transition -----------------------------------------------------

    def test_transition_reposts_echoed_params_to_absolute_url(self) -> None:
        t = sso_reload_transition(snap(sso_reload_html()), CREDS_POST_URL)
        assert t.kind == "sso_reload"
        # Tenant-relative urlPost resolves against the response URL.
        assert t.url == f"{MS_BASE}{SSO_RELOAD_URL_POST}"
        # The echoed credential form round-trips verbatim (field names only
        # asserted; values flow straight back to Microsoft, never to logs).
        assert set(t.data or {}) == set(SSO_RELOAD_FIELDS)

    @pytest.mark.parametrize(
        ("html", "kind"),
        [(sso_reload_html(), "sso_reload"), (mfa_html(), "mfa")],
        ids=["interstitial-is-repost", "converged-tfa-is-mfa-terminal"],
    )
    def test_classify_post_mfa(self, html: str, kind: str) -> None:
        assert classify_post_mfa(snap(html), CREDS_POST_URL).kind == kind

    @pytest.mark.parametrize(
        "url_post",
        [
            "https://evil.example/login?sso_reload=True",
            "//evil.example/login?sso_reload=True",
            *(f"{url}?sso_reload=True" for url in UNSAFE_MS_URLS),
            "https://login.microsoftonline.com:0/tenant-id/login?sso_reload=True",
            "https://login.microsoftonline.com:8443/tenant-id/login?sso_reload=True",
        ],
    )
    def test_reload_rejects_unsafe_url_shapes_without_echoing_target(
        self, url_post: str
    ) -> None:
        with pytest.raises(MicrosoftSSOError, match="unsafe re-POST target") as exc:
            sso_reload_transition(snap(sso_reload_html(url_post=url_post)), CREDS_POST_URL)
        assert url_post not in str(exc.value)

    @pytest.mark.parametrize(
        "location",
        ["/relative/login", "relative/login", "//evil.example/login", *UNSAFE_MS_URLS],
    )
    def test_initial_redirect_rejects_unsafe_url_before_next_request(
        self,
        scripted: ScriptedSession,
        location: str,
    ) -> None:
        scripted.enqueue(FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": location}))
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe Microsoft redirect"
        ) as exc:
            client._step_initiate_saml()
        assert location not in str(exc.value)
        assert scripted.calls == [("GET", LOGIN_INIT_URL)]

    def test_initial_script_redirect_rejects_relative_target(
        self,
        scripted: ScriptedSession,
    ) -> None:
        scripted.enqueue(
            FakeResponse(
                200,
                url=LOGIN_INIT_URL,
                html='<script>window.location="/relative/login"</script>',
            )
        )
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe Microsoft script redirect"
        ):
            client._step_initiate_saml()

    @pytest.mark.parametrize("url_post", ["//evil.example/common/login", *UNSAFE_MS_URLS])
    def test_password_post_rejects_unsafe_config_endpoint(
        self, scripted: ScriptedSession, url_post: str
    ) -> None:
        config = {
            "sFT": "FLOW-TOKEN",
            "sCtx": "CTX-TOKEN",
            "urlPost": url_post,
            "_ms_url": MS_SSO_URL,
        }
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe login endpoint"
        ) as exc:
            client._step_post_credentials(config, USERNAME, PASSWORD)
        assert url_post not in str(exc.value)
        assert scripted.calls == []

    def test_password_redirect_rejects_unsafe_location_before_get(
        self, scripted: ScriptedSession
    ) -> None:
        hostile = "https://login.microsoftonline.com.evil.example/after"
        scripted.enqueue(FakeResponse(302, url=CREDS_POST_URL, headers={"Location": hostile}))
        config = {
            "sFT": "FLOW-TOKEN",
            "sCtx": "CTX-TOKEN",
            "urlPost": CREDS_POST_URL,
            "_ms_url": MS_SSO_URL,
        }
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe Microsoft redirect"
        ) as exc:
            client._step_post_credentials(config, USERNAME, PASSWORD)
        assert hostile not in str(exc.value)
        assert scripted.calls == [("POST", CREDS_POST_URL)]

    @pytest.mark.parametrize(
        ("field", "safe_url", "expected_calls"),
        [
            ("urlBeginAuth", BEGIN_URL, []),
            ("urlEndAuth", END_URL, [("POST", BEGIN_URL)]),
            ("urlPost", PROCESS_URL, [("POST", BEGIN_URL)]),
        ],
    )
    def test_mfa_json_endpoint_rejected_before_mfa_post(
        self, scripted: ScriptedSession, field: str, safe_url: str, expected_calls: list[tuple[str, str]]
    ) -> None:
        hostile = "https://evil.example/common/SAS/endpoint"
        html = mfa_html().replace(f'"{field}": "{safe_url}"', f'"{field}": "{hostile}"')
        if expected_calls:
            scripted.enqueue(begin_success())
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe MFA endpoint"
        ) as exc:
            client._step_handle_mfa(
                snap(html, url=MFA_PAGE_URL),
                {"sFT": "FLOW", "sCtx": "CTX"},
                TOTP_CODE,
                mfa_method=MFA_METHOD_APP,
            )
        assert hostile not in str(exc.value)
        assert scripted.calls == expected_calls

    def test_saml_form_action_rejected_before_assertion_post(
        self, scripted: ScriptedSession
    ) -> None:
        hostile = "https://evil.example/d2l/lp/auth/saml/consume"
        html = (
            '<form method="POST" action="'
            + hostile
            + '"><input name="RelayState" value="state"></form>'
        )
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe D2L ACS endpoint"
        ) as exc:
            client._step_post_saml(SAML_TOKEN, html)
        assert hostile not in str(exc.value)
        assert scripted.calls == []

    def test_saml_307_redirect_rejected_before_assertion_repost(
        self, scripted: ScriptedSession
    ) -> None:
        hostile = "https://evil.example/d2l/home"
        scripted.enqueue(FakeResponse(307, url=ACS_URL, headers={"Location": hostile}))
        with closing(make_client(scripted)) as client, pytest.raises(
            MicrosoftSSOError, match="unsafe D2L redirect"
        ) as exc:
            client._step_post_saml(SAML_TOKEN)
        assert hostile not in str(exc.value)
        assert scripted.calls == [("POST", ACS_URL)]

    def test_saml_path_relative_redirect_uses_response_url(
        self, scripted: ScriptedSession
    ) -> None:
        next_url = f"{BASE}/d2l/lp/auth/saml/next"
        scripted.enqueue(
            FakeResponse(302, url=ACS_URL, headers={"Location": "next"}),
            FakeResponse(200, url=next_url),
        )
        with closing(make_client(scripted)) as client:
            response = client._post_with_redirects(ACS_URL, data={"SAMLResponse": SAML_TOKEN})

        assert response.status_code == 200
        assert scripted.calls == [("POST", ACS_URL), ("GET", next_url)]

    def test_explicit_default_https_port_is_same_origin(self) -> None:
        html = sso_reload_html(
            url_post=(
                "https://login.microsoftonline.com:443/tenant-id/login"
                "?sso_reload=True"
            )
        )
        transition = sso_reload_transition(snap(html), CREDS_POST_URL)

        assert transition.kind == "sso_reload"
        assert transition.url.startswith("https://login.microsoftonline.com:443/")

    def test_nested_reload_value_is_rejected_without_value_echo(self) -> None:
        html = sso_reload_html(o_post_params={"passwd": ["nested-secret"]})
        with pytest.raises(MicrosoftSSOError, match="unsupported value type") as exc:
            sso_reload_transition(snap(html), CREDS_POST_URL)
        assert "nested-secret" not in str(exc.value)

    # -- walk integration ----------------------------------------------------

    @pytest.mark.parametrize(
        "terminal",
        [
            # The pre-Aug-2026 ConvergedError wrong-password page.
            pytest.param(ERROR_HTML, id="converged-error"),
            # Live-shaped ConvergedSignIn error page with the OTC-flag
            # false-positive bait (see test_signin_error_page_is_not_mfa).
            pytest.param(
                "<html><head><title>Sign in to your account</title></head><body><script>\n"
                "$Config = {\n"
                '"pgid": "ConvergedSignIn",\n'
                '"sErrorCode": "50126",\n'
                '"fAvoidNewOTCGenerationWhenAlreadySent": true,\n'
                '"sErrTxt": ""\n'
                "};\n</script>\n"
                "apps like Microsoft Authenticator are available.\n"
                "</body></html>",
                id="converged-signin",
            ),
        ],
    )
    def test_login_reports_wrong_password_through_interstitial(
        self, scripted: ScriptedSession, isolated_config: Path, terminal: str
    ) -> None:
        """Wrong-password flow: password POST -> interstitial -> re-POST ->
        real error page -> clean 50126 wrong-password error."""
        scripted.enqueue(*sso_start(sso_reload_html(), terminal))
        with pytest.raises(MicrosoftSSOError) as ei:
            run_login(scripted)
        assert "50126" in str(ei.value)
        assert "2FA" not in str(ei.value)

    def test_walk_is_bounded_by_local_reload_budget(
        self, scripted: ScriptedSession, isolated_config: Path
    ) -> None:
        """A looping interstitial stops after the local safety budget."""
        scripted.enqueue(*sso_start(*[sso_reload_html()] * (_MAX_SSO_RELOADS + 2)))
        with pytest.raises(MicrosoftSSOError, match="reload limit exceeded"):
            run_login(scripted)
        # Exactly _MAX_SSO_RELOADS re-POSTs were issued (password POST + the
        # bounded reloads; the third interstitial is returned, never re-POSTed).
        reposts = [c for c in scripted.calls if c[0] == "POST"]
        assert len(reposts) == 1 + _MAX_SSO_RELOADS

    # -- leak guards ---------------------------------------------------------

    def test_flow_log_records_field_names_only(
        self, scripted: ScriptedSession, isolated_config: Path, tmp_path: Path
    ) -> None:
        """oPostParams echoes the password: the recorder must persist field
        NAMES and marker booleans only, never values."""
        flow_log = tmp_path / "flow.jsonl"
        scripted.enqueue(*sso_start(sso_reload_html(), ERROR_HTML))
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
        shape = describe_page_shape(snap(sso_reload_html()))
        assert "oPostParams=1" in shape
        assert "sso_reload=1" in shape
        assert "Redirecting" in shape
        # And the normal login page stays all-zeros for the new markers.
        assert "oPostParams=0" in describe_page_shape(snap(config_html()))

    def test_signin_error_page_is_not_mfa(self) -> None:
        """The ConvergedSignIn page the sso_reload walk lands on after a
        wrong password carries $Config flags like fAvoidNewOTCGeneration… —
        a bare "otc" substring made is_mfa_page misroute it to MFA handling
        (live regression, Aug 2026). Word-bounded matching keeps it an error."""
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
        assert not is_mfa_page(html)
        assert is_error_page(snap(html))
