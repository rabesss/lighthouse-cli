# Runbook: red CI on lighthouse-cli

Audience: maintainers and agents reacting to a failed check on a PR or `main`.

## 1. Identify the failing job

CI (`ci.yml`) has four jobs; each maps to one local command:

| Job | Local reproduction |
| --- | --- |
| `quality` | `ruff format --check . && ruff check . && mypy && lint-imports && deptry . && xenon -a B -m C -b F -e "*/ms_auth.py" lighthouse_cli` |
| `security` | `detect-secrets scan --baseline .secrets.baseline` (history scan runs gitleaks in CI) |
| `tests` | `pytest -q --cov` |
| `policies` | `pytest tests/test_repo_policies.py -q` |

## 2. Fix by category

- **Lint/format** — run `ruff format .` / `ruff check . --fix`; commit.
- **Types** — read the error; prefer real annotations or `cast` at API
  boundaries. Never add blanket `# type: ignore`.
- **Architecture (`lint-imports`)** — the import violates a layer in
  `pyproject.toml`. Move the code, not the contract.
- **Complexity (`xenon`)** — a block got worse than the ratchet. Extract a
  helper. `ms_auth.py` is excluded (known hotspot); do not widen the exclusion.
- **Secrets** — a real finding: rotate the credential first, then remove it
  from history (gitleaks) or the file. A false positive: audit it into
  `.secrets.baseline` with `detect-secrets scan ... > .secrets.baseline` and a
  comment in the PR explaining why it is benign.
- **Policies** — the repo lost a hygiene invariant (missing template, unpinned
  dep, dropped `from __future__ import annotations`). Restore the invariant
  rather than editing the test.

## 3. Escalation

If `main` is red: fix forward on a branch and request review; do not force-push
or disable gates.
