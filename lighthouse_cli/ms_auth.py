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
from typing import Any
from urllib.parse import urljoin

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

def _extract_config_json(html: str) -> dict[str, Any] | None:
    """Extract the ``$Config`` JavaScript object from Microsoft's login page.

    The page includes a ``<script>`` tag containing something like::

        $Config = {
            "sFT": "...",
            "sCtx": "...",
            "urlPost": "...",
            ...
        };

    Returns the parsed JSON dict, or ``None`` if extraction fails.
    """
    # Pattern: $Config = { ... };
    # Use BeautifulSoup to find all script tags, then regex-extract the JSON
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if not script.string:
            continue
        m = re.search(r'\$Config\s*=\s*(\{.*?\});', script.string, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
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
    ) -> dict[str, str]:
        """Execute the full login flow and return D2L session cookies.

        Args:
            username: Email address for Microsoft SSO (e.g. user@manipal.edu)
            password: Microsoft account password
            totp_code: 6-digit 2FA code (or None for interactive prompt)

        Returns:
            Dict mapping cookie names (d2lSecureSessionVal, etc.) to values.

        Raises:
            MicrosoftSSOError: On any authentication failure with details
                about what went wrong and how to recover.
        """
        # Step 1: Initiate D2L SAML login
        step1 = self._step_initiate_saml()

        # Step 2: GET Microsoft login page → extract $Config
        ms_config = self._step_get_ms_config(step1)

        # Step 3: POST credentials to Microsoft
        step3_resp = self._step_post_credentials(ms_config, username, password)

        # Step 4: Handle response — MFA or SAML or error
        if self._is_mfa_page(step3_resp):
            # Step 4a: Handle MFA
            step4_resp = self._step_handle_mfa(step3_resp, ms_config, totp_code)
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
        return config

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

        # Build the form data with the required flow tokens
        data: dict[str, str] = {
            "login": username,
            "loginfmt": username,
            "passwd": password,
        }

        # Include flow tokens from $Config
        for key in ("sFT", "sFTName", "sCtx", "canary", "hpgrequestid", "i2", "i17", "i18", "i19"):
            if key in config:
                data[key] = str(config[key])

        # Include any other tokens that look like flow parameters
        for key, val in config.items():
            if (
                isinstance(val, (str, int, float, bool))
                and key not in data
                and not key.startswith("_")
            ):
                # Include API and flow related tokens
                if key in (
                    "apiCanary", "canary", "correlationId",
                    "sessionId", "fid", "deviceId"
                ):
                    data[key] = str(val)

        resp = self._post(url_post, data=data)

        # Handle redirect (transparent re-auth)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if location:
                resolved = location if location.startswith("http") else urljoin(
                    url_post, location
                )
                return self._get(resolved)

        return resp

    def _is_error_page(self, resp: requests.Response) -> bool:
        """Check if the response is a Microsoft error page."""
        text = resp.text.lower()
        return (
            resp.status_code >= 400
            or "servererror" in text
            or "serrtxt" in text
            or "password is incorrect" in text
            or "account does not exist" in text
        )

    def _is_mfa_page(self, resp: requests.Response) -> bool:
        """Check if the response is a Microsoft MFA verification page."""
        text = resp.text
        text_lower = text.lower()
        otc_in_text = "otc" in text_lower
        verification_in_text = "verification" in text_lower
        authenticator_in_text = "authenticator" in text_lower
        return (
            "ConvergedTFA" in text
            or (otc_in_text and (verification_in_text or authenticator_in_text))
            or 'name="otc"' in text
            or "id=\"idDiv_SAOTCC_Description\"" in text
            or "Enter code" in text
        )

    def _step_handle_mfa(
        self,
        mfa_resp: requests.Response,
        original_config: dict[str, Any],
        totp_code: str | None,
    ) -> requests.Response:
        """Step 4: Handle MFA by posting the TOTP code."""
        import getpass as _getpass
        import sys as _sys

        if totp_code is None:
            if _sys.stdin.isatty():
                totp_code = _getpass.getpass("Enter 2FA code: ")
            else:
                totp_code = _sys.stdin.readline().strip()

        if not totp_code or not totp_code.strip():
            raise MicrosoftSSOError(
                "2FA code is required but was empty.",
                step="MFA",
                recovery="Provide a 2FA code via --totp flag or pipe.",
            )

        # Extract MFA configuration from the page
        soup = BeautifulSoup(mfa_resp.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise MicrosoftSSOError(
                "Could not find MFA form on the verification page.",
                step="MFA",
                recovery="Microsoft may have changed the MFA flow.",
            )

        action = form.get("action")
        if action:
            mfa_url = urljoin(mfa_resp.url, str(action))
        else:
            mfa_url = mfa_resp.url
        mfa_url = str(mfa_url)

        # Extract flow tokens from the MFA page
        mfa_data: dict[str, str] = {"otc": totp_code.strip()}

        # Include hidden form fields
        for hidden in form.find_all("input", attrs={"type": "hidden"}):
            name = hidden.get("name")
            value = hidden.get("value")
            if name:
                mfa_data[str(name)] = str(value) if value else ""

        # Also need flow tokens from $Config embedded on MFA page
        mfa_config = _extract_config_json(mfa_resp.text) or {}
        for key in ("sFT", "sCtx", "canary", "apiCanary", "hpgrequestid"):
            if key in mfa_config and key not in mfa_data:
                mfa_data[key] = str(mfa_config[key])

        # Merge any URL params from the original config that might be needed
        for key in ("sFT", "sCtx"):
            if key in original_config and key not in mfa_data:
                mfa_data[key] = str(original_config[key])

        resp = self._post(mfa_url, data=mfa_data)

        # If we still see MFA page, the code was rejected
        if self._is_mfa_page(resp):
            raise MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            )

        # Handle possible stay-signed-in prompt
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if location:
                resp2 = self._get(
                    location if location.startswith("http") else str(urljoin(mfa_url, location))
                )
                return resp2

        # Check if KMSI/Stay signed in page
        if resp.status_code == 200 and ("Kmsi" in resp.text or "Stay signed in" in resp.text):
            # Handle "Stay signed in?" - click Yes
            soup2 = BeautifulSoup(resp.text, "html.parser")
            form2 = soup2.find("form")
            if form2:
                kmsi_action = form2.get("action")
                if kmsi_action:
                    kmsi_url = urljoin(resp.url, str(kmsi_action))
                else:
                    kmsi_url = resp.url
                kmsi_url = str(kmsi_url)
                kmsi_data: dict[str, str] = {}
                for hidden in form2.find_all("input", attrs={"type": "hidden"}):
                    name = hidden.get("name")
                    value = hidden.get("value")
                    if name:
                        kmsi_data[str(name)] = str(value) if value else ""
                # Add DontShowAgain and/or StaySignedIn if present
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
