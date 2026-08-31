"""Pure HTTP authentication commands and login policy for lighthouse-cli.

The Microsoft SSO flow itself lives in ``lighthouse_cli.ms_auth``
(``MicrosoftSSOClient``); encrypted storage lives in
``lighthouse_cli.credential_store`` (``CredentialStore``).  This module owns:

- the pure login-policy layer: credential resolution
  (flags > env > CredentialStore > prompt), TOTP normalization, and login
  planning (``resume`` | ``fresh`` | ``defer``);
- the ``auth login`` / ``auth verify`` entry points, which share one
  persist→check→report success tail (seal cookies → validate session → only
  then optionally store credentials; verify never stores credentials).
"""

from __future__ import annotations

import functools
import getpass
import json
import os
import re
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .api import LighthouseClient, NetworkError, refresh_auth_from_browser
from .config import (
    clear_mfa_pending,
    ensure_config_dir,
    load_mfa_pending,
    missing_cookie_names,
    save_cookies,
)
from .credential_store import CredentialStore, CredentialStoreError
from .ms_auth import (
    MFA_METHOD_APP,
    MFA_METHOD_AUTH_IDS,
    MFA_METHOD_AUTO,
    MFA_METHOD_CALL,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_PUSH,
    MFA_METHOD_SMS,
    VALID_MFA_METHODS,
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
)
from .ms_mfa import format_user_proof, safe_auth_method_id

# ---------------------------------------------------------------------------
# Exceptions and uniform exits
# ---------------------------------------------------------------------------

class _PromptUnavailable(Exception):
    """A credential is missing and stdin cannot be prompted."""


_AUTH_ERROR_FALLBACK = "Authentication failed. Check your credentials and try again."
_EMAIL_RE = re.compile(r"(?i)\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{5,}\d)(?!\w)")
_AUTH_SECRET_FIELD_RE = re.compile(
    r"(?ix)(?<![a-z0-9_])"
    r"[\"']?(?:password|passwd|passphrase|pass|secret|token|"
    r"flow[\s_-]*token|cookie(?:s|value)?|session(?:val(?:ue)?|id)|"
    r"access[\s_-]*token|client[\s_-]*secret|api[\s_-]*key|"
    r"authorization|bearer|saml[\s_-]*(?:response|request))[\"']?"
    r"(?![a-z0-9])\s*(?:[:=]|\b(?:is|was|has)\b|\s+)"
    r"(?!(?:is|was|has|required|incorrect|expired|invalid|accepted|reset)\b)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;{}\[\]]+)"
)
_AUTH_CODE_FIELD_RE = re.compile(
    r"(?ix)(?<![a-z0-9_])[\"']?(?:otp|totp|canary|ctx)[\"']?"
    r"(?![a-z0-9])\s*(?:[:=]|\b(?:is|was)\b)\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;{}\[\]]+)"
)
_AUTH_SECRET_SHAPED_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9]*(?:password|passwd|passphrase|secret|token|"
    r"cookie|cookies|samlresponse|samlrequest|otp|totp|canary|session)"
    r"[a-z0-9_-]*\b\s*[:=]"
)
_AUTH_SECRET_FLAG_RE = re.compile(
    r"(?i)--(?:pass(?:word)?|token|secret)\s+[^\s,;{}\[\]]+"
)
_AUTH_CODE_RE = re.compile(r"(?:^|:\s*)\[(\d{3,8})\]\s*")
_AUTH_UNSAFE_BRACKET_RE = re.compile(r"[{}]|\[(?!\d{3,8}\])|(?<!\])\]")
_AUTH_SAFE_TYPE_RE = re.compile(r"^Unexpected error \([A-Za-z0-9_.]+\)\.")

