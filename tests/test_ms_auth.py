"""Tests for MicrosoftSSOClient — pure HTTP Microsoft SSO client."""

from __future__ import annotations

from contextlib import closing
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from lighthouse_cli.config import BASE_URL, COOKIE_NAMES
from lighthouse_cli.ms_auth import (
    MS_ERROR_CODES,
    VALID_MFA_METHODS,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    ResponseSnapshot,
    UserProof,
    _browser_cookies,
    _extract_config_json,
    _extract_error_code_and_msg,
    _parse_user_proofs,
    _select_user_proof,
    build_end_payload,
    build_sso_error,
    extract_saml_response,
    is_error_page,
    is_mfa_page,
    kmsi_page_detected,
    safe_upstream_text,
)
from lighthouse_cli.ms_errors import (
    CODELESS_APPROVAL_AUTH_IDS,
    MFA_METHOD_APP,
    MFA_METHOD_CALL,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_PUSH,
    MFA_METHOD_SMS,
    SERVER_SENT_CODE_AUTH_IDS,
)
from lighthouse_cli.ms_mfa import _prompt_user_proof_choice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MS_SSO_URL = "https://login.microsoftonline.com/common/oauth2/authorize"

SAMPLE_CONFIG_HTML = """<html>
<head><title>Sign in to your account</title></head>
<body>
<script>
$Config = {
    "sFT": "flow-token-123",
    "sCtx": "rQIIAQs...ctx-token...",
    "urlPost": "https://login.microsoftonline.com/common/login",
    "canary": "canary-token-456",
    "apiCanary": "api-canary-token",
    "hpgrequestid": "request-id-789",
    "correlationId": "corr-id-001",
    "sessionId": "session-id-002",
    "fid": "fid-003",
    "deviceId": "device-004"
};
</script>
<form id="loginForm" action="https://login.microsoftonline.com/common/login">
    <input name="login" type="email">
    <input name="passwd" type="password">
</form>
</body></html>"""

SAMPLE_MFA_HTML = """<html>
<head><title>Verify your identity</title></head>
<body>
<div id="idDiv_SAOTCC_Description">Enter your verification code</div>
<form id="mfaForm" action="https://login.microsoftonline.com/common/SAS/ProcessAuth">
    <input type="hidden" name="sFT" value="mfa-flow-token-999">
    <input type="hidden" name="sCtx" value="mfa-ctx-token">
    <input type="hidden" name="canary" value="mfa-canary">
    <input type="hidden" name="hpgrequestid" value="mfa-req-id">
    <input type="text" name="otc" placeholder="Enter code">
</form>
</body></html>"""

SAMPLE_SAML_HTML = """<html>
<body>
    <form method="POST" action="https://lighthouse.manipal.edu/d2l/lp/auth/saml/consume">
        <input type="hidden" name="SAMLResponse" value="PHNhbWxwOlJlc3BvbnNlIHhtbG5zOnNhbWxwPS...long-base64-string...">
        <input type="hidden" name="RelayState" value="https://lighthouse.manipal.edu/d2l/home">
    </form>
</body></html>"""

SAMPLE_ERROR_HTML = """<html>
<head><title>Sign in to your account</title></head>
<body>
<div id="loginError">Sorry, your password is incorrect</div>
<script>
$Config = {
    "sFT": "flow-token-error",
    "sCtx": "error-ctx",
    "urlPost": "https://login.microsoftonline.com/common/login",
    "serverError": "50126",
    "sErrTxt": "Invalid username or password."
};
</script>
</body></html>"""

SMS_PROOF = UserProof("OneWaySMS", "SMS", "+00 ***", True)


