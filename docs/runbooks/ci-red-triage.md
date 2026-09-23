# Runbook: red CI on lighthouse-cli

Audience: maintainers and agents reacting to a failed check on a PR or `main`.

## 1. Identify the failing job

CI (`ci.yml`) has four jobs; each maps to one local command:

| Job | Local reproduction |
| --- | --- |
| `quality` | `ruff check . && mypy && lint-imports && deptry . && xenon -a B -m C -b F -e "*/ms_auth.py" lighthouse_cli` |
| `security` | `python scripts/check_secrets.py` (working tree, rejecting) and `gitleaks git .` (history, uses `.gitleaks.toml`) |
| `tests` | `pytest -q --cov` |
| `policies` | `pytest tests/test_repo_policies.py tests/test_secret_gate.py -q` |

## 2. Fix by category

- **Lint** — run `ruff check . --fix`, review the result, commit. The code is
  not auto-formatted; keep edits in the style of the surrounding code.
- **Types** — read the error; prefer real annotations or `cast` at API
  boundaries. Never add blanket `# type: ignore`.
- **Architecture (`lint-imports`)** — the import violates a layer in
  `pyproject.toml`. Move the code, not the contract.
- **Complexity (`xenon`)** — the average (`-a B`) or a module (`-m C`) rank
  regressed. Extract helpers. `-b F` is the loosest block rank, so a single new
  rank-F function is *not* caught by CI; reviewers must flag it. `ms_auth.py`
  is excluded (known hotspot); do not widen the exclusion.
- **Secrets** — treat every finding as real until proven otherwise.
  - *Real leak:* revoke and rotate the credential first; then remove it from
    the file, and from history if it was pushed. Never "baseline" a real
    secret.
  - *False positive (working tree):* prefer an inline
    `# pragma: allowlist secret` on that one line. If a baseline entry is
    unavoidable, refresh deliberately with
    `detect-secrets scan --baseline .secrets.baseline`, then review each new or
    changed entry with `detect-secrets audit .secrets.baseline` and explain in
    the PR why each is benign. Never regenerate or blanket-approve the baseline
    to turn CI green.
  - *Stale baseline* (gate reports "stale"): tracked lines moved. Refresh as
    above and confirm the diff only moves line numbers of audited entries.
  - *False positive (gitleaks history):* add the narrowest rule-scoped
    allowlist to `.gitleaks.toml`; never disable a rule or exclude whole
    paths or history.
- **Policies** — the repo lost a hygiene invariant (missing template, unpinned
  dep, dropped `from __future__ import annotations`). Restore the invariant
  rather than editing the test.

## 3. Release PRs have no checks

`release.yml` runs release-please with the default `GITHUB_TOKEN`. GitHub does
not start workflows for PRs or pushes created with that token, so a release
PR shows the required checks as *expected/missing*, not green. That is not a
pass. Until a maintainer provisions a dedicated GitHub App (contents +
pull-requests write, installed on this repo only) and passes its token to the
action's `token:` input, run CI on the release PR manually. Push an empty
commit to its branch, or close and reopen it. Then merge only after all
required checks are green. Provisioning that credential is a maintainer
decision; agents must not create or store one.

## 4. Escalation

If `main` is red: fix forward on a branch and request review; do not force-push
or disable gates.
