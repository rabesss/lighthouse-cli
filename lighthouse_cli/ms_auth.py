"""Microsoft Azure AD SSO client: pure transitions over a thin HTTP driver.

The SSO flow is modelled as a state machine:

- **Transition input** is an immutable :class:`ResponseSnapshot` (url, status,
  ``Location`` header, html text) — never a live ``requests.Response``.
- **Transitions are pure functions** (:func:`classify_post_mfa`,
  :func:`is_mfa_page`, :func:`is_error_page`, the ``build_*`` payload
  builders): snapshot in, decision out, no I/O.
- The **driver** (:class:`MicrosoftSSOClient`) owns the
  ``requests.Session``, executes transitions, and checkpoints resumable MFA
  state through the encrypted store in ``config``.

Flow:
1. GET lighthouse.manipal.edu/d2l/lp/auth/saml/login -> 302 to Microsoft
2. GET Microsoft login page -> parse ``$Config`` JSON for flow tokens
3. POST credentials to ``urlPost`` -> response may be MFA page or SAML
4. Handle MFA (ConvergedTFA page) -> BeginAuth/EndAuth/ProcessAuth
5. Extract SAMLResponse from HTML form
6. POST SAMLResponse to D2L ACS -> capture d2l* session cookies

The username step uses a headless Chromium bootstrap (Playwright) when
available, because Microsoft binds ``esctx`` session cookies to in-page
JavaScript state that pure HTTP cannot reproduce for this tenant's SAML login.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from lighthouse_cli.config import (
    BASE_URL,
    COOKIE_SETTING_HOST,
    cookie_domain_accepted,
    missing_cookie_names,
)
from lighthouse_cli.credential_store import CredentialStoreError

# ---------------------------------------------------------------------------
# Re-exports from sub-modules (preserve public API)
# ---------------------------------------------------------------------------
from lighthouse_cli.ms_errors import (
    CODE_SUBMITTING_AUTH_IDS,
    CODELESS_APPROVAL_AUTH_IDS,
    LOGIN_PATH,
    MFA_AUTH_APP_NOTIFY,
    MFA_AUTH_APP_OTP,
    MFA_AUTH_SMS,
    MFA_METHOD_APP as MFA_METHOD_APP,
    MFA_METHOD_AUTH_IDS as MFA_METHOD_AUTH_IDS,
    MFA_METHOD_AUTO,
    MFA_METHOD_CALL as MFA_METHOD_CALL,
    MFA_METHOD_CHOOSE as MFA_METHOD_CHOOSE,
    MFA_METHOD_INSTRUCTIONS,
    MFA_METHOD_PUSH as MFA_METHOD_PUSH,
    MFA_METHOD_SMS as MFA_METHOD_SMS,
    MS_ERROR_CODES,
    SERVER_SENT_CODE_AUTH_IDS,
    VALID_MFA_METHODS,
    MfaPendingError,
    MicrosoftSSOError,
    PlaywrightUnavailableError,
    safe_diagnostic_text,
    safe_upstream_text,
)
from lighthouse_cli.ms_mfa import (
    MfaProbeResult,
    UserProof,
    format_user_proof,
    safe_proof_destination,
    _parse_user_proofs,
    _prompt_user_proof_choice as _prompt_user_proof_choice,
    _select_user_proof,
)
from lighthouse_cli.ms_parse import (
    _extract_balanced_json_object as _extract_balanced_json_object,
    _extract_config_json,
    _extract_error_code_and_msg,
)
from lighthouse_cli.ms_session import (
    _absolute_url,  # noqa: F401 - preserved public re-export
    _export_session_cookies,
    _import_session_cookies,
    _prune_stale_esctx_cookies,
    _safe_absolute_url,
    _tenant_id_from_ms_url,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Exact origins used by the Microsoft flow.  Do not turn these into substring
# checks: the values come from untrusted redirects and embedded page config.
_MICROSOFT_ALLOWED_HOSTS = frozenset({
    "login.microsoftonline.com",
    "login.live.com",
    "autologon.microsoftazuread-sso.com",
})
_D2L_ALLOWED_HOSTS = frozenset({COOKIE_SETTING_HOST})
_FLOW_ALLOWED_HOSTS = _MICROSOFT_ALLOWED_HOSTS | _D2L_ALLOWED_HOSTS
_SAFE_FIELD_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


def _trusted_url(
    base_url: str,
    candidate: str,
    allowed_hosts: frozenset[str],
    *,
    step: str,
    label: str,
) -> str:
    """Resolve an upstream URL without ever echoing it in an error."""
    try:
        return _safe_absolute_url(base_url, candidate, allowed_hosts)
    except (TypeError, ValueError):
        raise MicrosoftSSOError(
            f"Microsoft returned an unsafe {label} endpoint.",
            step=step,
            recovery="Retry the sign-in; the upstream endpoint was not trusted.",
        ) from None


def _safe_flow_location(url: object) -> str:
    """Render only a bounded hostname/path for the optional flow log."""
    if not isinstance(url, str) or not url:
        return "(no url)"
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return "(no url)"
    if not hostname or any(not char.isprintable() for char in hostname):
        return "(no url)"
    # ``hostname`` deliberately discards userinfo.  ``parsed.path`` already
    # excludes literal query strings and fragments, but an upstream can encode
    # those delimiters (and credential keys) one or more times.  Decode only a
    # bounded number of rounds, then cut at the first query-like or sensitive
    # marker before stripping controls and bounding the result.
    path = parsed.path[:1024]
    for _ in range(4):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if any(not char.isprintable() for char in path):
        path = ""
    else:
        delimiter = re.search(r"[?#]", path)
        if delimiter:
            path = path[:delimiter.start()]
        marker = re.search(
            r"(?i)(?<![a-z0-9])(?:password|passwd|passphrase|pass|otp|totp|"
            r"token|canary|ctx|flow[\s_-]*token|cookie(?:value)?|"
            r"session(?:val(?:ue)?|id)?|access[\s_-]*token|client[\s_-]*secret|"
            r"api[\s_-]*key|bearer|saml[\s_-]*(?:response|request)|"
            r"authorization)(?![a-z0-9])\s*(?:[:=/?]|\s+)",
            path,
        )
        if marker:
            path = path[:marker.start()].rstrip(";&,/")
        path = "".join(char for char in path if char.isprintable())[:256]
        if safe_diagnostic_text(path, fallback="") != path:
            path = ""
    return f"{hostname.lower()}{path}"


def _safe_flow_field_names(field_names: object) -> list[str]:
    """Keep only bounded HTML field identifiers, never key/value material."""
    if not isinstance(field_names, list):
        return ["(redacted)"]
    safe: list[str] = []
    redacted = False
    for field_name in field_names:
        if isinstance(field_name, str) and field_name in {"(redacted)", "(unparseable)"}:
            safe.append(field_name)
            continue
        if not isinstance(field_name, str) or not _SAFE_FIELD_NAME_RE.fullmatch(field_name):
            redacted = True
            continue
        if any(not char.isprintable() for char in field_name):
            redacted = True
            continue
        safe.append(field_name)
    if redacted:
        safe.append("(redacted)")
    return safe

_MAX_POST_MFA_HOPS = 12
_MAX_ENDAUTH_POLLS = 30
# Never let an upstream-provided interval make a single MFA attempt sleep
# indefinitely. The retry count above still bounds the total polling window.
_MAX_ENDAUTH_POLL_SECONDS = 30.0
# A finite wall-clock budget protects against an upstream that keeps returning
# ``Retry`` without silently shortening the advertised 30-poll window.
_MAX_ENDAUTH_TOTAL_SECONDS = _MAX_ENDAUTH_POLLS * _MAX_ENDAUTH_POLL_SECONDS
_SAFE_MFA_ENTROPY_RE = re.compile(r"[0-9]{1,3}\Z")
_INVALID_ENTROPY_SENTINEL = "__invalid_entropy__"
# Recovery text for errors that do not know which proof Microsoft selected.
_MFA_RECOVERY_HINT = (
    "Complete MFA with a supported method: run --mfa-method choose to select "
    "SMS, Authenticator app, voice, or push, then use auth verify <code> for a "
    "code or auth verify ok for an approval."
)
# Conservative client-side safety budget. The page's ``slMaxRetry`` belongs
# to Microsoft's script loader, not to the session-pull form submission.
_MAX_SSO_RELOADS = 2


def _safe_mfa_entropy(value: object) -> str | None:
    """Return a short number-match value, never arbitrary upstream text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value
    else:
        return None
    return candidate if _SAFE_MFA_ENTROPY_RE.fullmatch(candidate) else None


# ---------------------------------------------------------------------------
# Immutable transition input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseSnapshot:
    """Immutable view of one HTTP response for transition functions."""

    url: str
    status_code: int
    location: str
    html: str

    @classmethod
    def from_response(cls, resp: requests.Response) -> ResponseSnapshot:
        return cls(
            url=str(getattr(resp, "url", "") or ""),
            status_code=int(getattr(resp, "status_code", 0)),
            location=str(resp.headers.get("Location", "") or ""),
            html=resp.text,
        )


class Transition(NamedTuple):
    """A pure routing decision for the interstitial walk.

    ``kind`` is one of: ``saml`` (done), ``mfa`` (terminal — an MFA
    verification page the caller must handle), ``sso_reload`` (re-POST the
    echoed credential params), ``redirect``, ``hiddenform``, ``kmsi``,
    ``samlrequest``, ``stop``.
    """

    kind: str
    url: str = ""
    data: dict[str, str] | None = None
    saml_response: str | None = None


# ---------------------------------------------------------------------------
# Pure transition functions (no I/O — trivially snapshot-testable)
# ---------------------------------------------------------------------------


def extract_saml_response(html: str) -> str | None:
    """Extract the SAMLResponse value from an HTML form, or None."""
    soup = BeautifulSoup(html, "html.parser")
    for inp in soup.find_all("input", attrs={"name": "SAMLResponse"}):
        val = inp.get("value")
        if val and isinstance(val, str):
            return val

    m = re.search(r'name="SAMLResponse"\s+value="([^"]*)"', html)
    if m:
        return m.group(1)

    if "SAMLResponse" in html or "SAML" in html:
        m = re.search(r'SAMLResponse[=:]?\s*["\']?\s*([A-Za-z0-9+/=]{100,})["\']?', html)
        if m:
            return m.group(1)

    return None


