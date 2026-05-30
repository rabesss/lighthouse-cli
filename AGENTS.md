# AGENTS.md

Guidance for AI coding agents and AI code reviewers working in this repository
(OpenAI Codex, Google Jules, and any agent that reads `AGENTS.md`). Human
contributors should also follow this. The PR-review charter that the review
bots enforce lives in [`review.md`](review.md).

## Project

`lighthouse-cli` is a Python 3.11 command-line tool (Click + requests) for the
D2L Brightspace LMS at `lighthouse.manipal.edu`. It talks to the D2L REST API
directly. Authentication is a **pure-HTTP Microsoft Entra (Azure AD) SSO** flow;
a headless browser is only used to bootstrap the username step and (optionally)
to extract cookies for `auth refresh`.

## Setup, build, test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[auth,credentials]'
playwright install chromium   # only needed for the username bootstrap
pytest -q                     # full suite must stay green before a PR
```

There is no separate build step. Lint with `ruff` if available.

## Architecture (current)

- `lighthouse_cli/cli.py` — Click command wiring (entry point).
- `lighthouse_cli/commands.py` — command implementations (data fetch, format,
  `--json` vs human output).
- `lighthouse_cli/api.py` — `LighthouseClient` (D2L REST client, cookie mgmt,
  course-id resolution, CDP cookie extraction for `auth refresh`).
- `lighthouse_cli/auth.py` — `cmd_auth_login` / `cmd_auth_verify` and
  `CredentialStore` (Fernet + OS keyring).
- **Microsoft SSO is split into focused modules** (do not re-monolithize):
  `ms_auth.py` (`MicrosoftSSOClient`), `ms_parse.py` (HTML/`$Config` parsing),
  `ms_session.py` (cookie/session helpers), `ms_mfa.py` (MFA proof selection),
  `ms_errors.py` (exceptions + constants).
- `manifest.py`, `config.py`, `show.py`, `display.py`, `submit.py`,
  `assignments.py`, `course_config.py`, `utils.py` — supporting modules.

## Conventions agents MUST follow

- **Secrets:** NEVER log, print, `repr`, f-string, or put into exception text any
  password, session cookie (`d2lSecureSessionVal`, `d2lSessionVal`,
  `d2lSameSiteCanaryA/B`), TOTP/OTP code, or SAML token / `SAMLResponse`.
  Credentials flow only through `CredentialStore` (Fernet + keyring); refer to
  secrets by key name, never by value. Never commit `.env*` credential files.
- **`--json` contract:** when `--json` is passed, **stdout must be valid,
  machine-parseable JSON only**. All prompts, banners, progress, logs, and
  errors go to `sys.stderr`. `input()` writes its prompt to stdout — print the
  prompt to `sys.stderr` first, then call `input()` with no argument. Commands
  return exit code `0` on success, `1` on error.
- **MFA semantics (subtle — do not "simplify" away):** SMS/WhatsApp codes are
  **server-sent on `BeginAuth`**, so a literal `--totp <code>` cannot match —
  use the two-step `auth login` → `auth verify` flow. Offline Authenticator
  TOTP (`PhoneAppOTP`) is generated on-device, so a pre-provided `--totp` **is**
  valid for `--mfa-method app`. Resume a pending MFA session only when its saved
  method matches the requested method.
- **Typing:** keep `from __future__ import annotations`; use `X | None` (not
  `Optional[X]`); never use a bare `Callable` (parameterize it).
- **External processes:** wrap `subprocess`, Playwright, and CDP/websocket
  failures and raise a clean `MicrosoftSSOError`-style message to stderr — never
  leak a raw traceback to the user.
- **Dependencies:** keep versions pinned/bounded in `pyproject.toml`; do not add
  unpinned or unfamiliar (possible typosquat) packages.
- **Tests:** add or update tests under `tests/` for any behavior change; mirror
  the existing pytest + `unittest.mock` / Click `CliRunner` patterns.

## Review guidelines

(OpenAI Codex reads this section directly. Place stricter, path-scoped rules in
a nested `AGENTS.md` if needed — the closest file to a changed file wins.)

- **P0** — Flag any code path that could log, print, or embed a credential
  (password, cookie, TOTP, SAML token) in output or an exception.
- **P0** — Flag any `print()` / `click.echo()` of human text to stdout on a
  `--json` path (including `input()` prompts); these must go to stderr.
- **P1** — Flag MFA logic that treats a literal `--totp` as usable for the
  server-sent SMS/WhatsApp path, or that conflates SMS with offline `PhoneAppOTP`.
- **P1** — Flag missing `from __future__ import annotations`, use of
  `Optional[...]`, or bare `Callable`.
- **P1** — Flag unwrapped subprocess/Playwright/CDP calls that can surface raw
  tracebacks.
- **P1** — Flag new or loosened dependency ranges in `pyproject.toml`.
