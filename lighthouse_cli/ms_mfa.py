"""MFA data types and selection logic for Microsoft SSO."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from lighthouse_cli.ms_errors import (
    MFA_AUTH_APP_NOTIFY,
    MFA_AUTH_APP_OTP,
    MFA_AUTH_SMS,
    MFA_AUTH_VOICE_ALT_MOBILE,
    MFA_AUTH_VOICE_MOBILE,
    MFA_AUTH_VOICE_OFFICE,
    MFA_METHOD_AUTH_IDS,
    MFA_METHOD_AUTO,
    MFA_METHOD_CHOOSE,
    MicrosoftSSOError,
)
from lighthouse_cli.ms_session import _mask_phone_hint


_PROOF_METHOD_LABELS = {
    MFA_AUTH_SMS: "Text code (SMS or WhatsApp)",
    MFA_AUTH_APP_OTP: "Microsoft Authenticator code",
    MFA_AUTH_APP_NOTIFY: "Microsoft Authenticator approval",
    MFA_AUTH_VOICE_MOBILE: "Voice call to mobile",
    MFA_AUTH_VOICE_ALT_MOBILE: "Voice call to alternate mobile",
    MFA_AUTH_VOICE_OFFICE: "Voice call to office phone",
}

_MAX_PROOF_DATA_LENGTH = 256
_MASKED_PHONE_RE = re.compile(r"\*{3}\d{4}\Z")


@dataclass(frozen=True)
class UserProof:
    """A registered MFA method on the user's Microsoft account."""

    auth_method_id: str
    display: str
    data: str
    is_default: bool


@dataclass(frozen=True)
class MfaProbeResult:
    """Outcome of probing for MFA after the password step.

    ``page`` distinguishes the landing page: ``"no_mfa"`` (sign-in completed
    without any verification page), ``"legacy_form"`` (a form-based MFA page
    that carries no arrUserProofs list — 2FA IS required) or ``"converged"``
    (ConvergedTFA page with parsed ``proofs``).
    """

    page: str
    proofs: list[UserProof]


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


def _proof_method_label(proof: UserProof) -> str:
    """Return a static, safe label for a Microsoft auth method id."""
    auth_id = proof.auth_method_id if isinstance(proof.auth_method_id, str) else ""
    return _PROOF_METHOD_LABELS.get(
        auth_id,
        "Other verification method",
    )


def safe_auth_method_id(proof: UserProof) -> str:
    """Return a known method id or the fixed ``other`` category."""
    candidate = proof.auth_method_id if isinstance(proof.auth_method_id, str) else ""
    for auth_id in _PROOF_METHOD_LABELS:
        if candidate == auth_id:
            return auth_id
    return "other"


def _masked_proof_destination(proof: UserProof) -> str | None:
    """Return only the existing strong phone mask, never upstream display text.

    ``UserProof.data`` is also upstream-controlled, so the shared phone helper
    is accepted only when it produced its fixed ``***1234`` shape.  In
    particular, the helper's short-input fallback is rejected rather than
    echoed (which prevents malformed phone/email values from becoming output).
    """
    data = proof.data
    if not isinstance(data, str) or not data or len(data) > _MAX_PROOF_DATA_LENGTH:
        return None
    digits = re.sub(r"\D", "", data)
    if len(digits) < 4:
        return None
    masked = _mask_phone_hint(data)
    return masked if _MASKED_PHONE_RE.fullmatch(masked) else None


def safe_proof_destination(proof: UserProof) -> str:
    """Return a fixed masked destination or a non-identifying placeholder."""
    return _masked_proof_destination(proof) or "your phone"


def format_user_proof(proof: UserProof) -> str:
    """Describe an MFA proof without rendering Microsoft-provided text.

    ``arrUserProofs[].display`` is server-controlled and may contain a full
    phone number, email address, or terminal-control sequence.  It is kept on
    ``UserProof`` for flow compatibility, but all user-facing descriptions use
    the static method label and, when possible, only a fixed last-four mask
    derived from ``data``.
    """
    method = _proof_method_label(proof)
    destination = _masked_proof_destination(proof)
    return f"{method}: {destination}" if destination else method


def _prompt_user_proof_choice(proofs: list[UserProof]) -> UserProof:
    """Interactively pick one of several registered MFA methods."""
    if len(proofs) == 1:
        return proofs[0]
    if not sys.stdin.isatty():
        raise MicrosoftSSOError(
            "Multiple MFA methods are available; pick one with --mfa-method sms|app|call|push.",
            step="MFA",
            recovery="Re-run with --mfa-method or use a single-method account.",
        )
    print("\nChoose a verification method:", flush=True, file=sys.stderr)
    for idx, proof in enumerate(proofs, start=1):
        default = " (Microsoft default)" if proof.is_default else ""
        print(f"  {idx}) {format_user_proof(proof)}{default}", flush=True, file=sys.stderr)
    while True:
        print(f"Enter 1\u2013{len(proofs)} [1]: ", end="", flush=True, file=sys.stderr)
        choice = input().strip() or "1"
        if choice.isdigit() and 1 <= int(choice) <= len(proofs):
            return proofs[int(choice) - 1]
        print("Invalid choice, try again.", flush=True, file=sys.stderr)


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
        available = ", ".join(format_user_proof(proof) for proof in proofs)
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
