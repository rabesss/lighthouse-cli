from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:100]!r}")
    write(path, text.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, expected: int) -> None:
    text = read(path)
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}: {old[:100]!r}")
    write(path, text.replace(old, new))


def replace_def(path: str, name: str, source: str) -> None:
    text = read(path)
    tree = ast.parse(text)
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            matches.append(node)
    if len(matches) != 1:
        raise RuntimeError(f"{path}: expected one function {name}, found {len(matches)}")
    node = matches[0]
    start = min([node.lineno, *[d.lineno for d in node.decorator_list]]) - 1
    end = node.end_lineno
    lines = text.splitlines(keepends=True)
    replacement = dedent(source).strip("\n") + "\n"
    write(path, "".join(lines[:start]) + replacement + "".join(lines[end:]))


def append_once(path: str, marker: str, source: str) -> None:
    text = read(path)
    if marker in text:
        raise RuntimeError(f"{path}: append marker already present: {marker}")
    write(path, text.rstrip() + "\n\n\n" + dedent(source).strip("\n") + "\n")


# ---------------------------------------------------------------------------
# MFA taxonomy: SMS submits a server-sent code; app OTP submits an offline
# code; push and voice are codeless approval/polling methods.
# ---------------------------------------------------------------------------

replace_once(
    "lighthouse_cli/ms_errors.py",
    '''# Methods whose verification code is generated server-side and delivered on
# BeginAuth (SMS/WhatsApp text, or spoken during a phone call): a literal
# pre-provided code cannot match — always collect after the challenge.
SERVER_SENT_CODE_AUTH_IDS = frozenset({
    MFA_AUTH_SMS,
    MFA_AUTH_VOICE_MOBILE,
    MFA_AUTH_VOICE_ALT_MOBILE,
    MFA_AUTH_VOICE_OFFICE,
})

# Methods that submit a code through EndAuth's AdditionalAuthData: offline
# authenticator TOTP plus every server-sent code method. Codeless methods
# (push notification approval) never send one.
CODE_SUBMITTING_AUTH_IDS = frozenset({MFA_AUTH_APP_OTP}) | SERVER_SENT_CODE_AUTH_IDS

MFA_METHOD_AUTH_IDS: dict[str, tuple[str, ...]] = {
    MFA_METHOD_SMS: (MFA_AUTH_SMS,),
    MFA_METHOD_APP: (MFA_AUTH_APP_OTP, MFA_AUTH_APP_NOTIFY),
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
    MFA_AUTH_VOICE_MOBILE: "Answer the phone call and enter the code it reads out.",
    MFA_AUTH_VOICE_ALT_MOBILE: "Answer the phone call and enter the code it reads out.",
    MFA_AUTH_VOICE_OFFICE: "Answer the office phone call and enter the code it reads out.",
}
''',
    '''# SMS/WhatsApp generates a fresh server-side code on BeginAuth. A literal
# pre-provided code cannot belong to that fresh challenge.
SERVER_SENT_CODE_AUTH_IDS = frozenset({MFA_AUTH_SMS})

# Voice calls are codeless approvals: answer the call and press #. Authenticator
# push is also codeless (and may require number matching). Both are completed by
# polling EndAuth without AdditionalAuthData.
VOICE_APPROVAL_AUTH_IDS = frozenset({
    MFA_AUTH_VOICE_MOBILE,
    MFA_AUTH_VOICE_ALT_MOBILE,
    MFA_AUTH_VOICE_OFFICE,
})
CODELESS_APPROVAL_AUTH_IDS = frozenset({MFA_AUTH_APP_NOTIFY}) | VOICE_APPROVAL_AUTH_IDS

# Only SMS and offline Authenticator TOTP submit AdditionalAuthData.
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

MFA_AUTH_ID_METHOD: dict[str, str] = {
    auth_id: method
    for method, auth_ids in MFA_METHOD_AUTH_IDS.items()
    for auth_id in auth_ids
}

MFA_METHOD_INSTRUCTIONS: dict[str, str] = {
    MFA_AUTH_SMS: "Check the SMS or WhatsApp message on your registered phone.",
    MFA_AUTH_APP_OTP: "Open Microsoft Authenticator and enter the 6-digit code.",
    MFA_AUTH_APP_NOTIFY: (
        "Use the number printed by this command to approve the request in "
        "Microsoft Authenticator."
    ),
    MFA_AUTH_VOICE_MOBILE: "Answer the phone call and press # to approve.",
    MFA_AUTH_VOICE_ALT_MOBILE: "Answer the alternate-phone call and press # to approve.",
    MFA_AUTH_VOICE_OFFICE: "Answer the office-phone call and press # to approve.",
}
''',
)

replace_once(
    "lighthouse_cli/ms_mfa.py",
    '"Multiple MFA methods are available; pick one with --mfa-method sms|app.",',
    '"Multiple MFA methods are available; pick one with --mfa-method sms|app|call|push.",',
)


# ---------------------------------------------------------------------------
# CLI policy: reject incompatible literal/stdin codes before BeginAuth while
# preserving `auto --totp` as the pending-session resume spelling.
# ---------------------------------------------------------------------------

replace_once(
    "lighthouse_cli/auth.py",
    '''from .ms_auth import (
    MFA_METHOD_AUTO,
    MFA_METHOD_CALL,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_SMS,
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    VALID_MFA_METHODS,
)
''',
    '''from .ms_auth import (
    MFA_AUTH_ID_METHOD,
    MFA_METHOD_APP,
    MFA_METHOD_AUTO,
    MFA_METHOD_CALL,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_PUSH,
    MFA_METHOD_SMS,
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    VALID_MFA_METHODS,
)
''',
)

replace_def(
    "lighthouse_cli/auth.py",
    "normalize_totp",
    '''
def normalize_totp(
    totp_code: str | None,
    *,
    totp_stdin: bool,
    mfa_method: str,
) -> tuple[str | None, bool]:
    """Validate a pre-provided code against the requested MFA transport.

    A literal is valid for explicit ``app`` (offline PhoneAppOTP), and remains
    accepted for ``auto`` so it can resume an existing pending challenge. Fresh
    ``auto`` flows validate it again after Microsoft selects the proof.
    """
    if totp_stdin:
        if mfa_method in (MFA_METHOD_CALL, MFA_METHOD_PUSH):
            raise ValueError(
                f"--totp - cannot be used with --mfa-method {mfa_method}; "
                "this method is codeless."
            )
        return None, True

    if totp_code is None:
        return None, False
    if not totp_code.strip():
        raise ValueError("2FA code cannot be empty")
    if mfa_method == MFA_METHOD_SMS:
        raise ValueError(
            "A literal --totp cannot be used with --mfa-method sms because "
            "Microsoft sends a fresh code after BeginAuth. Run auth login, "
            "then auth verify <code>, or use --totp -."
        )
    if mfa_method in (MFA_METHOD_CALL, MFA_METHOD_PUSH):
        raise ValueError(
            f"A literal --totp cannot be used with --mfa-method {mfa_method}; "
            "this method is codeless."
        )
    if mfa_method == MFA_METHOD_CHOOSE:
        raise ValueError(
            "A literal --totp cannot be combined with --mfa-method choose; "
            "select app explicitly or omit --totp."
        )
    return totp_code, False
''',
)

