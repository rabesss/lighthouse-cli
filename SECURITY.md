# Security Policy

## Scope

This project handles LMS authentication state and local credential storage, so security issues are taken seriously.

## Do Not Share Publicly

Do not include real credentials, cookies, MFA codes, exported sessions, private course material, or private student data in issues, PRs, screenshots, logs, or test fixtures.

## Sensitive Areas

Changes to these areas need careful review:

- Microsoft SSO and MFA handling
- cookie/session storage
- credential encryption and keyring fallback
- file downloads and filename sanitization
- assignment submission
- JSON output contracts used by agents
- logging and error reporting

## Local Credential Guidance

Prefer environment variables or OS keyring-backed storage. Keep local config directories private to your user account. Do not commit `cookies.json`, `mfa_pending.json`, credential files, downloaded LMS material, or local manifests that reveal private course data.

## Reporting

Use GitHub private vulnerability reporting when available. If unavailable, open a minimal public issue asking for a private channel, without including sensitive details.