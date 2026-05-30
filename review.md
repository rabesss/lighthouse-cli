# Code Review Charter — lighthouse-cli

This is the **single source of truth** for what AI and human reviewers should
check on every pull request. The committed bot configs derive their rules from
it:

| Reviewer | How it reads these rules |
|----------|--------------------------|
| OpenAI Codex (`chatgpt-codex-connector`) | [`AGENTS.md`](AGENTS.md) → `## Review guidelines` |
| Google Jules | [`AGENTS.md`](AGENTS.md) (conventions) |
| Gemini Code Assist | [`.gemini/styleguide.md`](.gemini/styleguide.md) + [`.gemini/config.yaml`](.gemini/config.yaml) |
| CodeRabbit | [`.coderabbit.yaml`](.coderabbit.yaml) → `reviews.path_instructions` |
| Qodo Merge | [`.pr_agent.toml`](.pr_agent.toml) + [`best_practices.md`](best_practices.md) |
| Socket Security | [`socket.yml`](socket.yml) (supply-chain only) |
| Kilo Code | **dashboard-only** — paste this file's rules at <https://app.kilo.ai/code-reviews/review-md> |
| Pullfrog | **dashboard-only** — paste this file's rules into Console → Modes → Review instructions |

> Kilo and Pullfrog do not read a committed file for their GitHub reviews; this
> `review.md` is the version-controlled text to paste into their dashboards.

Severity: **P0 / Critical** = block merge; **P1 / High** = must address;
**P2 / Medium** = should address.

## P0 — Secrets & credentials

- Never log, print, `repr`, f-string, or include in exception/traceback text any
  **password, session cookie** (`d2lSecureSessionVal`, `d2lSessionVal`,
  `d2lSameSiteCanaryA`, `d2lSameSiteCanaryB`), **TOTP/OTP code**, or **SAML
  token / `SAMLResponse`**.
- Credentials must flow only through `CredentialStore` (Fernet + OS keyring).
  Never write secrets to disk in plaintext or to stdout/stderr. Redact by key
  name, never by value.
- No credential files (`.env*`, `credentials*.env`) may be committed.

## P0 — `--json` stdout contract

- When `--json` is passed, **stdout must contain only valid, machine-parseable
  JSON**. All prompts, banners, progress, logs, and errors go to `sys.stderr`.
- `input(prompt)` writes its prompt to stdout — print the prompt to
  `sys.stderr` first, then call `input()` with no argument. `getpass` already
  prompts on stderr.
- Commands return exit code `0` on success, `1` on error.

## P1 — MFA correctness

- SMS/WhatsApp codes are **server-sent on `BeginAuth`**; a literal `--totp`
  cannot match. Flag code that pre-supplies an SMS code instead of using the
  two-step `auth login` → `auth verify` flow.
- Offline Authenticator TOTP (`PhoneAppOTP`) is device-generated; a pre-provided
  `--totp` **is** valid for `--mfa-method app`. Do not flag that as wrong.
- Resume a pending MFA session only when its saved `mfa_method` matches the
  requested method (an explicit `--mfa-method app` must not hijack a stale SMS
  pending session).

## P1 — Static typing

- Require `from __future__ import annotations`. Prefer `X | None` over
  `Optional[X]`. Never use a bare `Callable` — parameterize it
  (`Callable[..., T]`).

## P1 — External-process robustness

- Wrap `subprocess`, Playwright, and CDP/websocket calls so external failures
  raise a clean `MicrosoftSSOError`-style message to stderr — never a raw
  traceback to the user.

## P2 — Dependencies / supply-chain

- Keep dependency versions pinned/bounded in `pyproject.toml`. Flag new
  unpinned/loosely-ranged deps or unfamiliar package names (typosquat risk).

## What NOT to flag (avoid false positives)

- Passing `--totp` for `--mfa-method app` (offline TOTP) is correct.
- `dict[str, Any]` for raw D2L REST JSON at the API boundary is intentional.
- Reading external JSON fields with `.get(...)` (optional fields) is correct.