replace_all(
    "lighthouse_cli/auth.py",
    "with suppress(CredentialStoreError):\n            stored = CredentialStore().load()",
    "with suppress(CredentialStoreError, OSError):\n            stored = CredentialStore().load()",
    2,
)

replace_once(
    "lighthouse_cli/auth.py",
    '''        except CredentialStoreError as exc:
            print(
                f"Warning: ignoring an unreadable MFA pending session ({exc}).",
                file=sys.stderr,
            )
''',
    '''        except (CredentialStoreError, OSError) as exc:
            print(
                "Warning: ignoring an unreadable MFA pending session "
                f"({exc.__class__.__name__}).",
                file=sys.stderr,
            )
''',
)

replace_def(
    "lighthouse_cli/auth.py",
    "cmd_auth_mfa_methods",
    '''
@_clean_auth_command
def cmd_auth_mfa_methods(
    username: str | None = None,
    password: str | None = None,
    *,
    json_output: bool = False,
) -> int:
    """Discover registered MFA methods without triggering BeginAuth.

    This performs a real Microsoft sign-in through the post-password stage and
    may advance KMSI/session state. It stops before BeginAuth, so it does not
    send an SMS, place a voice call, or trigger an Authenticator notification.
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
        result = sso_client.probe_mfa_methods(username, password)
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    finally:
        sso_client.close()

    methods = [
        {
            "id": proof.auth_method_id,
            "method": MFA_AUTH_ID_METHOD.get(proof.auth_method_id),
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
    for proof, method in zip(result.proofs, methods, strict=True):
        marker = " (Microsoft default)" if proof.is_default else ""
        selector = method["method"] or "unknown"
        print(
            f"  • {proof.display} — {proof.auth_method_id} "
            f"(--mfa-method {selector}){marker}"
        )
    return 0
''',
)

replace_once(
    "lighthouse_cli/auth.py",
    "        mfa_method: MFA delivery preference (auto, sms, app)\n",
    "        mfa_method: MFA preference (auto, sms, app, call, push, choose)\n",
)


# ---------------------------------------------------------------------------
# SSO driver hardening, codeless approvals, number matching, and typed walk
# exhaustion.
# ---------------------------------------------------------------------------