def is_mfa_page(html: str) -> bool:
    """True when the page is a Microsoft MFA verification page."""
    if "ConvergedTFA" in html:
        return True
    mfa_config = _extract_config_json(html)
    if mfa_config and mfa_config.get("arrUserProofs"):
        return True
    # Legacy form heuristics. The OTC match must be word-bounded: a bare
    # substring trips on $Config flags like "fAvoidNewOTCGenerationWhen
    # AlreadySent" that appear on the ordinary ConvergedSignIn page the
    # sso_reload walk lands on after a wrong password (live regression:
    # that page was misrouted to MFA handling instead of error handling).
    text_lower = html.lower()
    otc_in_text = bool(re.search(r"\botc\b", text_lower))
    verification_in_text = "verification" in text_lower
    authenticator_in_text = "authenticator" in text_lower
    return (
        (otc_in_text and (verification_in_text or authenticator_in_text))
        or 'name="otc"' in html
        or 'id="idDiv_SAOTCC_Description"' in html
        or "Enter code" in html
    )


def is_error_page(snapshot: ResponseSnapshot) -> bool:
    """True when the page is a Microsoft error page (and not an MFA page)."""
    if is_mfa_page(snapshot.html):
        return False
    text = snapshot.html.lower()
    cfg = _extract_config_json(snapshot.html) or {}
    if cfg.get("pgid") == "ConvergedError":
        return True
    err_code = cfg.get("sErrorCode") or cfg.get("iErrorCode")
    if err_code and str(err_code) not in ("50058",):
        return True
    if extract_saml_response(snapshot.html):
        return False
    return (
        snapshot.status_code >= 400
        or "servererror" in text
        or "serrtxt" in text
        or "password is incorrect" in text
        or "account does not exist" in text
    )


def describe_page_shape(snapshot: ResponseSnapshot) -> str:
    """One-line, secret-free summary of an unrecognized Microsoft page.

    Diagnostics only: host+path (query stripped — it can carry ctx/flowToken
    material), $Config pgid, and structural marker booleans.  Never includes
    HTML fragments or token values.
    """
    url = snapshot.url or ""
    location = _safe_flow_location(url)
    cfg = _extract_config_json(snapshot.html) or {}
    pgid = safe_diagnostic_text(cfg.get("pgid"), fallback="-")
    html = snapshot.html
    markers = {
        "arrUserProofs": bool(cfg.get("arrUserProofs")),
        "otc-input": 'name="otc"' in html,
        "ProcessAuth-form": "/SAS/ProcessAuth" in html,
        "KmsiInterrupt": "KmsiInterrupt" in html,
        "ConvergedTFA": "ConvergedTFA" in html,
        "SAMLResponse": "SAMLResponse" in html,
        "sFT-present": bool(cfg.get("sFT")),
        "urlPost": bool(cfg.get("urlPost")),
        "oPostParams": bool(cfg.get("oPostParams")),
        "sso_reload": "sso_reload" in str(cfg.get("urlPost") or "").lower(),
    }
    flags = " ".join(f"{k}={int(v)}" for k, v in markers.items())
    title_match = re.search(r"<title[^>]*>([^<]{0,80})", html)
    title = (
        safe_diagnostic_text(title_match.group(1).strip(), fallback="-")
        if title_match
        else "-"
    )
    return (
        f"page: status={snapshot.status_code} url={location} pgid={pgid} "
        f"title={title!r} {flags}"
    )


_PAGE_SHAPE_RE = re.compile(
    r"^page: status=\d{1,4} "
    r"url=(?:\(no url\)|[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._~:/-]*)?) "
    r"pgid=[A-Za-z0-9_.-]{1,80} title='[^'\r\n]{0,80}' "
    r"arrUserProofs=[01] otc-input=[01] ProcessAuth-form=[01] "
    r"KmsiInterrupt=[01] ConvergedTFA=[01] SAMLResponse=[01] "
    r"sFT-present=[01] urlPost=[01] oPostParams=[01] sso_reload=[01]$"
)


def _safe_page_shape_message(value: object) -> str | None:
    """Keep only the exact structural diagnostic generated by this module."""
    if not isinstance(value, str) or not value.startswith("Unexpected response — "):
        return None
    summary = value.removeprefix("Unexpected response — ")
    if not _PAGE_SHAPE_RE.fullmatch(summary):
        return None
    if safe_diagnostic_text(summary, fallback="") != summary:
        return None
    return value


def build_sso_error(code: int | None, msg: str | None, step: str) -> MicrosoftSSOError:
    """Build a descriptive MicrosoftSSOError from a Microsoft error code."""
    # ``describe_page_shape`` is a deliberately sanitized structural
    # diagnostic used for an unrecognized response.  Preserve only the exact
    # shape emitted by this module; all other upstream text is filtered.
    page_shape = _safe_page_shape_message(msg)
    if code is None and page_shape is not None:
        fallback_description = page_shape
    else:
        fallback_description = safe_upstream_text(msg, fallback="Unknown error")
    description = MS_ERROR_CODES.get(
        code or 0,
        fallback_description,
    )
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
        recovery = f"Multi-factor authentication is required. {_MFA_RECOVERY_HINT}"

    return MicrosoftSSOError(
        f"Authentication failed: {description}",
        step=step,
        recovery=recovery,
    )


def build_password_form_data(
    config: dict[str, Any], username: str, password: str
) -> dict[str, str]:
    """Form body for the Microsoft password POST (pure)."""
    i19 = str(config.get("i19") or "3120")
    data: dict[str, str] = {
        "i13": "0",
        "login": username,
        "loginfmt": username,
        "type": "11",
        "LoginOptions": "3",
        "passwd": password,
        "ps": "2",
        "canary": str(config.get("canary") or ""),
        "ctx": str(config["sCtx"]),
        "flowToken": str(config["sFT"]),
        "NewUser": "1",
        "fspost": "0",
        "i19": i19,
        "i21": "0",
        "CookieDisclosure": "0",
        "IsFidoSupported": "1",
        "isSignupPost": "0",
    }
    if config.get("sessionId"):
        data["hpgrequestid"] = str(config["sessionId"])
    return data


def build_begin_payload(proof: UserProof, mfa_config: dict[str, Any]) -> dict[str, Any]:
    """SAS BeginAuth request body (pure)."""
    return {
        "AuthMethodId": proof.auth_method_id,
        "Method": "BeginAuth",
        "ctx": str(mfa_config.get("sCtx") or ""),
        "flowToken": str(mfa_config.get("sFT") or ""),
    }


def build_end_payload(
    proof: UserProof,
    begin_data: dict[str, Any],
    totp_code: str,
    *,
    end_flow: str,
    end_ctx: str,
) -> dict[str, Any]:
    """SAS EndAuth polling request body (pure)."""
    payload: dict[str, Any] = {
        "AuthMethodId": proof.auth_method_id,
        "Method": "EndAuth",
        "ctx": end_ctx,
        "flowToken": end_flow,
        "SessionId": str(begin_data.get("SessionId") or ""),
    }
    if proof.auth_method_id in CODE_SUBMITTING_AUTH_IDS and totp_code:
        payload["AdditionalAuthData"] = totp_code
    return payload


def build_process_payload(
    mfa_config: dict[str, Any],
    flow_token: str,
    ctx: str,
    login_name: str = "",
) -> dict[str, str]:
    """SAS ProcessAuth form body (pure). EndAuth already consumed the OTP."""
    sft_name = str(mfa_config.get("sFTName") or "flowToken")
    data: dict[str, str] = {
        sft_name: flow_token,
        "request": ctx,
    }
    if login_name:
        data["login"] = login_name
    elif mfa_config.get("sPOST_Username"):
        data["login"] = str(mfa_config["sPOST_Username"])
    canary = mfa_config.get("canary")
    if canary:
        data["canary"] = str(canary)
    return data


def is_hiddenform_page(html: str) -> bool:
    """Microsoft auto-submit interstitial (common after ProcessAuth)."""
    if 'name="hiddenform"' in html or "name='hiddenform'" in html:
        soup = BeautifulSoup(html, "html.parser")
        if soup.find("form", attrs={"name": "hiddenform"}):
            return True
    return html.lstrip().startswith("Working...") and "hiddenform" in html


def hiddenform_transition(snapshot: ResponseSnapshot, base_url: str) -> Transition:
    """POST target + fields for the auto-submit hiddenform interstitial (pure)."""
    soup = BeautifulSoup(snapshot.html, "html.parser")
    form = soup.find("form", attrs={"name": "hiddenform"}) or soup.find("form")
    if not form:
        raise MicrosoftSSOError(
            "Expected hiddenform after MFA but none was found.",
            step="MFA",
        )
    action = form.get("action")
    post_url = (
        _trusted_url(
            base_url,
            str(action),
            _MICROSOFT_ALLOWED_HOSTS,
            step="MFA interstitial",
            label="hidden form",
        )
        if action
        else _trusted_url(
            base_url,
            base_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="MFA interstitial",
            label="hidden form",
        )
    )
    form_data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            form_data[str(name)] = str(inp.get("value") or "")
    return Transition(kind="hiddenform", url=post_url, data=form_data)


def find_saml_request_url(html: str) -> str | None:
    """Locate a JS ``window.location`` URL that carries SAMLRequest (pure)."""
    for fragment in html.split(";"):
        if "SAMLRequest" not in fragment:
            continue
        m = re.search(r"(https://[^\s'\"]+SAMLRequest[^\s'\"]*)", fragment)
        if m:
            return m.group(1)
    return None


def kmsi_page_detected(snapshot: ResponseSnapshot) -> bool:
    """True when the snapshot is a KMSI/CMSI 'Stay signed in' interrupt."""
    page_cfg = _extract_config_json(snapshot.html) or {}
    pgid = str(page_cfg.get("pgid") or "")
    return pgid in ("CmsiInterrupt", "KmsiInterrupt") or (
        snapshot.status_code == 200
        and ("Kmsi" in snapshot.html or "Stay signed in" in snapshot.html)
    )


