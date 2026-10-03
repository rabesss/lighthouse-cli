"""Microsoft SSO exceptions and constants."""

from __future__ import annotations

import re

_UPSTREAM_URL_RE = re.compile(r"(?i)(?:https?://|//)[^\s<>'\"]+")
_UPSTREAM_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_UPSTREAM_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d .()*-]{5,}\d(?!\d)")
_UPSTREAM_SECRET_MARKERS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "otp",
    "totp",
    "canary",
    "ctx",
    "bearer",
    "responsebody",
    "response_body",
    "response body",
    "flowtoken",
    "flow_token",
    "flow token",
    "opostparams",
    "cookievalue",
    "cookie",
    "sessionval",
    "sessionvalue",
    "sessionid",
    "access_token",
    "access token",
    "client_secret",
    "client secret",
    "samlresponse",
    "saml_response",
    "saml response",
    "samlrequest",
    "saml_request",
    "authorization",
    "api-key",
    "apikey",
)
_UPSTREAM_SECRET_KEY_VALUE_RE = re.compile(
    r"(?ix)(?<![a-z0-9])[\"']?(?:[a-z0-9_-]*(?:password|passwd|"
    r"passphrase|pass|secret|token|otp|totp|canary|ctx|flow[\s_-]*token|"
    r"cookie(?:value)?|session(?:val(?:ue)?|id)?|access[\s_-]*token|"
    r"client[\s_-]*secret|api[\s_-]*key|apikey|bearer|"
    r"saml[\s_-]*(?:response|request)|authorization)[a-z0-9_-]*)"
    r"[\"']?(?![a-z0-9])"
    r"(?:\s*(?:[:=]\s*|\b(?:is|was)\b\s+|\s+)"
    r"[\"']?[^\s,;}\]]+[\"']?)"
)
_UPSTREAM_FLAG_VALUE_RE = re.compile(
    r"(?i)--(?:pass(?:word)?|token|secret)\s+[^\s,;)}\]]+"
)
_STRUCTURAL_PAGE_MARKER_RE = re.compile(
    r"(?i)\b(?:arrUserProofs|otc-input|ProcessAuth-form|KmsiInterrupt|"
    r"ConvergedTFA|SAMLResponse|sFT-present|urlPost|oPostParams|sso_reload)=[01]\b"
)
_SAFE_UPSTREAM_PHRASES = frozenset({
    "password is incorrect",
    "password is incorrect.",
    "invalid username or password.",
    "your account is locked.",
    "account is locked.",
    "code send failed",
})


def _contains_upstream_secret(text: str) -> bool:
    """Detect secret-shaped fields after removing safe page-shape booleans."""
    checked_text = _STRUCTURAL_PAGE_MARKER_RE.sub("page-marker", text)
    lowered = checked_text.lower()
    return bool(
        any(char in text for char in "{}[]")
        or _UPSTREAM_URL_RE.search(checked_text)
        or _UPSTREAM_SECRET_KEY_VALUE_RE.search(checked_text)
        or _UPSTREAM_FLAG_VALUE_RE.search(checked_text)
        or any(marker in lowered for marker in _UPSTREAM_SECRET_MARKERS)
    )


def safe_upstream_text(value: object, *, fallback: str) -> str:
    """Return a short upstream detail only when it is plainly non-secret.

    Microsoft sometimes puts request bodies, cookies, flow tokens, or URLs in
    ``Message``/``ResultValue`` fields.  Those values must never be interpolated
    into an exception or a JSON error document.  This helper intentionally
    prefers a fixed category message whenever a sensitive marker is present.
    """
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text or len(text) > 512:
        return fallback
    if text.casefold() in _SAFE_UPSTREAM_PHRASES:
        return text
    if _contains_upstream_secret(text):
        return fallback
    # Upstream error strings are not an allowlist.  Keep unknown text opaque;
    # callers can still use ``safe_diagnostic_text`` for structural metadata.
    return fallback