_SAFE_CREDENTIALS_ERROR = (
    "Credentials required. Provide LIGHTHOUSE_USERNAME/LIGHTHOUSE_PASSWORD "
    "environment variables, use encrypted saved credentials, or run interactively."
)
_SAFE_MFA_RECOVERY_COMMANDS = (
    "lighthouse auth login --mfa-method sms",
    "lighthouse auth login",
    "lighthouse auth verify <code>",
    "lighthouse auth verify <current-app-code>",
    "lighthouse auth verify ok",
)
_SAFE_BROWSER_NETWORK_ERRORS = frozenset({
    "Could not run the local browser cookie helper.",
    "The local browser cookie helper failed.",
    "The local browser cookie helper returned invalid data.",
    "No usable Lighthouse cookies were found in the browser.",
})
_SAFE_FIRST_PARTY_AUTH_PREFIXES = (
    ("2fa verification timed out", "2FA verification timed out waiting for approval."),
    ("d2l acs redirect limit exceeded", "D2L ACS redirect limit exceeded."),
    ("d2l home redirect limit exceeded", "D2L home redirect limit exceeded."),
    (
        "microsoft session-pull requested an unsafe re-post target",
        "Microsoft session-pull requested an unsafe re-POST target.",
    ),
    (
        "2fa code required after verification was sent",
        "2FA code required after verification was sent.",
    ),
    (
        "a pre-provided --totp code is valid only for phoneappotp",
        "A pre-provided --totp code is valid only for PhoneAppOTP.",
    ),
    ("pending mfa session is incomplete", "Pending MFA session is incomplete."),
)