def kmsi_transition(snapshot: ResponseSnapshot, base_url: str) -> Transition:
    """Submit target + fields for a KMSI/CMSI interrupt (pure).

    Prefers the ``$Config`` SAS payload; falls back to scraping the page's
    first form when flow tokens are absent.
    """
    page_cfg = _extract_config_json(snapshot.html) or {}
    sft_name = str(page_cfg.get("sFTName") or "flowToken")
    kmsi_data: dict[str, str] = {
        sft_name: str(page_cfg.get("sFT") or ""),
        "ctx": str(page_cfg.get("sCtx") or ""),
        "LoginOptions": "1",
    }
    canary = page_cfg.get("canary")
    if canary:
        kmsi_data["canary"] = str(canary)
    session_id = page_cfg.get("sessionId") or page_cfg.get("correlationId")
    if session_id:
        kmsi_data["hpgrequestid"] = str(session_id)
    username = page_cfg.get("sPOST_Username")
    if username:
        kmsi_data["login"] = str(username)
        kmsi_data["loginfmt"] = str(username)

    url_post = page_cfg.get("urlPost")
    post_url = (
        _trusted_url(
            base_url,
            str(url_post),
            _MICROSOFT_ALLOWED_HOSTS,
            step="MFA interstitial",
            label="KMSI",
        )
        if url_post
        else _trusted_url(
            base_url,
            base_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="MFA interstitial",
            label="KMSI",
        )
    )
    if not kmsi_data.get(sft_name) or not kmsi_data.get("ctx"):
        soup = BeautifulSoup(snapshot.html, "html.parser")
        form = soup.find("form")
        if form:
            action = form.get("action")
            post_url = (
                _trusted_url(
                    base_url,
                    str(action),
                    _MICROSOFT_ALLOWED_HOSTS,
                    step="MFA interstitial",
                    label="KMSI",
                )
                if action
                else _trusted_url(
                    base_url,
                    base_url,
                    _MICROSOFT_ALLOWED_HOSTS,
                    step="MFA interstitial",
                    label="KMSI",
                )
            )
            kmsi_data = {}
            for hidden in form.find_all("input"):
                name = hidden.get("name")
                if name:
                    kmsi_data[str(name)] = str(hidden.get("value") or "")
            kmsi_data.setdefault("LoginOptions", "1")
    return Transition(kind="kmsi", url=post_url, data=kmsi_data)


def is_sso_reload_page(snapshot: ResponseSnapshot) -> bool:
    """True when the snapshot is Microsoft's session-pull reload interstitial.

    Observed in the wild since Aug 2026 after the password POST: an HTTP 200
    "Redirecting" page with **no forms** whose ``$Config`` carries
    ``iSessionPullType``/``slMaxRetry`` and, critically, ``urlPost`` (with
    ``sso_reload=True`` in its query) plus ``oPostParams`` — the entire
    credential form echoed back. Browsers re-POST those params via JS; a
    pure-HTTP client must perform the same hop to reach the real page.
    """
    cfg = _extract_config_json(snapshot.html) or {}
    url_post = str(cfg.get("urlPost") or "")
    params = cfg.get("oPostParams")
    try:
        query = parse_qs(urlparse(url_post).query, keep_blank_values=True)
    except (TypeError, ValueError):
        return False
    reload_values = [
        value
        for key, values in query.items()
        if key.lower() == "sso_reload"
        for value in values
    ]
    return (
        snapshot.status_code == 200
        and any(value.lower() == "true" for value in reload_values)
        and isinstance(params, dict)
        and bool(params)
    )


def sso_reload_transition(snapshot: ResponseSnapshot, base_url: str) -> Transition:
    """Re-POST target + echoed fields for the sso_reload interstitial (pure).

    ``oPostParams`` echoes the credential form — including the password — so
    the payload flows straight back to Microsoft over the existing session
    and is never logged, recorded, or embedded in errors (the flow recorder
    stores field *names* only).
    """
    cfg = _extract_config_json(snapshot.html) or {}
    url_post = str(cfg.get("urlPost") or "")
    params = cfg.get("oPostParams")
    try:
        target = _trusted_url(
            base_url,
            url_post,
            _MICROSOFT_ALLOWED_HOSTS,
            step="POST credentials",
            label="session-pull",
        )
    except MicrosoftSSOError:
        # Preserve the stable characterization without echoing the rejected
        # upstream URL (which may contain flow or credential material).
        raise MicrosoftSSOError(
            "Microsoft session-pull requested an unsafe re-POST target.",
            step="POST credentials",
            recovery="Retry the login; if it persists, Microsoft changed the sign-in flow.",
        ) from None
    source_url = snapshot.url or base_url

    try:
        trusted_source = _trusted_url(
            base_url,
            source_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="POST credentials",
            label="session-pull source",
        )
    except MicrosoftSSOError:
        raise MicrosoftSSOError(
            "Microsoft session-pull requested an unsafe re-POST target.",
            step="POST credentials",
            recovery="Retry the login; if it persists, Microsoft changed the sign-in flow.",
        ) from None
    source_parsed = urlparse(trusted_source)
    target_parsed = urlparse(target)
    source_origin = (source_parsed.hostname or "").lower(), source_parsed.port or 443
    target_origin = (target_parsed.hostname or "").lower(), target_parsed.port or 443
    if target_origin != source_origin:
        raise MicrosoftSSOError(
            "Microsoft session-pull requested an unsafe re-POST target.",
            step="POST credentials",
            recovery="Retry the login; if it persists, Microsoft changed the sign-in flow.",
        )
    if not isinstance(params, dict):
        raise MicrosoftSSOError(
            "Microsoft session-pull parameters were missing.",
            step="POST credentials",
        )
    form_data: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            raise MicrosoftSSOError(
                "Microsoft session-pull parameters had an unsupported value type.",
                step="POST credentials",
                recovery="Retry the login; if it persists, Microsoft changed the sign-in flow.",
            )
        form_data[key] = str(value)
    return Transition(kind="sso_reload", url=target, data=form_data)