replace_once(
    "lighthouse_cli/ms_auth.py",
    "from urllib.parse import urljoin, urlparse\n",
    "from contextlib import suppress\nfrom urllib.parse import parse_qs, urljoin, urlparse\n",
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''from lighthouse_cli.ms_errors import (  # noqa: F401
    CODE_SUBMITTING_AUTH_IDS,
''',
    '''from lighthouse_cli.ms_errors import (  # noqa: F401
    CODE_SUBMITTING_AUTH_IDS,
    CODELESS_APPROVAL_AUTH_IDS,
''',
)
replace_once(
    "lighthouse_cli/ms_auth.py",
    '''    MFA_AUTH_SMS,
    MFA_METHOD_APP,
''',
    '''    MFA_AUTH_SMS,
    MFA_AUTH_ID_METHOD,
    MFA_METHOD_APP,
''',
)
replace_once(
    "lighthouse_cli/ms_auth.py",
    '''    SERVER_SENT_CODE_AUTH_IDS,
    VALID_MFA_METHODS,
''',
    '''    SERVER_SENT_CODE_AUTH_IDS,
    VALID_MFA_METHODS,
    VOICE_APPROVAL_AUTH_IDS,
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''# Microsoft's session-pull reload interstitial declares slMaxRetry=2; the
# walk honors that bound so a broken tenant loop cannot ping-pong forever.
_MAX_SSO_RELOADS = 2
''',
    '''# Conservative client-side cap. The page's `slMaxRetry` belongs to its
# resource loader and is not a declared session-pull POST budget.
_MAX_SSO_RELOADS = 2
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''class Transition(NamedTuple):
''',
    '''class PlaywrightBootstrapUnavailable(MicrosoftSSOError):
    """Playwright/Chromium cannot start; the HTTP username mirror may be used."""


class Transition(NamedTuple):
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "is_sso_reload_page",
    '''
def is_sso_reload_page(snapshot: ResponseSnapshot) -> bool:
    """True for Microsoft's credential-echo session-pull interstitial."""
    cfg = _extract_config_json(snapshot.html) or {}
    url_post = str(cfg.get("urlPost") or "")
    query = parse_qs(urlparse(url_post).query)
    reload_requested = any(
        value.lower() == "true" for value in query.get("sso_reload", [])
    )
    params = cfg.get("oPostParams")
    return reload_requested and isinstance(params, dict) and bool(params)
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "sso_reload_transition",
    '''
def sso_reload_transition(snapshot: ResponseSnapshot, base_url: str) -> Transition:
    """Build the credential-echo re-POST without exposing any field values."""
    cfg = _extract_config_json(snapshot.html) or {}
    url_post = str(cfg.get("urlPost") or "")
    params = cfg.get("oPostParams")
    if not isinstance(params, dict) or not params:
        raise MicrosoftSSOError(
            "Microsoft session reload did not contain a usable form payload.",
            step="POST credentials",
        )

    target_url = _absolute_url(base_url, url_post)
    source = urlparse(snapshot.url or base_url)
    target = urlparse(target_url)
    if (
        target.scheme.lower() != "https"
        or not source.netloc
        or target.netloc.lower() != source.netloc.lower()
    ):
        raise MicrosoftSSOError(
            "Microsoft session reload target failed the same-origin HTTPS check.",
            step="POST credentials",
            recovery="Retry the login; if it persists, Microsoft may have changed the flow.",
        )

    form_data: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, str):
            encoded = value
        elif isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, (int, float)):
            encoded = str(value)
        else:
            raise MicrosoftSSOError(
                "Microsoft session reload contained a non-scalar form field.",
                step="POST credentials",
                recovery="Retry the login; if it persists, Microsoft may have changed the flow.",
            )
        form_data[str(key)] = encoded

    return Transition(kind="sso_reload", url=target_url, data=form_data)
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "probe_mfa_methods",
    '''
    def probe_mfa_methods(self, username: str, password: str) -> MfaProbeResult:
        """Perform a real sign-in through the MFA-selection page.

        The walk may advance KMSI/session state but stops before BeginAuth, so
        no SMS, voice call, or Authenticator notification is triggered.
        """
        ms_url = self._step_initiate_saml()
        config = self._step_get_ms_config(ms_url)
        config = self._step_prepare_username(config, username)
        snap = self._step_post_credentials(
            config, username, password, skip_username_prepare=True
        )
        snap = self._advance_to_saml(snap, snap.url, checkpoint_kmsi=False)
        if not is_mfa_page(snap.html):
            if is_error_page(snap):
                code, msg = _extract_error_code_and_msg(snap.html)
                raise build_sso_error(code, msg, "POST credentials")
            if extract_saml_response(snap.html):
                return MfaProbeResult(page="no_mfa", proofs=[])
            raise MicrosoftSSOError(
                "Could not determine MFA methods from Microsoft's response. "
                f"{describe_page_shape(snap)}",
                step="MFA discovery",
                recovery="Retry the command; Microsoft may have changed the sign-in flow.",
            )
        cfg = _extract_config_json(snap.html) or {}
        proofs = _parse_user_proofs(cfg)
        page = "converged" if proofs else "legacy_form"
        return MfaProbeResult(page=page, proofs=proofs)
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "_bootstrap_username_via_playwright",
    '''
    def _bootstrap_username_via_playwright(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Run the username step in Chromium, distinguishing launch failures."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlaywrightBootstrapUnavailable(
                "Playwright is unavailable.",
                step="prepare username",
                recovery="Install with: pip install playwright && playwright install chromium",
            ) from exc

        ms_url = str(config.get("_ms_url", ""))
        if not ms_url:
            raise MicrosoftSSOError(
                "Missing Microsoft login page URL.",
                step="prepare username",
            )

        user_agent = self._session.headers.get("User-Agent", "")
        export_cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
            }
            for cookie in self._session.cookies
        ]

        browser: Any = None
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:
                    raise PlaywrightBootstrapUnavailable(
                        f"Playwright/Chromium could not start ({exc.__class__.__name__}).",
                        step="prepare username",
                        recovery="Ensure Chromium is installed: playwright install chromium",
                    ) from None

                try:
                    context = browser.new_context(user_agent=user_agent)
                    if export_cookies:
                        context.add_cookies(export_cookies)
                    page = context.new_page()
                    page.goto(ms_url, wait_until="networkidle", timeout=60000)
                    login_input = page.query_selector('input[name="loginfmt"]')
                    if login_input:
                        page.fill('input[name="loginfmt"]', username)
                        page.click("#idSIButton9")
                        page.wait_for_selector('input[name="passwd"]', timeout=30000)
                    pw_cfg = page.evaluate(
                        """() => ({
                            urlPost: $Config.urlPost,
                            sFT: $Config.sFT,
                            sCtx: $Config.sCtx,
                            canary: $Config.canary,
                            sessionId: $Config.sessionId,
                            i19: $Config.i19
                        })"""
                    )
                    referer = page.url
                    pw_cookies = context.cookies()
                except Exception as exc:
                    raise MicrosoftSSOError(
                        f"Playwright username step failed ({exc.__class__.__name__}).",
                        step="prepare username",
                        recovery="Retry the login; Microsoft may have changed the username page.",
                    ) from None
                finally:
                    if browser is not None:
                        with suppress(Exception):
                            browser.close()
        except PlaywrightBootstrapUnavailable:
            raise
        except MicrosoftSSOError:
            raise
        except Exception as exc:
            raise PlaywrightBootstrapUnavailable(
                f"Playwright runtime could not start ({exc.__class__.__name__}).",
                step="prepare username",
                recovery="Ensure Playwright and Chromium are installed correctly.",
            ) from None

        self._import_playwright_cookies(pw_cookies)
        _prune_stale_esctx_cookies(self._session)

        updated = dict(config)
        for key in ("urlPost", "sFT", "sCtx", "canary", "sessionId", "i19"):
            if pw_cfg.get(key):
                updated[key] = pw_cfg[key]
        updated["_ms_url"] = referer
        return updated
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "_step_prepare_username",
    '''
    def _step_prepare_username(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Establish Microsoft session state after the username step."""
        if not config.get("urlGetCredentialType"):
            return config
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return self._step_prepare_username_http(config, username)
        try:
            return self._bootstrap_username_via_playwright(config, username)
        except PlaywrightBootstrapUnavailable:
            print(
                "Playwright/Chromium unavailable; using the pure-HTTP flow.",
                file=sys.stderr,
                flush=True,
            )
            return self._step_prepare_username_http(config, username)
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "_print_mfa_phase_banner",
    '''
    def _print_mfa_phase_banner(
        self,
        proofs: list[UserProof],
        selected: UserProof,
        *,
        code_sent_on_begin: bool,
    ) -> None:
        if not sys.stdin.isatty():
            return
        print("\n--- Second factor required ---", flush=True, file=sys.stderr)
        print("Registered verification methods on your account:", flush=True, file=sys.stderr)
        for proof in proofs:
            marker = " (selected)" if proof.auth_method_id == selected.auth_method_id else ""
            print(f"  • {proof.display}{marker}", flush=True, file=sys.stderr)
        hint = MFA_METHOD_INSTRUCTIONS.get(
            selected.auth_method_id,
            "Complete the verification method shown above.",
        )
        if selected.auth_method_id == MFA_AUTH_SMS and code_sent_on_begin:
            phone = _mask_phone_hint(selected.data)
            print(f"\nA verification code was just sent to {phone}.", flush=True, file=sys.stderr)
            print(
                "Delivery (SMS vs WhatsApp) is chosen by Microsoft; the CLI cannot force a channel.",
                flush=True,
                file=sys.stderr,
            )
        print(f"\n{hint}", flush=True, file=sys.stderr)
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''        Server-sent code methods (SMS/WhatsApp text, TwoWayVoice* phone calls)
        issue a fresh code on BeginAuth; PhoneAppOTP is an offline TOTP
        generated on the user's device, so a pre-provided code stays valid.
