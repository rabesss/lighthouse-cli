# Contributing

Thanks for helping improve `lighthouse-cli`.

## Local Setup

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[auth,credentials]'
pytest -q
```

Install Playwright Chromium only when working on browser-assisted auth:

```sh
playwright install chromium
```

## Quality Gates

CI runs on every PR and push to `main` (`.github/workflows/ci.yml`): formatting,
linting (`ruff`), strict type checking (`mypy`), architecture layers
(`import-linter`), dependency hygiene (`deptry`), a complexity gate
(`xenon`: average and per-module ranks only; it does not block individual
worst-case functions), secret scanning (gitleaks history scan plus a rejecting
`detect-secrets` check against the audited baseline), the test matrix (Python
3.10 and 3.13, both from the pinned lockfile), and repository policy tests
(`tests/test_repo_policies.py`, `tests/test_secret_gate.py`).

Install the pinned toolchain and reproduce any job locally:

```sh
pip install -r requirements-dev.txt
pip install -e . --no-deps
pre-commit install            # ruff + detect-secrets on every commit
```

The full local gate and per-job commands are documented in the
`contribution-toolchain` skill (`.agents/skills/contribution-toolchain/`).
If you change `pyproject.toml` dependencies, regenerate both lockfiles with
`uv pip compile` (exact commands in the skill) so CI stays reproducible.

## PR Guidelines

- Keep PRs small and focused.
- Run the full quality gate before opening a PR (see *Quality Gates*); at minimum `pytest -q` must be green.
- Update README/docs when changing command behavior or JSON output.
- Keep `--json` output stable for agent workflows.
- Do not commit local auth files, course data, private LMS files, local manifests, or screenshots containing student data.
- Prefer mocked API responses for tests. Live D2L access should not be required by default.

## Security-sensitive Changes

Auth, session storage, file downloads, assignment submission, and logging need extra review. Sanitized reproduction steps are welcome; private course material is not.
