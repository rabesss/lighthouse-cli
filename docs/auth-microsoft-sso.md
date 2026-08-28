# Microsoft SSO authentication

How `lighthouse auth login` works for the MAHE tenant (`lighthouse.manipal.edu` → Azure AD SAML).

## Why this design

**HTTP-first SSO** replays Microsoft’s real endpoints (`$Config`, SAS MFA, SAML ACS). That is fast, scriptable, and matches how tools like [saml2aws](https://github.com/Versent/saml2aws) authenticate Azure AD.

**Headless Playwright is used only for the username “Next” step** on this tenant. Playwright fills `loginfmt`, clicks Next, exports cookies into the `requests` session, then closes. If the Playwright runtime or Chromium cannot start, the client warns on stderr and falls back to the mirrored HTTP sequence. Once Chromium launches, navigation/selector/page-shape failures remain clean errors rather than being hidden by the fallback.

**One interactive command for people, two resumable commands for automation.**
In a terminal, `lighthouse auth login` keeps the same process alive while the
user chooses a registered method and enters the code or approves the request.
For non-interactive SMS MFA, `login` then `verify` preserves the same
`SessionId` / `FlowToken`; a second `login` would send a new code.

## Architecture

| Step | Mechanism | Why |
|------|-----------|-----|
| D2L SAML init | HTTP | `GET /d2l/lp/auth/saml/login` → redirect to Microsoft |
| Load login page, parse `$Config` | HTTP | Flow tokens (`sFT`, `sCtx`, `canary`, `urlPost`) |
| Username “Next” | **Playwright** (optional `[auth]` extra) | Sets `esctx-*` cookies; HTTP mirror exists as fallback |
| Password | HTTP | `POST urlPost` with synced tokens |
| Session-pull interstitial | HTTP | Re-POST echoed `oPostParams` to `urlPost` (see below) |
| Start MFA (`BeginAuth`) | HTTP | Sends SMS or starts a voice/push approval; may save `mfa_pending.json` |
| Complete MFA (`EndAuth`) | HTTP | Code for SMS/app OTP; codeless polling for voice/push |
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

Environment (optional): `LIGHTHOUSE_USERNAME`, `LIGHTHOUSE_PASSWORD`, `LIGHTHOUSE_MFA_METHOD` (`auto` | `sms` | `app` | `call` | `push` | `choose`).

### Interactive terminal (recommended for people)

```bash
lighthouse auth login
```

After the password is accepted, Microsoft returns the verification methods
registered on that account. If there is more than one, the CLI shows only
those methods and asks the user to choose. It then collects the fresh code or
waits for the approval, saves and verifies the D2L session, and shows useful
next commands.

### Inspect available methods without starting a challenge

```bash
lighthouse auth mfa-methods            # human output
lighthouse auth mfa-methods --json     # {"success": true, "page": "converged", "methods": [...]}
```

Performs a real sign-in through the post-password stage and may submit a
KMSI/CMSI continuation, but stops before `BeginAuth`: it sends no SMS, places
no call, and triggers no Authenticator notification. It lists each method with
the `--mfa-method` spelling that selects it:

| `authMethodId` | `--mfa-method` | Code source |
|----------------|----------------|-------------|
| `OneWaySMS` | `sms` | Server-sent text (SMS or WhatsApp — Microsoft picks) |
| `TwoWayVoiceMobile` / `TwoWayVoiceAlternateMobile` / `TwoWayVoiceOffice` | `call` | Codeless call — answer and press # |
| `PhoneAppOTP` | `app` | Offline Authenticator TOTP |
| `PhoneAppNotification` | `push` | Codeless — approve in Microsoft Authenticator |

SMS uses the two-step flow because its fresh code arrives only after
`BeginAuth`; a literal `--totp` is rejected, while `--totp -` reads the new
code after the challenge. `call` and `push` are codeless and reject all
`--totp` forms. Offline `PhoneAppOTP` accepts a literal code.

### SMS / WhatsApp (two-step — for agents, scripts, and recovery)

```bash
lighthouse auth login --mfa-method sms
# Wait for: "Verification code sent."

lighthouse auth verify 123456   # code from THAT message only
lighthouse auth status
```

If verify fails after MFA succeeded but before cookies were saved, run **`auth verify` again once** before starting a new `login` (KMSI checkpoint may be saved in `mfa_pending.json`).

### Pipe OTP after BeginAuth (same session)

```bash
lighthouse auth login --mfa-method sms --totp -
# Type code when prompted (after "code sent")
```

### Offline Authenticator (app TOTP) — one step

```bash
lighthouse auth login --mfa-method app --totp 123456
```

`PhoneAppOTP` codes are generated on-device, so (unlike SMS) a pre-provided
`--totp` is valid and login completes in a single command — no `verify` step.
An explicit `--mfa-method app` starts a fresh app flow and will not resume a
stale SMS `mfa_pending.json`.

### Voice call — codeless approval

```bash
lighthouse auth login --mfa-method call
# Answer the phone and press #, then let the saved session poll:
lighthouse auth verify ok
```

`TwoWayVoice*` never submits `AdditionalAuthData`; `verify ok` is only the
mechanical trigger that resumes EndAuth polling for the approval.

### Push approval — codeless

```bash
lighthouse auth login --mfa-method push
# Start polling; stderr prints the number match when Microsoft returns it:
lighthouse auth verify ok
```

`PhoneAppNotification` never sends an `AdditionalAuthData` code. `verify ok`
starts bounded EndAuth polling and prints number matching to stderr even under
`--json`/non-TTY use. Complete approval promptly; after the bounded polling
window expires, start a fresh login challenge.

## MFA session file

`~/.config/lighthouse-cli/mfa_pending.json` (mode `0600`) seals everything
secret via `CredentialStore` (Fernet envelope; see README "Encryption key
sources"). Sealed inside:

- Session cookies, `BeginAuth` response (`SessionId`, `FlowToken`, `Ctx`)
- MFA config URLs and selected proof (`OneWaySMS`, etc.)
- Checkpoints: `end_auth_flow` / `end_auth_ctx` after OTP accepted; `kmsi_checkpoint` before “Stay signed in”

Only `created_at` and `mfa_method` remain as plaintext metadata beside the
ciphertext.

Cleared only after D2L cookies are extracted successfully. If `auth verify` fails, the pending file is removed so a stale `end_auth_flow` checkpoint cannot block the next attempt — run `auth login` again for a new code.

## Protocol details (why the payloads look this way)

### The Aug-2026 session-pull interstitial (`sso_reload`)

Since August 2026 the password POST for this tenant returns not the
MFA/error page but an HTTP 200 “Redirecting” page with **no forms**:

- `$Config.urlPost` → `/<tenant>/login?…&sso_reload=True`
- `$Config.oPostParams` → the entire credential form echoed back
  (including `passwd`), plus `iSessionPullType=2`, `slMaxRetry=2`

The browser’s JavaScript re-POSTs those params to `urlPost`; the real page
(`ConvergedSignIn` error, `ConvergedTFA`, KMSI, or SAML) comes back on that
second hop. Both old and new pure-HTTP clients originally lacked the hop,
which is why logins began failing with “POST credentials (unexpected
response)” — an **upstream Microsoft change**, not a regression (verified by
running the pre-change code against the live tenant: identical failure).

The client detects the markers (`is_sso_reload_page`), re-POSTs the echoed
params through the same bounded walk used everywhere else
(`_MAX_SSO_RELOADS=2`, a conservative client-side safety cap), and the
flow recorder logs field **names only** — `oPostParams` echoes the password,
so values never reach any log.

### EndAuth vs ProcessAuth

- **EndAuth** (JSON): includes `AdditionalAuthData` only for OneWaySMS and PhoneAppOTP; voice/push poll without it.
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
| `scripts/probe_mfa_methods.py` | Legacy debug probe — superseded by `auth mfa-methods` |

## Verification

End-to-end on MAHE tenant (2026-05): `auth login --mfa-method sms` → `auth verify` → `auth status` reports valid session and all four D2L cookies.

Aug-2026 upstream change (session-pull interstitial): verified with a bounded wrong-password probe that the fixed client traverses the `sso_reload` hop and surfaces the clean `Authentication failed: [50126] Invalid username or password.` error, exactly as before Microsoft’s change; the flow log shows the password POST, the interstitial (`oPostParams=1 sso_reload=1`), and the re-POST with field names only.
