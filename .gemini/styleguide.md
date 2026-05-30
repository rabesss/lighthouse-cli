# lighthouse-cli Review Style Guide (Python 3.11, Click + requests)

This guide expands Gemini Code Assist's review. The canonical rules live in
[`REVIEW.md`](../REVIEW.md); the highest-priority ones are repeated here.

## Secrets & credentials (CRITICAL)

- Flag any logging, printing, f-string, `repr`, or exception message that could
  emit passwords, session cookies (`d2lSecureSessionVal`, `d2lSessionVal`,
  `d2lSameSiteCanaryA/B`), TOTP/OTP codes, or SAML tokens / `SAMLResponse`.
- Fernet keys and keyring secrets must never be echoed, committed, or written to
  stdout/stderr. Credentials flow only through `CredentialStore`; redact by key
  name.

## Machine-parseable stdout (HIGH)

- When `--json` is passed, stdout MUST be valid JSON only. Flag any `print()` /
  `click.echo()` of human text (prompts, banners, progress, logs) to stdout on a
  `--json` path — these must go to `sys.stderr`. Remember `input(prompt)` writes
  its prompt to stdout; the prompt must be printed to stderr first.

## MFA correctness (HIGH)

- SMS/WhatsApp codes are server-sent on `BeginAuth`; a literal `--totp` cannot
  match — flag code paths that try to pre-supply SMS codes instead of the
  two-step `auth login` → `auth verify` flow.
- Offline Authenticator TOTP (`PhoneAppOTP`) is device-generated; a pre-provided
  `--totp` IS valid for `--mfa-method app`. Do not flag that. Resuming a pending
  MFA session is only valid when its saved method matches the requested method.

## Static typing (MEDIUM)

- Require `from __future__ import annotations`; prefer `X | None` over
  `Optional[X]`; reject bare `Callable` (require parameterized signatures).

## External-process robustness (MEDIUM)

- Playwright, CDP websocket, Node fallback, and `subprocess` calls must wrap
  failures and surface clean errors to stderr — no raw tracebacks to the user.

## Dependencies / supply-chain (MEDIUM)

- Dependencies must be pinned/bounded in `pyproject.toml`; flag new unpinned deps
  or unfamiliar package names (typosquat risk).