def make_mock_response(
    status_code: int = 200,
    text: str = "",
    headers: dict | None = None,
    url: str = "https://example.com",
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    resp.url = url
    resp.raise_for_status = MagicMock()
    # For cookies, we mock at the session level
    return resp


def _json_response(payload: object) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    return response


def _retry_response() -> MagicMock:
    return _json_response({"Retry": True, "FlowToken": "flow", "Ctx": "ctx"})


def _poll_end_auth(
    client: MicrosoftSSOClient,
    proof: UserProof = SMS_PROOF,
    code: str = "123456",
    polling_interval: int | None = None,
) -> Any:
    config: dict[str, Any] = {"urlEndAuth": "/common/SAS/EndAuth"}
    if polling_interval is not None:
        config["oPerAuthPollingInterval"] = {proof.auth_method_id: polling_interval}
    return client._poll_end_auth(
        MS_SSO_URL, config, proof, {"FlowToken": "flow", "Ctx": "ctx"}, code,
    )


# ---------------------------------------------------------------------------
# _extract_config_json tests
# ---------------------------------------------------------------------------

SAMPLE_CONVERGED_TFA_HTML = """<html><body>ConvergedTFA
<script>
$Config = {
    "sFT": "mfa-flow",
    "sCtx": "mfa-ctx",
    "sFTName": "flowToken",
    "urlPost": "https://login.microsoftonline.com/common/SAS/ProcessAuth",
    "urlBeginAuth": "https://login.microsoftonline.com/common/SAS/BeginAuth",
    "urlEndAuth": "https://login.microsoftonline.com/common/SAS/EndAuth",
    "canary": "canary-1",
    "sPOST_Username": "user@manipal.edu",
    "arrUserProofs": [
        {"authMethodId": "PhoneAppOTP", "display": "Authenticator app", "data": "", "isDefault": true},
        {"authMethodId": "OneWaySMS", "display": "Text +91 ***1234", "data": "+919876541234", "isDefault": false}
    ]
};
</script>
</body></html>"""


def _proofs_from(html: str) -> list[UserProof]:
    return _parse_user_proofs(_extract_config_json(html) or {})


class TestMfaMethodSelection:
    @pytest.mark.parametrize(
        ("method", "expected"),
        [(MFA_METHOD_SMS, "OneWaySMS"), (MFA_METHOD_APP, "PhoneAppOTP"), ("auto", "PhoneAppOTP")],
        ids=["sms-when-registered", "app-when-requested", "auto-uses-default"],
    )
    def test_select_proof(self, method: str, expected: str) -> None:
        selected = _select_user_proof(_proofs_from(SAMPLE_CONVERGED_TFA_HTML), method)
        assert selected.auth_method_id == expected

    def test_choose_prompts_for_selection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proofs = _proofs_from(SAMPLE_CONVERGED_TFA_HTML)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "2")
        selected = _select_user_proof(proofs, MFA_METHOD_CHOOSE)
        assert selected.auth_method_id == "OneWaySMS"

    def test_choose_labels_same_destination_by_method(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        proofs = [
            UserProof("OneWaySMS", "+XX XXXXXXXX04", "+001234567804", True),
            UserProof("TwoWayVoiceMobile", "+XX XXXXXXXX04", "+001234567804", False),
        ]
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "1")

        selected = _select_user_proof(proofs, MFA_METHOD_CHOOSE)
        output = capsys.readouterr().err

        assert selected.auth_method_id == "OneWaySMS"
        assert "Text code (SMS or WhatsApp): ***7804" in output
        assert "Voice call to mobile: ***7804" in output
        assert "Microsoft default" in output
        assert "+001234567804" not in output

    def test_untrusted_display_never_reaches_selection_error_or_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        malicious = (
            "FULL-DISPLAY-SENTINEL user@example.com +919876541234\x1b[31m"
        )
        proofs = [
            UserProof("OneWaySMS", malicious, "+919876541234", True),
            UserProof("TwoWayVoiceMobile", malicious, "+919876541234", False),
        ]
        with pytest.raises(MicrosoftSSOError) as error:
            _select_user_proof(proofs, MFA_METHOD_APP)
        assert malicious not in str(error.value)

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "1")

        _select_user_proof(proofs, MFA_METHOD_CHOOSE)
        selection_output = capsys.readouterr().err

        with closing(MicrosoftSSOClient()) as client:
            client._print_mfa_phase_banner(
                proofs, proofs[0], code_sent_on_begin=False
            )
        banner_output = capsys.readouterr().err
        output = selection_output + banner_output

        assert "FULL-DISPLAY-SENTINEL" not in output
        assert "user@example.com" not in output
        assert "+919876541234" not in output
        assert "Text code (SMS or WhatsApp): ***1234" in output
        assert "Voice call to mobile: ***1234" in output

    def test_short_phone_data_uses_placeholder_in_sms_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        proof = UserProof("OneWaySMS", "ignored", "REAL_SECRET", True)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        with closing(MicrosoftSSOClient()) as client:
            client._print_mfa_phase_banner(
                [proof], proof, code_sent_on_begin=True
            )
        output = capsys.readouterr().err

        assert "REAL_SECRET" not in output
        assert "A verification code was just sent to your phone." in output

    def test_choose_single_proof_skips_prompt(self) -> None:
        single = [UserProof("OneWaySMS", "SMS", "+91", True)]
        assert _prompt_user_proof_choice(single).auth_method_id == "OneWaySMS"


