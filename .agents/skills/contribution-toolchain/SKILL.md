---
name: contribution-toolchain
description: >-
  Run the full local quality gate for lighthouse-cli before opening a PR
  (format, lint, strict types, architecture layers, dependency scan, tests
  with coverage). Use when a change is ready to validate or CI is red.
---

# Contribution toolchain

## When to use

- You changed anything under `lighthouse_cli/` or `tests/` and are about to
  open or update a PR.
- A CI run is red and you need to reproduce the failing gate locally.

## Setup (once)

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e . --no-deps
pre-commit install
```

`requirements-dev.txt` is the pinned superset (runtime + dev tools). Regenerate
both lockfiles after touching `pyproject.toml`:

```sh
uv pip compile pyproject.toml --python-version 3.10 -o requirements.txt
uv pip compile pyproject.toml --extra dev --extra auth --extra credentials \
  --extra cdp --extra rich --python-version 3.10 -o requirements-dev.txt
```

## The full gate (run in this order)

```sh
ruff format --check .   # formatter
ruff check .            # lint
mypy                    # strict type check (config in pyproject.toml)
lint-imports            # layered architecture contracts
deptry .                # unused/undeclared dependency scan
xenon -a B -m C -b F -e "*/ms_auth.py" lighthouse_cli   # complexity ratchet
pytest -q --durations=10 --cov --cov-report=term
```

Everything above must pass. CI (`.github/workflows/ci.yml`) runs the same
commands, so a green local run means a green CI run.

## Common failures

- **`ruff format --check` fails** — run `ruff format .`, never hand-align.
- **`mypy` no-any-return at an API boundary** — raw D2L JSON is intentionally
  `Any` at the boundary; `cast(...)` to the declared shape at the return site.
- **`lint-imports` breaks** — you added an import that violates the layer
  contract in `pyproject.toml` ([tool.importlinter]); the layers mirror the real
  dependency graph, so either move the code down or hoist the helper into a
  lower layer. Do not edit the contract to make an import legal.
- **`xenon` fails** — new block exceeds the ratchet; extract helpers. The
  `ms_auth.py` SSO state machine is a known, deliberately excluded hotspot.

## CI mapping

| CI job | Local command |
| --- | --- |
| quality | ruff format/check, mypy, lint-imports, deptry, xenon |
| security | gitleaks history scan + detect-secrets baseline scan |
| tests | pytest matrix (3.10 pinned, 3.13 latest) |
| policies | `pytest tests/test_repo_policies.py -q` |
