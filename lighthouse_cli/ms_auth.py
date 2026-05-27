"""Pure HTTP Microsoft SSO client for lighthouse-cli.

Implements the full Microsoft Azure AD SSO login flow using only
``requests`` and ``BeautifulSoup`` -- no browser, no Playwright, no CDP.

Flow:
1. GET lighthouse.manipal.edu/d2l/lp/auth/saml/login → 302 to Microsoft
2. GET Microsoft login page → parse ``$Config`` JSON for flow tokens
3. POST credentials to ``urlPost`` → response may be MFA page or SAML
4. Handle MFA (ConvergedTFA page) → POST TOTP code
5. Extract SAMLResponse from HTML form
6. POST SAMLResponse to D2L ACS → capture d2l* session cookies
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://lighthouse.manipal.edu"
LOGIN_PATH = "/d2l/lp/auth/saml/login"
D2L_COOKIE_NAMES = (
    "d2lSecureSessionVal",
    "d2lSessionVal",
    "d2lSameSiteCanaryA",
    "d2lSameSiteCanaryB",
)

# CLI / env preference: auto | sms | app
MFA_METHOD_AUTO = "auto"
MFA_METHOD_SMS = "sms"
MFA_METHOD_APP = "app"
MFA_METHOD_CHOOSE = "choose"
VALID_MFA_METHODS = (MFA_METHOD_AUTO, MFA_METHOD_SMS, MFA_METHOD_APP, MFA_METHOD_CHOOSE)

# Microsoft SAS AuthMethodId values (see saml2aws AzureAD provider)
MFA_AUTH_SMS = "OneWaySMS"
MFA_AUTH_APP_OTP = "PhoneAppOTP"
MFA_AUTH_APP_NOTIFY = "PhoneAppNotification"

MFA_METHOD_AUTH_IDS: dict[str, tuple[str, ...]] = {
    MFA_METHOD_SMS: (MFA_AUTH_SMS,),
    MFA_METHOD_APP: (MFA_AUTH_APP_OTP, MFA_AUTH_APP_NOTIFY),
}

MFA_METHOD_INSTRUCTIONS: dict[str, str] = {
    MFA_AUTH_SMS: "Check the SMS text message on your registered phone.",
    MFA_AUTH_APP_OTP: "Open Microsoft Authenticator and enter the 6-digit code.",
    MFA_AUTH_APP_NOTIFY: "Approve the sign-in request in Microsoft Authenticator.",
}

# Microsoft error codes and their meanings
MS_ERROR_CODES: dict[int, str] = {
    50034: "User account does not exist in this tenant. Check your email address.",
    50053: "Account is locked. Too many sign-in attempts.",
    50055: "Password is expired.",
    50056: "Password is invalid or null.",
    50057: "User account is disabled.",
    50058: "Sign-in required. User needs to complete sign-in.",
    50059: "Service unavailable.",
    50064: "Credential validation failed.",
    50072: "User needs to perform multi-factor authentication.",
    50074: "Strong authentication is required.",
    50076: "User needs to perform multi-factor authentication (MFA).",
    50079: "User needs to enroll in multi-factor authentication.",
    50126: "Invalid username or password.",
    50128: "Domain hint is invalid.",
    50131: "Device is not in required device state.",
    50133: "Password is incorrect or account is locked.",
    50140: "User needs to accept Terms of Use.",
    50144: "User's password has expired.",
    50158: "External security challenge not satisfied.",
    50173: "Fresh token needed.",
    53000: "Device is not compliant.",
    53003: "Access blocked by conditional access policy.",
    65001: "Application needs permission to access resources.",
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class MicrosoftSSOError(Exception):
    """Raised when any step of the Microsoft SSO flow fails."""

    def __init__(self, message: str, step: str | None = None, recovery: str | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.recovery = recovery

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.step:
            parts.append(f"  Step: {self.step}")
        if self.recovery:
            parts.append(f"  Fix: {self.recovery}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Token extraction helpers
# ---------------------------------------------------------------------------

def _extract_balanced_json_object(text: str, start: int) -> str | None:
    """Return a ``{...}`` JSON object substring starting at ``start`` (must be ``{``)."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _extract_config_json(html: str) -> dict[str, Any] | None:
    """Extract the ``$Config`` JavaScript object from Microsoft's login page.

    Uses brace-balanced parsing because ``$Config`` contains deeply nested JSON
    (non-greedy regex stops at the first ``}`` and drops ``sFT`` / ``sCtx``).
    """
    pos = 0
    while True:
        m = re.search(r"\$Config\s*=", html[pos:])
        if not m:
            break
        match_end = pos + m.end()
        brace = html.find("{", match_end)
        if brace < 0:
            pos = match_end
            continue
        blob = _extract_balanced_json_object(html, brace)
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        pos = match_end
    return None


def _extract_error_code_and_msg(html: str) -> tuple[int | None, str | None]:
    """Extract Microsoft error code and message from an error page.

    Looks for patterns like ``serverError\":"50126"`` or ``sErrTxt\":"..."``
    in the page's JavaScript or HTML.
    """
    # Try serverError in a script — "serverError": "50126" (JSON-style)
    m = re.search(r'''serverError["']?\s*:\s*["']([0-9]+)["']''', html)
    if not m:
        # Try without the key quote: serverError": "50126"
        m = re.search(r'serverError["\'][^:]*:\s*["\']([0-9]+)["\']', html)
    code = int(m.group(1)) if m else None

    # Try sErrTxt — flexible pattern for JSON key
    m = re.search(r'''sErrTxt["']?\s*:\s*["'](.+?)["']''', html, re.DOTALL)
    msg = m.group(1) if m else None

    # Fallback: look for <div class="error"> text (case-insensitive)
    if not msg:
        soup = BeautifulSoup(html, "html.parser")
        for err_div in soup.find_all(
            lambda tag: tag.name == "div"
            and any(
                "error" in (tag.get(attr, "") or "").lower()
                for attr in ("id", "class")
            )
        ):
            text = err_div.get_text(strip=True)
            if text:
                msg = text
                break

    return code, msg


@dataclass(frozen=True)
class UserProof:
    """A registered MFA method on the user's Microsoft account."""

    auth_method_id: str
    display: str
    data: str
    is_default: bool


def _parse_user_proofs(config: dict[str, Any]) -> list[UserProof]:
    proofs: list[UserProof] = []
    for raw in config.get("arrUserProofs") or []:
        if not isinstance(raw, dict):
            continue
        auth_id = str(raw.get("authMethodId") or "")
        if not auth_id:
            continue
        proofs.append(
            UserProof(
                auth_method_id=auth_id,
                display=str(raw.get("display") or auth_id),
                data=str(raw.get("data") or ""),
                is_default=bool(raw.get("isDefault")),
            )
        )
    return proofs


def _prompt_user_proof_choice(proofs: list[UserProof]) -> UserProof:
    """Interactively pick one of several registered MFA methods."""
    if len(proofs) == 1:
        return proofs[0]
    if not sys.stdin.isatty():
        raise MicrosoftSSOError(
            "Multiple MFA methods are available; pick one with --mfa-method sms|app.",
            step="MFA",
            recovery="Re-run with --mfa-method or use a single-method account.",
        )
    print("\nChoose a verification method:", flush=True)
    for idx, proof in enumerate(proofs, start=1):
        default = " (Microsoft default)" if proof.is_default else ""
        print(f"  {idx}) {proof.display}{default}", flush=True)
    while True:
        choice = input(f"Enter 1–{len(proofs)} [1]: ").strip() or "1"
        if choice.isdigit() and 1 <= int(choice) <= len(proofs):
            return proofs[int(choice) - 1]
        print("Invalid choice, try again.", flush=True)


def _select_user_proof(proofs: list[UserProof], preference: str) -> UserProof:
    """Pick an MFA method based on user preference and tenant defaults."""
    if not proofs:
        raise MicrosoftSSOError(
            "No MFA methods are registered on this account.",
            step="MFA",
            recovery="Enroll SMS or Authenticator in your Microsoft account security settings.",
        )

    if preference == MFA_METHOD_CHOOSE:
        return _prompt_user_proof_choice(proofs)

    if preference != MFA_METHOD_AUTO:
        for auth_id in MFA_METHOD_AUTH_IDS.get(preference, ()):
            for proof in proofs:
                if proof.auth_method_id == auth_id:
                    return proof
        available = ", ".join(p.display for p in proofs)
        raise MicrosoftSSOError(
            f"Requested MFA method '{preference}' is not available. Options: {available}",
            step="MFA",
            recovery="Use --mfa-method auto, choose, or register the method in Microsoft security settings.",
        )

    # auto: tenant default, else first registered method
    for proof in proofs:
        if proof.is_default:
            return proof
    return proofs[0]


def _absolute_url(base_url: str, path: str) -> str:
    """Resolve Microsoft login URLs (often tenant-relative paths)."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if path.startswith("/"):
        return f"{origin}{path}"
    return urljoin(f"{origin}/", path)


def _mask_phone_hint(data: str) -> str:
    digits = re.sub(r"\D", "", data)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    if data:
        return data
    return "your phone"


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class MicrosoftSSOClient:
    """Pure HTTP client for Microsoft Azure AD SSO + D2L login.

    Uses ``requests.Session`` for automatic cookie management across the
    multi-step flow.  No browser or JavaScript engine is required.

    Usage::

        client = MicrosoftSSOClient()
        cookies = client.login("user@manipal.edu", "password", "123456")
        # cookies is a dict of d2l cookie name → value
    """

    def __init__(
        self,
        *,
        timeout: int = 30,
        user_agent: str | None = None,
    ) -> None:
        self._session = requests.Session()
        self._timeout = timeout
        self._session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    # -- helpers --------------------------------------------------------------

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET with timeout and allow_redirects=False."""
        return self._session.get(
            url,
            allow_redirects=False,
            timeout=self._timeout,
            **kwargs,
        )

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        """POST with timeout and allow_redirects=False."""
        return self._session.post(
            url,
            allow_redirects=False,
            timeout=self._timeout,
            **kwargs,
        )

    def _follow_redirect(self, resp: requests.Response, step: str) -> requests.Response:
        """Follow a 302 redirect to its Location."""
        location = resp.headers.get("Location", "")
        if not location:
            raise MicrosoftSSOError(
                f"Expected redirect, got HTTP {resp.status_code} with no Location",
                step=step,
                recovery="Check your network or try again later.",
            )
        # Handle relative URLs
        if location.startswith("/"):
            # Reconstruct from the original request URL
            location = urljoin(resp.url, location)
        return self._get(location)

    # -- auth flow -----------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        totp_code: str | None = None,
        *,
        mfa_method: str = MFA_METHOD_AUTO,
        on_credentials_submitted: Callable[[], None] | None = None,
    ) -> dict[str, str]:
        """Execute the full login flow and return D2L session cookies.

        Args:
            username: Email address for Microsoft SSO (e.g. user@manipal.edu)
            password: Microsoft account password
            totp_code: 6-digit 2FA code (or None for interactive prompt after password)
            mfa_method: ``auto``, ``sms`` (text message), or ``app`` (Authenticator)
            on_credentials_submitted: Optional callback after password POST succeeds

        Returns:
            Dict mapping cookie names (d2lSecureSessionVal, etc.) to values.

        Raises:
            MicrosoftSSOError: On any authentication failure with details
                about what went wrong and how to recover.
        """
        if mfa_method not in VALID_MFA_METHODS:
            raise MicrosoftSSOError(
                f"Invalid mfa_method {mfa_method!r}. Use: {', '.join(VALID_MFA_METHODS)}",
                step="MFA",
            )

        # Step 1: Initiate D2L SAML login
        step1 = self._step_initiate_saml()

        # Step 2: GET Microsoft login page → extract $Config
        ms_config = self._step_get_ms_config(step1)

        # Step 3: POST credentials to Microsoft
        step3_resp = self._step_post_credentials(ms_config, username, password)
        if on_credentials_submitted is not None:
            on_credentials_submitted()

        # Step 4: Handle response — MFA or SAML or error
        if self._is_mfa_page(step3_resp):
            # Step 4a: Handle MFA (two-phase: code collected after password accepted)
            step4_resp = self._step_handle_mfa(
                step3_resp,
                ms_config,
                totp_code,
                mfa_method=mfa_method,
            )
            saml_response = self._extract_saml_response(step4_resp.text)
        elif self._is_error_page(step3_resp):
            code, msg = _extract_error_code_and_msg(step3_resp.text)
            raise self._build_error(step3_resp, code, msg, "POST credentials")
        else:
            # Response might already contain SAML
            saml_response = self._extract_saml_response(step3_resp.text)
            if not saml_response:
                code, msg = _extract_error_code_and_msg(step3_resp.text)
                raise self._build_error(
                    step3_resp, code, msg, "POST credentials (unexpected response)"
                )

        # Step 5: POST SAMLResponse to D2L ACS
        if saml_response is None:
            raise MicrosoftSSOError(
                "No SAML response found in login flow.",
                step="extract SAML",
                recovery="Try again or check your account status.",
            )
        self._step_post_saml(saml_response)

        # Step 6: Extract D2L cookies
        return self._extract_d2l_cookies()

    # -- step implementations ------------------------------------------------

    def _step_initiate_saml(self) -> str:
        """Step 1: GET D2L SAML login → follow redirect to Microsoft.

        Returns the Microsoft login page URL.
        """
        resp = self._get(f"{BASE_URL}{LOGIN_PATH}")
        if resp.status_code in (301, 302, 303, 307, 308):
            ms_url = resp.headers.get("Location", "")
            if "microsoftonline.com" in ms_url or "login.microsoft" in ms_url:
                return ms_url if ms_url.startswith("http") else (
                    f"https://login.microsoftonline.com{ms_url}"
                    if ms_url.startswith("/") else ms_url
                )

        # If we got a 200, the page might use a meta-refresh or JS redirect
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            meta = soup.find("meta", attrs={"http-equiv": "refresh"})
            if meta:
                content = meta.get("content", "")
                if isinstance(content, list):
                    content = content[0] if content else ""
                if isinstance(content, str):
                    m = re.search(r'url=(.+)', content, re.IGNORECASE)
                    if m:
                        return m.group(1).strip("'\"")
            # Look for a JavaScript redirect
            m = re.search(r'window\.location\s*=\s*["\'](.+?)["\']', resp.text)
            if m:
                return m.group(1)

        raise MicrosoftSSOError(
            f"Failed to redirect to Microsoft SSO. Got HTTP {resp.status_code}",
            step="initiate SAML",
            recovery="Check that lighthouse.manipal.edu is reachable.",
        )

    def _step_get_ms_config(self, ms_url: str) -> dict[str, Any]:
        """Step 2: GET Microsoft login page, extract $Config JSON."""
        resp = self._get(ms_url)

        # If we get a redirect from Microsoft (already authenticated at MS level),
        # follow it through to get the SAML response
        if resp.status_code in (301, 302, 303, 307, 308):
            return {"_redirect": True, "_location": resp.headers.get("Location", "")}

        # Microsoft login page has embedded $Config
        config = _extract_config_json(resp.text)
        if config is None:
            # The page might be a different form (e.g., organization login)
            # Try to find the login form directly
            soup = BeautifulSoup(resp.text, "html.parser")
            form = soup.find("form")
            if form:
                action = form.get("action", "")
                action_str = str(action) if action else ""
                config = {
                    "urlPost": urljoin(resp.url, action_str) if action_str else resp.url,
                }
                # Extract hidden inputs
                for hidden in form.find_all("input", type="hidden"):
                    hidden_name = hidden.get("name")
                    hidden_value = hidden.get("value")
                    if hidden_name:
                        config[str(hidden_name)] = str(hidden_value) if hidden_value else ""
            else:
                raise MicrosoftSSOError(
                    "Could not find Microsoft login configuration on the page.",
                    step="get MS config",
                    recovery="Microsoft may have changed their login page. Try again later.",
                )

        # Store the MS page URL for later (needed for form action resolution)
        config["_ms_url"] = resp.url
        return self._hydrate_ms_flow_config(config)

    def _hydrate_ms_flow_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Fetch flowToken/ctx when the first Microsoft page omits them (common on SAML2)."""
        if config.get("sFT") and config.get("sCtx"):
            return config
        url_post = config.get("urlPost")
        if not url_post:
            return config
        ms_base = str(config.get("_ms_url", "https://login.microsoftonline.com"))
        post_page_url = _absolute_url(ms_base, str(url_post))
        resp = self._get(post_page_url)
        if resp.status_code != 200:
            return config
        hydrated = _extract_config_json(resp.text)
        if not hydrated:
            return config
        merged = dict(config)
        for key, val in hydrated.items():
            if key.startswith("_"):
                continue
            if key in ("sFT", "sCtx", "urlPost", "canary", "apiCanary", "sessionId", "pgid"):
                merged[key] = val
            elif key not in merged:
                merged[key] = val
        merged["_ms_url"] = resp.url
        return merged

    def _step_get_credential_type(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Call GetCredentialType to refresh flowToken before password POST."""
        gct_url = config.get("urlGetCredentialType")
        if not gct_url or not config.get("sFT") or not config.get("sCtx"):
            return config

        gct_full = _absolute_url(str(config.get("_ms_url", "")), str(gct_url))
        payload = {
            "username": username,
            "isOtherIdpSupported": True,
            "checkPhones": False,
            "isRemoteNGCSupported": bool(config.get("fIsRemoteNGCSupported", True)),
            "isCookieBannerShown": False,
            "isFidoSupported": bool(config.get("fIsFidoSupported", True)),
            "originalRequest": str(config["sCtx"]),
            "flowToken": str(config["sFT"]),
            "country": "IN",
            "forceotclogin": False,
            "isExternalFederationDisallowed": False,
            "isRemoteConnectSupported": True,
            "federationFlags": 0,
            "isSignup": False,
            "isAccessPassSupported": bool(config.get("fIsAccessPassSupported", True)),
        }
        headers = {
            "Content-Type": "application/json",
            "canary": str(config.get("apiCanary") or config.get("canary") or ""),
            "client-request-id": str(config.get("correlationId") or ""),
            "hpgact": str(config.get("hpgact", "0")),
            "hpgid": str(config.get("hpgid", "0")),
            "hpgrequestid": str(config.get("sessionId") or ""),
            "Referer": str(config.get("_ms_url", "")),
        }
        resp = self._session.post(
            gct_full,
            json=payload,
            headers=headers,
            allow_redirects=False,
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            return config
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return config

        updated = dict(config)
        if data.get("FlowToken"):
            updated["sFT"] = data["FlowToken"]
        if data.get("apiCanary"):
            updated["apiCanary"] = data["apiCanary"]
        return updated

    def _step_post_credentials(
        self,
        config: dict[str, Any],
        username: str,
        password: str,
    ) -> requests.Response:
        """Step 3: POST username + password to Microsoft."""
        # When already authenticated at MS level, follow the redirect
        if config.get("_redirect"):
            location = config.get("_location", "")
            if "lighthouse.manipal.edu" in location or location.startswith("/d2l/"):
                resolved = location if location.startswith("http") else f"{BASE_URL}{location}"
                return self._get(resolved)
            resolved = location if location.startswith("http") else urljoin(
                "https://login.microsoftonline.com", location
            )
            return self._get(resolved)

        url_post = config.get("urlPost", "")
        if not url_post:
            raise MicrosoftSSOError(
                "No urlPost in Microsoft $Config. Login page structure may have changed.",
                step="POST credentials",
                recovery="Microsoft may have changed their login flow.",
            )

        config = self._hydrate_ms_flow_config(config)
        config = self._step_get_credential_type(config, username)
        if not config.get("sFT") or not config.get("sCtx"):
            raise MicrosoftSSOError(
                "Microsoft login flow tokens (flowToken/ctx) are missing.",
                step="POST credentials",
                recovery="Microsoft may have changed their login page. Try again later.",
            )

        ms_base = str(config.get("_ms_url", "https://login.microsoftonline.com"))
        login_url = _absolute_url(ms_base, str(url_post))

        sft_name = str(config.get("sFTName") or "flowToken")
        data: dict[str, str] = {
            "login": username,
            "loginfmt": username,
            "passwd": password,
            sft_name: str(config["sFT"]),
            "ctx": str(config["sCtx"]),
            "canary": str(config.get("canary") or ""),
        }
        if config.get("sessionId"):
            data["hpgrequestid"] = str(config["sessionId"])

        resp = self._post(
            login_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(config.get("_ms_url", "")),
            },
        )

        if "error.aspx" in resp.text:
            m = re.search(r"error\.aspx\?err=(\d+)", resp.text)
            code = m.group(1) if m else "unknown"
            raise MicrosoftSSOError(
                f"Microsoft login returned error {code}.",
                step="POST credentials",
                recovery="Verify your password in a browser, then try again.",
            )

        # Handle redirect (transparent re-auth)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if location:
                resolved = location if location.startswith("http") else urljoin(
                    login_url, location
                )
                return self._get(resolved)

        return resp

    def _is_error_page(self, resp: requests.Response) -> bool:
        """Check if the response is a Microsoft error page."""
        text = resp.text.lower()
        cfg = _extract_config_json(resp.text) or {}
        err_code = cfg.get("sErrorCode")
        if err_code and str(err_code) not in ("50058",):
            return True
        return (
            resp.status_code >= 400
            or "error.aspx" in text
            or "servererror" in text
            or "serrtxt" in text
            or "password is incorrect" in text
            or "account does not exist" in text
        )

    def _is_mfa_page(self, resp: requests.Response) -> bool:
        """Check if the response is a Microsoft MFA verification page."""
        text = resp.text
        if "ConvergedTFA" in text:
            return True
        mfa_config = _extract_config_json(text)
        if mfa_config and mfa_config.get("arrUserProofs"):
            return True
        text_lower = text.lower()
        otc_in_text = "otc" in text_lower
        verification_in_text = "verification" in text_lower
        authenticator_in_text = "authenticator" in text_lower
        return (
            (otc_in_text and (verification_in_text or authenticator_in_text))
            or 'name="otc"' in text
            or "id=\"idDiv_SAOTCC_Description\"" in text
            or "Enter code" in text
        )

    def _resolve_mfa_url(self, resp: requests.Response, path: str) -> str:
        return _absolute_url(resp.url, path)

    def _print_mfa_phase_banner(
        self,
        proofs: list[UserProof],
        selected: UserProof,
        *,
        sms_triggered: bool,
    ) -> None:
        if not sys.stdin.isatty():
            return
        print("\n--- Second factor required ---", flush=True)
        print("Registered verification methods on your account:", flush=True)
        for proof in proofs:
            marker = " (selected)" if proof.auth_method_id == selected.auth_method_id else ""
            print(f"  • {proof.display}{marker}", flush=True)
        hint = MFA_METHOD_INSTRUCTIONS.get(
            selected.auth_method_id,
            "Enter the verification code from the method shown above.",
        )
        if selected.auth_method_id == MFA_AUTH_SMS and sms_triggered:
            phone = _mask_phone_hint(selected.data)
            print(f"\nA code was requested for {phone}.", flush=True)
            print(
                "Delivery (SMS vs WhatsApp) is chosen by Microsoft; the CLI cannot force a channel.",
                flush=True,
            )
        print(f"\n{hint}", flush=True)

    def _prompt_mfa_code(self, selected: UserProof) -> str:
        if sys.stdin.isatty():
            label = "Enter verification code: "
            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY:
                label = "Press Enter after approving in Authenticator: "
            return input(label).strip()
        return sys.stdin.readline().strip()

    def _step_handle_mfa(
        self,
        mfa_resp: requests.Response,
        original_config: dict[str, Any],
        totp_code: str | None,
        *,
        mfa_method: str = MFA_METHOD_AUTO,
    ) -> requests.Response:
        """Step 4: Handle MFA — ConvergedTFA SAS API or legacy form fallback."""
        mfa_config = _extract_config_json(mfa_resp.text) or {}
        proofs = _parse_user_proofs(mfa_config)
        if proofs:
            return self._step_handle_mfa_converged(
                mfa_resp,
                mfa_config,
                proofs,
                totp_code,
                mfa_method=mfa_method,
            )
        return self._step_handle_mfa_legacy_form(
            mfa_resp,
            original_config,
            totp_code,
            mfa_config=mfa_config,
        )

    def _step_handle_mfa_converged(
        self,
        mfa_resp: requests.Response,
        mfa_config: dict[str, Any],
        proofs: list[UserProof],
        totp_code: str | None,
        *,
        mfa_method: str,
    ) -> requests.Response:
        """Handle ConvergedTFA via BeginAuth → EndAuth → ProcessAuth."""
        selected = _select_user_proof(proofs, mfa_method)
        begin_url = mfa_config.get("urlBeginAuth") or "/common/SAS/BeginAuth"
        end_url = mfa_config.get("urlEndAuth") or "/common/SAS/EndAuth"
        process_url = mfa_config.get("urlPost") or "/common/SAS/ProcessAuth"
        sft_name = str(mfa_config.get("sFTName") or "flowToken")
        flow_token = str(mfa_config.get("sFT") or "")
        ctx = str(mfa_config.get("sCtx") or "")
        login_name = str(mfa_config.get("sPOST_Username") or "")

        begin_payload = {
            "AuthMethodId": selected.auth_method_id,
            "Method": "BeginAuth",
            "ctx": ctx,
            "flowToken": flow_token,
        }
        begin_resp = self._session.post(
            self._resolve_mfa_url(mfa_resp, str(begin_url)),
            json=begin_payload,
            allow_redirects=False,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )
        try:
            begin_data: dict[str, Any] = begin_resp.json()
        except json.JSONDecodeError as exc:
            raise MicrosoftSSOError(
                "Microsoft MFA BeginAuth returned an invalid response.",
                step="MFA",
                recovery="Try again or use --mfa-method auto.",
            ) from exc

        if not begin_data.get("Success"):
            message = begin_data.get("Message") or begin_data.get("ResultValue") or "unknown error"
            raise MicrosoftSSOError(
                f"MFA setup failed: {message}",
                step="MFA BeginAuth",
                recovery="Try a different --mfa-method or check your Microsoft security settings.",
            )

        sms_triggered = selected.auth_method_id == MFA_AUTH_SMS
        self._print_mfa_phase_banner(proofs, selected, sms_triggered=sms_triggered)

        if selected.auth_method_id == MFA_AUTH_APP_NOTIFY:
            if totp_code is None and sys.stdin.isatty():
                totp_code = self._prompt_mfa_code(selected)
        elif totp_code is None:
            totp_code = self._prompt_mfa_code(selected)

        if selected.auth_method_id != MFA_AUTH_APP_NOTIFY:
            if not totp_code or not totp_code.strip():
                raise MicrosoftSSOError(
                    "2FA code is required but was empty.",
                    step="MFA",
                    recovery="Provide a code via --totp or enter it when prompted.",
                )
            totp_code = totp_code.strip()

        session_id = str(begin_data.get("SessionId") or "")
        end_flow = str(begin_data.get("FlowToken") or flow_token)
        end_ctx = str(begin_data.get("Ctx") or ctx)
        polling = mfa_config.get("oPerAuthPollingInterval") or {}
        poll_seconds = float(polling.get(selected.auth_method_id, 2))

        end_data: dict[str, Any] = {}
        for attempt in range(30):
            end_payload: dict[str, Any] = {
                "AuthMethodId": selected.auth_method_id,
                "Method": "EndAuth",
                "ctx": end_ctx,
                "flowToken": end_flow,
                "SessionId": session_id,
            }
            if selected.auth_method_id in (MFA_AUTH_SMS, MFA_AUTH_APP_OTP) and totp_code:
                end_payload["AdditionalAuthData"] = totp_code

            end_resp = self._session.post(
                self._resolve_mfa_url(mfa_resp, str(end_url)),
                json=end_payload,
                allow_redirects=False,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
            try:
                end_data = end_resp.json()
            except json.JSONDecodeError as exc:
                raise MicrosoftSSOError(
                    "Microsoft MFA EndAuth returned an invalid response.",
                    step="MFA",
                ) from exc

            if end_data.get("Success"):
                break
            if not end_data.get("Retry"):
                err_code = end_data.get("ErrCode")
                result = end_data.get("ResultValue") or end_data.get("Message")
                raise MicrosoftSSOError(
                    f"2FA verification failed: {result or err_code or 'unknown'}",
                    step="MFA",
                    recovery="Request a new code and try again.",
                )
            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY and attempt == 0:
                entropy = end_data.get("Entropy")
                if entropy and sys.stdin.isatty():
                    print(
                        f"Approve sign-in in Authenticator (number shown: {entropy}).",
                        flush=True,
                    )
            time.sleep(poll_seconds)
            end_flow = str(end_data.get("FlowToken") or end_flow)
            end_ctx = str(end_data.get("Ctx") or end_ctx)
        else:
            raise MicrosoftSSOError(
                "2FA verification timed out waiting for approval.",
                step="MFA",
                recovery="Try again and complete verification promptly.",
            )

        process_data: dict[str, str] = {
            sft_name: str(end_data.get("FlowToken") or end_flow),
            "request": str(end_data.get("Ctx") or end_ctx),
        }
        if login_name:
            process_data["login"] = login_name
        if selected.auth_method_id in (MFA_AUTH_SMS, MFA_AUTH_APP_OTP) and totp_code:
            process_data["otc"] = totp_code
            process_data["mfaAuthMethod"] = selected.auth_method_id
            process_data["type"] = "18"
            process_data["GeneralVerify"] = "false"
        canary = mfa_config.get("canary")
        if canary:
            process_data["canary"] = str(canary)

        resp = self._post(self._resolve_mfa_url(mfa_resp, str(process_url)), data=process_data)

        if self._is_mfa_page(resp):
            raise MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            )

        return self._follow_post_mfa_response(resp, str(process_url))

    def _step_handle_mfa_legacy_form(
        self,
        mfa_resp: requests.Response,
        original_config: dict[str, Any],
        totp_code: str | None,
        *,
        mfa_config: dict[str, Any],
    ) -> requests.Response:
        """Legacy MFA form POST (older Microsoft pages without arrUserProofs)."""
        import getpass as _getpass

        if totp_code is None:
            if sys.stdin.isatty():
                print("\n--- Second factor required ---", flush=True)
                print("Enter the verification code shown on the Microsoft sign-in page.", flush=True)
                totp_code = _getpass.getpass("Enter verification code: ")
            else:
                totp_code = sys.stdin.readline().strip()

        if not totp_code or not totp_code.strip():
            raise MicrosoftSSOError(
                "2FA code is required but was empty.",
                step="MFA",
                recovery="Provide a 2FA code via --totp flag or pipe.",
            )

        soup = BeautifulSoup(mfa_resp.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise MicrosoftSSOError(
                "Could not find MFA form on the verification page.",
                step="MFA",
                recovery="Microsoft may have changed the MFA flow.",
            )

        action = form.get("action")
        mfa_url = urljoin(mfa_resp.url, str(action)) if action else mfa_resp.url
        mfa_url = str(mfa_url)

        mfa_data: dict[str, str] = {"otc": totp_code.strip()}
        for hidden in form.find_all("input", attrs={"type": "hidden"}):
            name = hidden.get("name")
            value = hidden.get("value")
            if name:
                mfa_data[str(name)] = str(value) if value else ""

        for key in ("sFT", "sCtx", "canary", "apiCanary", "hpgrequestid"):
            if key in mfa_config and key not in mfa_data:
                mfa_data[key] = str(mfa_config[key])
        for key in ("sFT", "sCtx"):
            if key in original_config and key not in mfa_data:
                mfa_data[key] = str(original_config[key])

        resp = self._post(mfa_url, data=mfa_data)
        if self._is_mfa_page(resp):
            raise MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            )
        return self._follow_post_mfa_response(resp, mfa_url)

    def _follow_post_mfa_response(self, resp: requests.Response, base_url: str) -> requests.Response:
        """Follow redirects and optional KMSI after MFA ProcessAuth."""
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if location:
                resolved = location if location.startswith("http") else str(urljoin(base_url, location))
                resp = self._get(resolved)

        if resp.status_code == 200 and ("Kmsi" in resp.text or "Stay signed in" in resp.text):
            soup2 = BeautifulSoup(resp.text, "html.parser")
            form2 = soup2.find("form")
            if form2:
                kmsi_action = form2.get("action")
                kmsi_url = urljoin(resp.url, str(kmsi_action)) if kmsi_action else resp.url
                kmsi_url = str(kmsi_url)
                kmsi_data: dict[str, str] = {}
                for hidden in form2.find_all("input", attrs={"type": "hidden"}):
                    name = hidden.get("name")
                    value = hidden.get("value")
                    if name:
                        kmsi_data[str(name)] = str(value) if value else ""
                for inp in form2.find_all("input"):
                    inp_name = inp.get("name")
                    if inp_name and str(inp_name) in ("DontShowAgain", "StaySignedIn"):
                        inp_value = inp.get("value")
                        kmsi_data[str(inp_name)] = str(inp_value) if inp_value else "true"
                kmsi_data["LoginOptions"] = kmsi_data.get("LoginOptions", "1")
                resp = self._post(kmsi_url, data=kmsi_data)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if location:
                        resolved = location if location.startswith("http") else urljoin(
                            kmsi_url, location
                        )
                        return self._get(str(resolved))

        return resp

    def _extract_saml_response(self, html: str) -> str | None:
        """Extract the SAMLResponse value from an HTML form.

        Returns the SAMLResponse string, or None if not found.
        """
        # Method 1: hidden input named SAMLResponse
        soup = BeautifulSoup(html, "html.parser")
        for inp in soup.find_all("input", attrs={"name": "SAMLResponse"}):
            val = inp.get("value")
            if val and isinstance(val, str):
                return val

        # Method 2: Look for SAMLResponse in any form
        m = re.search(r'name="SAMLResponse"\s+value="([^"]*)"', html)
        if m:
            return m.group(1)

        # Method 3: Base64-encoded SAML assertion in page text
        if "SAMLResponse" in html or "SAML" in html:
            m = re.search(r'SAMLResponse[=:]?\s*["\']?\s*([A-Za-z0-9+/=]{100,})["\']?', html)
            if m:
                return m.group(1)

        return None

    def _step_post_saml(self, saml_response: str) -> None:
        """Step 5: POST the SAMLResponse to the D2L ACS endpoint.

        The SAML form typically has an action pointing to D2L's ACS.
        """
        acs_url = f"{BASE_URL}/d2l/lp/auth/saml/consume"

        # The SAML response might include the ACS URL. Try to find it in the
        # HTML that would have been around the SAMLResponse
        data = {
            "SAMLResponse": saml_response,
        }

        resp = self._post(acs_url, data=data)

        # D2L ACS may redirect to the home page on success
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if location:
                # Follow the redirect to finalize cookies
                self._session.get(
                    location if location.startswith("http") else f"{BASE_URL}{location}",
                    allow_redirects=True,
                    timeout=self._timeout,
                )
                return

        # If ACS returns 200 but we have Set-Cookie headers, that's also OK
        if resp.status_code == 200:
            # Check if we got d2l cookies
            if any(n.startswith("d2l") for n in self._session.cookies.keys()):
                return

        raise MicrosoftSSOError(
            f"SAML POST to D2L ACS failed with HTTP {resp.status_code}",
            step="POST SAML",
            recovery="SAML assertion may be expired or invalid. Try logging in again.",
        )

    def _extract_d2l_cookies(self) -> dict[str, str]:
        """Step 6: Extract D2L session cookies from the session cookie jar."""
        cookies: dict[str, str] = {}
        d2l_domains = ("lighthouse.manipal.edu", ".manipal.edu", "manipal.edu")

        for cookie in self._session.cookies:
            if cookie.name.startswith("d2l") and any(
                d in (cookie.domain or "") for d in d2l_domains
            ):
                cookie_val = cookie.value if cookie.value is not None else ""
                cookies[cookie.name] = cookie_val

        missing = [n for n in D2L_COOKIE_NAMES if n not in cookies]
        if missing:
            raise MicrosoftSSOError(
                f"Missing required D2L cookies after SSO: {missing}",
                step="extract cookies",
                recovery="The login may have completed but cookies were not set. "
                         "Try again or check your account status.",
            )

        return cookies

    def _build_error(
        self,
        resp: requests.Response,
        code: int | None,
        msg: str | None,
        step: str,
    ) -> MicrosoftSSOError:
        """Build a descriptive MicrosoftSSOError from the error response."""
        description = MS_ERROR_CODES.get(code or 0, msg or "Unknown error")
        if code:
            description = f"[{code}] {description}"

        recovery = "Check your credentials and try again."

        if code == 50126:
            recovery = (
                "Double-check your email and password. "
                "If using @manipal.edu, ensure your account is active."
            )
        elif code == 50034:
            recovery = "This email is not associated with a Microsoft account in this tenant."
        elif code in (50056, 50133):
            recovery = "Password is incorrect. If you recently changed your password, try again."
        elif code == 50055:
            recovery = "Your password has expired. Reset it via the Microsoft portal."
        elif code == 50057:
            recovery = "Your account has been disabled. Contact IT support."
        elif code == 50053:
            recovery = "Account is temporarily locked. Wait a few minutes and try again."
        elif code == 50058:
            recovery = "Additional sign-in verification required. Check your authenticator app."
        elif code in (50076, 50072):
            recovery = (
                "Multi-factor authentication is required. "
                "Use --totp flag to provide your 2FA code."
            )

        return MicrosoftSSOError(
            f"Authentication failed: {description}",
            step=step,
            recovery=recovery,
        )

    def close(self) -> None:
        """Close the underlying requests session."""
        self._session.close()