class TestCollectTotpAfterChallenge:
    def test_app_otp_keeps_preprovided_code(self) -> None:
        """PhoneAppOTP is offline TOTP: a pre-provided --totp code must be kept, not discarded."""
        selected = UserProof("PhoneAppOTP", "Authenticator app", "", True)
        with closing(MicrosoftSSOClient()) as client:
            code = client._collect_totp_after_challenge(
                selected, "123456", read_totp_after_challenge=False, code_sent_on_begin=False
            )
        assert code == "123456"


class TestExtractConfigJson:
    def test_extracts_valid_config(self) -> None:
        config = _extract_config_json(SAMPLE_CONFIG_HTML)
        assert config is not None
        assert config["sFT"] == "flow-token-123"
        assert config["urlPost"] == "https://login.microsoftonline.com/common/login"
        assert config["sCtx"] == "rQIIAQs...ctx-token..."

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            pytest.param("<html><body>No config here</body></html>", None, id="no-config"),
            pytest.param('<script>$Config = {bad: "json"};</script>', None, id="malformed-json"),
            # Trailing comma (invalid JSON) should fail gracefully.
            pytest.param('<script>$Config = {"key": "value",};</script>', None, id="trailing-comma"),
            pytest.param(
                '<script>var x=1;</script><script>$Config = {"key": "value"};</script>',
                {"key": "value"},
                id="multiple-scripts",
            ),
            pytest.param(
                '<script>$Config = {"outer": {"inner": "val"}};</script>',
                {"outer": {"inner": "val"}},
                id="nested-objects",
            ),
            pytest.param(
                '<script>$Config = {"urlPost": "https://example.com/login\\u002fpage"};</script>',
                {"urlPost": "https://example.com/login/page"},
                id="escaped-chars",
            ),
        ],
    )
    def test_edge_cases(self, html: str, expected: dict[str, Any] | None) -> None:
        assert _extract_config_json(html) == expected


# ---------------------------------------------------------------------------
# _extract_error_code_and_msg tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(SAMPLE_ERROR_HTML, (50126, "Invalid username or password."), id="code-and-msg"),
        pytest.param("<html><body>OK</body></html>", (None, None), id="no-error"),
        pytest.param(
            '<div id="loginError">Your account is locked.</div>',
            (None, "Your account is locked."),
            id="fallback-to-div-error",
        ),
        # B8: mixed-case "Error.aspx" on a ConvergedTFA page is a benign 504.
        pytest.param(
            '<html><script>$Config={"serverError":"504"};</script>'
            "ConvergedTFA redirect to /common/Error.aspx?err=504</html>",
            (None, None),
            id="504-error-aspx-suppressed-case-insensitive",
        ),
    ],
)
def test_extract_error_code_and_msg(html: str, expected: tuple[Any, Any]) -> None:
    assert _extract_error_code_and_msg(html) == expected


# ---------------------------------------------------------------------------
# MicrosoftSSOClient unit tests
# ---------------------------------------------------------------------------

class TestMicrosoftSSOClientInit:
    def test_default_init(self) -> None:
        with closing(MicrosoftSSOClient()) as client:
            assert client._timeout == 30
            assert "User-Agent" in client._session.headers

    def test_custom_timeout_and_user_agent(self) -> None:
        with closing(MicrosoftSSOClient(timeout=15, user_agent="MyApp/1.0")) as client:
            assert client._timeout == 15
            assert client._session.headers["User-Agent"] == "MyApp/1.0"


