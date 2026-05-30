# GitHub Copilot instructions — lighthouse-cli

Python 3.11 CLI (Click + requests) for the D2L Brightspace LMS. The full review
policy is in [REVIEW.md](../REVIEW.md); agent/architecture conventions are in
[AGENTS.md](../AGENTS.md). Follow both when reviewing or generating code.

## Flag these (review focus)

- **Secrets:** never log, print, f-string, `repr`, or put into exception text a
  password, session cookie (`d2lSecureSessionVal`, `d2lSessionVal`,
  `d2lSameSiteCanaryA/B`), TOTP/OTP code, or SAML token / `SAMLResponse`.
  Credentials flow only through `CredentialStore` (Fernet + OS keyring).
- **`--json` contract:** when `--json` is passed, stdout must be valid,
  machine-parseable JSON only. All prompts, banners, logs, and `input()` prompts
  go to `sys.stderr`. Commands return exit `0` on success, `1` on error.
- **MFA:** SMS/WhatsApp codes are server-sent on `BeginAuth` (a literal `--totp`
  cannot match — use `auth login` → `auth verify`); offline Authenticator
  `PhoneAppOTP` codes are valid pre-provided. Resume a pending MFA session only
  when its saved method matches the requested method.
- **Typing:** require `from __future__ import annotations`; `X | None` (not
  `Optional`); no bare `Callable`.
- **Robustness:** wrap `subprocess`/Playwright/CDP failures into clean errors —
  no raw tracebacks. Keep dependencies pinned in `pyproject.toml`.

## Do NOT flag

- Passing `--totp` for `--mfa-method app` (offline TOTP) is correct.
- `dict[str, Any]` for raw D2L REST JSON at the API boundary is intentional.
