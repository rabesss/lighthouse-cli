# Unattended sign-in with a stored authenticator

An opt-in way for an agent or script to keep using `lighthouse` past the
~5-day session limit without a person entering the password and a second
factor.

## Why it is needed

- The D2L session lasts about 5 days.
- MAHE's Microsoft sign-in frequency is also about 5 days: even with "Stay
  signed in", Microsoft asks for the password and a second factor again.
- So the CLI has to complete MFA itself. It can do that only with an
  authenticator app's time-based code (`PhoneAppOTP`), the one MFA path where a
  code the server did not send is valid. SMS, voice and push always need a
  person.

## How it works

- `lighthouse auth totp-setup` seals the secret of an authenticator app
  registered on your Microsoft account. It is stored next to your saved
  username and password, bound to that username.
- When a read (GET or HEAD, on the built-in Lighthouse connection) finds the
  session expired or the cookies missing, the CLI signs in once. It uses the
  pure-HTTP flow with `--mfa-method app`, then retries the request once. A
  notice goes to stderr; stdout carries only the command's normal output.
- D2L answers some reads of an expired session with 403 ("Permission
  denied") instead of 401. On a 403, the CLI asks `whoami` once; the 403
  counts as an expired session only if `whoami` is signed out too. Any other
  answer keeps the 403.
- The code is generated only after Microsoft starts the challenge
  (`BeginAuth`). It is submitted once and never shown.
- `lighthouse auth login` uses the stored authenticator when you give no
  `--totp` and no `--mfa-method` (or `--mfa-method app`), and the username
  matches the one the secret is bound to. It prints "Using the stored
  authenticator for verification." to stderr and never prompts for a code.
- One sign-in runs at a time. Other `lighthouse` processes wait for it (up to
  2 minutes) and then use the new session instead of signing in again.

**What never signs in automatically:**

- uploads and other writes (`submit`, quiz answers, any POST/PUT/DELETE);
- reads marked unsafe to repeat, and the quiz-attempt flows;
- connections other than the built-in Lighthouse one.

These fail with "Session expired" as before. Run `lighthouse auth login`, or
any read command, to sign in.

## Setup

You need an encryption key source (`LIGHTHOUSE_SECRETS_PASSPHRASE` or the OS
keyring; see "Encryption key sources" in the README).

1. Sign in once and save your username and password:

   ```bash
   lighthouse auth login --save-credentials
   ```

2. Open <https://mysignins.microsoft.com/security-info> and choose **Add
   sign-in method** → **Authenticator app** → **Set up a different
   authentication app**. When the QR code appears, choose **Can't scan
   image?** to see the secret key. Keep this page open.
3. Store the secret. Paste it at the hidden prompt:

   ```bash
   lighthouse auth totp-setup
   ```

   Or pipe it on stdin, for example from a password manager:

   ```bash
   pass show mahe/authenticator | lighthouse auth totp-setup --json
   ```

   It accepts the bare base32 key or an `otpauth://totp/...` URI with SHA1,
   6 digits and a 30-second period. There is no command-line argument for it,
   because process listings show arguments.
4. Scan the same QR code with an authenticator app on your phone. That app is
   your backup, and it gives you the code Microsoft asks for to finish
   registration. The CLI never shows a code.
5. Finish registration on the Microsoft page with the phone app's code.
6. Wait until the phone app shows a new code, then sign in with the stored
   authenticator:

   ```bash
   lighthouse auth login
   ```

   The first sign-in after setup can wait up to 30 seconds. The CLI never
   reuses a time step, and setup reserves the current one.
7. Check that it is configured:

   ```bash
   lighthouse auth status --json
   # {"valid": true, ..., "unattended_login": {"configured": true, "blocked": false}}
   ```

If step 6 fails with `code_rejected` while the phone app accepts the same
secret, stop and report it. Do not retry in a loop. The account may have more
than one authenticator-app method, and the CLI cannot choose between them.

## Commands

| Command | What it does |
|---------|--------------|
| `lighthouse auth totp-setup [--json]` | Store the authenticator secret (hidden prompt or stdin). Needs saved credentials. Clears the breaker. JSON: `{"success": true, "configured": true}` |
| `lighthouse auth totp-remove [--json]` | Delete the stored secret and the breaker state. Saved credentials and cookies stay. JSON: `{"success": true, "removed": true}` (`false` if nothing was stored) |
| `lighthouse auth login [--json]` | With the stored authenticator configured, signs in with no prompt. Any `--totp`, or `--mfa-method sms\|call\|push\|choose\|auto`, keeps the person-driven flow |
| `lighthouse auth status [--json]` | Signs in again if needed, like any read. Reports `renewed` and `unattended_login: {configured, blocked}` |

## When automatic sign-in fails

Failures are recorded in `relogin_state.json`, so a wrong password or secret
cannot cause repeated failed sign-ins on your account. The file holds only a
category and timestamps. Commands then fail with:

```
Session expired; automatic sign-in failed (<category>). Run: lighthouse auth login
```

| Category | Meaning | Effect | Fix |
|----------|---------|--------|-----|
| `credentials_rejected` | Microsoft rejected the saved username or password | Blocked | See "Password change" below |
| `code_rejected` | Microsoft rejected the generated code | Blocked | Check the clock. If it is right, register the app again and run `auth totp-setup` |
| `method_unavailable` | The account offers no authenticator-app code method | Blocked | Register the app, then `auth totp-setup` |
| `clock_skew` | No unused time step is available (the clock went backwards) | Blocked | Fix the clock, then `auth login` |
| `interrupt` | Microsoft showed a page that needs a person (for example a security-info prompt) | Blocked | Sign in once in a browser, then `auth login` |
| `unknown` | Anything else, including an unreadable state file | Blocked | `auth login --mfa-method choose` |
| `network` | Microsoft or D2L could not be reached | 5-minute wait between automatic attempts | Retry later |
| `busy`, `store`, `pending_human_login` | Another sign-in is running; local files unreadable; a two-step `auth login` is waiting for `auth verify` | Not recorded | Retry; check the key source; finish with `auth verify` |

**While blocked**, automatic sign-in is refused without contacting Microsoft.
These clear the block:

- a successful `lighthouse auth login` with the stored authenticator (explicit
  login is never refused by the breaker);
- `lighthouse auth totp-setup`;
- `lighthouse auth totp-remove`.

A login by SMS, call or push gives you a session but leaves the block in place.

## Clock

Codes depend on the system clock. Keep it synced with NTP (for example
`timedatectl set-ntp true`, `systemd-timesyncd` or `chrony`). A clock that is
more than about 30 seconds off gives `code_rejected`. A clock that goes back
past the last step used gives `clock_skew`. Both block automatic sign-in until
you fix the clock and sign in.

## Password change

A plain `auth login` keeps using the saved password. After changing it, save
the new one without typing it into the command line:

```bash
read -rsp 'New password: ' LIGHTHOUSE_PASSWORD; echo
LIGHTHOUSE_PASSWORD="$LIGHTHOUSE_PASSWORD" lighthouse auth login --save-credentials
unset LIGHTHOUSE_PASSWORD
```

## Security trade-off

The password and the second factor sit in the same sealed store. Anyone who
can unseal it can sign in to your whole MAHE Microsoft 365 account, not only
Lighthouse. That means anyone with your OS keyring session, or with
`LIGHTHOUSE_SECRETS_PASSPHRASE`. Use this only on a machine you control, and
keep the passphrase out of shell history and shared files.

The CLI never prints, logs or stores in plain text the secret, a generated
code, the password, the cookies or the username. Each code is submitted once,
and time steps only move forward.

## Revoking

1. `lighthouse auth totp-remove`
2. At <https://mysignins.microsoft.com/security-info>, delete the
   authenticator app you registered for the CLI.
3. If the machine or passphrase may be exposed, change your MAHE password and
   delete `credentials.json` and `cookies.json` from the config directory
   (`~/.config/lighthouse-cli/`, or `LIGHTHOUSE_CONFIG_DIR`).

## Out of scope

- Unattended SMS, voice call or push approval (including Microsoft
  Authenticator push).
- Other MAHE sign-in methods: app passwords (legacy protocols only; they cannot
  complete the web sign-in), hardware tokens (the secret cannot be extracted),
  passkeys (need a person present), alternate or office phone (voice calls),
  and email (password reset only).
- Keeping Microsoft's "Stay signed in" cookie between sign-ins.
- Reading mail or using Microsoft Graph.

## Files

All in the config directory, mode `0600`:

| File | Contents |
|------|----------|
| `credentials.json` | Username and password (sealed) |
| `authenticator.json` | Authenticator secret, its username and the last time step used (sealed). Plaintext metadata: kind, algorithm, digits, period |
| `relogin_state.json` | Breaker state: blocked flag, category, timestamps. No secrets |
| `relogin.lock` | Empty lock file that keeps sign-ins one at a time |