@pytest.mark.parametrize(
    ("status_code", "html", "expected"),
    [
        pytest.param(400, "", True, id="400-status"),
        pytest.param(200, "serverError: 50126", True, id="servererror-in-body"),
        pytest.param(200, "<html>Login page</html>", False, id="ok-page"),
    ],
)
def test_is_error_page(status_code: int, html: str, expected: bool) -> None:
    snap = ResponseSnapshot(url="https://x", status_code=status_code, location="", html=html)
    assert is_error_page(snap) is expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param("ConvergedTFA page content", True, id="converged-tfa"),
        pytest.param(SAMPLE_MFA_HTML, True, id="otc-input"),
        pytest.param('<div>Enter code</div>', True, id="enter-code-text"),
        pytest.param(SAMPLE_SAML_HTML, False, id="saml-page"),
    ],
)
def test_is_mfa_page(html: str, expected: bool) -> None:
    assert is_mfa_page(html) is expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            SAMPLE_SAML_HTML,
            "PHNhbWxwOlJlc3BvbnNlIHhtbG5zOnNhbWxwPS...long-base64-string...",
            id="hidden-input",
        ),
        pytest.param("<html>No SAML here</html>", None, id="not-present"),
        pytest.param(
            '<input name="SAMLResponse" value="BASE64SAMLTOKEN">', "BASE64SAMLTOKEN",
            id="name-value-pattern",
        ),
    ],
)
def test_extract_saml_response(html: str, expected: str | None) -> None:
    assert extract_saml_response(html) == expected


class TestMicrosoftSSOClientExtractD2lCookies:
    def test_extracts_only_the_four_d2l_cookies(self) -> None:
        d2l = {
            "d2lSecureSessionVal": "sec123",
            "d2lSessionVal": "ses123",
            "d2lSameSiteCanaryA": "canaryA",
            "d2lSameSiteCanaryB": "canaryB",
        }
        with closing(MicrosoftSSOClient()) as client:
            for name, value in d2l.items():
                client._session.cookies.set(name, value, domain="lighthouse.manipal.edu")
            client._session.cookies.set("_ga", "tracking", domain="lighthouse.manipal.edu")
            client._session.cookies.set("session", "other", domain="example.com")

            assert client._extract_d2l_cookies() == d2l

    def test_raises_on_missing_cookies(self) -> None:
        with closing(MicrosoftSSOClient()) as client:
            with pytest.raises(MicrosoftSSOError, match="Missing required D2L cookies"):
                client._extract_d2l_cookies()


# ---------------------------------------------------------------------------
# Full login flow tests with mocked HTTP
# ---------------------------------------------------------------------------

def test_fresh_login_clears_stale_pending_checkpoint_before_network() -> None:
    with closing(MicrosoftSSOClient()) as client:
        with patch("lighthouse_cli.config.clear_mfa_pending") as clear_pending, \
                patch.object(
                    client,
                    "_step_initiate_saml",
                    side_effect=MicrosoftSSOError("stopped", step="test"),
                ):
            with pytest.raises(MicrosoftSSOError, match="stopped"):
                client.login("user@example.invalid", "not-a-real-password")

    clear_pending.assert_called_once_with()


def _mock_session(
    get_responses: list[MagicMock],
    post_responses: list[MagicMock],
    cookie_prefix: str | None = None,
) -> MagicMock:
    """A requests.Session stand-in for the fresh session login() creates.

    The first two GETs are always the SAML init (302 to Microsoft) and the
    Microsoft login page carrying ``$Config``.
    """
    session = MagicMock()
    session.headers = {}
    session.cookies = requests.cookies.RequestsCookieJar()
    if cookie_prefix is not None:
        for name in COOKIE_NAMES:
            session.cookies.set(name, f"{cookie_prefix}{name}", domain="lighthouse.manipal.edu")
    session.get = MagicMock(side_effect=[
        make_mock_response(302, headers={"Location": MS_SSO_URL}),
        make_mock_response(200, text=SAMPLE_CONFIG_HTML, url=MS_SSO_URL),
        *get_responses,
    ])
    session.post = MagicMock(side_effect=post_responses)
    return session


