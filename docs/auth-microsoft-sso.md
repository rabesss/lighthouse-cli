# Microsoft SSO authentication

How `lighthouse auth login` works for the MAHE tenant (`lighthouse.manipal.edu` → Azure AD SAML).

## Why this design

**HTTP-first SSO** replays Microsoft’s real endpoints (`$Config`, SAS MFA, SAML ACS). That is fast, scriptable, and matches how tools like [saml2aws](https://github.com/Versent/saml2aws) authenticate Azure AD.

**Headless Playwright is used only for the username “Next” step** on this tenant. Pure HTTP can post the password, but Microsoft does not set the `esctx-*` cookies the tenant expects until the username step runs in a browser context. Playwright fills `loginfmt`, clicks Next, exports cookies into the `requests` session, then closes.

**Two CLI commands for SMS MFA** (`login` then `verify`) because each `auth login` calls `BeginAuth`, which **sends a new code**. Completing MFA in a second command (or one interactive TTY session) keeps the same `SessionId` / `FlowToken` as the SMS you received.

## Architecture

| Step | Mechanism | Why |
|------|-----------|-----|
| D2L SAML init | HTTP | `GET /d2l/lp/auth/saml/login` → redirect to Microsoft |
| Load login page, parse `$Config` | HTTP | Flow tokens (`sFT`, `sCtx`, `canary`, `urlPost`) |
| Username “Next” | **Playwright** (optional `[auth]` extra) | Sets `esctx-*` cookies; HTTP mirror exists as fallback |
| Password | HTTP | `POST urlPost` with synced tokens |
| Send SMS (`BeginAuth`) | HTTP | May stop here and save `mfa_pending.json` |
| Submit OTP (`EndAuth`) | HTTP | OTP in `AdditionalAuthData` only here |
| Continue sign-in (`ProcessAuth`) | HTTP | Tokens only — **no `otc`** (see below) |
| “Stay signed in” (KMSI) | HTTP | POST to `/appverify` with `canary` + `hpgrequestid` |
| SAML → D2L | HTTP | POST `SAMLResponse` to ACS with redirects enabled |

## Commands

### Install (Arch / PEP 668: use a venv)

```bash
cd lighthouse-cli
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[auth,credentials,dev]'
playwright install chromium   # once, for username bootstrap
```

Environment (optional): `LIGHTHOUSE_USERNAME`, `LIGHTHOUSE_PASSWORD`, `LIGHTHOUSE_MFA_METHOD` (`auto` | `sms` | `app` | `choose`).

### SMS / WhatsApp (two-step — recommended for agents)

```bash
lighthouse auth login --mfa-method sms
# Wait for: "Verification code sent."

lighthouse auth verify 123456   # code from THAT message only
lighthouse auth status
```

If verify fails after MFA succeeded but before cookies were saved, run **`auth verify` again once** before starting a new `login` (KMSI checkpoint may be saved in `mfa_pending.json`).

### Interactive terminal (one process)

```bash
lighthouse auth login --mfa-method sms
# Prompts for the code after it is sent
```

### Pipe OTP after BeginAuth (same session)

```bash
lighthouse auth login --mfa-method sms --totp -
# Type code when prompted (after "code sent")
```

## MFA session file

`~/.config/lighthouse-cli/mfa_pending.json` (mode `0600`) holds:

- Session cookies, `BeginAuth` response (`SessionId`, `FlowToken`, `Ctx`)
- MFA config URLs and selected proof (`OneWaySMS`, etc.)
- Checkpoints: `end_auth_flow` / `end_auth_ctx` after OTP accepted; `kmsi_checkpoint` before “Stay signed in”

Cleared only after D2L cookies are extracted successfully. If `auth verify` fails, the pending file is removed so a stale `end_auth_flow` checkpoint cannot block the next attempt — run `auth login` again for a new code.

## Protocol details (why the payloads look this way)

### EndAuth vs ProcessAuth

- **EndAuth** (JSON): includes `AdditionalAuthData` = your 6-digit code.
- **ProcessAuth** (form): `flowToken`, `request` (Ctx), `login`, `canary` only — same pattern as saml2aws. Sending `otc` again here makes Microsoft return the MFA page even when EndAuth succeeded.

### KMSI / `appverify`

After ProcessAuth, Microsoft may show “Stay signed in” (`CmsiInterrupt`). The client POSTs to `$Config.urlPost` (often `/appverify`) with:

- `flowToken`, `ctx`, `LoginOptions=1`
- `canary`, `hpgrequestid` (from `sessionId`), `login` / `loginfmt`

Without `canary` and `hpgrequestid`, Azure returns `AADSTS165000` (missing user-context tokens).

### SAML ACS

POST `SAMLResponse` (and `RelayState` from the HTML form) to D2L’s ACS with **`allow_redirects=True`** so `d2lSecureSessionVal`, `d2lSessionVal`, and SameSite canaries are set on the redirect chain.

## What we intentionally do not do

| Approach | Why not |
|----------|---------|
| Full-browser SSO for the whole flow | Slow, brittle for agents/CI; HTTP covers MFA and SAML. |
| `auth login --totp CODE` for SMS | Starts a new `BeginAuth` and invalidates the code from the previous run. |
| `pip install` into Arch system Python | PEP 668 / broken Playwright paths — use a project venv. |
| Second `auth login` while waiting for OTP | Same as above: new SMS, old code useless. |

## Files

| Path | Role |
|------|------|
| `lighthouse_cli/ms_auth.py` | SSO + MFA + SAML implementation |
| `lighthouse_cli/auth.py` | CLI orchestration, credential store |
| `lighthouse_cli/config.py` | `cookies.json`, `mfa_pending.json` |
| `scripts/probe_mfa_methods.py` | List ConvergedTFA proofs after password (debug) |

## Verification

End-to-end on MAHE tenant (2026-05): `auth login --mfa-method sms` → `auth verify` → `auth status` reports valid session and all four D2L cookies.
