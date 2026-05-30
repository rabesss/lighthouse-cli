"""Tests for MicrosoftSSOClient — pure HTTP Microsoft SSO client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from lighthouse_cli.ms_auth import (
    MFA_METHOD_APP,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_SMS,
    _prompt_user_proof_choice,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    UserProof,
    _absolute_url,
    _extract_config_json,
    _extract_error_code_and_msg,
    _parse_user_proofs,
    _select_user_proof,
    BASE_URL,
    D2L_COOKIE_NAMES,
    MS_ERROR_CODES,
)


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


class TestMfaMethodSelection:
    def test_select_sms_when_registered(self) -> None:
        proofs = _parse_user_proofs(_extract_config_json(SAMPLE_CONVERGED_TFA_HTML) or {})
        selected = _select_user_proof(proofs, MFA_METHOD_SMS)
        assert selected.auth_method_id == "OneWaySMS"

    def test_select_app_when_requested(self) -> None:
        proofs = _parse_user_proofs(_extract_config_json(SAMPLE_CONVERGED_TFA_HTML) or {})
        selected = _select_user_proof(proofs, MFA_METHOD_APP)
        assert selected.auth_method_id == "PhoneAppOTP"

    def test_auto_uses_default(self) -> None:
        proofs = _parse_user_proofs(_extract_config_json(SAMPLE_CONVERGED_TFA_HTML) or {})
        selected = _select_user_proof(proofs, "auto")
        assert selected.auth_method_id == "PhoneAppOTP"

    def test_choose_prompts_for_selection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proofs = _parse_user_proofs(_extract_config_json(SAMPLE_CONVERGED_TFA_HTML) or {})
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "2")
        selected = _select_user_proof(proofs, MFA_METHOD_CHOOSE)
        assert selected.auth_method_id == "OneWaySMS"

    def test_choose_single_proof_skips_prompt(self) -> None:
        single = [UserProof("OneWaySMS", "SMS", "+91", True)]
        assert _prompt_user_proof_choice(single).auth_method_id == "OneWaySMS"


class TestCollectTotpAfterChallenge:
    def test_app_otp_keeps_preprovided_code(self) -> None:
        """PhoneAppOTP is offline TOTP: a pre-provided --totp code must be kept, not discarded."""
        client = MicrosoftSSOClient()
        selected = UserProof("PhoneAppOTP", "Authenticator app", "", True)
        code = client._collect_totp_after_challenge(
            selected, "123456", read_totp_after_challenge=False, sms_triggered=False
        )
        assert code == "123456"
        client.close()


class TestAbsoluteUrl:
    def test_resolves_tenant_relative_kmsi_path(self) -> None:
        base = "https://login.microsoftonline.com/29bebd42-f1ff-4c3d-9688-067e3460dc1f/login"
        assert _absolute_url(base, "/kmsi") == "https://login.microsoftonline.com/kmsi"


class TestExtractConfigJson:
    def test_extracts_valid_config(self) -> None:
        config = _extract_config_json(SAMPLE_CONFIG_HTML)
        assert config is not None
        assert config["sFT"] == "flow-token-123"
        assert config["urlPost"] == "https://login.microsoftonline.com/common/login"
        assert config["sCtx"] == "rQIIAQs...ctx-token..."

    def test_returns_none_for_no_config(self) -> None:
        config = _extract_config_json("<html><body>No config here</body></html>")
        assert config is None

    def test_returns_none_for_malformed_json(self) -> None:
        html = '<script>$Config = {bad: "json"};</script>'
        config = _extract_config_json(html)
        assert config is None

    def test_handles_multiple_scripts(self) -> None:
        html = '<script>var x=1;</script><script>$Config = {"key": "value"};</script>'
        config = _extract_config_json(html)
        assert config == {"key": "value"}

    def test_handles_nested_objects(self) -> None:
        html = '<script>$Config = {"outer": {"inner": "val"}};</script>'
        config = _extract_config_json(html)
        assert config == {"outer": {"inner": "val"}}


# ---------------------------------------------------------------------------
# _extract_error_code_and_msg tests
# ---------------------------------------------------------------------------

class TestExtractErrorCode:
    def test_extracts_both_code_and_msg(self) -> None:
        code, msg = _extract_error_code_and_msg(SAMPLE_ERROR_HTML)
        assert code == 50126
        assert msg == "Invalid username or password."

    def test_returns_none_when_no_error(self) -> None:
        code, msg = _extract_error_code_and_msg("<html><body>OK</body></html>")
        assert code is None
        assert msg is None

    def test_fallback_to_div_error(self) -> None:
        html = '<div id="loginError">Your account is locked.</div>'
        code, msg = _extract_error_code_and_msg(html)
        assert msg == "Your account is locked."

    def test_504_error_aspx_suppressed_case_insensitive(self) -> None:
        # B8: mixed-case "Error.aspx" on a ConvergedTFA page is a benign 504.
        html = (
            '<html><script>$Config={"serverError":"504"};</script>'
            "ConvergedTFA redirect to /common/Error.aspx?err=504</html>"
        )
        code, _ = _extract_error_code_and_msg(html)
        assert code is None


# ---------------------------------------------------------------------------
# MicrosoftSSOClient unit tests
# ---------------------------------------------------------------------------

class TestMicrosoftSSOClientInit:
    def test_default_init(self) -> None:
        client = MicrosoftSSOClient()
        assert client._timeout == 30
        assert "User-Agent" in client._session.headers
        client.close()

    def test_custom_timeout(self) -> None:
        client = MicrosoftSSOClient(timeout=15)
        assert client._timeout == 15
        client.close()

    def test_custom_user_agent(self) -> None:
        client = MicrosoftSSOClient(user_agent="MyApp/1.0")
        assert client._session.headers["User-Agent"] == "MyApp/1.0"
        client.close()


class TestMicrosoftSSOClientIsErrorPage:
    def test_detects_400_status(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(400, "")
        assert client._is_error_page(resp) is True
        client.close()

    def test_detects_servererror_in_body(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, "serverError: 50126")
        assert client._is_error_page(resp) is True
        client.close()

    def test_ok_page_not_error(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, "<html>Login page</html>")
        assert client._is_error_page(resp) is False
        client.close()


class TestMicrosoftSSOClientIsMfaPage:
    def test_detects_converged_tfa(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, "ConvergedTFA page content")
        assert client._is_mfa_page(resp) is True
        client.close()

    def test_detects_otc_input(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, SAMPLE_MFA_HTML)
        assert client._is_mfa_page(resp) is True
        client.close()

    def test_enter_code_text(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, '<div>Enter code</div>')
        assert client._is_mfa_page(resp) is True
        client.close()

    def test_saml_page_not_mfa(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, SAMPLE_SAML_HTML)
        assert client._is_mfa_page(resp) is False
        client.close()


class TestMicrosoftSSOClientExtractSamlResponse:
    def test_extracts_from_hidden_input(self) -> None:
        client = MicrosoftSSOClient()
        result = client._extract_saml_response(SAMPLE_SAML_HTML)
        assert result == "PHNhbWxwOlJlc3BvbnNlIHhtbG5zOnNhbWxwPS...long-base64-string..."
        client.close()

    def test_returns_none_when_not_present(self) -> None:
        client = MicrosoftSSOClient()
        result = client._extract_saml_response("<html>No SAML here</html>")
        assert result is None
        client.close()

    def test_extracts_from_name_value_pattern(self) -> None:
        html = '<input name="SAMLResponse" value="BASE64SAMLTOKEN">'
        client = MicrosoftSSOClient()
        result = client._extract_saml_response(html)
        assert result == "BASE64SAMLTOKEN"
        client.close()


class TestMicrosoftSSOClientExtractD2lCookies:
    def test_extracts_all_four_cookies(self) -> None:
        import http.cookiejar

        client = MicrosoftSSOClient()
        client._session.cookies.set(
            "d2lSecureSessionVal", "sec123", domain="lighthouse.manipal.edu"
        )
        client._session.cookies.set(
            "d2lSessionVal", "ses123", domain="lighthouse.manipal.edu"
        )
        client._session.cookies.set(
            "d2lSameSiteCanaryA", "canaryA", domain="lighthouse.manipal.edu"
        )
        client._session.cookies.set(
            "d2lSameSiteCanaryB", "canaryB", domain="lighthouse.manipal.edu"
        )

        cookies = client._extract_d2l_cookies()
        assert len(cookies) == 4
        assert cookies["d2lSecureSessionVal"] == "sec123"
        assert cookies["d2lSessionVal"] == "ses123"
        client.close()

    def test_raises_on_missing_cookies(self) -> None:
        client = MicrosoftSSOClient()
        # No cookies set at all
        with pytest.raises(MicrosoftSSOError, match="Missing required D2L cookies"):
            client._extract_d2l_cookies()
        client.close()

    def test_ignores_non_d2l_cookies(self) -> None:
        client = MicrosoftSSOClient()
        client._session.cookies.set("d2lSecureSessionVal", "sec", domain="lighthouse.manipal.edu")
        client._session.cookies.set("d2lSessionVal", "ses", domain="lighthouse.manipal.edu")
        client._session.cookies.set("d2lSameSiteCanaryA", "a", domain="lighthouse.manipal.edu")
        client._session.cookies.set("d2lSameSiteCanaryB", "b", domain="lighthouse.manipal.edu")
        client._session.cookies.set("_ga", "tracking", domain="lighthouse.manipal.edu")
        client._session.cookies.set("session", "other", domain="example.com")

        cookies = client._extract_d2l_cookies()
        assert len(cookies) == 4
        assert "_ga" not in cookies
        client.close()


# ---------------------------------------------------------------------------
# Full login flow tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestFullLoginFlow:
    """Test the complete login flow using mocked requests.Session."""

    def _setup_login_mocks(self, client: MicrosoftSSOClient) -> list[MagicMock]:
        """Set up a sequence of mock responses for the full login flow."""
        responses = []

        # Step 1: D2L SAML init → 302 to Microsoft
        resp_saml_init = make_mock_response(
            302,
            headers={"Location": MS_SSO_URL},
        )
        responses.append(resp_saml_init)

        # Step 2: GET Microsoft login page → config HTML
        resp_ms_config = make_mock_response(
            200,
            text=SAMPLE_CONFIG_HTML,
            url=MS_SSO_URL,
        )
        responses.append(resp_ms_config)

        # Step 3: POST credentials → MFA page (with 2FA)
        resp_post_creds = make_mock_response(
            200,
            text=SAMPLE_MFA_HTML,
            url="https://login.microsoftonline.com/common/SAS/ProcessAuth",
        )
        responses.append(resp_post_creds)

        # Step 4a: POST TOTP → redirect to SAML
        resp_post_totp = make_mock_response(
            302,
            headers={"Location": "https://lighthouse.manipal.edu/d2l/lp/auth/saml/consume?SAMLResponse=..."},
            url="https://login.microsoftonline.com/common/SAS/ProcessAuth",
        )
        responses.append(resp_post_totp)

        # Step 4b: follow redirect → SAML HTML
        resp_saml = make_mock_response(
            200,
            text=SAMPLE_SAML_HTML,
            url="https://lighthouse.manipal.edu/d2l/lp/auth/saml/consume",
        )
        responses.append(resp_saml)

        # Step 5: POST SAML to D2L ACS → 302 to D2L home
        resp_acs = make_mock_response(
            302,
            headers={"Location": f"{BASE_URL}/d2l/home"},
        )
        responses.append(resp_acs)

        # Step 5b: follow redirect → home page (with cookies)
        resp_home = make_mock_response(
            200,
            text="<html>D2L Home</html>",
            url=f"{BASE_URL}/d2l/home",
        )
        responses.append(resp_home)

        client._session.get = MagicMock(side_effect=responses)
        client._session.post = MagicMock(side_effect=[
            responses[2],  # POST credentials
            responses[3],  # POST TOTP
            responses[5],  # POST SAML
        ])

        return responses

    def test_full_login_flow_with_mfa(self) -> None:
        """Complete login flow: SAML init -> MS config -> POST creds -> MFA -> SAML -> cookies."""
        client = MicrosoftSSOClient()

        # Build mock session that will be used when login() creates a fresh session.
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.cookies = requests.cookies.RequestsCookieJar()

        # Set up cookie jar simulation
        for name in D2L_COOKIE_NAMES:
            mock_session.cookies.set(
                name, f"test-{name}",
                domain="lighthouse.manipal.edu",
            )

        # Set up GET mocks
        resp_saml_init = make_mock_response(302, headers={"Location": MS_SSO_URL})
        resp_ms_config = make_mock_response(200, text=SAMPLE_CONFIG_HTML, url=MS_SSO_URL)
        resp_mfa = make_mock_response(200, text=SAMPLE_MFA_HTML)
        resp_post_totp_redirect = make_mock_response(
            302,
            headers={"Location": f"{BASE_URL}/d2l/lp/auth/saml/consume"},
        )
        resp_saml = make_mock_response(200, text=SAMPLE_SAML_HTML)
        resp_acs = make_mock_response(
            302,
            headers={"Location": f"{BASE_URL}/d2l/home"},
        )

        # GET sequence: init, config, follow TOTP redirect, ACS redirect follow
        get_responses = [
            resp_saml_init,      # Step 1: GET SAML init
            resp_ms_config,      # Step 2: GET MS config
            resp_saml,           # Step 4a: follow redirect from TOTP POST -> SAML page
            resp_acs,            # Step 5b: follow ACS redirect
        ]
        mock_session.get = MagicMock(side_effect=get_responses)

        # POST sequence: credentials, TOTP, SAML
        post_responses = [
            resp_mfa,            # Step 3: POST credentials -> MFA
            resp_post_totp_redirect,  # Step 4: POST TOTP
            resp_acs,            # Step 5: POST SAML
        ]
        mock_session.post = MagicMock(side_effect=post_responses)

        with patch("requests.Session", return_value=mock_session):
            cookies = client.login("test@manipal.edu", "password123", "123456")

        assert len(cookies) == 4
        for name in D2L_COOKIE_NAMES:
            assert name in cookies
        client.close()

    def test_login_with_invalid_credentials(self) -> None:
        """Invalid credentials raise MicrosoftSSOError with descriptive message."""
        client = MicrosoftSSOClient()

        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.cookies = requests.cookies.RequestsCookieJar()

        resp_saml_init = make_mock_response(302, headers={"Location": MS_SSO_URL})
        resp_ms_config = make_mock_response(200, text=SAMPLE_CONFIG_HTML, url=MS_SSO_URL)
        resp_error = make_mock_response(200, text=SAMPLE_ERROR_HTML)

        mock_session.get = MagicMock(side_effect=[resp_saml_init, resp_ms_config])
        mock_session.post = MagicMock(return_value=resp_error)

        with patch("requests.Session", return_value=mock_session):
            with pytest.raises(MicrosoftSSOError, match="50126"):
                client.login("bad@manipal.edu", "wrong_password", "123456")
        client.close()

    def test_login_mfa_with_wrong_code(self) -> None:
        """Wrong 2FA code raises MicrosoftSSOError."""
        client = MicrosoftSSOClient()

        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.cookies = requests.cookies.RequestsCookieJar()

        resp_saml_init = make_mock_response(302, headers={"Location": MS_SSO_URL})
        resp_ms_config = make_mock_response(200, text=SAMPLE_CONFIG_HTML, url=MS_SSO_URL)
        resp_mfa = make_mock_response(200, text=SAMPLE_MFA_HTML)
        # Wrong 2FA code -> stay on MFA page (200, still shows MFA)
        resp_mfa_again = make_mock_response(200, text=SAMPLE_MFA_HTML)

        mock_session.get = MagicMock(side_effect=[
            resp_saml_init,
            resp_ms_config,
        ])
        # POST creds -> MFA; POST wrong TOTP -> MFA page again
        mock_session.post = MagicMock(side_effect=[
            resp_mfa,        # POST creds
            resp_mfa_again,  # POST wrong TOTP -> MFA page again
        ])

        # _step_handle_mfa will detect MFA page and raise error
        with patch("requests.Session", return_value=mock_session):
            with pytest.raises(MicrosoftSSOError, match="2FA verification failed"):
                client.login("test@manipal.edu", "password123", "000000")
        client.close()

    def test_login_without_mfa(self) -> None:
        """Login without MFA (direct SAML after credentials)."""
        client = MicrosoftSSOClient()

        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.cookies = requests.cookies.RequestsCookieJar()

        for name in D2L_COOKIE_NAMES:
            mock_session.cookies.set(name, f"val-{name}", domain="lighthouse.manipal.edu")

        resp_saml_init = make_mock_response(302, headers={"Location": MS_SSO_URL})
        resp_ms_config = make_mock_response(200, text=SAMPLE_CONFIG_HTML, url=MS_SSO_URL)
        # Credentials POST returns SAML directly (no MFA)
        resp_saml_direct = make_mock_response(200, text=SAMPLE_SAML_HTML)
        resp_acs = make_mock_response(
            302, headers={"Location": f"{BASE_URL}/d2l/home"}
        )

        mock_session.get = MagicMock(side_effect=[resp_saml_init, resp_ms_config, resp_acs])
        mock_session.post = MagicMock(side_effect=[resp_saml_direct, resp_acs])

        with patch("requests.Session", return_value=mock_session):
            cookies = client.login("test@manipal.edu", "password123", None)
        assert len(cookies) == 4
        client.close()

    def test_saml_init_reaches_microsoft(self) -> None:
        """Step 1: SAML init redirects to Microsoft."""
        client = MicrosoftSSOClient()
        ms_url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=..."
        resp = make_mock_response(302, headers={"Location": ms_url})
        client._session.get = MagicMock(return_value=resp)

        url = client._step_initiate_saml()
        assert "login.microsoftonline.com" in url
        client.close()


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


class TestBuildError:
    def test_known_error_code(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200)
        err = client._build_error(resp, 50126, None, "POST credentials")
        assert "50126" in str(err)
        assert "Invalid username" in str(err)
        client.close()

    def test_unknown_error_code(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(200)
        err = client._build_error(resp, 99999, "Custom error text", "some step")
        assert "[99999]" in str(err)
        client.close()

    def test_error_with_msg_fallback(self) -> None:
        client = MicrosoftSSOClient()
        resp = make_mock_response(400)
        err = client._build_error(resp, None, "Password is incorrect", "POST credentials")
        assert "Password is incorrect" in str(err)
        client.close()


class TestClose:
    def test_close_cleans_up(self) -> None:
        client = MicrosoftSSOClient()
        client.close()
        # After close, session should be available but closed
        # Just verify it doesn't raise


# ---------------------------------------------------------------------------
# Config extraction edge cases
# ---------------------------------------------------------------------------

class TestConfigExtractionEdgeCases:
    def test_config_with_escaped_chars(self) -> None:
        html = '<script>$Config = {"urlPost": "https://example.com/login\\u002fpage"};</script>'
        config = _extract_config_json(html)
        assert config is not None
        assert "urlPost" in config

    def test_config_with_comments(self) -> None:
        """Config with trailing comma (invalid JSON) should fail gracefully."""
        html = '<script>$Config = {"key": "value",};</script>'
        config = _extract_config_json(html)
        assert config is None  # Invalid JSON


class TestMicrosoftSSOClientStaySignedIn:
    def test_kmsi_page_handling_disabled(self) -> None:
        """KMSI page detection should not crash."""
        html = '<form><input name="LoginOptions" value="1"></form>KmsiInterrupt'
        client = MicrosoftSSOClient()
        resp = make_mock_response(200, text=html)
        # Just verify is_mfa_page doesn't crash on KMSI
        is_mfa = client._is_mfa_page(resp)
        # KMSI is not an MFA page
        assert is_mfa is False
        client.close()