def _login(session: MagicMock, *args: Any, **kwargs: Any) -> dict[str, str]:
    with closing(MicrosoftSSOClient()) as client:
        with patch("requests.Session", return_value=session):
            return client.login(*args, **kwargs)


class TestFullLoginFlow:
    """Test the complete login flow using mocked requests.Session."""

    def test_full_login_flow_with_mfa(self) -> None:
        """Complete login flow: SAML init -> MS config -> POST creds -> MFA -> SAML -> cookies."""
        resp_acs = make_mock_response(302, headers={"Location": f"{BASE_URL}/d2l/home"})
        session = _mock_session(
            [
                make_mock_response(200, text=SAMPLE_SAML_HTML),  # follow TOTP redirect -> SAML page
                resp_acs,  # follow ACS redirect
            ],
            [
                make_mock_response(200, text=SAMPLE_MFA_HTML),  # POST credentials -> MFA
                make_mock_response(  # POST TOTP
                    302, headers={"Location": f"{BASE_URL}/d2l/lp/auth/saml/consume"},
                ),
                resp_acs,  # POST SAML
            ],
            cookie_prefix="test-",
        )

        cookies = _login(
            session, "test@manipal.edu", "password123", "123456", mfa_method=MFA_METHOD_APP,
        )

        assert len(cookies) == 4
        assert set(cookies) == set(COOKIE_NAMES)

    def test_login_with_invalid_credentials(self) -> None:
        """Invalid credentials raise MicrosoftSSOError with descriptive message."""
        session = _mock_session([], [make_mock_response(200, text=SAMPLE_ERROR_HTML)])

        with pytest.raises(MicrosoftSSOError, match="50126"):
            _login(session, "bad@manipal.edu", "wrong_password", "123456")

    def test_login_mfa_with_wrong_code(self) -> None:
        """Wrong 2FA code raises MicrosoftSSOError."""
        # POST creds -> MFA; POST wrong TOTP -> MFA page again (200, still shows MFA),
        # which _step_handle_mfa detects and raises on.
        session = _mock_session(
            [],
            [make_mock_response(200, text=SAMPLE_MFA_HTML), make_mock_response(200, text=SAMPLE_MFA_HTML)],
        )

        with pytest.raises(MicrosoftSSOError, match="2FA verification failed"):
            _login(session, "test@manipal.edu", "password123", "000000", mfa_method=MFA_METHOD_APP)

    def test_login_without_mfa(self) -> None:
        """Login without MFA (direct SAML after credentials)."""
        resp_acs = make_mock_response(302, headers={"Location": f"{BASE_URL}/d2l/home"})
        # Credentials POST returns SAML directly (no MFA)
        session = _mock_session(
            [resp_acs],
            [make_mock_response(200, text=SAMPLE_SAML_HTML), resp_acs],
            cookie_prefix="val-",
        )

        cookies = _login(session, "test@manipal.edu", "password123", None)
        assert len(cookies) == 4

    def test_saml_init_reaches_microsoft(self) -> None:
        """Step 1: SAML init redirects to Microsoft."""
        ms_url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=..."
        with closing(MicrosoftSSOClient()) as client:
            client._session.get = MagicMock(
                return_value=make_mock_response(302, headers={"Location": ms_url}),
            )
            url = client._step_initiate_saml()
        assert "login.microsoftonline.com" in url


class TestMicrosoftSSOError:
    def test_error_with_step_and_recovery(self) -> None:
        err = MicrosoftSSOError(
            "Failed to authenticate",
            step="POST credentials",
            recovery="Check your password.",
        )
        msg = str(err)
        assert "Failed to authenticate" in msg
        assert "POST credentials" in msg
        assert "Check your password" in msg

    def test_error_without_step(self) -> None:
        err = MicrosoftSSOError("Simple error")
        assert str(err) == "Simple error"