def _safe_mfa_recovery(value: object) -> str | None:
    """Return only a fixed MFA recovery command, never pasted arguments."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if ":" in text and text.casefold().split(":", 1)[0] in {"run", "try", "use"}:
        text = text.split(":", 1)[1].strip()
    for command in _SAFE_MFA_RECOVERY_COMMANDS:
        if text == command or text.startswith(f"{command} "):
            return command
    return None


def _safe_auth_category(text: str, code: str | None) -> str:
    """Map an auth error to a fixed, non-upstream diagnostic."""
    lowered = text.casefold()
    if lowered.startswith("authentication failed"):
        return f"Authentication failed ({code})." if code else _AUTH_ERROR_FALLBACK
    if lowered.startswith("login completed but session verification failed"):
        return "Login completed but session verification failed. Try: lighthouse auth login"
    if lowered.startswith("credentials required"):
        return _SAFE_CREDENTIALS_ERROR
    if lowered.startswith("username and password are required"):
        return "Username and password are required."
    if lowered.startswith("username cannot be empty"):
        return "Username cannot be empty."
    if lowered.startswith("password cannot be empty"):
        return "Password cannot be empty."
    if lowered.startswith("no pending mfa session"):
        return "No pending MFA session. Run: lighthouse auth login --mfa-method sms"
    if lowered.startswith("pending mfa session is corrupted"):
        return "Pending MFA session is corrupted. Run: lighthouse auth login --mfa-method sms"
    if lowered.startswith("no encryption key source"):
        return (
            "No encryption key source is available. Set LIGHTHOUSE_SECRETS_PASSPHRASE "
            "or configure a system keyring backend."
        )
    if lowered.startswith("browser session is missing required d2l cookies"):
        return "Browser session is missing required D2L cookies."
    if lowered.startswith("the local browser cookie helper failed"):
        return "The local browser cookie helper failed."
    if lowered.startswith("could not run the local browser cookie helper"):
        return "Could not run the local browser cookie helper."
    if lowered.startswith("the local browser cookie helper returned invalid data"):
        return "The local browser cookie helper returned invalid data."
    if lowered.startswith("no usable lighthouse cookies"):
        return "No usable Lighthouse cookies were found in the browser."
    if lowered.startswith("cdp port must be an integer"):
        return "CDP port must be an integer from 1 to 65535"
    if lowered.startswith("invalid mfa method"):
        return "Invalid MFA method. Use auto, sms, app, call, push, or choose."
    if lowered.startswith("requested mfa method"):
        return "Requested MFA method is not available. Use --mfa-method auto or choose."
    if lowered.startswith("multiple mfa methods"):
        return "Multiple MFA methods are available; choose one with --mfa-method."
    if lowered.startswith("no mfa methods"):
        return "No MFA methods are registered on this account."
    if lowered.startswith("2fa code cannot be empty"):
        return "2FA code cannot be empty."
    if lowered.startswith("2fa code is required"):
        return "2FA code is required. Provide a code when prompted or use auth verify."
    if lowered.startswith("2fa verification failed"):
        return "2FA verification failed. Request a new code and try again."
    if lowered.startswith("mfa setup failed"):
        return "MFA setup failed. Try a different --mfa-method."
    if lowered.startswith("failed to redirect"):
        return "Failed to redirect to Microsoft SSO."
    if lowered.startswith("could not find microsoft"):
        return "Could not find Microsoft login configuration on the page."
    if lowered.startswith("microsoft returned"):
        return "Microsoft returned an unexpected response."
    if lowered.startswith("verification code sent"):
        return "Verification code sent."
    if lowered.startswith("authenticator approval requested"):
        return "Authenticator approval requested."
    if lowered.startswith("authenticator code required"):
        return "Authenticator code required."
    if lowered.startswith("voice approval call started"):
        return "Voice approval call started."
    if lowered.startswith("--totp"):
        return "--totp cannot be used with the selected MFA method."
    if "codeless" in lowered and "--totp" in lowered:
        return "--mfa-method is codeless; do not use --totp."
    if safe_type := _AUTH_SAFE_TYPE_RE.match(text):
        return safe_type.group(0)
    if lowered == "invalid username or password.":
        return text
    if code and "invalid username or password" in lowered:
        return f"Authentication failed ({code})."
    return _AUTH_ERROR_FALLBACK


def _safe_auth_error_message(msg: object) -> str:
    """Keep auth diagnostics bounded and free of upstream secret material.

    Auth exceptions include a detailed ``Step``/``Fix`` rendering, and their
    first line may contain an upstream value.  Only fixed categories are
    retained; structured or option-bearing text is discarded as a whole.
    """
    if not isinstance(msg, str) or not msg:
        return _AUTH_ERROR_FALLBACK
    text = " ".join(msg.split())
    if text in _SAFE_BROWSER_NETWORK_ERRORS:
        return text
    lowered = text.casefold()
    for prefix, safe_message in _SAFE_FIRST_PARTY_AUTH_PREFIXES:
        if lowered.startswith(prefix):
            return safe_message
    if (
        not text
        or len(text) > 512
        or any(not char.isprintable() for char in text)
    ):
        return _AUTH_ERROR_FALLBACK
    code_match = _AUTH_CODE_RE.search(text)
    code = code_match.group(1) if code_match else None
    if code_match:
        text = text[:code_match.start()] + text[code_match.end():]
    if (
        _AUTH_UNSAFE_BRACKET_RE.search(text)
        or _AUTH_SECRET_FIELD_RE.search(text)
        or _AUTH_CODE_FIELD_RE.search(text)
        or _AUTH_SECRET_SHAPED_RE.search(text)
        or _AUTH_SECRET_FLAG_RE.search(text)
        or _EMAIL_RE.search(text)
        or _PHONE_RE.search(text)
        or "http://" in text.casefold()
        or "https://" in text.casefold()
    ):
        return _AUTH_ERROR_FALLBACK
    return _safe_auth_category(text, code)


def _auth_error(msg: str, json_output: bool, code: int = 1) -> int:
    """Print an auth error and return an exit code."""
    safe_msg = _safe_auth_error_message(msg)
    if json_output:
        print(json.dumps({"success": False, "error": safe_msg}))
        print(f"Error: {safe_msg}", file=sys.stderr)
    else:
        print(f"Error: {safe_msg}", file=sys.stderr)
    return code


def _interrupted(json_output: bool) -> int:
    """Uniform Ctrl+C exit (130)."""
    if json_output:
        print(json.dumps({"success": False, "error": "Interrupted by user"}))
        print("Error: Interrupted by user", file=sys.stderr)
    else:
        print("\nInterrupted.", file=sys.stderr)
    return 130


def _clean_auth_command(fn: Callable[..., int]) -> Callable[..., int]:
    """Wrap an auth command so unexpected failures exit cleanly, not traceback."""

    @functools.wraps(fn)
    def wrapper(*args: Any, json_output: bool = False, **kwargs: Any) -> int:
        try:
            return fn(*args, json_output=json_output, **kwargs)
        except KeyboardInterrupt:
            return _interrupted(json_output)
        except CredentialStoreError as exc:
            # First-party errors: messages are authored to be actionable and
            # secret-free (key resolution, unsealing, sealing failures).
            return _auth_error(str(exc), json_output)
        except NetworkError as exc:
            message = str(exc)
            safe_message = (
                message
                if message in _SAFE_BROWSER_NETWORK_ERRORS
                else _safe_auth_error_message(message)
            )
            return _auth_error(safe_message, json_output)
        except Exception as exc:  # deliberate last-resort guard
            # Never forward raw third-party exception text — str(exc) may
            # embed URLs, tokens, or page content. Only the type is shown.
            return _auth_error(
                f"Unexpected error ({exc.__class__.__name__}). "
                "Re-run the command; if it persists, check your connection "
                "or report the issue with the steps to reproduce.",
                json_output,
            )

    return wrapper


# ---------------------------------------------------------------------------
# Interactive credential prompts (the only I/O in credential resolution)
# ---------------------------------------------------------------------------

def _is_interactive() -> bool:
    """Check if stdin is a TTY."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _prompt_username(json_output: bool) -> str:
    """Read the username from stdin; the banner goes to stderr under --json."""
    stream = sys.stderr if json_output else sys.stdout
    print("Username (email): ", end="", flush=True, file=stream)
    return sys.stdin.readline().strip()