''',
    '''        SMS/WhatsApp issues a fresh server-side code on BeginAuth;
        PhoneAppOTP is generated offline on the user's device. Voice and push
        are codeless and do not pass through this collector.
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "_step_handle_mfa_converged",
    '''
    def _step_handle_mfa_converged(
        self,
        mfa_snap: ResponseSnapshot,
        mfa_config: dict[str, Any],
        proofs: list[UserProof],
        totp_code: str | None,
        *,
        mfa_method: str,
        read_totp_after_challenge: bool = False,
        defer_mfa_to_pending: bool = False,
    ) -> ResponseSnapshot:
        """Handle ConvergedTFA via BeginAuth → EndAuth → ProcessAuth."""
        selected = _select_user_proof(proofs, mfa_method)

        if selected.auth_method_id in CODELESS_APPROVAL_AUTH_IDS and (
            totp_code is not None or read_totp_after_challenge
        ):
            raise MicrosoftSSOError(
                "The selected MFA method is codeless; do not provide --totp.",
                step="MFA",
                recovery="Re-run without --totp.",
            )
        if (
            selected.auth_method_id == MFA_AUTH_SMS
            and totp_code is not None
            and not read_totp_after_challenge
        ):
            raise MicrosoftSSOError(
                "A literal code cannot be used for a fresh SMS challenge.",
                step="MFA",
                recovery=(
                    "Run auth login, then auth verify <code>, or use --totp - "
                    "to read the freshly sent code."
                ),
            )
        if (
            totp_code is not None
            and selected.auth_method_id != MFA_AUTH_APP_OTP
        ):
            raise MicrosoftSSOError(
                "A literal --totp is valid only for offline PhoneAppOTP.",
                step="MFA",
                recovery="Select --mfa-method app or omit --totp.",
            )

        begin_url = mfa_config.get("urlBeginAuth") or "/common/SAS/BeginAuth"
        begin_resp = self._post(
            self._resolve_mfa_url(mfa_snap.url, str(begin_url)),
            json=build_begin_payload(selected, mfa_config),
            headers={"Content-Type": "application/json"},
        )
        try:
            begin_data: dict[str, Any] = begin_resp.json()
        except ValueError as exc:
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

        code_sent_on_begin = selected.auth_method_id == MFA_AUTH_SMS
        self._print_mfa_phase_banner(
            proofs, selected, code_sent_on_begin=code_sent_on_begin
        )

        if defer_mfa_to_pending:
            from lighthouse_cli.config import save_mfa_pending

            save_mfa_pending({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mfa_method": mfa_method,
                "mfa_page_url": mfa_snap.url,
                "mfa_config": {
                    k: mfa_config[k]
                    for k in (
                        "sFT", "sCtx", "canary", "urlEndAuth", "urlPost", "sFTName",
                        "oPerAuthPollingInterval", "sPOST_Username",
                    )
                    if k in mfa_config
                },
                "begin": begin_data,
                "selected_proof": {
                    "auth_method_id": selected.auth_method_id,
                    "display": selected.display,
                    "data": selected.data,
                    "is_default": selected.is_default,
                },
                "cookies": _export_session_cookies(self._session),
            })
            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY:
                raise MfaPendingError(
                    "Authenticator approval requested.",
                    step="MFA",
                    recovery=(
                        "Run: lighthouse auth verify ok  "
                        "(it will display the number and wait for approval)"
                    ),
                )
            if selected.auth_method_id in VOICE_APPROVAL_AUTH_IDS:
                raise MfaPendingError(
                    "Voice approval call requested.",
                    step="MFA",
                    recovery=(
                        "Run: lighthouse auth verify ok, answer the call, and press #."
                    ),
                )
            if selected.auth_method_id == MFA_AUTH_APP_OTP:
                raise MfaPendingError(
                    "Authenticator code required.",
                    step="MFA",
                    recovery="Run: lighthouse auth verify <code>.",
                )
            raise MfaPendingError(
                "Verification code sent.",
                step="MFA",
                recovery="Run: lighthouse auth verify <code>  (use the code from this message)",
            )

        if selected.auth_method_id in CODELESS_APPROVAL_AUTH_IDS:
            totp_code = ""
        else:
            totp_code = self._collect_totp_after_challenge(
                selected,
                totp_code,
                read_totp_after_challenge=read_totp_after_challenge,
                code_sent_on_begin=code_sent_on_begin,
            )
            if not totp_code:
                raise MicrosoftSSOError(
                    "2FA code is required but was empty.",
                    step="MFA",
                    recovery="Provide a code when prompted or use: lighthouse auth verify <code>",
                )

        return self._mfa_finish_after_begin(
            mfa_snap.url,
            mfa_config,
            selected,
            begin_data,
            totp_code or "",
            str(mfa_config.get("sPOST_Username") or ""),
        )
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY and attempt == 0:
                entropy = end_data.get("Entropy")
                if entropy and sys.stdin.isatty():
                    print(
                        f"Approve sign-in in Authenticator (number shown: {entropy}).",
                        flush=True,
                        file=sys.stderr,
                    )
''',
    '''            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY and attempt == 0:
                entropy = end_data.get("Entropy")
                if entropy:
                    print(
                        f"Approve sign-in in Authenticator (number shown: {entropy}).",
                        flush=True,
                        file=sys.stderr,
                    )
''',
)

replace_def(
    "lighthouse_cli/ms_auth.py",
    "_advance_to_saml",
    '''
    def _advance_to_saml(
        self, snapshot: ResponseSnapshot, base_url: str, *, checkpoint_kmsi: bool = True
    ) -> ResponseSnapshot:
        """Advance through bounded reload, KMSI, form, and redirect hops."""
        sso_reloads = 0
        for _ in range(_MAX_POST_MFA_HOPS):
            transition = classify_post_mfa(snapshot, base_url)

            if transition.kind in ("saml", "mfa"):
                return snapshot
            if transition.kind == "stop":
                return snapshot

            if transition.kind == "sso_reload":
                if sso_reloads >= _MAX_SSO_RELOADS:
                    raise MicrosoftSSOError(
                        "Microsoft session reload exceeded the client safety limit.",
                        step="POST credentials",
                        recovery="Retry the login; Microsoft may have changed the sign-in flow.",
                    )
                sso_reloads += 1
                snapshot = self._snapshot(
                    self._post(transition.url, data=transition.data or {})
                )
                base_url = snapshot.url
                continue

            if transition.kind == "redirect":
                snapshot = self._snapshot(self._get(transition.url))
                base_url = snapshot.url
                continue
            if transition.kind == "hiddenform":
                snapshot = self._snapshot(
                    self._post(transition.url, data=transition.data or {})
                )
                base_url = snapshot.url
                continue
            if transition.kind == "kmsi":
                if checkpoint_kmsi:
                    self._checkpoint_mfa_pending(
                        kmsi_checkpoint={"url": snapshot.url, "html": snapshot.html},
                    )
                snapshot = self._snapshot(
                    self._post(transition.url, data=transition.data or {})
                )
                base_url = snapshot.url
                continue
            if transition.kind == "samlrequest":
                snapshot = self._snapshot(self._get(transition.url))
                base_url = snapshot.url
                continue

            raise MicrosoftSSOError(
                "Microsoft sign-in returned an unsupported transition.",
                step="SSO interstitial walk",
            )

        raise MicrosoftSSOError(
            "Microsoft sign-in interstitial chain exceeded the client safety limit.",
            step="SSO interstitial walk",
            recovery="Retry the login; Microsoft may have changed the sign-in flow.",
        )
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''    elif code in (50076, 50072):
        recovery = (
            "Multi-factor authentication is required. "
            "Use --totp flag to provide your 2FA code."
        )
''',
    '''    elif code in (50076, 50072):
        recovery = (
            "Multi-factor authentication is required. Use auth mfa-methods to "
            "discover the registered method, then run auth login."
        )
''',
)