class TestMSErrorCodes:
    def test_all_error_codes_have_messages(self) -> None:
        """All MS error codes should have descriptive messages."""
        assert len(MS_ERROR_CODES) > 0
        for code, msg in MS_ERROR_CODES.items():
            assert isinstance(code, int)
            assert isinstance(msg, str)
            assert len(msg) > 0

    def test_common_codes(self) -> None:
        assert 50126 in MS_ERROR_CODES
        assert MS_ERROR_CODES[50126] == "Invalid username or password."
        assert 50034 in MS_ERROR_CODES
        assert 50053 in MS_ERROR_CODES


class TestBuildSsoError:
    @pytest.mark.parametrize(
        ("code", "msg", "step", "needles"),
        [
            pytest.param(50126, None, "POST credentials", ["50126", "Invalid username"], id="known-code"),
            pytest.param(99999, "Custom error text", "some step", ["[99999]"], id="unknown-code"),
            pytest.param(
                None, "Password is incorrect", "POST credentials", ["Password is incorrect"],
                id="msg-fallback",
            ),
        ],
    )
    def test_rendered_message(
        self, code: int | None, msg: str | None, step: str, needles: list[str],
    ) -> None:
        rendered = str(build_sso_error(code, msg, step))
        for needle in needles:
            assert needle in rendered

    def test_mfa_required_recovery_does_not_assume_code_channel(self) -> None:
        err = build_sso_error(50076, None, "POST credentials")
        recovery = err.recovery or ""

        assert "--mfa-method choose" in recovery
        assert "auth verify <code>" in recovery
        assert "auth verify ok" in recovery
        assert "--totp" not in recovery

    @pytest.mark.parametrize(
        "raw",
        [
            "responseBody=BODY_SENTINEL",
            "flow_token=FLOW_SENTINEL",
            'oPostParams={"password":"PASSWORD_SENTINEL"}',
            "cookieValue=COOKIE_SENTINEL",
            "sessionVal=SESSION_SENTINEL",
            "access_token=ACCESS_SENTINEL",
            "client_secret=CLIENT_SENTINEL",
            'headers={"Cookie":"COOKIE_SENTINEL"}',
            '{"password":"PASSWORD_SENTINEL"}',
            '{"X-Api-Key":"API_SENTINEL"}',
            'error: {"password":"REAL_PASSWORD"}',
            'error={"apiKey":"REAL_KEY"}',
            "Unexpected response — page: responseBody=BODY_SENTINEL",
            "OTP 123456",
            "TOTP 123456",
            "token abc123",
            "canary abc123",
            "error: OTP 123456",
            "error: TOTP 123456",
            "error: token abc123",
            "error: canary abc123",
            "api key: APISECRET",
            "apikey: APISECRET",
            "passphrase: SECRET",
            "bearer REAL",
            "session: REAL",
            "error: api key: APISECRET",
            "error: apikey: APISECRET",
            "error: passphrase: SECRET",
            "error: bearer REAL",
            "error: session: REAL",
            "password is PASSWORD_SENTINEL",
            "token is TOKEN_SENTINEL",
            "canary: CANARY_SENTINEL",
            "otp: OTP_SENTINEL",
            "password-hash: SECRET",
            "passwordValue: SECRET",
            "foo secret SECRET",
            "secret: SECRET",
            "error: password-hash: SECRET",
            "error: passwordValue: SECRET",
            "error: foo secret SECRET",
            "error: secret: SECRET",
            "password hunter2",
            "Run: lighthouse auth login --pass PASSWORD_SENTINEL",
        ],
    )
    def test_sso_error_never_echoes_secret_shaped_upstream_text(self, raw: str) -> None:
        rendered = str(build_sso_error(None, raw, "MFA"))

        # "SENTINEL", "SECRET" and "REAL" also cover every *_SENTINEL,
        # APISECRET, REAL_PASSWORD and REAL_KEY value above.
        for leaked in ("SENTINEL", "hunter2", "123456", "abc123", "SECRET", "REAL"):
            assert leaked not in rendered


def test_endauth_total_deadline_skips_sleep_when_budget_is_exhausted() -> None:
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(return_value=_retry_response())
        with patch("lighthouse_cli.ms_auth.time.monotonic", side_effect=[0.0, 0.0, 901.0]):
            with patch("lighthouse_cli.ms_auth.time.sleep") as sleep:
                with pytest.raises(MicrosoftSSOError, match="timed out"):
                    _poll_end_auth(client, polling_interval=999999)

    assert client._post.call_count == 1
    sleep.assert_not_called()