def classify_post_mfa(snapshot: ResponseSnapshot, base_url: str) -> Transition:
    """Pure router for one step of the interstitial walk.

    Order matters and mirrors the wire protocol: an extracted SAMLResponse
    ends the walk; an MFA verification page is terminal for the caller to
    handle; the sso_reload session-pull hop re-POSTs echoed credentials;
    redirects are followed; then auto-submit forms (hiddenform, KMSI/CMSI);
    then the SAMLRequest JS redirect; otherwise stop.
    """
    saml = extract_saml_response(snapshot.html)
    if saml:
        return Transition(kind="saml", saml_response=saml)

    if is_mfa_page(snapshot.html):
        return Transition(kind="mfa")

    if is_sso_reload_page(snapshot):
        return sso_reload_transition(snapshot, base_url)

    if snapshot.status_code in _REDIRECT_STATUSES and snapshot.location:
        resolved = _trusted_url(
            base_url,
            snapshot.location,
            _FLOW_ALLOWED_HOSTS,
            step="SSO interstitial walk",
            label="redirect",
        )
        return Transition(kind="redirect", url=resolved)

    if is_hiddenform_page(snapshot.html):
        return hiddenform_transition(snapshot, base_url)

    if kmsi_page_detected(snapshot):
        return kmsi_transition(snapshot, base_url)

    if "SAMLRequest" in snapshot.html and "SAMLResponse" not in snapshot.html:
        url = find_saml_request_url(snapshot.html)
        if url:
            return Transition(
                kind="samlrequest",
                url=_trusted_url(
                    base_url,
                    url,
                    _FLOW_ALLOWED_HOSTS,
                    step="SSO interstitial walk",
                    label="SAML request",
                ),
            )

    return Transition(kind="stop")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class MicrosoftSSOClient:
    """HTTP driver for the Microsoft Azure AD SSO + D2L login state machine.

    Uses ``requests`` for the full flow.  Each call to :meth:`login` creates a
    fresh ``requests.Session`` so the client is safely reusable between login
    attempts.

    Usage::

        client = MicrosoftSSOClient()
        cookies = client.login("user@manipal.edu", "password", "123456")
        # cookies is a dict of d2l cookie name -> value
    """

    def __init__(
        self,
        *,
        timeout: int = 30,
        user_agent: str | None = None,
        flow_log: str | None = None,
    ) -> None:
        self._user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
        # Note: login() creates its own fresh session for each login attempt.
        # This constructor session is used by complete_mfa_pending(), which
        # resumes an existing flow without going through login().
        self._session = requests.Session()
        self._timeout = timeout
        # Diagnostics: when LIGHTHOUSE_DEBUG_FLOW names a file, append one
        # sanitized JSON record per HTTP step (method, origin+path, status,
        # form field NAMES, page shape). Never request/response bodies, never
        # headers, cookies, tokens, or query strings.
        self._flow_log = flow_log or os.environ.get("LIGHTHOUSE_DEBUG_FLOW") or ""
        self._session.headers.update({
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    # -- transport -------------------------------------------------------------

    def _record_flow(
        self,
        method: str,
        url: str,
        status: int | None = None,
        *,
        field_names: list[str] | None = None,
        page_shape: str | None = None,
    ) -> None:
        """Append one sanitized step record when LIGHTHOUSE_DEBUG_FLOW is set."""
        if not self._flow_log:
            return
        try:
            entry: dict[str, Any] = {
                "method": method,
                "url": _safe_flow_location(url),
            }
            if status is not None:
                entry["status"] = status
            if field_names is not None:
                entry["form_fields"] = _safe_flow_field_names(field_names)
            if page_shape:
                safe_page = safe_diagnostic_text(page_shape, fallback="")
                if safe_page:
                    entry["page"] = safe_page
            with open(self._flow_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # diagnostics must never break the login flow

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET with timeout and allow_redirects=False."""
        resp = self._session.get(
            url,
            allow_redirects=False,
            timeout=self._timeout,
            **kwargs,
        )
        self._record_flow("GET", url, resp.status_code)
        return resp

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        """POST with timeout and allow_redirects=False."""
        data = kwargs.get("data")
        if isinstance(data, dict):
            self._record_flow("POST", url, field_names=sorted(data.keys()))
        resp = self._session.post(
            url,
            allow_redirects=False,
            timeout=self._timeout,
            **kwargs,
        )
        self._record_flow("POST", url, resp.status_code)
        return resp

    def _post_with_redirects(self, url: str, **kwargs: Any) -> requests.Response:
        """POST the D2L ACS and follow only exact-origin redirects.

        ``requests`` follows 307/308 redirects with the original POST body,
        which would leak the SAML assertion if an upstream supplied a hostile
        Location.  Follow redirects explicitly so every destination is
        validated before a body is transmitted.
        """
        current = _trusted_url(
            BASE_URL,
            url,
            _D2L_ALLOWED_HOSTS,
            step="POST SAML",
            label="D2L ACS",
        )
        post_kwargs = dict(kwargs)
        post_kwargs.pop("allow_redirects", None)
        post_kwargs.pop("timeout", None)

        def post_current() -> requests.Response:
            data = post_kwargs.get("data")
            if isinstance(data, dict):
                self._record_flow("POST", current, field_names=sorted(data.keys()))
            response = self._session.post(
                current,
                allow_redirects=False,
                timeout=self._timeout,
                **post_kwargs,
            )
            self._record_flow("POST", current, response.status_code)
            return response

        response = post_current()
        post_mode = True
        for _ in range(8):
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location", "")
            if not location:
                return response
            redirect_base = getattr(response, "url", current)
            if not isinstance(redirect_base, str) or not redirect_base:
                redirect_base = current
            current = _trusted_url(
                redirect_base,
                str(location),
                _D2L_ALLOWED_HOSTS,
                step="POST SAML",
                label="D2L redirect",
            )
            d2l_cookies = {
                cookie.name: str(cookie.value or "")
                for cookie in self._session.cookies
                if cookie.name.startswith("d2l")
                and cookie_domain_accepted(cookie.domain or "")
            }
            if not missing_cookie_names(d2l_cookies):
                return response
            with suppress(Exception):
                response.close()
            if response.status_code in (307, 308) and post_mode:
                response = post_current()
                continue
            # 301/302/303 transitions are GETs; never carry the SAML body.
            response = self._session.get(
                current,
                allow_redirects=False,
                timeout=self._timeout,
            )
            self._record_flow("GET", current, response.status_code)
            post_mode = False
        raise MicrosoftSSOError(
            "D2L ACS redirect limit exceeded.",
            step="POST SAML",
            recovery="Retry the login; the D2L sign-in redirect chain may be looping.",
        )

    def _snapshot(self, resp: requests.Response) -> ResponseSnapshot:
        return ResponseSnapshot.from_response(resp)

    @staticmethod
    def _resolve_mfa_url(base_url: str, path: str) -> str:
        return _trusted_url(
            base_url,
            path,
            _MICROSOFT_ALLOWED_HOSTS,
            step="MFA",
            label="MFA",
        )

    # -- checkpointing -----------------------------------------------------------

    def _checkpoint_mfa_pending(self, **updates: Any) -> None:
        """Persist in-progress MFA state so verify can resume after interruptions."""
        from lighthouse_cli.config import update_mfa_pending

        updates.setdefault("cookies", _export_session_cookies(self._session))
        update_mfa_pending(updates)

    @staticmethod
    def _parse_mfa_pending(pending: dict[str, Any]) -> tuple[UserProof, dict[str, Any], dict[str, Any], str]:
        """Validate pending MFA file shape; raise if corrupted."""
        required = ("mfa_page_url", "mfa_config", "begin", "selected_proof")
        missing = [k for k in required if k not in pending]
        if missing:
            raise MicrosoftSSOError(
                f"Pending MFA session is incomplete ({', '.join(missing)}).",
                step="MFA verify",
                recovery="Run: lighthouse auth login --mfa-method sms",
            )
        try:
            selected = UserProof(**pending["selected_proof"])
            mfa_config = pending["mfa_config"]
            begin_data = pending["begin"]
            mfa_page_url = str(pending["mfa_page_url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MicrosoftSSOError(
                "Pending MFA session is corrupted.",
                step="MFA verify",
                recovery="Run: lighthouse auth login --mfa-method sms",
            ) from exc
        if not isinstance(mfa_config, dict) or not isinstance(begin_data, dict):
            raise MicrosoftSSOError(
                "Pending MFA session is corrupted.",
                step="MFA verify",
                recovery="Run: lighthouse auth login --mfa-method sms",
            )
        return selected, mfa_config, begin_data, mfa_page_url

    def complete_mfa_pending(self, totp_code: str) -> dict[str, str]:
        """Finish login from saved state after ``auth login`` (no new BeginAuth)."""
        from lighthouse_cli.config import clear_mfa_pending, load_mfa_pending

        pending = load_mfa_pending()
        if not pending:
            raise MicrosoftSSOError(
                "No pending MFA session. Run: lighthouse auth login --mfa-method sms",
                step="MFA verify",
                recovery="Start login first; when a code is sent, run: lighthouse auth verify <code>",
            )

        try:
            selected, mfa_config, begin_data, mfa_page_url = self._parse_mfa_pending(pending)
            _import_session_cookies(self._session, pending.get("cookies") or [])

            kmsi_cp = pending.get("kmsi_checkpoint")
            if isinstance(kmsi_cp, dict) and kmsi_cp.get("html"):
                kmsi_snap = ResponseSnapshot(
                    url=str(kmsi_cp.get("url") or mfa_page_url),
                    status_code=200,
                    location="",
                    html=str(kmsi_cp["html"]),
                )
                step_snap = self._advance_to_saml(
                    self._submit_kmsi(kmsi_snap),
                    # Resolve a possibly tenant-relative urlPost against the
                    # snapshot URL so the walk never bases on a relative URL.
                    urljoin(kmsi_snap.url, str(mfa_config.get("urlPost") or mfa_page_url)),
                )
            else:
                skip_end_auth = bool(
                    pending.get("end_auth_flow") and pending.get("end_auth_ctx")
                )
                step_snap = self._mfa_finish_after_begin(
                    mfa_page_url,
                    mfa_config,
                    selected,
                    begin_data,
                    totp_code.strip(),
                    skip_end_auth=skip_end_auth,
                    end_auth_flow=str(pending["end_auth_flow"]) if skip_end_auth else None,
                    end_auth_ctx=str(pending["end_auth_ctx"]) if skip_end_auth else None,
                )

            saml_response = extract_saml_response(step_snap.html)
            if not saml_response and is_error_page(step_snap):
                code, msg = _extract_error_code_and_msg(step_snap.html)
                raise build_sso_error(code, msg, "MFA verify")
            if not saml_response:
                raise MicrosoftSSOError(
                    "No SAML response after MFA verification.",
                    step="MFA verify",
                    recovery="Run: lighthouse auth login --mfa-method sms, then verify with a fresh code.",
                )

            self._step_post_saml(saml_response, step_snap.html)
            cookies = self._extract_d2l_cookies()
            clear_mfa_pending()
            return cookies
        except MicrosoftSSOError:
            # A rejected/expired code has no reusable EndAuth state and must
            # be discarded.  Once EndAuth succeeds, however, the checkpoint
            # contains the tokens needed to retry ProcessAuth/KMSI after a
            # post-EndAuth failure; keep that checkpoint recoverable.  Reload
            # the file here because ``_poll_end_auth`` writes those fields
            # after the local ``pending`` snapshot was loaded.
            resumable = False
            with suppress(CredentialStoreError):
                checkpoint = load_mfa_pending() or {}
                resumable = bool(
                    checkpoint.get("end_auth_flow")
                    and checkpoint.get("end_auth_ctx")
                )
            if not resumable:
                clear_mfa_pending()
            raise

    # -- auth flow ---------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        totp_code: str | None = None,
        *,
        mfa_method: str = MFA_METHOD_AUTO,
        on_credentials_submitted: Callable[[], None] | None = None,
        read_totp_after_challenge: bool = False,
        defer_mfa_to_pending: bool = False,
    ) -> dict[str, str]:
        """Execute the full login flow and return D2L session cookies.

        Args:
            username: Email address for Microsoft SSO (e.g. user@manipal.edu)
            password: Microsoft account password
            totp_code: 2FA code for push/legacy flows, or None to prompt/read after challenge
            mfa_method: ``auto``, ``sms`` (text message), or ``app`` (Authenticator)
            on_credentials_submitted: Optional callback after password POST succeeds
            read_totp_after_challenge: If True, read ``totp_code`` from stdin only after
                BeginAuth sends an SMS/OTP (used with ``--totp -``).
            defer_mfa_to_pending: If True, stop after BeginAuth and save session for
                ``lighthouse auth verify`` (no second BeginAuth on verify).

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

        # Starting a new login invalidates any previous BeginAuth checkpoint.
        # Resume uses ``complete_mfa_pending`` and never enters this method.
        from lighthouse_cli.config import clear_mfa_pending

        clear_mfa_pending()

        # Create a fresh session for each login attempt (safe reuse).
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # Step 1: Initiate D2L SAML login
        ms_url = self._step_initiate_saml()

        # Step 2: GET Microsoft login page → extract $Config
        ms_config = self._step_get_ms_config(ms_url)

        # Step 2b: Username step (Playwright bootstrap when available for this tenant)
        ms_config = self._step_prepare_username(ms_config, username)

        # Step 3: POST credentials to Microsoft
        snap = self._step_post_credentials(
            ms_config, username, password, skip_username_prepare=True
        )
        self._record_flow(
            "PAGE", snap.url, snap.status_code, page_shape=describe_page_shape(snap)
        )
        # Aug-2026 Microsoft session-pull interstitial: a form-less 200
        # "Redirecting" page whose $Config asks the client to re-POST the
        # echoed credential params (oPostParams) to urlPost. The bounded
        # walk also follows any KMSI/hiddenform/redirect hops so the MFA /
        # error / SAML classification below sees the real page. No KMSI
        # checkpointing here: pre-MFA interrupts are handled inline.
        snap = self._advance_to_saml(snap, snap.url, checkpoint_kmsi=False)
        if is_mfa_page(snap.html):
            if on_credentials_submitted is not None:
                on_credentials_submitted()
            # Step 4a: Handle MFA (two-phase: code collected after password accepted)
            snap = self._step_handle_mfa(
                snap,
                ms_config,
                totp_code,
                mfa_method=mfa_method,
                read_totp_after_challenge=read_totp_after_challenge,
                defer_mfa_to_pending=defer_mfa_to_pending,
            )
            saml_html = snap.html
            saml_response = extract_saml_response(saml_html)
        elif is_error_page(snap):
            code, msg = _extract_error_code_and_msg(snap.html)
            raise build_sso_error(code, msg, "POST credentials")
        else:
            # Response might already contain SAML
            saml_html = snap.html
            saml_response = extract_saml_response(saml_html)
            if not saml_response:
                code, msg = _extract_error_code_and_msg(saml_html)
                raise build_sso_error(
                    code,
                    msg or f"Unexpected response — {describe_page_shape(snap)}",
                    "POST credentials (unexpected response)",
                )

        # Step 5: POST SAMLResponse to D2L ACS
        if saml_response is None:
            raise MicrosoftSSOError(
                "No SAML response found in login flow.",
                step="extract SAML",
                recovery="Try again or check your account status.",
            )
        self._step_post_saml(saml_response, saml_html)
        # Step 6: Extract D2L cookies. Only after the complete inline flow has
        # produced a valid session do we remove a stale/recovery checkpoint.
        cookies = self._extract_d2l_cookies()
        clear_mfa_pending()
        return cookies

    def probe_mfa_methods(self, username: str, password: str) -> MfaProbeResult:
        """Discover MFA methods and normalize unexpected failures safely."""
        try:
            return self._probe_mfa_methods_impl(username, password)
        except MicrosoftSSOError:
            raise
        except Exception:
            raise MicrosoftSSOError(
                "MFA method discovery failed.",
                step="MFA discovery",
                recovery="Retry the command and check your connection.",
            ) from None

    def _probe_mfa_methods_impl(self, username: str, password: str) -> MfaProbeResult:
        """Run the flow up to the MFA page and report registered methods.

        Stops before any code submission; never sends BeginAuth.  The result
        distinguishes accounts that need no verification at all from tenants
        serving the legacy form-based MFA page (no arrUserProofs).
        """
        ms_url = self._step_initiate_saml()
        config = self._step_get_ms_config(ms_url)
        config = self._step_prepare_username(config, username)
        snap = self._step_post_credentials(
            config, username, password, skip_username_prepare=True
        )
        # Same session-pull interstitial as login(): re-POST echoed params
        # (bounded) before deciding whether an MFA page was reached.
        snap = self._advance_to_saml(snap, snap.url, checkpoint_kmsi=False)
        if not is_mfa_page(snap.html):
            if is_error_page(snap):
                code, msg = _extract_error_code_and_msg(snap.html)
                raise build_sso_error(code, msg, "POST credentials")
            if extract_saml_response(snap.html):
                # Password accepted, no second factor requested.
                return MfaProbeResult(page="no_mfa", proofs=[])
            raise MicrosoftSSOError(
                "Microsoft returned an unrecognized page while discovering MFA methods.",
                step="MFA discovery",
                recovery=(
                    "Retry the command. If it persists, enable LIGHTHOUSE_DEBUG_FLOW "
                    "and report the sanitized page-shape trace."
                ),
            )
        cfg = _extract_config_json(snap.html) or {}
        proofs = _parse_user_proofs(cfg)
        page = "converged" if proofs else "legacy_form"
        return MfaProbeResult(page=page, proofs=proofs)

    # -- step implementations ------------------------------------------------------

    def _step_initiate_saml(self) -> str:
        """Step 1: GET D2L SAML login → follow redirect to Microsoft.

        Returns the Microsoft login page URL.
        """
        login_url = f"{BASE_URL}{LOGIN_PATH}"
        resp = self._get(login_url)
        if resp.status_code in _REDIRECT_STATUSES:
            ms_url = resp.headers.get("Location", "")
            if ms_url:
                return _trusted_url(
                    login_url,
                    str(ms_url),
                    _MICROSOFT_ALLOWED_HOSTS,
                    step="initiate SAML",
                    label="Microsoft redirect",
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
                        return _trusted_url(
                            login_url,
                            m.group(1).strip("'\""),
                            _MICROSOFT_ALLOWED_HOSTS,
                            step="initiate SAML",
                            label="Microsoft meta-refresh",
                        )
            # Look for a JavaScript redirect
            m = re.search(r'window\.location\s*=\s*["\'](.+?)["\']', resp.text)
            if m:
                return _trusted_url(
                    login_url,
                    m.group(1),
                    _MICROSOFT_ALLOWED_HOSTS,
                    step="initiate SAML",
                    label="Microsoft script redirect",
                )

        raise MicrosoftSSOError(
            f"Failed to redirect to Microsoft SSO. Got HTTP {resp.status_code}",
            step="initiate SAML",
            recovery="Check that lighthouse.manipal.edu is reachable.",
        )

    def _step_get_ms_config(self, ms_url: str) -> dict[str, Any]:
        """Step 2: GET Microsoft login page, extract $Config JSON."""
        trusted_ms_url = _trusted_url(
            "https://login.microsoftonline.com",
            ms_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="get MS config",
            label="Microsoft login",
        )
        resp = self._get(trusted_ms_url)

        # If we get a redirect from Microsoft (already authenticated at MS level),
        # follow it through to get the SAML response
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location", "")
            if location:
                redirect_base = (
                    BASE_URL
                    if isinstance(location, str) and location.startswith("/d2l/")
                    else trusted_ms_url
                )
                location = _trusted_url(
                    redirect_base,
                    str(location),
                    _FLOW_ALLOWED_HOSTS,
                    step="get MS config",
                    label="Microsoft redirect",
                )
            return {"_redirect": True, "_location": location}

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
                    "urlPost": (
                        _trusted_url(
                            trusted_ms_url,
                            action_str,
                            _MICROSOFT_ALLOWED_HOSTS,
                            step="get MS config",
                            label="login form",
                        )
                        if action_str
                        else trusted_ms_url
                    ),
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
        response_url = str(getattr(resp, "url", "") or trusted_ms_url)
        config["_ms_url"] = _trusted_url(
            trusted_ms_url,
            response_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="get MS config",
            label="Microsoft page",
        )
        return self._hydrate_ms_flow_config(config)

    def _hydrate_ms_flow_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Fetch flowToken/ctx when the first Microsoft page omits them (common on SAML2)."""
        if config.get("sFT") and config.get("sCtx"):
            return config
        url_post = config.get("urlPost")
        if not url_post:
            return config
        ms_base = str(config.get("_ms_url", "https://login.microsoftonline.com"))
        post_page_url = _trusted_url(
            ms_base,
            str(url_post),
            _MICROSOFT_ALLOWED_HOSTS,
            step="hydrate MS config",
            label="Microsoft flow",
        )
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
        saml_referer = config.get("_ms_url")
        if saml_referer:
            merged["_ms_url"] = saml_referer
        _prune_stale_esctx_cookies(self._session)
        return merged

    def _post_dsso_status(self, config: dict[str, Any], canary: str) -> None:
        """Report desktop SSO probe result (browser fires this around username entry)."""
        referer = str(config.get("_ms_url", ""))
        self._record_flow(
            "POST",
            "https://login.microsoftonline.com/common/instrumentation/dssostatus",
            field_names=["resultCode", "ssoDelay", "log"],
        )
        resp = self._session.post(
            "https://login.microsoftonline.com/common/instrumentation/dssostatus",
            json={
                "resultCode": 2,
                "ssoDelay": 0,
                "log": "Probe image error event fired",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json",
                "canary": canary,
                "client-request-id": str(config.get("correlationId") or ""),
                "hpgact": str(config.get("hpgact", "1900")),
                "hpgid": str(config.get("hpgid", "1104")),
                "hpgrequestid": str(config.get("sessionId") or ""),
                "Referer": referer,
            },
            allow_redirects=False,
            timeout=self._timeout,
        )
        self._record_flow(
            "POST",
            "https://login.microsoftonline.com/common/instrumentation/dssostatus",
            resp.status_code,
        )

    def _import_playwright_cookies(self, pw_cookies: list[dict[str, Any]]) -> None:
        for cookie in pw_cookies:
            domain = cookie.get("domain") or ""
            if not domain.startswith(".") and domain:
                domain = f".{domain}" if "microsoft" in domain or "live.com" in domain else domain
            self._session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=domain,
                path=cookie.get("path", "/"),
            )

    def _bootstrap_username_via_playwright(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Run the username step in headless Chromium; sync cookies + tokens to HTTP session."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlaywrightUnavailableError(
                "Playwright is not installed.",
                step="prepare username",
                recovery="Install with: pip install playwright && playwright install chromium",
            ) from exc

        ms_url = str(config.get("_ms_url", ""))
        if not ms_url:
            raise MicrosoftSSOError(
                "Missing Microsoft login page URL.",
                step="prepare username",
            )
        ms_url = _trusted_url(
            "https://login.microsoftonline.com",
            ms_url,
            _MICROSOFT_ALLOWED_HOSTS,
            step="prepare username",
            label="Microsoft login",
        )

        user_agent = self._session.headers.get("User-Agent", "")
        export_cookies: list[dict[str, Any]] = []
        for cookie in self._session.cookies:
            export_cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
            })

        try:
            manager = sync_playwright()
            playwright = manager.__enter__()
        except Exception as exc:
            raise PlaywrightUnavailableError(
                f"Playwright runtime could not start ({exc.__class__.__name__}).",
                step="prepare username",
                recovery="Ensure Playwright and its driver are installed correctly.",
            ) from None

        browser = None
        try:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                raise PlaywrightUnavailableError(
                    f"Chromium could not launch ({exc.__class__.__name__}).",
                    step="prepare username",
                    recovery="Install Chromium with: playwright install chromium",
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
                referer = _trusted_url(
                    ms_url,
                    str(page.url),
                    _MICROSOFT_ALLOWED_HOSTS,
                    step="prepare username",
                    label="Microsoft page",
                )
                pw_cookies = context.cookies()
            except MicrosoftSSOError:
                raise
            except Exception as exc:
                raise MicrosoftSSOError(
                    f"Playwright username step failed ({exc.__class__.__name__}).",
                    step="prepare username",
                    recovery=(
                        "Microsoft may have changed the username page. Retry, or "
                        "remove the auth extra to force the mirrored HTTP path."
                    ),
                ) from None
        finally:
            if browser is not None:
                with suppress(Exception):
                    browser.close()
            with suppress(Exception):
                manager.__exit__(None, None, None)

        self._import_playwright_cookies(pw_cookies)
        _prune_stale_esctx_cookies(self._session)

        updated = dict(config)
        for key in ("urlPost", "sFT", "sCtx", "canary", "sessionId", "i19"):
            if pw_cfg.get(key):
                updated[key] = pw_cfg[key]
        updated["_ms_url"] = referer
        return updated

    def _step_prepare_username_http(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Mirror the browser's pre-password HTTP requests (Me.htm, SSO probe, GCT)."""
        if not config.get("sFT") or not config.get("sCtx"):
            return config

        referer = _trusted_url(
            "https://login.microsoftonline.com",
            str(config.get("_ms_url", "")),
            _MICROSOFT_ALLOWED_HOSTS,
            step="prepare username",
            label="Microsoft referer",
        )
        tenant_id = _tenant_id_from_ms_url(referer)
        client_request_id = str(config.get("correlationId") or "")

        self._get(
            "https://login.live.com/Me.htm?v=3",
            headers={"Referer": referer},
        )

        ssoprobe_url = (
            "https://autologon.microsoftazuread-sso.com/"
            f"{tenant_id}/winauth/ssoprobe?client-request-id={client_request_id}"
        )
        probe_resp = self._session.get(
            ssoprobe_url,
            headers={"Referer": referer},
            allow_redirects=False,
            timeout=self._timeout,
        )
        self._record_flow("GET", ssoprobe_url, probe_resp.status_code)

        canary_hdr = str(config.get("apiCanary") or config.get("canary") or "")
        self._post_dsso_status(config, canary_hdr)

        updated = self._step_get_credential_type(config, username)

        ssoprobe_url_2 = f"{ssoprobe_url}&_={int(time.time() * 1000)}"
        probe2_resp = self._session.get(
            ssoprobe_url_2,
            headers={"Referer": referer},
            allow_redirects=False,
            timeout=self._timeout,
        )
        self._record_flow("GET", ssoprobe_url_2, probe2_resp.status_code)

        post_gct_canary = str(updated.get("apiCanary") or canary_hdr)
        self._post_dsso_status(updated, post_gct_canary)

        self._session.cookies.set(
            "brcap", "0", domain=".login.microsoftonline.com", path="/"
        )
        _prune_stale_esctx_cookies(self._session)
        return updated

    def _step_prepare_username(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Establish Microsoft session state after the user enters their username."""
        if not config.get("urlGetCredentialType"):
            return config
        try:
            return self._bootstrap_username_via_playwright(config, username)
        except PlaywrightUnavailableError:
            # Import/runtime/launch failures can use the mirrored HTTP path.
            # Semantic browser-flow failures remain errors and are not hidden.
            print(
                "Playwright username bootstrap unavailable; using the pure-HTTP flow.",
                file=sys.stderr,
                flush=True,
            )
            return self._step_prepare_username_http(config, username)

    def _step_get_credential_type(
        self, config: dict[str, Any], username: str
    ) -> dict[str, Any]:
        """Call GetCredentialType to refresh flowToken before password POST."""
        gct_url = config.get("urlGetCredentialType")
        if not gct_url or not config.get("sFT") or not config.get("sCtx"):
            return config

        gct_base = str(config.get("_ms_url", "https://login.microsoftonline.com"))
        gct_full = _trusted_url(
            gct_base,
            str(gct_url),
            _MICROSOFT_ALLOWED_HOSTS,
            step="prepare username",
            label="GetCredentialType",
        )
        payload = {
            "username": username,
            "isOtherIdpSupported": True,
            "checkPhones": True,
            "isRemoteNGCSupported": bool(config.get("fIsRemoteNGCSupported", True)),
            "isCookieBannerShown": False,
            "isFidoSupported": bool(config.get("fIsFidoSupported", True)),
            "originalRequest": str(config["sCtx"]),
            "flowToken": str(config["sFT"]),
            "country": "IN",
            "forceotclogin": False,
            "isExternalFederationDisallowed": False,
            "isRemoteConnectSupported": False,
            "federationFlags": 0,
            "isSignup": False,
            "isAccessPassSupported": bool(config.get("fIsAccessPassSupported", True)),
            "isQrCodePinSupported": bool(config.get("fIsQrCodePinSupported", True)),
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
        self._record_flow("POST", gct_full, field_names=sorted(payload.keys()))
        resp = self._session.post(
            gct_full,
            json=payload,
            headers=headers,
            allow_redirects=False,
            timeout=self._timeout,
        )
        gct_keys: list[str] = []
        data: Any = None
        if resp.status_code == 200:
            try:
                data = resp.json()
                gct_keys = sorted(data.keys()) if isinstance(data, dict) else []
            except ValueError:
                # ValueError covers every JSON parser requests can use:
                # stdlib json.JSONDecodeError and simplejson.JSONDecodeError
                # both subclass it, so a 200 HTML body degrades gracefully.
                gct_keys = ["(unparseable)"]
        self._record_flow(
            "POST",
            gct_full,
            resp.status_code,
            field_names=gct_keys,
        )
        if resp.status_code != 200 or not isinstance(data, dict):
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
        *,
        skip_username_prepare: bool = False,
    ) -> ResponseSnapshot:
        """Step 3: POST username + password to Microsoft."""
        # When already authenticated at MS level, follow the redirect
        if config.get("_redirect"):
            location = config.get("_location", "")
            if not isinstance(location, str) or not location:
                raise MicrosoftSSOError(
                    "Microsoft redirect was missing a destination.",
                    step="POST credentials",
                )
            resolved = _trusted_url(
                "https://login.microsoftonline.com",
                location,
                _FLOW_ALLOWED_HOSTS,
                step="POST credentials",
                label="Microsoft redirect",
            )
            return self._snapshot(self._get(resolved))

        url_post = config.get("urlPost", "")
        if not url_post:
            raise MicrosoftSSOError(
                "No urlPost in Microsoft $Config. Login page structure may have changed.",
                step="POST credentials",
                recovery="Microsoft may have changed their login flow.",
            )

        config = self._hydrate_ms_flow_config(config)
        if not skip_username_prepare:
            config = self._step_prepare_username(config, username)
        if not config.get("sFT") or not config.get("sCtx"):
            raise MicrosoftSSOError(
                "Microsoft login flow tokens (flowToken/ctx) are missing.",
                step="POST credentials",
                recovery="Microsoft may have changed their login page. Try again later.",
            )

        ms_base = str(config.get("_ms_url", "https://login.microsoftonline.com"))
        url_post = config.get("urlPost", "")
        login_url = _trusted_url(
            ms_base,
            str(url_post),
            _MICROSOFT_ALLOWED_HOSTS,
            step="POST credentials",
            label="login",
        )
        referer = _trusted_url(
            "https://login.microsoftonline.com",
            ms_base,
            _MICROSOFT_ALLOWED_HOSTS,
            step="POST credentials",
            label="Microsoft referer",
        )

        data = build_password_form_data(config, username, password)

        resp = self._post(
            login_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": referer,
                "Origin": "https://login.microsoftonline.com",
            },
        )

        # Handle redirect (transparent re-auth)
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location", "")
            if location:
                resolved = _trusted_url(
                    login_url,
                    str(location),
                    _FLOW_ALLOWED_HOSTS,
                    step="POST credentials",
                    label="Microsoft redirect",
                )
                return self._snapshot(self._get(resolved))

        return self._snapshot(resp)

    # -- MFA handling -----------------------------------------------------------

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
            print(f"  • {format_user_proof(proof)}{marker}", flush=True, file=sys.stderr)
        hint = MFA_METHOD_INSTRUCTIONS.get(
            selected.auth_method_id,
            "Enter the verification code from the method shown above.",
        )
        if selected.auth_method_id in SERVER_SENT_CODE_AUTH_IDS and code_sent_on_begin:
            phone = safe_proof_destination(selected)
            print(f"\nA verification code was just sent to {phone}.", flush=True, file=sys.stderr)
            if selected.auth_method_id == MFA_AUTH_SMS:
                print(
                    "Delivery (SMS vs WhatsApp) is chosen by Microsoft; the CLI cannot force a channel.",
                    flush=True,
                    file=sys.stderr,
                )
        print(f"\n{hint}", flush=True, file=sys.stderr)

    def _prompt_mfa_code(self) -> str:
        if sys.stdin.isatty():
            # Prompt on stderr so stdout stays JSON-only under --json in a TTY.
            print("Enter verification code: ", end="", flush=True, file=sys.stderr)
            return input().strip()
        return sys.stdin.readline().strip()

    def _collect_totp_after_challenge(
        self,
        selected: UserProof,
        totp_code: str | None,
        *,
        read_totp_after_challenge: bool,
        code_sent_on_begin: bool,
    ) -> str:
        """Collect OTP after BeginAuth.

        SMS/WhatsApp issues a fresh code on BeginAuth; PhoneAppOTP is an
        offline TOTP generated on the user's device, so a pre-provided code
        stays valid. Codeless methods never call this helper.
        """
        needs_fresh_code = selected.auth_method_id in SERVER_SENT_CODE_AUTH_IDS
        if needs_fresh_code and totp_code and not read_totp_after_challenge:
            totp_code = None

        if read_totp_after_challenge:
            if not sys.stdin.isatty() and sys.stdin.readable():
                line = sys.stdin.readline().strip()
                if line:
                    return line
            if sys.stdin.isatty():
                return self._prompt_mfa_code()
            raise MicrosoftSSOError(
                "2FA code required after verification was sent.",
                step="MFA",
                recovery=(
                    "Run without --totp and enter the code when prompted, or pipe after "
                    "BeginAuth: lighthouse auth login --mfa-method sms --totp -"
                ),
            )

        if totp_code is None or (needs_fresh_code and not totp_code):
            if sys.stdin.isatty():
                if needs_fresh_code and code_sent_on_begin:
                    print(
                        "A verification code was just sent. "
                        "Enter the code from this message (not an older one):",
                        flush=True,
                        file=sys.stderr,
                    )
                return self._prompt_mfa_code()
            raise MicrosoftSSOError(
                "2FA code is required but was empty.",
                step="MFA",
                recovery=(
                    "Run interactively, or use --totp - and pipe the code after "
                    "you receive it."
                ),
            )

        return totp_code.strip()

    def _step_handle_mfa(
        self,
        mfa_snap: ResponseSnapshot,
        original_config: dict[str, Any],
        totp_code: str | None,
        *,
        mfa_method: str = MFA_METHOD_AUTO,
        read_totp_after_challenge: bool = False,
        defer_mfa_to_pending: bool = False,
    ) -> ResponseSnapshot:
        """Step 4: Handle MFA — ConvergedTFA SAS API or legacy form fallback."""
        mfa_config = _extract_config_json(mfa_snap.html) or {}
        proofs = _parse_user_proofs(mfa_config)
        if proofs:
            return self._step_handle_mfa_converged(
                mfa_snap,
                mfa_config,
                proofs,
                totp_code,
                mfa_method=mfa_method,
                read_totp_after_challenge=read_totp_after_challenge,
                defer_mfa_to_pending=defer_mfa_to_pending,
            )
        return self._step_handle_mfa_legacy_form(
            mfa_snap,
            original_config,
            totp_code,
            mfa_config=mfa_config,
            mfa_method=mfa_method,
        )

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
        is_codeless = selected.auth_method_id in CODELESS_APPROVAL_AUTH_IDS
        if totp_code is not None and selected.auth_method_id != MFA_AUTH_APP_OTP:
            raise MicrosoftSSOError(
                "A pre-provided --totp code is valid only for PhoneAppOTP.",
                step="MFA",
                recovery=(
                    "Use --mfa-method app for an offline Authenticator code, "
                    "or start the selected two-step challenge without --totp."
                ),
            )
        if read_totp_after_challenge and is_codeless:
            raise MicrosoftSSOError(
                "The selected MFA method is codeless and cannot read --totp from stdin.",
                step="MFA",
                recovery="Start login without --totp, then run auth verify ok.",
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
        if not isinstance(begin_data, dict):
            raise MicrosoftSSOError(
                "Microsoft MFA BeginAuth returned an invalid response.",
                step="MFA",
                recovery="Try again or use --mfa-method auto.",
            )

        if not begin_data.get("Success"):
            message = safe_upstream_text(
                begin_data.get("Message") or begin_data.get("ResultValue"),
                fallback="unknown error",
            )
            raise MicrosoftSSOError(
                f"MFA setup failed: {message}",
                step="MFA BeginAuth",
                recovery="Try a different --mfa-method or check your Microsoft security settings.",
            )

        code_sent_on_begin = selected.auth_method_id in SERVER_SENT_CODE_AUTH_IDS
        self._print_mfa_phase_banner(proofs, selected, code_sent_on_begin=code_sent_on_begin)

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
                        "Run: lighthouse auth verify ok  (displays any number "
                        "match and waits for approval)"
                    ),
                )
            if selected.auth_method_id in CODELESS_APPROVAL_AUTH_IDS:
                raise MfaPendingError(
                    "Voice approval call started — answer and press #.",
                    step="MFA",
                    recovery="Run: lighthouse auth verify ok  (waits for the call approval)",
                )
            if selected.auth_method_id == MFA_AUTH_APP_OTP:
                raise MfaPendingError(
                    "Authenticator code required.",
                    step="MFA",
                    recovery="Run: lighthouse auth verify <current-app-code>",
                )
            raise MfaPendingError(
                "Verification code sent.",
                step="MFA",
                recovery="Run: lighthouse auth verify <code>  (use the code from this message)",
            )

        if not is_codeless:
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
            mfa_snap.url, mfa_config, selected, begin_data, totp_code or "",
            str(mfa_config.get("sPOST_Username") or ""),
        )

    def _mfa_finish_after_begin(
        self,
        base_url: str,
        mfa_config: dict[str, Any],
        selected: UserProof,
        begin_data: dict[str, Any],
        totp_code: str,
        login_name: str = "",
        *,
        skip_end_auth: bool = False,
        end_auth_flow: str | None = None,
        end_auth_ctx: str | None = None,
    ) -> ResponseSnapshot:
        """EndAuth + ProcessAuth after a successful BeginAuth."""
        process_url = mfa_config.get("urlPost") or "/common/SAS/ProcessAuth"
        process_endpoint = self._resolve_mfa_url(base_url, str(process_url))

        # Poll EndAuth until success or failure.
        flow_token, ctx, _end_data = self._poll_end_auth(
            base_url,
            mfa_config,
            selected,
            begin_data,
            totp_code,
            skip_end_auth=skip_end_auth,
            end_auth_flow=end_auth_flow,
            end_auth_ctx=end_auth_ctx,
        )

        # ProcessAuth: EndAuth already consumed the OTP; only pass tokens (saml2aws pattern).
        process_data = build_process_payload(mfa_config, flow_token, ctx, login_name)
        resp = self._post(process_endpoint, data=process_data)
        snap = self._snapshot(resp)

        page_cfg = _extract_config_json(snap.html) or {}
        if page_cfg.get("pgid") in ("CmsiInterrupt", "KmsiInterrupt"):
            self._checkpoint_mfa_pending(
                kmsi_checkpoint={"url": snap.url, "html": snap.html},
            )

        if is_mfa_page(snap.html):
            raise MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            )

        return self._advance_to_saml(snap, process_endpoint)

    def _poll_end_auth(
        self,
        base_url: str,
        mfa_config: dict[str, Any],
        selected: UserProof,
        begin_data: dict[str, Any],
        totp_code: str,
        *,
        skip_end_auth: bool = False,
        end_auth_flow: str | None = None,
        end_auth_ctx: str | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        """Poll the EndAuth API until MFA verification succeeds.

        Returns:
            Tuple of (flow_token, ctx, end_data) for use in ProcessAuth.
        """
        end_url = mfa_config.get("urlEndAuth") or "/common/SAS/EndAuth"
        flow_token = str(mfa_config.get("sFT") or "")
        ctx = str(mfa_config.get("sCtx") or "")

        end_flow = str(begin_data.get("FlowToken") or flow_token)
        end_ctx = str(begin_data.get("Ctx") or ctx)
        polling = mfa_config.get("oPerAuthPollingInterval") or {}
        try:
            raw_poll_seconds = (
                polling.get(selected.auth_method_id, 2)
                if isinstance(polling, dict)
                else 2
            )
            poll_seconds = float(raw_poll_seconds)
        except (TypeError, ValueError):
            poll_seconds = 2.0
        if not math.isfinite(poll_seconds):
            poll_seconds = 2.0
        poll_seconds = min(_MAX_ENDAUTH_POLL_SECONDS, max(0.5, poll_seconds))

        end_data: dict[str, Any] = {}
        shown_entropy: str | None = None
        deadline = time.monotonic() + _MAX_ENDAUTH_TOTAL_SECONDS
        if skip_end_auth and end_auth_flow and end_auth_ctx:
            end_flow = end_auth_flow
            end_ctx = end_auth_ctx
            end_data = {"FlowToken": end_flow, "Ctx": end_ctx, "Success": True}
        for poll_index in range(_MAX_ENDAUTH_POLLS):
            if skip_end_auth:
                break
            if time.monotonic() >= deadline:
                raise MicrosoftSSOError(
                    "2FA verification timed out waiting for approval.",
                    step="MFA",
                    recovery="Try again and complete verification promptly.",
                )
            end_resp = self._post(
                self._resolve_mfa_url(base_url, str(end_url)),
                json=build_end_payload(
                    selected, begin_data, totp_code,
                    end_flow=end_flow, end_ctx=end_ctx,
                ),
                headers={"Content-Type": "application/json"},
            )
            try:
                end_data = end_resp.json()
            except ValueError as exc:
                raise MicrosoftSSOError(
                    "Microsoft MFA EndAuth returned an invalid response.",
                    step="MFA",
                ) from exc
            if not isinstance(end_data, dict):
                raise MicrosoftSSOError(
                    "Microsoft MFA EndAuth returned an invalid response.",
                    step="MFA",
                )

            if end_data.get("Success"):
                self._checkpoint_mfa_pending(
                    end_auth_flow=str(end_data.get("FlowToken") or end_flow),
                    end_auth_ctx=str(end_data.get("Ctx") or end_ctx),
                )
                break
            if not end_data.get("Retry"):
                err_code = end_data.get("ErrCode")
                result = str(end_data.get("ResultValue") or end_data.get("Message") or "")
                if result == "AuthenticationPreviouslyCompleted":
                    from lighthouse_cli.config import load_mfa_pending

                    checkpoint = load_mfa_pending() or {}
                    saved_flow = checkpoint.get("end_auth_flow")
                    saved_ctx = checkpoint.get("end_auth_ctx")
                    if saved_flow and saved_ctx:
                        end_flow = str(saved_flow)
                        end_ctx = str(saved_ctx)
                        end_data = {"FlowToken": end_flow, "Ctx": end_ctx, "Success": True}
                        break
                    raise MicrosoftSSOError(
                        "This verification code was already accepted. "
                        "Run: lighthouse auth login --mfa-method sms for a new code.",
                        step="MFA verify",
                        recovery="If login succeeded but cookies were not saved, run verify again once.",
                    )
                detail = safe_upstream_text(
                    result,
                    fallback=(
                        str(err_code)
                        if isinstance(err_code, int) and not isinstance(err_code, bool)
                        else "unknown error"
                    ),
                )
                raise MicrosoftSSOError(
                    f"2FA verification failed: {detail}",
                    step="MFA",
                    recovery="Request a new code and try again.",
                )
            if selected.auth_method_id == MFA_AUTH_APP_NOTIFY:
                raw_entropy = end_data.get("Entropy")
                entropy = _safe_mfa_entropy(raw_entropy)
                if entropy and entropy != shown_entropy:
                    shown_entropy = entropy
                    print(
                        f"Approve sign-in in Authenticator (number shown: {entropy}).",
                        flush=True,
                        file=sys.stderr,
                    )
                elif (
                    raw_entropy not in (None, "")
                    and shown_entropy != _INVALID_ENTROPY_SENTINEL
                ):
                    shown_entropy = _INVALID_ENTROPY_SENTINEL
                    print(
                        "Approve sign-in in Authenticator to continue.",
                        flush=True,
                        file=sys.stderr,
                    )
            end_flow = str(end_data.get("FlowToken") or end_flow)
            end_ctx = str(end_data.get("Ctx") or end_ctx)
            if poll_index + 1 < _MAX_ENDAUTH_POLLS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MicrosoftSSOError(
                        "2FA verification timed out waiting for approval.",
                        step="MFA",
                        recovery="Try again and complete verification promptly.",
                    )
                time.sleep(min(poll_seconds, remaining))
        else:
            raise MicrosoftSSOError(
                "2FA verification timed out waiting for approval.",
                step="MFA",
                recovery="Try again and complete verification promptly.",
            )

        return (
            str(end_data.get("FlowToken") or end_flow),
            str(end_data.get("Ctx") or end_ctx),
            end_data,
        )

    def _step_handle_mfa_legacy_form(
        self,
        mfa_snap: ResponseSnapshot,
        original_config: dict[str, Any],
        totp_code: str | None,
        *,
        mfa_config: dict[str, Any],
        mfa_method: str,
    ) -> ResponseSnapshot:
        """Legacy MFA form POST (older Microsoft pages without arrUserProofs)."""
        import getpass as _getpass

        if totp_code is not None and mfa_method != MFA_METHOD_APP:
            raise MicrosoftSSOError(
                "A pre-provided --totp code cannot be validated for a legacy MFA form.",
                step="MFA",
                recovery=(
                    "Re-run without a literal --totp value and enter or pipe "
                    "the code only after Microsoft presents the challenge."
                ),
            )

        if totp_code is None:
            if sys.stdin.isatty():
                print("\n--- Second factor required ---", flush=True, file=sys.stderr)
                print("Enter the verification code shown on the Microsoft sign-in page.", flush=True, file=sys.stderr)
                totp_code = _getpass.getpass("Enter verification code: ")
            else:
                totp_code = sys.stdin.readline().strip()

        if not totp_code or not totp_code.strip():
            raise MicrosoftSSOError(
                "2FA code is required but was empty.",
                step="MFA",
                recovery="Provide a 2FA code via --totp flag or pipe.",
            )

        soup = BeautifulSoup(mfa_snap.html, "html.parser")
        form = soup.find("form")
        if not form:
            raise MicrosoftSSOError(
                "Could not find MFA form on the verification page.",
                step="MFA",
                recovery="Microsoft may have changed the MFA flow.",
            )

        action = form.get("action")
        mfa_url = (
            self._resolve_mfa_url(mfa_snap.url, str(action))
            if action
            else self._resolve_mfa_url(mfa_snap.url, mfa_snap.url)
        )

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
        snap = self._snapshot(resp)
        if is_mfa_page(snap.html):
            raise MicrosoftSSOError(
                "2FA verification failed: invalid or expired code.",
                step="MFA",
                recovery="Request a new 2FA code and try again.",
            )
        return self._advance_to_saml(snap, mfa_url)

    # -- post-MFA interstitial walk ----------------------------------------------

    def _submit_kmsi(self, snapshot: ResponseSnapshot) -> ResponseSnapshot:
        """Execute a classified KMSI/CMSI interrupt submission."""
        t = kmsi_transition(snapshot, snapshot.url)
        return self._snapshot(self._post(t.url, data=t.data or {}))

    def _advance_to_saml(
        self, snapshot: ResponseSnapshot, base_url: str, *, checkpoint_kmsi: bool = True
    ) -> ResponseSnapshot:
        """Advance through sso_reload, KMSI, hiddenform, and SAMLRequest pages.

        Pure classification (:func:`classify_post_mfa`) decides each hop; this
        driver only performs the HTTP side effects. KMSI checkpointing is
        enabled for the post-MFA walk (where a checkpointed page lets
        ``auth verify`` resume) and disabled for the post-credentials walk
        (which continues inline through any interrupts before MFA).

        Terminal outcomes: a page carrying SAMLResponse, an MFA page, or an
        unrecognized page returned to the caller. Exhausting either safety
        budget raises a clean error.
        """
        sso_reloads = 0
        for _ in range(_MAX_POST_MFA_HOPS):
            transition = classify_post_mfa(snapshot, base_url)

            if transition.kind == "saml":
                return snapshot

            if transition.kind == "mfa":
                return snapshot

            if transition.kind == "sso_reload":
                if sso_reloads >= _MAX_SSO_RELOADS:
                    raise MicrosoftSSOError(
                        "Microsoft session-pull reload limit exceeded.",
                        step="SSO interstitial walk",
                        recovery="Retry the login; the upstream sign-in flow may be looping.",
                    )
                sso_reloads += 1
                snapshot = self._snapshot(self._post(transition.url, data=transition.data or {}))
                base_url = snapshot.url
                continue

            if transition.kind == "redirect":
                snapshot = self._snapshot(self._get(transition.url))
                base_url = snapshot.url
                continue

            if transition.kind == "hiddenform":
                snapshot = self._snapshot(self._post(transition.url, data=transition.data or {}))
                base_url = snapshot.url
                continue

            if transition.kind == "kmsi":
                if checkpoint_kmsi:
                    self._checkpoint_mfa_pending(
                        kmsi_checkpoint={"url": snapshot.url, "html": snapshot.html},
                    )
                snapshot = self._snapshot(self._post(transition.url, data=transition.data or {}))
                base_url = snapshot.url
                continue

            if transition.kind == "samlrequest":
                snapshot = self._snapshot(self._get(transition.url))
                base_url = snapshot.url
                continue

            return snapshot

        raise MicrosoftSSOError(
            "Microsoft sign-in interstitial hop limit exceeded.",
            step="SSO interstitial walk",
            recovery="Retry the login; the upstream sign-in flow may be looping.",
        )

    # -- SAML completion -------------------------------------------------------

    def _step_post_saml(self, saml_response: str, html: str = "") -> None:
        """Step 5: POST the SAMLResponse to the D2L ACS endpoint.

        The SAML form typically has an action pointing to D2L's ACS.
        ACS MUST follow redirects because d2l cookies are set during the
        redirect chain.
        """
        acs_url = f"{BASE_URL}/d2l/lp/auth/saml/consume"
        data: dict[str, str] = {"SAMLResponse": saml_response}

        if html:
            soup = BeautifulSoup(html, "html.parser")
            form = soup.find("form")
            if form:
                action = form.get("action")
                if action:
                    acs_url = _trusted_url(
                        BASE_URL,
                        str(action),
                        _D2L_ALLOWED_HOSTS,
                        step="POST SAML",
                        label="D2L ACS",
                    )
                for inp in form.find_all("input"):
                    name = inp.get("name")
                    if name and name not in data:
                        data[str(name)] = str(inp.get("value") or "")

        self._post_with_redirects(acs_url, data=data)

        d2l_cookies = {
            cookie.name: str(cookie.value or "")
            for cookie in self._session.cookies
            if cookie.name.startswith("d2l")
            and cookie_domain_accepted(cookie.domain or "")
        }
        if not missing_cookie_names(d2l_cookies):
            return

        # Some ACS flows set cookies only after landing on /d2l/home
        home_url = f"{BASE_URL}/d2l/home"
        home_resp = self._get(home_url)
        for _ in range(8):
            if home_resp.status_code not in _REDIRECT_STATUSES:
                break
            location = home_resp.headers.get("Location", "")
            if not location:
                break
            next_home = _trusted_url(
                str(getattr(home_resp, "url", "") or home_url),
                str(location),
                _D2L_ALLOWED_HOSTS,
                step="extract cookies",
                label="D2L redirect",
            )
            home_resp = self._get(next_home)
        else:
            raise MicrosoftSSOError(
                "D2L home redirect limit exceeded.",
                step="extract cookies",
                recovery="Retry the login; the D2L redirect chain may be looping.",
            )
        d2l_cookies = {
            cookie.name: str(cookie.value or "")
            for cookie in self._session.cookies
            if cookie.name.startswith("d2l")
            and cookie_domain_accepted(cookie.domain or "")
        }
        if home_resp.status_code < 400 and not missing_cookie_names(d2l_cookies):
            return

        raise MicrosoftSSOError(
            "SAML POST to D2L ACS did not set session cookies.",
            step="POST SAML",
            recovery="SAML assertion may be expired or invalid. Try logging in again.",
        )

    def _extract_d2l_cookies(self) -> dict[str, str]:
        """Step 6: Extract D2L session cookies from the session cookie jar."""
        cookies: dict[str, str] = {}

        for cookie in self._session.cookies:
            if cookie.name.startswith("d2l") and cookie_domain_accepted(
                cookie.domain or ""
            ):
                cookie_val = cookie.value if cookie.value is not None else ""
                cookies[cookie.name] = cookie_val

        missing = missing_cookie_names(cookies)
        if missing:
            raise MicrosoftSSOError(
                f"Missing required D2L cookies after SSO: {missing}",
                step="extract cookies",
                recovery="The login may have completed but cookies were not set. "
                         "Try again or check your account status.",
            )

        return cookies

    def close(self) -> None:
        """Close the underlying requests session."""
        self._session.close()