def _prompt_password() -> str:
    """Read the password without echo."""
    return getpass.getpass("Password: ").strip()


# ---------------------------------------------------------------------------
# Login policy — pure decisions: plain args in, plain values out
# ---------------------------------------------------------------------------

CredentialPrompt = Callable[[str], str]


def resolve_credentials(
    flag_username: str | None,
    flag_password: str | None,
    env_username: str | None,
    env_password: str | None,
    stored: tuple[str, str] | None,
    prompt: CredentialPrompt | None,
) -> tuple[str | None, str | None]:
    """Resolve login credentials by precedence: flags > env > store > prompt.

    Every source arrives as a plain value; ``prompt("username")`` /
    ``prompt("password")`` is injected and invoked only for fields still
    unresolved (it may raise to signal that prompting is impossible).  A flag
    given but empty (``--user ''``) skips the environment yet still falls
    through to the store and the prompt — long-standing behaviour, kept.

    Returns:
        ``(username, password)``; either may be None when unresolved.
    """
    username = flag_username if flag_username is not None else env_username or None
    password = flag_password if flag_password is not None else env_password or None
    if stored is not None:
        stored_username, stored_password = stored
        username = username or stored_username or None
        password = password or stored_password or None
    if prompt is not None:
        if not username:
            username = prompt("username")
        if not password:
            password = prompt("password")
    return username or None, password or None


def validate_totp_usage(
    totp_code: str | None,
    *,
    totp_stdin: bool,
    mfa_method: str,
) -> None:
    """Reject code options that cannot belong to the requested fresh challenge.

    ``auto`` is decided only after Microsoft returns the account's proof list,
    so the SSO driver performs the equivalent selected-proof check before
    BeginAuth. A literal ``auto`` value can also resume an existing checkpoint.
    """
    literal = totp_code is not None and not totp_stdin
    if literal and mfa_method == MFA_METHOD_SMS:
        raise ValueError(
            "--totp <code> cannot be used with --mfa-method sms because "
            "Microsoft sends a fresh code after BeginAuth. Run auth login, "
            "then auth verify <code>, or use --totp -."
        )
    if (literal or totp_stdin) and mfa_method in (MFA_METHOD_CALL, MFA_METHOD_PUSH):
        raise ValueError(
            f"--mfa-method {mfa_method} is codeless; do not use --totp. "
            "Start login, complete the approval, then run auth verify ok."
        )
    if literal and mfa_method == MFA_METHOD_CHOOSE:
        raise ValueError(
            "A literal --totp code is ambiguous with --mfa-method choose. "
            "Select --mfa-method app for an offline TOTP, or start the "
            "two-step flow without --totp."
        )


def normalize_totp(
    totp_code: str | None,
    *,
    totp_stdin: bool,
) -> tuple[str | None, bool]:
    """Return the literal code or defer stdin reading until after BeginAuth."""
    if totp_stdin:
        return None, True
    if totp_code is not None and not totp_code.strip():
        raise ValueError("2FA code cannot be empty")
    return totp_code, False


@dataclass(frozen=True)
class LoginPlan:
    """Outcome of login planning for one ``auth login`` invocation.

    ``mode`` is ``"resume"`` (finish an existing pending checkpoint with the
    supplied code), ``"defer"`` (stop after BeginAuth; ``auth verify``
    finishes) or ``"fresh"`` (run the full flow now).
    """

    mode: str
    totp_code: str | None = field(repr=False)
    read_totp_after_challenge: bool
    defer_mfa_to_pending: bool