replace_once(
    "lighthouse_cli/ms_auth.py",
    '''    def _step_post_saml(self, saml_response: str, html: str = "") -> None:
''',
    '''    def _has_accepted_d2l_cookie(self) -> bool:
        return any(
            cookie.name.startswith("d2l")
            and cookie_domain_accepted(cookie.domain or "")
            for cookie in self._session.cookies
        )

    def _step_post_saml(self, saml_response: str, html: str = "") -> None:
''',
)
replace_once(
    "lighthouse_cli/ms_auth.py",
    '''        if any(n.startswith("d2l") for n in self._session.cookies.keys()):
            return
''',
    '''        if self._has_accepted_d2l_cookie():
            return
''',
)
replace_once(
    "lighthouse_cli/ms_auth.py",
    '''        if home_resp.status_code < 400 and any(
            n.startswith("d2l") for n in self._session.cookies.keys()
        ):
            return
''',
    '''        if home_resp.status_code < 400 and self._has_accepted_d2l_cookie():
            return
''',
)


# ---------------------------------------------------------------------------
# Cookie merge policy and KDF work-factor validation.
# ---------------------------------------------------------------------------

replace_once(
    "lighthouse_cli/config.py",
    '''# Domain variants accepted when extracting fresh D2L cookies from a login session jar.
COOKIE_EXTRACTION_DOMAINS = ("lighthouse.manipal.edu", ".manipal.edu", "manipal.edu")

''',
    "",
)
replace_once(
    "lighthouse_cli/config.py",
    "    return host_only or domain_scoped\n",
    "    return {**domain_scoped, **host_only}\n",
)

replace_once(
    "lighthouse_cli/credential_store.py",
    '''                raw_iterations = envelope.get("kdf_iterations")
                iterations = (
                    raw_iterations
                    if (
                        isinstance(raw_iterations, int)
                        and not isinstance(raw_iterations, bool)
                        and raw_iterations > 0
                    )
                    else _LEGACY_KDF_ITERATIONS
                )
''',
    '''                if "kdf_iterations" not in envelope:
                    iterations = _LEGACY_KDF_ITERATIONS
                else:
                    raw_iterations = envelope["kdf_iterations"]
                    if (
                        not isinstance(raw_iterations, int)
                        or isinstance(raw_iterations, bool)
                        or raw_iterations not in {
                            _LEGACY_KDF_ITERATIONS,
                            _KDF_ITERATIONS,
                        }
                    ):
                        raise CredentialStoreError(
                            "Sealed data records an unsupported KDF iteration count."
                        )
                    iterations = raw_iterations
''',
)


# ---------------------------------------------------------------------------
# User-facing help and protocol documentation.
# ---------------------------------------------------------------------------

replace_once(
    "lighthouse_cli/cli.py",
    '''    MFA: --mfa-method auto (default), sms, call, app, push, or choose (pick from
    a list). Text codes may arrive via SMS or WhatsApp depending on Microsoft;
    the CLI cannot select the delivery channel. Voice codes (call) are spoken
    during the phone call; push is approved in Microsoft Authenticator.
''',
    '''    MFA: --mfa-method auto (default), sms, call, app, push, or choose (pick from
    a list). Text codes may arrive via SMS or WhatsApp depending on Microsoft;
    the CLI cannot select the delivery channel. Voice calls are approved by
    answering and pressing #. Push approval may require number matching; the
    number is printed to stderr, including under --json.
''',
)
replace_once(
    "lighthouse_cli/cli.py",
    '''    """List the MFA methods registered on the account (no code is sent).

    Runs the SSO flow up to — but not including — the verification step and
    reports the methods Microsoft offers: OneWaySMS (sms), TwoWayVoice*
    (call), PhoneAppOTP (app), PhoneAppNotification (push).
    """
''',
    '''    """List registered MFA methods without triggering a challenge.

    Performs a real sign-in through the post-password stage and may advance
    KMSI/session state. It stops before BeginAuth, so it sends no SMS, places no
    voice call, and triggers no Authenticator notification.
    """
''',
)
replace_once(
    "lighthouse_cli/cli.py",
    '''    ))




# ---------------------------------------------------------------------------
''',
    '''    ))


# ---------------------------------------------------------------------------
''',
)

replace_once(
    "AGENTS.md",
    '''- **MFA semantics (subtle — do not "simplify" away):** SMS/WhatsApp codes are
  **server-sent on `BeginAuth`**, so a literal `--totp <code>` cannot match —
  use the two-step `auth login` → `auth verify` flow. Offline Authenticator
  TOTP (`PhoneAppOTP`) is generated on-device, so a pre-provided `--totp` **is**
  valid for `--mfa-method app`. Resume a pending MFA session only when its saved
  method matches the requested method.
''',
    '''- **MFA semantics (subtle — do not "simplify" away):** SMS/WhatsApp codes are
  server-sent after `BeginAuth`, so a literal `--totp <code>` is invalid for a
  fresh SMS flow; use `auth login` → `auth verify` (or `--totp -`). Offline
  `PhoneAppOTP` is generated on-device, so a pre-provided code is valid only for
  `--mfa-method app`. `PhoneAppNotification` and `TwoWayVoice*` are codeless
  approval methods: push polls EndAuth (printing number matching to stderr), and
  voice is approved by answering and pressing #. Resume a pending session only
  when its saved method matches the request.
''',
)
replace_once(
    "AGENTS.md",
    '''- **P1** — Flag MFA logic that treats a literal `--totp` as usable for the
  server-sent SMS/WhatsApp path, or that conflates SMS with offline `PhoneAppOTP`.
''',
    '''- **P1** — Flag MFA logic that treats a literal `--totp` as usable for a
  fresh SMS challenge, conflates `PhoneAppOTP` with push, or submits a code for
  codeless push/voice approval.
''',
)

