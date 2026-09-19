---
name: mfa-auth-debugging
description: >-
  Diagnose lighthouse-cli Microsoft SSO / MFA failures (login loops, TOTP
  rejection, pending-verify resume, cookie refresh) without breaking the
  subtle method semantics. Use when auth login/verify/refresh misbehaves.
---

# MFA / auth debugging

## Invariants (do not "simplify" these)

1. SMS/WhatsApp codes are **server-sent on `BeginAuth`** — a literal
   `--totp <code>` can never match. Use the two-step
   `auth login` → `auth verify` flow.
2. Offline Authenticator TOTP (`PhoneAppOTP`) is generated on-device, so a
   pre-provided `--totp` **is** valid with `--mfa-method app`.
3. `TwoWayVoice*` and `PhoneAppNotification` are **codeless** approvals: poll
   EndAuth without `AdditionalAuthData`.
4. Resume a pending MFA session only when its saved method matches the
   requested method.

## Triage flow

```sh
lighthouse auth status --json      # cookie presence/age, no network
lighthouse auth login --json       # starts SSO; may return mfa_pending
lighthouse auth verify --json --totp <code>   # completes server-sent codes
lighthouse auth mfa-methods --json           # lists methods, triggers nothing
```

- **`SessionExpiredError` mid-flow**: cookies are origin-bound; check
  `~/.config/lighthouse/cookies.json` age via `auth status`. Never print cookie
  values.
- **Loop between KMSI/SAML pages**: the state machine lives in
  `ms_auth.py` (`_step_*` methods); reproduce with the recorded fixtures in
  `tests/test_ms_auth*.py` before touching logic.
- **Pending checkpoint mismatch**: `CredentialStore` seals the pending state;
  a method mismatch must abort, not overwrite.

## Debugging rules

- All diagnostics go to stderr (never stdout on `--json` paths).
- Never log cookies, TOTP codes, or SAML tokens; refer to them by key name.
- Wrap any new subprocess/Playwright/CDP failure in `MicrosoftSSOError` with a
  recovery hint; no raw tracebacks to users.

## Deep background

- `docs/auth-microsoft-sso.md` — protocol walk-through.
- `docs/adr/0002-headless-browser-auth-with-flexible-2fa.md` — design decisions.
- `docs/auth-login-handoff.md` — the login→verify hand-off contract.