def plan_login(
    *,
    totp_code: str | None,
    read_totp_after_challenge: bool,
    mfa_method: str,
    pending: dict | None,
    interactive: bool,
) -> LoginPlan:
    """Decide resume vs fresh vs defer from flags + pending state + interactivity.

    ``pending`` is the loaded MFA checkpoint (or None); callers load it only
    when a literal code is in play.  Resume requires the checkpoint's saved
    method to match exactly. ``auto`` starts a fresh flow because a literal
    value alone cannot prove whether the caller intends a saved SMS challenge,
    an app TOTP, or a codeless approval.
    """
    if (
        pending is not None
        and totp_code is not None
        and not read_totp_after_challenge
        and mfa_method == pending.get("mfa_method")
    ):
        return LoginPlan("resume", totp_code, False, False)
    defer_mfa_to_pending = (
        not interactive and totp_code is None and not read_totp_after_challenge
    )
    mode = "defer" if defer_mfa_to_pending else "fresh"
    return LoginPlan(mode, totp_code, read_totp_after_challenge, defer_mfa_to_pending)


# ---------------------------------------------------------------------------
# Shared success tail
# ---------------------------------------------------------------------------

def _print_command_guide() -> None:
    """Print a small map of the CLI without running another command."""
    print("\nCommand guide:")
    print(
        "  Read only:      courses, semesters, content, grades, announcements,\n"
        "                  calendar, assignments, quizzes, quiz"
    )
    print("  Local files:    download, sync")
    print("  Account/setup:  auth status, config courses")
    print("  Remote change:  submit (uploads a file to Lighthouse)")
    print("\nUse lighthouse <command> --help for details.")