replace_once(
    "README.md",
    '''Microsoft SSO login (HTTP + optional Playwright for the username step). For
SMS/WhatsApp and voice-call codes, agents should use **`auth verify`** after
login sends the code — see [docs/auth-microsoft-sso.md](docs/auth-microsoft-sso.md).
Session cookies usually expire after ~5 days; re-run login when `auth status` fails.
''',
    '''Microsoft SSO login (HTTP + optional Playwright for the username step).
SMS/WhatsApp uses **`auth login` → `auth verify <code>`**. Voice and push are
codeless approvals resumed with **`auth verify ok`**; voice is approved by
pressing # and push may display a number-matching value on stderr. See
[docs/auth-microsoft-sso.md](docs/auth-microsoft-sso.md). Session cookies
usually expire after ~5 days; re-run login when `auth status` fails.
''',
)
replace_once(
    "README.md",
    '''3. Session-pull interstitial hop (re-POST echoed params, bounded — Aug 2026 upstream change)
4. `BeginAuth` sends the code (or the approval prompt for `push`); may exit and save `mfa_pending.json`
5. `lighthouse auth verify <code>` → EndAuth, ProcessAuth, KMSI, SAML → D2L cookies
''',
    '''3. Session-pull interstitial hop (same-origin HTTPS re-POST, locally bounded)
4. `BeginAuth` sends SMS or starts codeless push/voice approval; may save `mfa_pending.json`
5. `auth verify <code>` (SMS/app) or `auth verify ok` (push/voice) → EndAuth, ProcessAuth, KMSI, SAML
''',
)
replace_once(
    "README.md",
    '''Discover the MFA methods registered on the account without sending any code.
Runs the SSO flow up to (not including) the verification step and reports
each method's `authMethodId` (OneWaySMS, TwoWayVoice*, PhoneAppOTP,
PhoneAppNotification) with the `--mfa-method` spelling that selects it.

**Human output:**
```
Auth login successful. Cookies stored.
```

**JSON output (`--json`):**
```json
{
  "valid": true,
  "cookies": ["d2lSameSiteCanaryA", "d2lSameSiteCanaryB", "d2lSecureSessionVal", "d2lSessionVal"]
}
```
''',
    '''Performs a real Microsoft sign-in through the post-password stage and may
advance KMSI/session state, but stops before `BeginAuth`; it sends no SMS,
places no call, and triggers no Authenticator notification. Output includes
Microsoft's masked display, `authMethodId`, default status, and the matching
`--mfa-method` spelling.

**JSON output (`--json`):**
```json
{"success": true, "page": "converged", "methods": [
  {"id": "PhoneAppOTP", "method": "app", "display": "Authenticator app", "is_default": true}
]}
```
''',
)

replace_once(
    "docs/auth-microsoft-sso.md",
    '''**Headless Playwright is used only for the username “Next” step** on this tenant. Pure HTTP can post the password, but Microsoft does not set the `esctx-*` cookies the tenant expects until the username step runs in a browser context. Playwright fills `loginfmt`, clicks Next, exports cookies into the `requests` session, then closes. If Playwright is importable but cannot launch (Chromium not installed, sandbox denied, driver mismatch), the client warns on stderr and falls back to the mirrored HTTP sequence instead of failing the login.
''',
    '''**Headless Playwright is preferred only for the username “Next” step** on this tenant. It fills `loginfmt`, clicks Next, exports cookies into the `requests` session, then closes. If Playwright/Chromium cannot start, the client emits a sanitized stderr warning and falls back to the mirrored HTTP sequence. Semantic browser-flow failures (unexpected Microsoft page, selector/evaluation failure) do not silently fall back.
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''| `TwoWayVoiceMobile` / `TwoWayVoiceAlternateMobile` / `TwoWayVoiceOffice` | `call` | Server speaks the code during a phone call |
''',
    '''| `TwoWayVoiceMobile` / `TwoWayVoiceAlternateMobile` / `TwoWayVoiceOffice` | `call` | Codeless — answer and press # |
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''Server-sent methods (`sms`, `call`) always use the two-step flow: the code
arrives only after `BeginAuth`, so a literal `--totp` is discarded. `push` is
codeless: BeginAuth sends the approval prompt and EndAuth polls until you
approve; nothing to type.
''',
    '''SMS uses the two-step flow because its fresh code arrives after `BeginAuth`;
a literal `--totp` is rejected. Voice and push are codeless: `BeginAuth`
starts the approval, and `auth verify ok` polls `EndAuth`. Voice is approved by
pressing #. Push number matching is printed to stderr, including under `--json`.
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''### Voice call — two step, like SMS

```bash
lighthouse auth login --mfa-method call
# Answer the phone; the code is spoken
lighthouse auth verify 123456
```
''',
    '''### Voice call — codeless approval

```bash
lighthouse auth login --mfa-method call
lighthouse auth verify ok
# Answer the call and press # to approve.
```
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''lighthouse auth login --mfa-method push
# Approve on your phone, then:
lighthouse auth verify ok
```

`PhoneAppNotification` never sends an `AdditionalAuthData` code; `verify ok`
is just the mechanical trigger to resume polling `EndAuth` after you approve.
''',
    '''lighthouse auth login --mfa-method push
lighthouse auth verify ok
# The command prints the number-matching value, then waits for approval.
```

`PhoneAppNotification` never sends `AdditionalAuthData`; `verify ok` is only a
mechanical resume token. EndAuth polling is bounded (normally about 15–60s,
depending on Microsoft's interval). A timeout clears the pending checkpoint,
so start a fresh `auth login --mfa-method push` before retrying.
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''params through the same bounded walk used everywhere else
(`_MAX_SSO_RELOADS=2`, matching the page’s declared `slMaxRetry`), and the
flow recorder logs field **names only** — `oPostParams` echoes the password,
''',
    '''params through the same bounded walk used everywhere else. The client uses
a conservative local `_MAX_SSO_RELOADS=2` safety cap; the page's `slMaxRetry`
belongs to its resource loader and is not treated as a session-pull budget.
The target must remain same-origin HTTPS, and the flow recorder logs field
**names only** — `oPostParams` echoes the password,
''',
)
replace_once(
    "docs/auth-microsoft-sso.md",
    '''Runs the flow up to (not including) `BeginAuth` — **no code is sent** — and
lists the methods Microsoft offers for the account, with the `--mfa-method`
spelling that selects each:
''',
    '''Performs a real sign-in through the post-password stage and may advance
KMSI/session state. It stops before `BeginAuth` — no SMS, call, or push challenge
is triggered — and lists the methods with the selecting `--mfa-method` spelling:
''',
)


# ---------------------------------------------------------------------------
# Update assertions that encoded the old behavior.
# ---------------------------------------------------------------------------

replace_def(
    "tests/test_auth_login.py",
    "test_normalize_sms_and_choose_totp_discarded",
    '''
def test_normalize_incompatible_literals_are_rejected() -> None:
    for method in ("sms", "call", "push", "choose"):
        with pytest.raises(ValueError):
            normalize_totp("123456", totp_stdin=False, mfa_method=method)
''',
)
replace_def(
    "tests/test_auth_login.py",
    "test_normalize_discarded_code_not_validated",
    '''
def test_normalize_whitespace_is_rejected_before_transport_validation() -> None:
    with pytest.raises(ValueError, match="2FA code cannot be empty"):
        normalize_totp("  ", totp_stdin=False, mfa_method="sms")
''',
)
replace_def(
    "tests/test_auth_login.py",
    "test_call_discards_literal_totp",
    '''
    def test_call_rejects_literal_totp(self) -> None:
        with pytest.raises(ValueError, match="codeless"):
            normalize_totp("123456", totp_stdin=False, mfa_method="call")
''',
)
replace_def(
    "tests/test_auth_login.py",
    "test_push_accepts_stdin_deferral",
    '''
    def test_push_rejects_stdin_code_deferral(self) -> None:
        with pytest.raises(ValueError, match="codeless"):
            normalize_totp(None, totp_stdin=True, mfa_method="push")
''',
)

