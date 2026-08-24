"""Microsoft SSO exceptions and constants."""

from __future__ import annotations

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