def _print_login_next_steps() -> None:
    """Continue a successful interactive login into a short command guide."""
    print("\nTry next:")
    print("  lighthouse courses")
    print("  lighthouse semesters")
    print("  lighthouse assignments <course>")
    print("  lighthouse download <course> --dry-run")
    print("  lighthouse --help")
    print(
        "\nShow the full command guide? [Y/n]: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        answer = input().strip().casefold()
    except (EOFError, KeyboardInterrupt, OSError):
        print(file=sys.stderr)
        return
    if answer in {"", "y", "yes"}:
        _print_command_guide()


def _persist_check_report(
    cookies: dict[str, str],
    *,
    json_output: bool,
    failure_hint: str = "",
    save_credentials_pair: tuple[str, str] | None = None,
    success_message: str = "Login complete. Session saved and verified.",
    include_cookie_names: bool = True,
    show_next_steps: bool = False,
) -> int:
    """Shared login/verify success tail: persist → check → report.

    Security ordering is fixed: cookies are sealed to disk first, the D2L
    session is validated second, and only after a verified session may the
    username/password pair be stored.  Verify passes no pair — it NEVER saves
    credentials.
    """
    save_cookies(cookies)

    hint = f" {failure_hint}" if failure_hint else ""
    if not LighthouseClient().check_auth():
        return _auth_error(
            f"Login completed but session verification failed.{hint}",
            json_output,
        )

    if save_credentials_pair is not None:
        stored_username, stored_password = save_credentials_pair
        try:
            CredentialStore().save(stored_username, stored_password)
        except CredentialStoreError as exc:
            print(
                "Warning: Could not save credentials: "
                f"{_safe_auth_error_message(str(exc))}",
                file=sys.stderr,
            )

    if json_output:
        print(json.dumps({"success": True, "cookies": list(cookies.keys())}))
    else:
        suffix = f" Cookies: {', '.join(cookies.keys())}" if include_cookie_names else ""
        print(f"{success_message}{suffix}")
        if show_next_steps:
            _print_login_next_steps()
    return 0


# ---------------------------------------------------------------------------
# Command entry points
# ---------------------------------------------------------------------------

@_clean_auth_command
def cmd_auth_refresh(
    cdp_port: int | str | None = None,
    *,
    json_output: bool = False,
) -> int:
    """Extract a signed-in browser's D2L cookies through loopback CDP."""
    resolved_port: int | None = None
    if cdp_port is not None:
        try:
            resolved_port = int(cdp_port)
        except (TypeError, ValueError):
            return _auth_error("CDP port must be an integer from 1 to 65535", json_output)
        if not 1 <= resolved_port <= 65535:
            return _auth_error("CDP port must be an integer from 1 to 65535", json_output)

    ensure_config_dir()

    # Cookie extraction is a side effect and the result must be sealed. Fail
    # before contacting the browser when no usable encryption key is present.
    CredentialStore().preflight()
    cookies = refresh_auth_from_browser(resolved_port)
    if missing := missing_cookie_names(cookies):
        return _auth_error(
            "Browser session is missing required D2L cookies: " + ", ".join(missing),
            json_output,
        )

    return _persist_check_report(
        cookies,
        json_output=json_output,
        failure_hint="Open lighthouse.manipal.edu in the browser and sign in, then retry.",
        success_message="Auth refreshed and verified.",
    )

@_clean_auth_command
def cmd_auth_verify(totp_code: str | None, *, json_output: bool = False) -> int:
    """Complete MFA using saved state from ``auth login`` (same BeginAuth session)."""
    ensure_config_dir()

    if not totp_code or not totp_code.strip():
        return _auth_error("2FA code cannot be empty", json_output, 2)

    # A missing checkpoint is a local usage error, not an encryption failure.
    # Check the path before preflight so an installation without a configured
    # key source still reports the useful "No pending MFA session" message.
    store = CredentialStore()
    if not store.mfa_pending_file.exists():
        return _auth_error(
            "No pending MFA session. Run: lighthouse auth login --mfa-method sms",
            json_output,
        )

    # Preflight the encryption key source BEFORE any auth side effects: verify
    # submits the code and then must seal cookies.  Fail here, not mid-flow.
    try:
        store.preflight()
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)

    try:
        pending_present = load_mfa_pending() is not None
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)
    if not pending_present:
        return _auth_error(
            "No pending MFA session. Run: lighthouse auth login --mfa-method sms",
            json_output,
        )

    sso_client = MicrosoftSSOClient()
    try:
        cookies = sso_client.complete_mfa_pending(totp_code.strip())
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)
    except (KeyError, TypeError, ValueError) as exc:
        return _auth_error(
            f"Pending MFA session is corrupted: {exc}. "
            "Run: lighthouse auth login --mfa-method sms",
            json_output,
        )
    finally:
        sso_client.close()

    # No save_credentials_pair by construction: verify never stores secrets.
    return _persist_check_report(
        cookies,
        json_output=json_output,
        include_cookie_names=False,
        show_next_steps=_is_interactive() and not json_output,
    )


def _cli_method_for_auth_id(auth_method_id: str) -> str | None:
    """Return the explicit CLI selector for a Microsoft authMethodId."""
    for method in (MFA_METHOD_SMS, MFA_METHOD_CALL, MFA_METHOD_APP, MFA_METHOD_PUSH):
        if auth_method_id in MFA_METHOD_AUTH_IDS.get(method, ()):
            return method
    return None


