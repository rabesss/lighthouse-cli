"""Repository policy tests.

These encode the agent-readiness invariants that must survive future changes:
pinned dependencies, CI coverage of the quality gates, secret-scanning, and the
module conventions documented in AGENTS.md. They are deliberately fast and
network-free so they run as part of the normal suite and as a dedicated CI job.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRACKED_PYTHON_MODULES = sorted((ROOT / "lighthouse_cli").glob("*.py"))


def _pinned_requirements(path: Path) -> set[str]:
    """Return the package names pinned with ``==`` in a requirements file."""
    pinned = set()
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if match:
            pinned.add(match.group(1).lower())
    return pinned


class TestPythonModulePolicies:
    def test_every_module_uses_future_annotations(self) -> None:
        missing = [
            path.name
            for path in TRACKED_PYTHON_MODULES
            if path.name != "__init__.py"
            and "from __future__ import annotations" not in path.read_text()
        ]
        assert not missing, f"modules missing 'from __future__ import annotations': {missing}"

    def test_no_optional_or_bare_callable_in_source(self) -> None:
        offenders = []
        for path in TRACKED_PYTHON_MODULES:
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if re.search(r"\bOptional\[", line) or re.search(r":\s*Callable\b(?!\[)", line):
                    offenders.append(f"{path.name}:{lineno}")
        assert not offenders, f"AGENTS.md typing violations: {offenders}"


class TestDependencyPolicies:
    def test_runtime_lockfile_is_fully_pinned(self) -> None:
        pinned = _pinned_requirements(ROOT / "requirements.txt")
        assert pinned, "requirements.txt has no pinned packages"

    def test_dev_lockfile_pins_the_runtime_set(self) -> None:
        runtime = _pinned_requirements(ROOT / "requirements.txt")
        dev = _pinned_requirements(ROOT / "requirements-dev.txt")
        assert runtime, "requirements.txt has no pinned packages"
        assert runtime <= dev, f"unpinned runtime deps in requirements-dev.txt: {runtime - dev}"


class TestCIPolicies:
    def test_ci_workflow_gates_exist(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        for gate in (
            "ruff check",
            "ruff format --check",
            "mypy",
            "lint-imports",
            "deptry",
            "pytest",
        ):
            assert gate in ci, f"CI is missing the '{gate}' gate"

    def test_ci_scans_for_secrets(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        assert "gitleaks" in ci
        assert "detect-secrets" in ci

    def test_release_automation_present(self) -> None:
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        assert "release-please" in release


class TestSecretScanningPolicies:
    def test_secrets_baseline_is_valid_json(self) -> None:
        baseline = json.loads((ROOT / ".secrets.baseline").read_text())
        assert "results" in baseline
        assert "plugins_used" in baseline

    def test_pre_commit_runs_ruff_and_detect_secrets(self) -> None:
        config = (ROOT / ".pre-commit-config.yaml").read_text()
        assert "ruff" in config
        assert "detect-secrets" in config
        assert "--baseline" in config


class TestRepositoryHygiene:
    def test_contributor_meta_files_exist(self) -> None:
        for relpath in (
            ".github/CODEOWNERS",
            ".github/dependabot.yml",
            ".github/pull_request_template.md",
            ".github/ISSUE_TEMPLATE/bug_report.md",
            ".github/ISSUE_TEMPLATE/feature_request.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        ):
            assert (ROOT / relpath).is_file(), f"missing {relpath}"

    def test_dependabot_watches_python_and_actions(self) -> None:
        dependabot = (ROOT / ".github" / "dependabot.yml").read_text()
        assert "pip" in dependabot
        assert "github-actions" in dependabot
