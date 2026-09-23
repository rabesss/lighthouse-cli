"""Regression tests for the rejecting working-tree secret gate.

``scripts/check_secrets.py`` must fail on an unaudited finding, pass on an
audited one, and never modify the baseline. Each test builds a disposable git
repository; the canary secret is assembled at runtime so no secret-shaped
literal exists in this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "check_secrets.py"

pytest.importorskip("detect_secrets")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

# AWS access-key shape, built at runtime: "AKIA" + 16 uppercase characters.
CANARY = "AKIA" + "QWERTYUIOPASDFGH"


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, check=False)


def _make_repo(repo: Path, files: dict[str, str]) -> None:
    _run(repo, "git", "init", "-q")
    for name, content in files.items():
        (repo / name).write_text(content)
    _run(repo, "git", "add", "--", *files)


def _baseline_for(repo: Path) -> bytes:
    scan = _run(repo, sys.executable, "-m", "detect_secrets", "scan")
    assert scan.returncode == 0, scan.stderr
    (repo / ".secrets.baseline").write_text(scan.stdout)
    _run(repo, "git", "add", ".secrets.baseline")
    return (repo / ".secrets.baseline").read_bytes()


def _gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(repo, sys.executable, str(GATE))


def test_unaudited_canary_fails_and_baseline_is_untouched(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"clean.py": "x = 1\n"})
    baseline = _baseline_for(tmp_path)
    _make_repo(tmp_path, {"leak.py": f"key = {CANARY!r}\n"})

    result = _gate(tmp_path)

    assert result.returncode != 0
    assert "leak.py" in result.stdout + result.stderr
    assert CANARY not in result.stdout + result.stderr
    assert (tmp_path / ".secrets.baseline").read_bytes() == baseline


def test_audited_fixture_passes_and_baseline_is_untouched(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"fixture.py": f"key = {CANARY!r}\n"})
    baseline = _baseline_for(tmp_path)

    result = _gate(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".secrets.baseline").read_bytes() == baseline


def test_untracked_files_are_out_of_scope(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"clean.py": "x = 1\n"})
    baseline = _baseline_for(tmp_path)
    (tmp_path / "scratch.py").write_text(f"key = {CANARY!r}\n")

    assert _gate(tmp_path).returncode == 0
    assert (tmp_path / ".secrets.baseline").read_bytes() == baseline


def test_stale_baseline_fails_without_being_rewritten(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"fixture.py": f"key = {CANARY!r}\n"})
    _baseline_for(tmp_path)
    # Shift the audited finding to another line: the hook would want to
    # refresh the recorded line number, which the gate must refuse to do.
    _make_repo(tmp_path, {"fixture.py": f"\n\nkey = {CANARY!r}\n"})
    baseline = (tmp_path / ".secrets.baseline").read_bytes()

    result = _gate(tmp_path)

    assert result.returncode != 0
    assert (tmp_path / ".secrets.baseline").read_bytes() == baseline
