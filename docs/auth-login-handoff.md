# Auth login — agent quick reference

**Full design:** [auth-microsoft-sso.md](./auth-microsoft-sso.md)

## Workflow

```bash
source .venv/bin/activate   # required on Arch
lighthouse auth login --mfa-method sms
# → "Verification code sent."
lighthouse auth verify <code>
lighthouse auth status
```

## Rules

1. Do **not** pass `--totp` on `login` for SMS (new `BeginAuth` → new code).
2. Do **not** run `login` twice while waiting for OTP.
3. Use the code from the **latest** “code sent” message only.
4. Install: `pip install -e '.[auth,credentials]'` in a venv + `playwright install chromium`.
5. **App/Authenticator** instead of SMS: `lighthouse auth login --mfa-method app --totp <code>`
   completes in one step (offline TOTP — no `verify`).