@_clean_auth_command
def cmd_auth_mfa_methods(
    username: str | None = None,
    password: str | None = None,
    *,
    json_output: bool = False,
) -> int:
    """Discover the MFA methods registered on the account (no challenge is sent).

    Performs a real sign-in through the post-password stage and may submit a
    KMSI/CMSI continuation, but stops before BeginAuth: no SMS, call, or push
    challenge is triggered. Reports the ``arrUserProofs`` list Microsoft
    serves. Use it to decide which
    ``--mfa-method`` value ``auth login`` supports: sms (OneWaySMS text),
    call (TwoWayVoice* phone call), app (Authenticator OTP), or push
    (Authenticator approval).
    """
    ensure_config_dir()

    interactive = _is_interactive()
    stored: tuple[str, str] | None = None
    if not username or not password:
        with suppress(CredentialStoreError, OSError):
            stored = CredentialStore().load()

    def _prompt(field: str) -> str:
        if not interactive:
            raise _PromptUnavailable(field)
        if field == "username":
            return _prompt_username(json_output)
        return _prompt_password()

    try:
        username, password = resolve_credentials(
            username,
            password,
            os.getenv("LIGHTHOUSE_USERNAME", "").strip(),
            os.getenv("LIGHTHOUSE_PASSWORD", "").strip(),
            stored,
            _prompt,
        )
    except _PromptUnavailable:
        return _auth_error(_SAFE_CREDENTIALS_ERROR, json_output)
    if not username or not password:
        return _auth_error("Username and password are required", json_output)

    sso_client = MicrosoftSSOClient()
    try:
        result = sso_client.probe_mfa_methods(username or "", password or "")
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    finally:
        sso_client.close()

    # Report allowlisted method ids + descriptions derived from static labels
    # and a fixed last-four mask; neither upstream display nor raw data leaves
    # the probe.
    methods = [
        {
            "id": safe_auth_method_id(proof),
            "method": _cli_method_for_auth_id(proof.auth_method_id),
            "display": format_user_proof(proof),
            "is_default": proof.is_default,
        }
        for proof in result.proofs
    ]
    if json_output:
        print(json.dumps({"success": True, "page": result.page, "methods": methods}))
        return 0

    if result.page == "no_mfa":
        print("This account signed in without a second factor.")
        return 0
    if not methods:
        print(
            "A second factor is required, but this tenant serves a legacy "
            "form page without a method list. Use: lighthouse auth login"
        )
        return 0
    print("MFA methods registered on this account:")
    for proof in result.proofs:
        marker = " (Microsoft default)" if proof.is_default else ""
        method = _cli_method_for_auth_id(proof.auth_method_id)
        advice = (
            f"use --mfa-method {method}"
            if method is not None
            else "no supported --mfa-method selector"
        )
        print(
            f"  • {format_user_proof(proof)} — "
            f"{safe_auth_method_id(proof)}; {advice}{marker}"
        )
    return 0


