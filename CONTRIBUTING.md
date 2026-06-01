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

## PR Guidelines

- Keep PRs small and focused.
- Run `pytest -q` before opening a PR.
- Update README/docs when changing command behavior or JSON output.
- Keep `--json` output stable for agent workflows.
- Do not commit local auth files, course data, private LMS files, local manifests, or screenshots containing student data.
- Prefer mocked API responses for tests. Live D2L access should not be required by default.

## Security-sensitive Changes

Auth, session storage, file downloads, assignment submission, and logging need extra review. Sanitized reproduction steps are welcome; private course material is not.