def safe_diagnostic_text(value: object, *, fallback: str) -> str:
    """Keep bounded diagnostics while masking URLs, secrets, and PII."""
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text or len(text) > 512:
        return fallback
    if (
        _UPSTREAM_EMAIL_RE.search(text)
        or _UPSTREAM_PHONE_RE.search(text)
        or _contains_upstream_secret(text)
    ):
        return fallback
    return text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGIN_PATH = "/d2l/lp/auth/saml/login"

# CLI / env preference: auto | sms | app | call | push | choose
MFA_METHOD_AUTO = "auto"
MFA_METHOD_SMS = "sms"
MFA_METHOD_APP = "app"
MFA_METHOD_CALL = "call"
MFA_METHOD_PUSH = "push"
MFA_METHOD_CHOOSE = "choose"
VALID_MFA_METHODS = (
    MFA_METHOD_AUTO,
    MFA_METHOD_SMS,
    MFA_METHOD_APP,
    MFA_METHOD_CALL,
    MFA_METHOD_PUSH,
    MFA_METHOD_CHOOSE,
)

# Microsoft SAS AuthMethodId values (see saml2aws AzureAD provider and the
# StrongAuthenticationMethod names in Microsoft Entra)
MFA_AUTH_SMS = "OneWaySMS"
MFA_AUTH_APP_OTP = "PhoneAppOTP"
MFA_AUTH_APP_NOTIFY = "PhoneAppNotification"
MFA_AUTH_VOICE_MOBILE = "TwoWayVoiceMobile"
MFA_AUTH_VOICE_ALT_MOBILE = "TwoWayVoiceAlternateMobile"
MFA_AUTH_VOICE_OFFICE = "TwoWayVoiceOffice"

# Methods whose verification code is generated server-side and delivered on
# BeginAuth. A literal pre-provided code cannot match the new SMS/WhatsApp code.
SERVER_SENT_CODE_AUTH_IDS = frozenset({MFA_AUTH_SMS})

# Methods completed by approving on another device instead of typing a code.
# Voice calls prompt the user to press #; Authenticator push may require number
# matching. EndAuth is polled without AdditionalAuthData for all of them.
CODELESS_APPROVAL_AUTH_IDS = frozenset({
    MFA_AUTH_APP_NOTIFY,
    MFA_AUTH_VOICE_MOBILE,
    MFA_AUTH_VOICE_ALT_MOBILE,
    MFA_AUTH_VOICE_OFFICE,
})

# Methods that submit a code through EndAuth's AdditionalAuthData: the
# server-sent SMS/WhatsApp code and the offline Authenticator TOTP.
CODE_SUBMITTING_AUTH_IDS = frozenset({MFA_AUTH_SMS, MFA_AUTH_APP_OTP})

MFA_METHOD_AUTH_IDS: dict[str, tuple[str, ...]] = {
    MFA_METHOD_SMS: (MFA_AUTH_SMS,),
    MFA_METHOD_APP: (MFA_AUTH_APP_OTP,),
    MFA_METHOD_CALL: (
        MFA_AUTH_VOICE_MOBILE,
        MFA_AUTH_VOICE_ALT_MOBILE,
        MFA_AUTH_VOICE_OFFICE,
    ),
    MFA_METHOD_PUSH: (MFA_AUTH_APP_NOTIFY,),
}

