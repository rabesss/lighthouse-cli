---
name: Bug report
about: Something in lighthouse-cli misbehaves
labels: ["type: bug"]
assignees: []
---

**What happened?**

A clear description of the wrong behaviour (command, expected vs actual).

**How to reproduce**

```console
$ lighthouse <command> ...
```

Steps, flags, and the exit code. Redacted stderr is fine — **never paste real
passwords, cookies (`d2lSecureSessionVal`, `d2lSessionVal`), TOTP codes, or
SAML tokens.**

**Environment**

- lighthouse-cli version (`lighthouse --version`):
- Python version:
- OS:

**Additional context**

Add any other context. If `--json` was used, confirm stdout contained only JSON.
