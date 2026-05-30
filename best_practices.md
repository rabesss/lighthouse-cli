# Best Practices — lighthouse-cli

Repo-wide coding standards Qodo Merge checks PRs against. Canonical, fuller
rules: [`review.md`](review.md).

## Secrets handling
Never log, print, or include credentials, session cookies, TOTP codes, or SAML
tokens in messages or exceptions. Use key names, not values. Credentials flow
only through `CredentialStore` (Fernet + keyring).

## --json stdout contract
Under `--json`, write only machine-parseable JSON to stdout; route all prompts,
banners, and logs to `sys.stderr`. Print `input()` prompts to stderr first.

## MFA correctness
SMS/WhatsApp codes are server-sent on `BeginAuth` (a literal `--totp` cannot
match; use `auth login` → `auth verify`). Offline Authenticator `PhoneAppOTP`
codes are valid pre-provided. Resume a pending session only when its method
matches.

## Static typing
Use `from __future__ import annotations` and `X | None`; never a bare `Callable`.

## Subprocess / Playwright robustness
Wrap external (subprocess/Playwright/CDP) calls; surface a clean error, never a
raw traceback.

## Dependencies
Pin dependency versions in `pyproject.toml` (no open ranges); flag unpinned or
suspicious package names.
