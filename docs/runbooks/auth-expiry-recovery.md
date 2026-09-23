# Runbook: recovering from expired D2L sessions

Audience: users and agents when commands fail with `SessionExpiredError` or
auth-related errors after previously working.

## Symptoms

- Any command exits 1 with a session/auth error.
- `lighthouse auth status --json` reports missing or stale cookies
  (`d2lSecureSessionVal` and friends).

## Recovery steps

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
