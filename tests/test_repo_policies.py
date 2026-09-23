"""Repository policy tests.

These encode the agent-readiness invariants that must survive future changes:
pinned dependencies, CI coverage of the quality gates, secret-scanning, and the
module conventions documented in AGENTS.md. They are deliberately fast and
network-free so they run as part of the normal suite and as a dedicated CI job.

Each policy is a small checker function exercised twice: once against a
synthetic fixture that must be rejected (so the checker cannot silently pass
everything) and once against the real repository tree.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

_REQUIREMENT_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(\[[^\]]*\])?==([^\s;#]+)")
_FULL_SHA_USES = re.compile(r"^[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_modules(package: Path) -> list[Path]:
    """Every Python module in a package, including nested subpackages."""
    return sorted(package.rglob("*.py"))


def _missing_future_annotations(modules: list[Path]) -> list[Path]:
    return [
        path
        for path in modules
        if path.name != "__init__.py"
        and "from __future__ import annotations" not in path.read_text()
    ]


def _annotation_nodes(tree: ast.AST) -> list[ast.expr]:
    """Every annotation expression (and bare type-alias value) in a module."""
    nodes: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            nodes.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            nodes.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            nodes.append(node.annotation)
            if node.value is not None:
                nodes.append(node.value)  # `Fn: TypeAlias = Callable`
        elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            nodes.append(node.value)  # implicit alias such as `Fn = Callable`
    # String annotations are still annotations.
    for node in list(nodes):
        for const in ast.walk(node):
            if isinstance(const, ast.Constant) and isinstance(const.value, str):
                with contextlib.suppress(SyntaxError):
                    parsed = ast.parse(const.value, mode="eval").body
                    ast.increment_lineno(parsed, const.lineno - 1)
                    nodes.append(parsed)
    return nodes


def _typing_offenders(source: str) -> list[int]:
    """Line numbers of annotations using ``Optional[...]`` or a bare ``Callable``.

    Works on the AST, so imports, comments, and prose never match.
    """
    offenders: set[int] = set()
    for annotation in _annotation_nodes(ast.parse(source)):
        subscripted = {
            id(node.value) for node in ast.walk(annotation) if isinstance(node, ast.Subscript)
        }
        for node in ast.walk(annotation):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if name == "Optional" and id(node) in subscripted:
                offenders.add(node.lineno)
            elif name == "Callable" and id(node) not in subscripted:
                offenders.add(node.lineno)
    return sorted(offenders)


def _parse_requirements(text: str) -> tuple[dict[str, str], list[str]]:
    """Return (``name -> pinned version``, requirement lines not pinned with ``==``)."""
    pins: dict[str, str] = {}
    unpinned: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue  # blank, comment, or uv's indented "# via" annotation
        match = _REQUIREMENT_LINE.match(raw)
        if match:
            pins[_normalize(match.group(1))] = match.group(3)
        else:
            unpinned.append(raw.strip())
    return pins, unpinned


def _runtime_pin_mismatches(runtime: dict[str, str], dev: dict[str, str]) -> dict[str, str]:
    """Runtime pins that are missing from, or pinned differently in, the dev lockfile."""
    return {
        name: f"{version} != {dev.get(name, '<missing>')}"
        for name, version in runtime.items()
        if dev.get(name) != version
    }


def _unpinned_actions(workflow: str) -> list[str]:
    """``uses:`` references that are not pinned to a full commit SHA."""
    refs = re.findall(r"^\s*(?:-\s*)?uses:\s*(.+?)\s*$", workflow, flags=re.MULTILINE)
    return [ref for ref in refs if not ref.startswith("./") and not _FULL_SHA_USES.match(ref)]


# Vendor-generated workflows ("DO NOT EDIT") whose installer tracks a major
# tag. Listed explicitly so any other floating ref, or a new one here, fails.
_VENDOR_MANAGED_ACTIONS = {"pullfrog.yml": ["pullfrog/pullfrog@v0"]}


def _read_lock(name: str) -> tuple[dict[str, str], list[str]]:
    return _parse_requirements((ROOT / name).read_text())


class TestPythonModulePolicies:
    def test_module_discovery_includes_nested_packages(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        top = tmp_path / "top.py"
        nested = tmp_path / "sub" / "nested.py"
        top.write_text("x = 1\n")
        nested.write_text("from __future__ import annotations\n")
        assert _python_modules(tmp_path) == [nested, top]
        assert _missing_future_annotations(_python_modules(tmp_path)) == [top]

    def test_every_module_uses_future_annotations(self) -> None:
        missing = _missing_future_annotations(_python_modules(ROOT / "lighthouse_cli"))
        assert not missing, f"modules missing 'from __future__ import annotations': {missing}"

    @pytest.mark.parametrize(
        "line",
        [
            "def f(x: Optional[int]) -> None: ...",
            "def f(cb: Callable) -> None: ...",
            "def f() -> Callable: ...",
            "handler: Callable = print",
            "handlers: dict[str, Callable] = {}",
            "def f(x: list[Callable] | None) -> None: ...",
            "def f(cb: 'Callable') -> None: ...",
            "Fn = Callable",
        ],
    )
    def test_typing_checker_rejects(self, line: str) -> None:
        assert _typing_offenders(line) == [1]

    @pytest.mark.parametrize(
        "line",
        [
            "from collections.abc import Callable",
            "def f(cb: Callable[[int], str]) -> Callable[..., None]: ...",
            "def f(x: int | None) -> None: ...",
            "from collections.abc import (\n    Callable,\n)",
            "# a bare Callable is not allowed; neither is Optional[int]",
            'def f() -> None:\n    """cb (Callable): prose about Optional[int]."""',
            'value = settings["Optional[int]"]',
        ],
    )
    def test_typing_checker_accepts(self, line: str) -> None:
        assert _typing_offenders(line) == []

    def test_no_optional_or_bare_callable_in_source(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}:{lineno}"
            for path in _python_modules(ROOT / "lighthouse_cli")
            for lineno in _typing_offenders(path.read_text())
        ]
        assert not offenders, f"AGENTS.md typing violations: {offenders}"


class TestDependencyPolicies:
    def test_requirement_parser_flags_every_unpinned_line(self) -> None:
        pins, unpinned = _parse_requirements(
            "# header\n"
            "click==8.5.0\n"
            "    # via lighthouse-cli\n"
            "    indented>=1.0\n"
            "Typing_Extensions==4.15.0 ; python_version < '3.11'\n"
            "requests>=2.31\n"
            "rich\n"
            "\n"
        )
        assert pins == {"click": "8.5.0", "typing-extensions": "4.15.0"}
        assert unpinned == ["indented>=1.0", "requests>=2.31", "rich"]

    def test_runtime_mismatch_compares_versions_not_names(self) -> None:
        mismatches = _runtime_pin_mismatches(
            {"click": "8.5.0", "requests": "2.33.0", "rich": "14.0.0"},
            {"click": "8.5.0", "requests": "2.32.0"},
        )
        assert set(mismatches) == {"requests", "rich"}

    @pytest.mark.parametrize("lockfile", ["requirements.txt", "requirements-dev.txt"])
    def test_lockfile_is_fully_pinned(self, lockfile: str) -> None:
        pins, unpinned = _read_lock(lockfile)
        assert pins, f"{lockfile} has no pinned packages"
        assert not unpinned, f"unpinned requirements in {lockfile}: {unpinned}"

    def test_dev_lockfile_pins_the_same_runtime_versions(self) -> None:
        runtime, _ = _read_lock("requirements.txt")
        dev, _ = _read_lock("requirements-dev.txt")
        mismatches = _runtime_pin_mismatches(runtime, dev)
        assert not mismatches, f"runtime pins differ in requirements-dev.txt: {mismatches}"

    def test_pre_commit_ruff_matches_the_locked_ruff(self) -> None:
        config = (ROOT / ".pre-commit-config.yaml").read_text()
        hook = re.search(r"ruff-pre-commit\s*\n(?:\s*#.*\n)*\s*rev:\s*v?(\S+)", config)
        dev, _ = _read_lock("requirements-dev.txt")
        assert hook, "ruff pre-commit hook not found"
        assert hook.group(1) == dev["ruff"], "pre-commit ruff rev drifted from requirements-dev.txt"

    def test_detect_secrets_version_is_consistent(self) -> None:
        # The gate, the pre-commit hook, the lockfile, and the baseline format
        # must agree, or the gate reports a spuriously "stale" baseline.
        dev, _ = _read_lock("requirements-dev.txt")
        ci = re.search(r"pip install detect-secrets==([0-9.]+)", (WORKFLOWS / "ci.yml").read_text())
        hook = re.search(
            r"Yelp/detect-secrets\s*\n\s*rev:\s*v?(\S+)",
            (ROOT / ".pre-commit-config.yaml").read_text(),
        )
        baseline = json.loads((ROOT / ".secrets.baseline").read_text())["version"]
        assert ci and hook, "detect-secrets pin missing from ci.yml or pre-commit"
        assert {dev["detect-secrets"], ci.group(1), hook.group(1), baseline} == {baseline}


class TestCIPolicies:
    def test_ci_workflow_gates_exist(self) -> None:
        ci = (WORKFLOWS / "ci.yml").read_text()
        for gate in (
            "ruff check",
            "ruff format --check",
            "mypy",
            "lint-imports",
            "deptry",
            "xenon",
            "pytest",
        ):
            assert gate in ci, f"CI is missing the '{gate}' gate"

    def test_ci_scans_for_secrets_with_a_rejecting_gate(self) -> None:
        ci = (WORKFLOWS / "ci.yml").read_text()
        assert "gitleaks" in ci
        assert "scripts/check_secrets.py" in ci
        # `detect-secrets scan --baseline` rewrites the baseline and exits 0 on
        # new findings, so it must never be the CI gate again.
        assert "detect-secrets scan" not in ci

    def test_every_ci_leg_installs_the_lockfile(self) -> None:
        ci = (WORKFLOWS / "ci.yml").read_text()
        installs = re.findall(r"pip install [^\n]*", ci)
        floating = [cmd for cmd in installs if "requirements-dev.txt" not in cmd]
        # The secret job installs one exact pin; everything else uses the lock.
        assert floating == ["pip install detect-secrets==1.5.0"], floating

    def test_unpinned_action_checker(self) -> None:
        workflow = (
            "    - uses: actions/checkout@v4\n"
            f"    - uses: actions/setup-python@{'a' * 40} # v5.6.0\n"
            "      uses: ./local-action\n"
        )
        assert _unpinned_actions(workflow) == ["actions/checkout@v4"]

    def test_every_workflow_action_is_sha_pinned(self) -> None:
        unpinned = {
            path.name: refs
            for path in sorted(WORKFLOWS.glob("*.y*ml"))
            if (refs := _unpinned_actions(path.read_text()))
        }
        assert unpinned == _VENDOR_MANAGED_ACTIONS, f"mutable action refs: {unpinned}"

    def test_release_automation_present(self) -> None:
        release = (WORKFLOWS / "release.yml").read_text()
        assert "release-please" in release


class TestSecretScanningPolicies:
    def test_secrets_baseline_is_valid_json(self) -> None:
        baseline = json.loads((ROOT / ".secrets.baseline").read_text())
        assert "results" in baseline
        assert "plugins_used" in baseline

    def test_every_baseline_entry_records_an_audit_decision(self) -> None:
        # `detect-secrets scan` output has no is_secret field; only an audit
        # (`detect-secrets audit`) records one. A real secret must never be
        # baselined, so every entry must be an explicit false positive.
        results = json.loads((ROOT / ".secrets.baseline").read_text())["results"]
        unaudited = [
            f"{path}:{entry['line_number']}"
            for path, entries in results.items()
            for entry in entries
            if entry.get("is_secret") is not False
        ]
        assert not unaudited, f"unaudited .secrets.baseline entries: {unaudited}"

    def test_pre_commit_runs_ruff_and_detect_secrets(self) -> None:
        config = (ROOT / ".pre-commit-config.yaml").read_text()
        assert "ruff" in config
        assert "detect-secrets" in config
        assert "--baseline" in config

    def test_gitleaks_allowlist_stays_rule_and_path_scoped(self) -> None:
        config = (ROOT / ".gitleaks.toml").read_text()
        assert "useDefault = true" in config
        lines = [line.strip() for line in config.splitlines()]
        # A global allowlist, the plural [[allowlists]] or the legacy singular
        # [allowlist] (both honored by gitleaks 8.30; TOML allows indenting
        # them), ignores `condition = "AND"` and would allow every finding in
        # its paths.
        assert "[[allowlists]]" not in lines
        assert "[allowlist]" not in lines
        # Exactly one allowlist, scoped to generic-api-key, the baseline file,
        # and the hashed_secret line shape; widening any of them must fail.
        assert lines.count("[[rules.allowlists]]") == 1
        assert "[rules.allowlist]" not in lines
        for expected in (
            'id = "generic-api-key"',
            'condition = "AND"',
            'regexTarget = "line"',
            r"paths = ['''^\.secrets\.baseline$''']",
            r"""regexes = ['''^\s*"hashed_secret": "[0-9a-f]{40}",?$''']""",
        ):
            assert expected in lines, f".gitleaks.toml lost: {expected}"


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
        assert "package-ecosystem: pip" in dependabot.replace('"', "")
        assert "package-ecosystem: github-actions" in dependabot.replace('"', "")
        # Not a Dependabot option; `cooldown` is the supported burn-in setting.
        assert "minimum-update-age" not in dependabot
        assert "cooldown:" in dependabot