@_clean_auth_command
def cmd_auth_login(
    username: str | None = None,
    password: str | None = None,
    totp_code: str | None = None,
    totp_stdin: bool = False,
    save_credentials: bool = False,
    json_output: bool = False,
    mfa_method: str | None = None,
) -> int:
    """Authenticate via Microsoft SSO using pure HTTP (no browser).

    Flow:
    1. Resolve credentials (flags > env > stored > prompt)
    2. Plan the run: resume a pending checkpoint, defer to ``auth verify``,
       or run the fresh flow now
    3. Authenticate via MicrosoftSSOClient (pure HTTP)
    4. Seal cookies to disk, then validate the session
    5. Optionally save encrypted credentials (only after a verified session)

    Args:
        username: Username from --user flag
        password: Password supplied by an internal caller, environment, store, or prompt
        totp_code: 2FA code from --totp flag (omit for two-phase interactive login)
        totp_stdin: If True, read TOTP from stdin
        save_credentials: If True, save credentials encrypted
        json_output: If True, output JSON
        mfa_method: MFA delivery preference (auto, sms, app, call, push, choose)

    Returns:
        Exit code (0=success, 1=auth failure, 2=CLI usage error, 130=interrupted)
    """
    ensure_config_dir()

    interactive = _is_interactive()
    stored: tuple[str, str] | None = None
    if not username or not password:
        with suppress(CredentialStoreError, OSError):
            stored = CredentialStore().load()

    def _prompt(field: str) -> str:
        if not interactive:
            raise _PromptUnavailable(field)
        if field == "username":
            return _prompt_username(json_output)
        return _prompt_password()

    try:
        username, password = resolve_credentials(
            username,
            password,
            os.getenv("LIGHTHOUSE_USERNAME", "").strip(),
            os.getenv("LIGHTHOUSE_PASSWORD", "").strip(),
            stored,
            _prompt,
        )
    except _PromptUnavailable:
        return _auth_error(_SAFE_CREDENTIALS_ERROR, json_output)

    if not username:
        return _auth_error("Username cannot be empty", json_output)
    if not password:
        return _auth_error("Password cannot be empty", json_output)

    configured_mfa_method = mfa_method or os.getenv("LIGHTHOUSE_MFA_METHOD", "").strip()
    if configured_mfa_method:
        resolved_mfa_method = configured_mfa_method.lower()
    elif interactive and not json_output and totp_code is None and not totp_stdin:
        # A person running the plain command should see only the verification
        # methods Microsoft reports for their account. Scripts retain the
        # tenant-default ``auto`` policy, and a literal app code remains
        # unambiguous without requiring a new flag.
        resolved_mfa_method = MFA_METHOD_CHOOSE
    else:
        resolved_mfa_method = MFA_METHOD_AUTO
    if resolved_mfa_method not in VALID_MFA_METHODS:
        return _auth_error(
            f"Invalid MFA method {resolved_mfa_method!r}. "
            f"Use: {', '.join(VALID_MFA_METHODS)}",
            json_output,
            2,
        )

    try:
        validate_totp_usage(
            totp_code, totp_stdin=totp_stdin, mfa_method=resolved_mfa_method
        )
        code, read_totp_after_challenge = normalize_totp(
            totp_code, totp_stdin=totp_stdin
        )
    except ValueError as exc:
        return _auth_error(str(exc), json_output, 2)

    # --- Preflight the encryption key source ---
    # Login reads the pending checkpoint below and sends BeginAuth (a side
    # effect); both require a usable key source — fail here with an
    # actionable message before either happens. (Stored credentials may have
    # been read earlier, but only under error suppression.)
    try:
        CredentialStore().preflight()
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)

    # Resume the pending MFA session only when the provided code belongs to it.
    # An explicit --mfa-method that differs from the pending session (e.g. an
    # offline app TOTP after a stale SMS pending) starts a fresh flow instead.
    # An unopenable checkpoint (e.g. sealed under a different key source) must
    # not abort a login that would never resume it — degrade to a fresh flow.
    pending = None
    if code is not None:
        try:
            pending = load_mfa_pending()
        except CredentialStoreError as exc:
            print(
                "Warning: ignoring an unreadable MFA pending session ("
                f"{_safe_auth_error_message(str(exc))}).",
                file=sys.stderr,
            )
    plan = plan_login(
        totp_code=code,
        read_totp_after_challenge=read_totp_after_challenge,
        mfa_method=resolved_mfa_method,
        pending=pending,
        interactive=interactive,
    )
    if plan.mode == "resume":
        return cmd_auth_verify(plan.totp_code, json_output=json_output)

    def _on_password_accepted() -> None:
        if json_output or not interactive:
            return
        print("Password accepted. Completing second factor...", flush=True)

    if interactive and not json_output and plan.totp_code is None:
        print(
            "Two-step sign-in: enter email and password first; "
            "then complete the verification method Microsoft selects.",
            flush=True,
        )
        if resolved_mfa_method == MFA_METHOD_SMS:
            print("MFA preference: text message (--mfa-method sms).", flush=True)
        elif resolved_mfa_method == MFA_METHOD_CHOOSE:
            print("You will be asked to pick a verification method.", flush=True)

    # --- Authenticate via HTTP ---
    sso_client = MicrosoftSSOClient()
    try:
        cookies = sso_client.login(
            username,
            password,
            plan.totp_code,
            mfa_method=resolved_mfa_method,
            on_credentials_submitted=_on_password_accepted,
            read_totp_after_challenge=plan.read_totp_after_challenge,
            defer_mfa_to_pending=plan.defer_mfa_to_pending,
        )
    except MfaPendingError as exc:
        safe_message = _safe_auth_error_message(str(exc))
        safe_recovery = _safe_mfa_recovery(exc.recovery)
        if json_output:
            print(json.dumps({
                "success": False,
                "mfa_pending": True,
                "message": safe_message,
                "recovery": safe_recovery,
            }))
            print(f"Error: {safe_message}", file=sys.stderr)
        else:
            print(safe_message, flush=True)
            if safe_recovery:
                print(f"Fix: {safe_recovery}", flush=True)
        return 0
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)
    finally:
        sso_client.close()

    # ``MicrosoftSSOClient.login`` normally clears its own checkpoint after
    # extracting the D2L cookies.  Keep that lifecycle guarantee at the
    # command boundary too: a successful inline login must never leave a
    # previous deferred challenge eligible for the next ``auth login``.
    # This is deliberately after ``login`` returns; deferred MFA and any
    # post-EndAuth failure raise before reaching this point and therefore keep
    # their recovery checkpoint intact.
    clear_mfa_pending()

    return _persist_check_report(
        cookies,
        json_output=json_output,
        failure_hint="Try: lighthouse auth login",
        save_credentials_pair=(username, password) if save_credentials else None,
        include_cookie_names=False,
        show_next_steps=interactive and not json_output,
    )
