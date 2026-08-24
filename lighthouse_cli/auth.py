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
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .api import LighthouseClient
from .config import ensure_config_dir, load_mfa_pending, save_cookies
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

# ---------------------------------------------------------------------------
# Exceptions and uniform exits
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Raised when authentication fails (wrong credentials, 2FA, etc.)."""


class _PromptUnavailable(Exception):
    """A credential is missing and stdin cannot be prompted."""


def _auth_error(msg: str, json_output: bool, code: int = 1) -> int:
    """Print an auth error and return an exit code."""
    if json_output:
        print(json.dumps({"success": False, "error": msg}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return code


def _interrupted(json_output: bool) -> int:
    """Uniform Ctrl+C exit (130)."""
    if json_output:
        print(json.dumps({"success": False, "error": "Interrupted by user"}))
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
    mfa_method: str,
) -> tuple[str | None, bool]:
    """Return the literal code or defer stdin reading until after BeginAuth."""
    del mfa_method  # compatibility parameter; validation happens separately.
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
    method to match (auto matches anything) — an explicit method that differs
    starts fresh rather than verifying a stale session.
    """
    if (
        pending is not None
        and totp_code is not None
        and not read_totp_after_challenge
        and mfa_method in (MFA_METHOD_AUTO, pending.get("mfa_method"))
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

def _persist_check_report(
    cookies: dict[str, str],
    *,
    json_output: bool,
    failure_hint: str = "",
    save_credentials_pair: tuple[str, str] | None = None,
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
            print(f"Warning: Could not save credentials: {exc}", file=sys.stderr)

    if json_output:
        print(json.dumps({"success": True, "cookies": list(cookies.keys())}))
    else:
        print(f"Login successful. Session valid. Cookies: {', '.join(cookies.keys())}")
    return 0


# ---------------------------------------------------------------------------
# Command entry points
# ---------------------------------------------------------------------------

@_clean_auth_command
def cmd_auth_verify(totp_code: str | None, *, json_output: bool = False) -> int:
    """Complete MFA using saved state from ``auth login`` (same BeginAuth session)."""
    ensure_config_dir()

    if not totp_code or not totp_code.strip():
        return _auth_error("2FA code cannot be empty", json_output, 2)

    # Preflight the encryption key source BEFORE any auth side effects: verify
    # submits the code and then must seal cookies.  Fail here, not mid-flow.
    try:
        CredentialStore().preflight()
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)

    try:
        pending_present = load_mfa_pending() is not None
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)
    except OSError as exc:
        return _auth_error(
            f"Pending MFA session could not be read ({exc.__class__.__name__}). "
            "Run: lighthouse auth login",
            json_output,
        )
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
    return _persist_check_report(cookies, json_output=json_output)


def _cli_method_for_auth_id(auth_method_id: str) -> str:
    """Return the explicit CLI selector for a Microsoft authMethodId."""
    for method in (MFA_METHOD_SMS, MFA_METHOD_CALL, MFA_METHOD_APP, MFA_METHOD_PUSH):
        if auth_method_id in MFA_METHOD_AUTH_IDS.get(method, ()):
            return method
    return "unknown"


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
        return _auth_error(
            "Credentials required. Provide --user/--pass, "
            "LIGHTHOUSE_USERNAME/LIGHTHOUSE_PASSWORD env vars, "
            "or run interactively.",
            json_output,
        )
    if not username or not password:
        return _auth_error("Username and password are required", json_output)

    sso_client = MicrosoftSSOClient()
    try:
        result = sso_client.probe_mfa_methods(username or "", password or "")
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    finally:
        sso_client.close()

    # Report ids + the masked display strings only; `proof.data` can carry
    # raw phone numbers and never leaves the probe.
    methods = [
        {
            "id": proof.auth_method_id,
            "method": _cli_method_for_auth_id(proof.auth_method_id),
            "display": proof.display,
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
        print(
            f"  • {proof.display} — {proof.auth_method_id}; "
            f"use --mfa-method {method}{marker}"
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
        password: Password from --pass flag
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
        return _auth_error(
            "Credentials required. Provide --user/--pass, "
            "LIGHTHOUSE_USERNAME/LIGHTHOUSE_PASSWORD env vars, "
            "or run interactively.",
            json_output,
        )

    if not username:
        return _auth_error("Username cannot be empty", json_output)
    if not password:
        return _auth_error("Password cannot be empty", json_output)

    resolved_mfa_method = (
        mfa_method or os.getenv("LIGHTHOUSE_MFA_METHOD") or MFA_METHOD_AUTO
    ).lower()
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
            totp_code, totp_stdin=totp_stdin, mfa_method=resolved_mfa_method
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
                f"Warning: ignoring an unreadable MFA pending session ({exc}).",
                file=sys.stderr,
            )
        except OSError as exc:
            print(
                "Warning: ignoring an unreadable MFA pending session "
                f"({exc.__class__.__name__}).",
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
        if json_output:
            print(json.dumps({
                "success": False,
                "mfa_pending": True,
                "message": str(exc),
                "recovery": exc.recovery,
            }))
        else:
            print(str(exc), flush=True)
        return 0
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    except CredentialStoreError as exc:
        return _auth_error(str(exc), json_output)
    finally:
        sso_client.close()

    return _persist_check_report(
        cookies,
        json_output=json_output,
        failure_hint="Try: lighthouse auth login",
        save_credentials_pair=(username, password) if save_credentials else None,
    )