replace_def(
    "tests/test_ms_auth.py",
    "test_voice_codes_are_server_sent",
    '''
    def test_voice_is_codeless_approval(self) -> None:
        from lighthouse_cli.ms_errors import (
            CODELESS_APPROVAL_AUTH_IDS,
            SERVER_SENT_CODE_AUTH_IDS,
        )

        assert "TwoWayVoiceMobile" in CODELESS_APPROVAL_AUTH_IDS
        assert "TwoWayVoiceMobile" not in SERVER_SENT_CODE_AUTH_IDS
''',
)
replace_def(
    "tests/test_ms_auth.py",
    "test_end_payload_carries_code_for_voice",
    '''
    def test_end_payload_never_carries_code_for_voice(self) -> None:
        from lighthouse_cli.ms_auth import build_end_payload

        proof = UserProof("TwoWayVoiceOffice", "Call office", "", False)
        payload = build_end_payload(
            proof, {"SessionId": "sid"}, "ignored", end_flow="f", end_ctx="c"
        )
        assert "AdditionalAuthData" not in payload
''',
)

replace_def(
    "tests/test_ms_auth_characterization.py",
    "test_probe_reports_no_mfa_shape_on_stderr",
    '''
    def test_probe_rejects_unrecognized_post_credentials_page(
        self, scripted: ScriptedSession, isolated_config: Path,
    ) -> None:
        scripted.enqueue(
            FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
            FakeResponse(200, html=config_html(), url=MS_SSO_URL),
            FakeResponse(
                200,
                html="<html><head><title>Mystery</title></head><body>huh</body></html>",
                url=CREDS_POST_URL,
            ),
        )
        client = make_client(scripted)
        with pytest.raises(MicrosoftSSOError, match="Could not determine MFA methods"):
            client.probe_mfa_methods(USERNAME, PASSWORD)
''',
)
replace_def(
    "tests/test_ms_auth_characterization.py",
    "test_walk_is_bounded_by_slmaxretry",
    '''
    def test_walk_is_bounded_by_local_reload_limit(
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
        with pytest.raises(MicrosoftSSOError, match="session reload.*safety limit"):
            run_login(scripted)
        reposts = [c for c in scripted.calls if c[0] == "POST"]
        assert len(reposts) == 1 + _MAX_SSO_RELOADS
''',
)

replace_once(
    "tests/test_session_identity.py",
    '''config.py is the single owner of D2L session identity: COOKIE_NAMES,
BASE_URL, COOKIE_SETTING_HOST (exact host cookies are written/filtered for),
COOKIE_EXTRACTION_DOMAINS (variants accepted when validating fresh logins),
and missing_cookie_names().
''',
    '''config.py is the single owner of D2L session identity: COOKIE_NAMES,
BASE_URL, COOKIE_SETTING_HOST, cookie_domain_accepted(), and
missing_cookie_names().
''',
)
replace_once(
    "tests/test_session_identity.py",
    '''    COOKIE_EXTRACTION_DOMAINS,
''',
    "",
)
replace_def(
    "tests/test_session_identity.py",
    "test_extraction_domains_exact_variant_set",
    '''
    def test_domain_predicate_accepts_tenant_boundaries(self) -> None:
        from lighthouse_cli.config import cookie_domain_accepted

        assert cookie_domain_accepted("lighthouse.manipal.edu")
        assert cookie_domain_accepted(".manipal.edu")
        assert cookie_domain_accepted("sub.manipal.edu")
        assert not cookie_domain_accepted("manipal.edu.evil.com")
''',
)
replace_once(
    "tests/test_session_identity.py",
    '''    @pytest.mark.parametrize("domain", COOKIE_EXTRACTION_DOMAINS)
''',
    '''    @pytest.mark.parametrize(
        "domain", ("lighthouse.manipal.edu", ".manipal.edu", "manipal.edu")
    )
''',
)
replace_def(
    "tests/test_session_identity.py",
    "test_ensure_config_dir_created_restrictive_under_permissive_umask",
    '''
def test_ensure_config_dir_created_restrictive_under_permissive_umask(
    tmp_path, monkeypatch
):
    """The mkdir mode itself is restrictive even when chmod is unavailable."""
    import os
    from pathlib import Path as _P

    import lighthouse_cli.config as cfg

    target = tmp_path / "cfg-mode"
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(target))
    monkeypatch.setattr(
        _P,
        "chmod",
        lambda self, mode: (_ for _ in ()).throw(OSError("unsupported")),
    )
    old_umask = os.umask(0o022)
    try:
        out = cfg.ensure_config_dir()
    finally:
        os.umask(old_umask)
    assert out == target and out.is_dir()
    assert (out.stat().st_mode & 0o777) == 0o700
''',
)


# ---------------------------------------------------------------------------
# Focused regressions for the new behavior.
# ---------------------------------------------------------------------------

append_once(
    "tests/test_auth_login.py",
    "test_explicit_sms_literal_fails_before_sso",
    '''
def test_explicit_sms_literal_fails_before_sso(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    with patch.object(auth_mod, "MicrosoftSSOClient") as sso_cls:
        result = _invoke_login(
            cli_runner,
            ["--mfa-method", "sms", "--totp", "123456", "--json"],
        )
    assert result.exit_code == 2
    assert "fresh code after BeginAuth" in json.loads(result.stdout)["error"]
    sso_cls.assert_not_called()


def test_pending_load_oserror_degrades_to_fresh_login(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    with patch.object(auth_mod, "load_mfa_pending", side_effect=PermissionError("denied")):
        with _mock_sso() as (sso, _client):
            result = _invoke_login(
                cli_runner,
                ["--mfa-method", "app", "--totp", "123456", "--json"],
            )
    assert result.exit_code == 0
    assert sso.login.called
    assert "PermissionError" in result.stderr
    assert "denied" not in result.stderr


def test_mfa_methods_json_includes_cli_selectors(
    cli_runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "secret")
    with patch.object(
        auth_mod.MicrosoftSSOClient,
        "probe_mfa_methods",
        MagicMock(return_value=_probe_result()),
    ):
        result = cli_runner.invoke(
            cli, ["auth", "mfa-methods", "--json"], catch_exceptions=False
        )
    payload = json.loads(result.stdout)
    assert [m["method"] for m in payload["methods"]] == ["sms", "call", "app"]
''',
)

