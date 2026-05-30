#!/usr/bin/env python3
"""Probe Microsoft MFA methods after password (no code submission).

Requires LIGHTHOUSE_USERNAME and LIGHTHOUSE_PASSWORD in the environment.
Prints registered arrUserProofs from ConvergedTFA — does not print secrets.
"""

from __future__ import annotations

import os
import sys

from lighthouse_cli.ms_auth import (
    MicrosoftSSOClient,
    MicrosoftSSOError,
    _extract_config_json,
    _extract_error_code_and_msg,
    _parse_user_proofs,
)


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
        ms_url = client._step_initiate_saml()
        ms_config = client._step_get_ms_config(ms_url)
        ms_config = client._step_prepare_username(ms_config, username)
        resp = client._step_post_credentials(
            ms_config, username, password, skip_username_prepare=True
        )
        if not client._is_mfa_page(resp) and client._is_error_page(resp):
            raise client._build_error(
                resp, *_extract_error_code_and_msg(resp.text), "POST credentials"
            )
        if not client._is_mfa_page(resp):
            print("No MFA page returned (account may not require 2FA on this login).")
            return 0
        cfg = _extract_config_json(resp.text) or {}
        proofs = _parse_user_proofs(cfg)
        print("ConvergedTFA:", "ConvergedTFA" in resp.text)
        print("BeginAuth URL:", cfg.get("urlBeginAuth", "(missing)"))
        for p in proofs:
            default = " [default]" if p.is_default else ""
            print(f"  - {p.auth_method_id}: {p.display}{default}")
        if not proofs:
            print("  (no arrUserProofs — legacy form MFA page)")
        return 0
    except MicrosoftSSOError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
