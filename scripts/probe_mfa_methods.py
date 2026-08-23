#!/usr/bin/env python3
"""Probe Microsoft MFA methods after password (no code submission).

Superseded by the CLI command ``lighthouse auth mfa-methods``; kept as a
thin diagnostic. Requires LIGHTHOUSE_USERNAME and LIGHTHOUSE_PASSWORD in the
environment. Prints registered arrUserProofs from ConvergedTFA — does not
print secrets.
"""

from __future__ import annotations

import os
import sys

from lighthouse_cli.ms_auth import MicrosoftSSOClient, MicrosoftSSOError
from lighthouse_cli.ms_mfa import MfaProbeResult


def _print_proofs(result: MfaProbeResult) -> None:
    if result.page == "no_mfa":
        print("No MFA required: sign-in completed without a verification page.")
        return
    if result.page == "legacy_form":
        print("Legacy form-based MFA page detected: 2FA IS required for this")
        print("  account, but the page exposes no arrUserProofs method list.")
        return
    if not result.proofs:
        print("ConvergedTFA page returned no registered methods (empty arrUserProofs).")
        return
    for p in result.proofs:
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
        result = client.probe_mfa_methods(username, password)
        _print_proofs(result)
        return 0
    except MicrosoftSSOError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