append_once(
    "tests/test_ms_auth.py",
    "test_app_selector_never_falls_through_to_push",
    '''
def test_app_selector_never_falls_through_to_push() -> None:
    proofs = [UserProof("PhoneAppNotification", "Approve", "", True)]
    with pytest.raises(MicrosoftSSOError, match="not available"):
        _select_user_proof(proofs, MFA_METHOD_APP)


def test_fresh_auto_literal_rejected_before_begin_auth() -> None:
    client = MicrosoftSSOClient()
    snap = ResponseSnapshot(
        url="https://login.microsoftonline.com/common/SAS/ProcessAuth",
        status_code=200,
        location="",
        html="ConvergedTFA",
    )
    config = {
        "sFT": "flow",
        "sCtx": "ctx",
        "urlBeginAuth": "/common/SAS/BeginAuth",
    }
    proofs = [UserProof("OneWaySMS", "Text", "***1234", True)]
    try:
        with patch.object(client, "_post") as post:
            with pytest.raises(MicrosoftSSOError, match="fresh SMS challenge"):
                client._step_handle_mfa_converged(
                    snap,
                    config,
                    proofs,
                    "123456",
                    mfa_method="auto",
                )
            post.assert_not_called()
    finally:
        client.close()
''',
)

append_once(
    "tests/test_ms_auth_characterization.py",
    "test_sso_reload_rejects_cross_origin_target",
    '''
def test_sso_reload_rejects_cross_origin_target() -> None:
    from lighthouse_cli.ms_auth import ResponseSnapshot, sso_reload_transition

    snap = ResponseSnapshot(
        url=CREDS_POST_URL,
        status_code=200,
        location="",
        html=sso_reload_html(
            url_post="https://attacker.example/login?sso_reload=True"
        ),
    )
    with pytest.raises(MicrosoftSSOError, match="same-origin HTTPS"):
        sso_reload_transition(snap, CREDS_POST_URL)


def test_sso_reload_rejects_non_scalar_parameter() -> None:
    from lighthouse_cli.ms_auth import ResponseSnapshot, sso_reload_transition

    snap = ResponseSnapshot(
        url=CREDS_POST_URL,
        status_code=200,
        location="",
        html=sso_reload_html(o_post_params={"passwd": ["nested"]}),
    )
    with pytest.raises(MicrosoftSSOError, match="non-scalar"):
        sso_reload_transition(snap, CREDS_POST_URL)


def test_probe_reload_exhaustion_is_not_reported_as_no_mfa(
    scripted: ScriptedSession, isolated_config: Path,
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
    with pytest.raises(MicrosoftSSOError, match="session reload.*safety limit"):
        client.probe_mfa_methods(USERNAME, PASSWORD)


def test_voice_defer_and_verify_is_codeless(
    scripted: ScriptedSession, isolated_config: Path,
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
    with pytest.raises(MfaPendingError, match="Voice approval"):
        run_login(scripted, mfa_method="call", defer_mfa_to_pending=True)

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


def test_push_number_matching_visible_on_non_tty_stderr(
    scripted: ScriptedSession,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    scripted.enqueue(
        FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_SSO_URL}),
        FakeResponse(200, html=config_html(), url=MS_SSO_URL),
        FakeResponse(
            200,
            html=mfa_html(auth_method_id="PhoneAppNotification", display="Approve"),
            url=MFA_PAGE_URL,
        ),
        begin_success(),
    )
    with pytest.raises(MfaPendingError, match="Authenticator approval"):
        run_login(scripted, mfa_method="push", defer_mfa_to_pending=True)

    scripted.enqueue(
        FakeResponse(json_data={"Retry": True, "Entropy": "73"}),
        end_success(),
        FakeResponse(200, html=SAML_HTML, url=PROCESS_URL),
        FakeResponse(200, html="<html>D2L home</html>", url=f"{BASE}/d2l/home"),
    )
    set_d2l_cookies(scripted)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("lighthouse_cli.ms_auth.time.sleep", lambda _: None)
    client = make_client(scripted)
    try:
        client.complete_mfa_pending("ok")
    finally:
        client.close()
    captured = capsys.readouterr()
    assert "number shown: 73" in captured.err
    assert captured.out == ""


def test_semantic_playwright_failure_does_not_fall_back(
    scripted: ScriptedSession,
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import ModuleType
    from unittest.mock import MagicMock

    fake_api = ModuleType("playwright.sync_api")
    fake_api.sync_playwright = MagicMock()  # type: ignore[attr-defined]
    fake_root = ModuleType("playwright")
    fake_root.sync_api = fake_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_root)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    monkeypatch.setattr(
        MicrosoftSSOClient,
        "_bootstrap_username_via_playwright",
        lambda self, config, username: (_ for _ in ()).throw(
            MicrosoftSSOError("semantic page failure")
        ),
    )
    fallback = MagicMock()
    monkeypatch.setattr(MicrosoftSSOClient, "_step_prepare_username_http", fallback)

    client = make_client(scripted)
    with pytest.raises(MicrosoftSSOError, match="semantic page failure"):
        client._step_prepare_username(
            {"urlGetCredentialType": "/gct"}, USERNAME
        )
    fallback.assert_not_called()
''',
)

append_once(
    "tests/test_session_identity.py",
    "test_mixed_scope_cookie_names_are_merged",
    '''
def test_mixed_scope_cookie_names_are_merged() -> None:
    from lighthouse_cli.config import d2l_cookies_from_entries

    entries = [
        {"name": "d2lSecureSessionVal", "value": "domain-sec", "domain": ".manipal.edu"},
        {"name": "d2lSessionVal", "value": "domain-session", "domain": ".manipal.edu"},
        {"name": "d2lSecureSessionVal", "value": "host-sec", "domain": "lighthouse.manipal.edu"},
        {"name": "d2lSameSiteCanaryA", "value": "host-a", "domain": "lighthouse.manipal.edu"},
    ]
    assert d2l_cookies_from_entries(entries) == {
        "d2lSecureSessionVal": "host-sec",
        "d2lSessionVal": "domain-session",
        "d2lSameSiteCanaryA": "host-a",
    }
''',
)

append_once(
    "tests/test_credential_store.py",
    "test_invalid_kdf_iteration_header_is_rejected_before_derivation",
    '''
@pytest.mark.parametrize("value", [True, 0, "600000", 600_001, 10**12])
def test_invalid_kdf_iteration_header_is_rejected_before_derivation(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    doc = _sealed_doc(config_dir, monkeypatch)
    doc["kdf_iterations"] = value
    (config_dir / "credentials.json").write_text(json.dumps(doc))
    with pytest.raises(CredentialStoreError, match="unsupported KDF iteration"):
        CredentialStore().load()
''',
)

print("PR 12 follow-up patch applied")