MFA_METHOD_INSTRUCTIONS: dict[str, str] = {
    MFA_AUTH_SMS: "Check the SMS text message on your registered phone.",
    MFA_AUTH_APP_OTP: "Open Microsoft Authenticator and enter the 6-digit code.",
    MFA_AUTH_APP_NOTIFY: "Approve the sign-in request in Microsoft Authenticator.",
    MFA_AUTH_VOICE_MOBILE: "Answer the phone call and press # to approve the sign-in.",
    MFA_AUTH_VOICE_ALT_MOBILE: "Answer the phone call and press # to approve the sign-in.",
    MFA_AUTH_VOICE_OFFICE: "Answer the office phone call and press # to approve the sign-in.",
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
    50131: "An organizational access or security policy prevented sign-in.",
    50133: "Password is incorrect or account is locked.",
    50140: "The 'Keep me signed in' prompt needs to be completed.",
    50144: "User's password has expired.",
    50158: "External security challenge not satisfied.",
    50173: "Fresh token needed.",
    53000: "Device is not compliant.",
    53001: "A domain-joined device is required.",
    53002: "The application is not approved by your organization.",
    53003: "Access blocked by conditional access policy.",
    53004: "MFA registration was blocked by sign-in risk.",
    65001: "Application consent is missing.",
    65004: "Application consent was not completed.",
    90094: "Administrator consent is required.",
}

# Recovery text for errors that do not know which proof Microsoft selected.
_MFA_RECOVERY_HINT = (
    "Complete MFA with a supported method: run --mfa-method choose to select "
    "SMS, Authenticator app, voice, or push, then use auth verify <code> for a "
    "code or auth verify ok for an approval."
)

# Static, code-specific advice only. These are diagnostics, never instructions
# for the state machine to grant consent, alter policy, or retry a blocked flow.
# Microsoft reference: https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes
MS_ERROR_RECOVERY: dict[int, str] = {
    50034: "This email is not associated with a Microsoft account in this tenant.",
    50053: "Account is temporarily locked. Wait a few minutes and try again.",
    50055: "Your password has expired. Reset it via the Microsoft portal.",
    50056: "Password is incorrect. If you recently changed your password, try again.",
    50057: "Your account has been disabled. Contact IT support.",
    50058: "Additional sign-in verification required. Check your authenticator app.",
    50072: f"Multi-factor authentication is required. {_MFA_RECOVERY_HINT}",
    50076: f"Multi-factor authentication is required. {_MFA_RECOVERY_HINT}",
    50126: (
        "Double-check your email and password. "
        "If using @manipal.edu, ensure your account is active."
    ),
    50131: "Contact IT support to review the sign-in and required device or security policy.",
    50133: "Password is incorrect. If you recently changed your password, try again.",
    50140: "Complete the 'Keep me signed in' prompt in the normal browser sign-in flow.",
    53000: "Use a device that meets your organization's compliance requirements, or contact IT support.",
    53001: "Use a device joined as required by your organization, or contact IT support.",
    53002: "Use an application approved by your organization, or contact IT support.",
    53003: "Contact IT support to review the blocked sign-in and your organization's access requirements.",
    53004: (
        "Use your organization's allowed MFA registration process. "
        "If registration remains blocked, contact IT support."
    ),
    65001: (
        "Review the requested application permissions in your organization's normal sign-in flow. "
        "If administrator approval is required, contact IT support."
    ),
    65004: (
        "Review the consent request in your organization's normal sign-in flow. "
        "If you requested administrator approval, wait for the review before retrying."
    ),
    90094: (
        "Ask your organization's administrator to review the requested application permissions. "
        "The CLI cannot grant administrator consent."
    ),
}

# Preserve this fixed guidance in CLI output instead of incorrectly suggesting
# another password attempt. Unknown codes remain opaque; do not infer policy
# from code ranges or from untrusted page text.
MS_INTERACTIVE_ERROR_CODES = frozenset({
    50131, 50140, 53000, 53001, 53002, 53003, 53004, 65001, 65004, 90094,
})


# ---------------------------------------------------------------------------
# Exceptions
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


class PlaywrightUnavailableError(MicrosoftSSOError):
    """The Playwright runtime or Chromium executable could not be launched."""


class MfaPendingError(MicrosoftSSOError):
    """BeginAuth succeeded; complete with ``lighthouse auth verify <code>``."""
