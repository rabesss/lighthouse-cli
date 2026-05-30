"""MFA data types and selection logic for Microsoft SSO."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from lighthouse_cli.ms_errors import (
    MFA_METHOD_AUTH_IDS,
    MFA_METHOD_AUTO,
    MFA_METHOD_CHOOSE,
    MicrosoftSSOError,
)


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
    print("\nChoose a verification method:", flush=True, file=sys.stderr)
    for idx, proof in enumerate(proofs, start=1):
        default = " (Microsoft default)" if proof.is_default else ""
        print(f"  {idx}) {proof.display}{default}", flush=True, file=sys.stderr)
    while True:
        choice = input(f"Enter 1\u2013{len(proofs)} [1]: ").strip() or "1"
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