def test_endauth_approval_can_complete_after_old_120_second_budget() -> None:
    success = _json_response({"Success": True, "FlowToken": "done-flow", "Ctx": "done-ctx"})
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(side_effect=[_retry_response(), success])
        client._checkpoint_mfa_pending = MagicMock()
        with patch(
            "lighthouse_cli.ms_auth.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 121.0],
        ), patch("lighthouse_cli.ms_auth.time.sleep"):
            flow, ctx, data = _poll_end_auth(client)

    assert (flow, ctx) == ("done-flow", "done-ctx")
    assert data["Success"] is True


def test_begin_auth_rejects_non_object_json_response() -> None:
    snap = ResponseSnapshot(url=MS_SSO_URL, status_code=200, location="", html="")
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(return_value=_json_response([]))
        with pytest.raises(MicrosoftSSOError, match="BeginAuth returned an invalid response"):
            client._step_handle_mfa_converged(
                snap,
                {"urlBeginAuth": "/common/SAS/BeginAuth"},
                [SMS_PROOF],
                None,
                mfa_method="sms",
            )


def test_end_auth_rejects_non_object_json_response() -> None:
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(return_value=_json_response([]))
        with pytest.raises(MicrosoftSSOError, match="EndAuth returned an invalid response"):
            _poll_end_auth(client)


def test_safe_upstream_text_rejects_prefixed_credential_values() -> None:
    for raw in (
        "error: api key: APISECRET",
        "error: apikey: APISECRET",
        "error: passphrase: SECRET",
        "error: bearer REAL",
        "error: session: REAL",
        "error: password is PASSWORD_SENTINEL",
        "error: token is TOKEN_SENTINEL",
        "error: canary: CANARY_SENTINEL",
        "error: otp: OTP_SENTINEL",
        "error: password-hash: SECRET",
        "error: passwordValue: SECRET",
        "error: foo secret SECRET",
        "error: secret: SECRET",
    ):
        assert safe_upstream_text(raw, fallback="FALLBACK") == "FALLBACK"


