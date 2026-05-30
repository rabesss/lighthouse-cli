# Headless browser authentication with flexible 2FA input

> **Update (2026-05) — partially superseded.** `auth login` is now a
> **pure-HTTP Microsoft SSO** flow (`ms_auth.py`); a headless browser is used
> only to bootstrap the username step and for `auth refresh` cookie extraction,
> not for the whole login. The flexible-2FA rationale below still holds
> (interactive, `--totp`, stdin pipe, env var), and offline Authenticator TOTP
> now completes in one step. See
> [auth-microsoft-sso.md](../auth-microsoft-sso.md).

The CLI supports a full headless login flow (`auth login`) through Microsoft SSO + 2FA, not just cookie extraction from an existing browser (`auth refresh`). This is necessary because server-based agents have no local browser to extract cookies from, and D2L sessions expire every ~5 days behind Manipal's Azure AD with mandatory 2FA.

The 2FA code is accepted through multiple input paths: interactive prompt, `--totp` flag, stdin pipe, or env var. Credentials are stored encrypted at rest using system keyring. The alternative — automated TOTP generation from a stored secret — was considered but deferred to avoid requiring users to export their authenticator secrets. The multi-path input approach supports all use cases: interactive users, local agents with browser access, and remote agents that receive 2FA codes via chat interfaces (openclaw, hermes).

Status: accepted

Considered options:
- Cookie ferrying from local machine to server (external ops concern, not CLI's job)
- Automated TOTP from stored secret (deferred — security trade-off, user may not want secrets on server)
- Interactive 2FA only (rejected — doesn't support unattended agent workflows)
