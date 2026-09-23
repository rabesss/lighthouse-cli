## Summary

What this PR changes and why. Reference the issue ("Closes #N") when applicable.

## Changes

-

## Reviewer checklist

- [ ] `pytest -q` passes locally (full suite green).
- [ ] `ruff check .` is clean.
- [ ] `mypy` (strict) is clean.
- [ ] `--json` stdout contract preserved: stdout is machine-parseable JSON
      only; prompts/banners go to stderr (AGENTS.md).
- [ ] No credentials, session cookies, TOTP codes, or SAML tokens in code,
      logs, tests, or exception text.
- [ ] MFA semantics untouched or explicitly justified (SMS/WhatsApp codes are
      server-sent; `PhoneAppOTP` is offline; codeless approvals never send
      `AdditionalAuthData`).
- [ ] New/changed behaviour has tests under `tests/`.
- [ ] Dependencies stay pinned/bounded in `pyproject.toml`.