@pytest.mark.parametrize(
    ("entropy", "leaked"),
    [
        ("password=ENTROPY_SECRET", "ENTROPY_SECRET"),
        ("42\nwith-control", "with-control"),
        ("1234567", "1234567"),
        ({"password": "ENTROPY_SECRET"}, "ENTROPY_SECRET"),
    ],
)
def test_invalid_mfa_entropy_uses_fixed_approval_instruction(
    entropy: object,
    leaked: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    retry = _json_response({"Retry": True, "Entropy": entropy})
    success = _json_response({"Success": True, "FlowToken": "flow", "Ctx": "ctx"})
    proof = UserProof("PhoneAppNotification", "Authenticator", "", True)
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(side_effect=[retry, success])
        client._checkpoint_mfa_pending = MagicMock()
        with patch("lighthouse_cli.ms_auth.time.sleep"):
            _poll_end_auth(client, proof, code="")

    captured = capsys.readouterr()
    assert leaked not in captured.err
    assert "Approve sign-in in Authenticator to continue." in captured.err
    assert captured.out == ""


def test_endauth_poll_interval_is_capped_and_final_retry_does_not_sleep() -> None:
    with closing(MicrosoftSSOClient()) as client:
        client._post = MagicMock(return_value=_retry_response())
        with patch("lighthouse_cli.ms_auth.time.sleep") as sleep:
            with pytest.raises(MicrosoftSSOError, match="timed out"):
                _poll_end_auth(client, polling_interval=999999)

    assert sleep.call_count == 29
    assert all(call.args == (30.0,) for call in sleep.call_args_list)


class TestStaySignedInDetection:
    def test_kmsi_page_is_not_mfa_and_is_detected(self) -> None:
        """KMSI pages are not MFA pages but are detected for auto-submit."""
        html = '<form><input name="LoginOptions" value="1"></form>KmsiInterrupt'
        assert is_mfa_page(html) is False
        snap = ResponseSnapshot(url="https://x", status_code=200, location="", html=html)
        assert kmsi_page_detected(snap) is True


# ---------------------------------------------------------------------------
# Voice-call and push MFA vocabulary (TwoWayVoice*, PhoneAppNotification)
# ---------------------------------------------------------------------------

VOICE_PROOFS_HTML = """<html><body>ConvergedTFA
<script>
$Config = {
    "sFT": "mfa-flow",
    "sCtx": "mfa-ctx",
    "arrUserProofs": [
        {"authMethodId": "TwoWayVoiceMobile", "display": "Call +91 ***1234", "data": "+919876541234", "isDefault": false},
        {"authMethodId": "TwoWayVoiceAlternateMobile", "display": "Call +91 ***5678", "data": "+919876545678", "isDefault": false},
        {"authMethodId": "TwoWayVoiceOffice", "display": "Call office", "data": "+918012345678", "isDefault": false},
        {"authMethodId": "PhoneAppNotification", "display": "Approve in app", "data": "", "isDefault": true},
        {"authMethodId": "PhoneAppOTP", "display": "Authenticator code", "data": "", "isDefault": false}
    ]
};
</script>
</body></html>"""


class TestVoiceAndPushMethods:
    @pytest.mark.parametrize(
        ("method", "expected"),
        [(MFA_METHOD_CALL, "TwoWayVoiceMobile"), (MFA_METHOD_PUSH, "PhoneAppNotification")],
        ids=["call-selects-mobile-voice-first", "push-selects-notification-only"],
    )
    def test_selects_proof(self, method: str, expected: str) -> None:
        selected = _select_user_proof(_proofs_from(VOICE_PROOFS_HTML), method)
        assert selected.auth_method_id == expected

    @pytest.mark.parametrize(
        ("proof", "method"),
        [
            pytest.param(
                UserProof("PhoneAppOTP", "Authenticator app", "", True), MFA_METHOD_CALL,
                id="call-without-voice-methods",
            ),
            pytest.param(
                UserProof("PhoneAppNotification", "Approve", "", True), MFA_METHOD_APP,
                id="app-does-not-fall-through-to-push",
            ),
        ],
    )
    def test_unregistered_method_errors_with_options(self, proof: UserProof, method: str) -> None:
        with pytest.raises(MicrosoftSSOError, match="not available"):
            _select_user_proof([proof], method)

    def test_call_is_a_valid_method(self) -> None:
        assert MFA_METHOD_CALL in VALID_MFA_METHODS
        assert MFA_METHOD_PUSH in VALID_MFA_METHODS

    def test_voice_is_codeless_approval(self) -> None:
        assert "TwoWayVoiceMobile" in CODELESS_APPROVAL_AUTH_IDS
        assert "TwoWayVoiceMobile" not in SERVER_SENT_CODE_AUTH_IDS

    @pytest.mark.parametrize(
        "proof",
        [
            UserProof("TwoWayVoiceOffice", "Call office", "", False),
            UserProof("PhoneAppNotification", "Approve in app", "", True),
        ],
        ids=["voice", "push"],
    )
    def test_end_payload_never_carries_code(self, proof: UserProof) -> None:
        payload = build_end_payload(
            proof, {"SessionId": "sid"}, "998877", end_flow="f", end_ctx="c"
        )
        assert "AdditionalAuthData" not in payload


class TestBrowserCookieExport:
    def test_cookies_are_normalized_for_playwright(self) -> None:
        session = requests.Session()
        session.cookies.set("esctx", "SYNTHETIC", domain="login.microsoftonline.com", path="/")
        # A value-less cookie (``cookie.value is None``); cookies.set(name, None) would delete it.
        session.cookies.set_cookie(
            requests.cookies.create_cookie("flag", None, domain=".microsoftonline.com")
        )
        session.cookies.set("hostless", "SYNTHETIC")

        cookies = sorted(_browser_cookies(session), key=lambda c: c["name"])

        assert cookies == [
            {
                "name": "esctx",
                "value": "SYNTHETIC",
                "domain": "login.microsoftonline.com",
                "path": "/",
            },
            {"name": "flag", "value": "", "domain": ".microsoftonline.com", "path": "/"},
        ]
