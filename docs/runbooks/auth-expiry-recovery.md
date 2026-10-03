# Runbook: recovering from expired D2L sessions

Audience: users and agents when commands fail with `SessionExpiredError` or
auth-related errors after previously working.

## Symptoms

- Any command exits 1 with a session/auth error.
- `lighthouse auth status --json` reports missing or stale cookies
  (`d2lSecureSessionVal` and friends).

## Recovery steps

First check whether Microsoft reported an access-policy or consent error.
These are not ordinary expired sessions, and repeating the same password or
MFA attempt does not resolve them:

- `65001`: application consent is missing; review the requested permissions
  through the organization's normal sign-in flow. This code alone does not
  mean administrator approval is mandatory.
- `90094`: administrator consent is required; ask IT to review the requested
  permissions. The CLI cannot grant consent.
- `65004`: consent was not completed. If an administrator-approval request
  was submitted, wait for its review rather than repeatedly signing in.
- `50131`, `53000`–`53003`: an access/security policy or required device/app
  condition prevented sign-in. Use the required device/app or contact IT;
  do not change security policy to make the CLI work.
- `53004`: sign-in risk blocked MFA registration; use the allowed
  registration process or contact IT.
- `50140`: complete the normal browser's **Keep me signed in** prompt.
  This is not a Terms of Use error.

Human and `--json` diagnostics preserve these codes with fixed, local
guidance. They never print Microsoft's raw error body, flow state, or URLs.
This guidance does not retry sign-in, accept consent, or change stored
credentials. See Microsoft's [error reference](https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes)
and [consent troubleshooting](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/application-sign-in-unexpected-user-consent-error).

For an ordinary missing or expired D2L session:

```sh
lighthouse auth status --json     # confirm state (never prints cookie values)
lighthouse auth login --json      # fresh Microsoft SSO flow
```

- If MFA is server-sent (SMS/WhatsApp): `auth login` returns a pending
  checkpoint; finish with `lighthouse auth verify <CODE> --json`, passing
  the code you received as the positional argument. A literal `--totp` only works for offline `PhoneAppOTP`
  (`--mfa-method app`).
- If a signed-in desktop browser exists: `lighthouse auth refresh` re-extracts
  cookies over CDP (requires the `cdp` extra and a browser started with a
  remote-debugging port).
- Last resort: `lighthouse auth import-session` with origin-bound cookie JSON
  on stdin.

## After recovery

```sh
lighthouse courses --json        # smoke test
```

## If login itself loops or fails

See the `mfa-auth-debugging` skill (`.agents/skills/mfa-auth-debugging/`) and
`docs/auth-microsoft-sso.md`. Report credential-adjacent bugs privately via
GitHub security advisories, never in a public issue.
