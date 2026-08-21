#!/usr/bin/env python3
"""Probe Microsoft MFA methods after password (no code submission).

Requires LIGHTHOUSE_USERNAME and LIGHTHOUSE_PASSWORD in the environment.
Prints registered arrUserProofs from ConvergedTFA — does not print secrets.
"""

from __future__ import annotations

import os
import sys

from lighthouse_cli.ms_auth import MicrosoftSSOClient, MicrosoftSSOError, UserProof


def _print_proofs(proofs: list[UserProof]) -> None:
    if not proofs:
        print("No MFA page returned (account may not require 2FA on this login).")
        print("  (no arrUserProofs — legacy form MFA page)")
        return
    for p in proofs:
        default = " [default]" if p.is_default else ""
        print(f"  - {p.auth_method_id}: {p.display}{default}")


def main() -> int:
    username = os.getenv("LIGHTHOUSE_USERNAME", "").strip()
    password = os.getenv("LIGHTHOUSE_PASSWORD", "").strip()
    if not username or not password:
        print(
            "Set LIGHTHOUSE_USERNAME and LIGHTHOUSE_PASSWORD to probe MFA methods.",
            file=sys.stderr,
        )
        return 2

    client = MicrosoftSSOClient()
    try:
        proofs = client.probe_mfa_methods(username, password)
        _print_proofs(proofs)
        return 0
    except MicrosoftSSOError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
