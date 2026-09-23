"""Rejecting working-tree secret gate backed by the audited detect-secrets baseline.

Usage (from anywhere inside the repository)::

    python scripts/check_secrets.py

Scans every git-tracked file with ``detect-secrets-hook`` against a scratch copy
of ``.secrets.baseline`` and exits nonzero when a finding is not in the audited
baseline, or when the baseline is stale. The real baseline is never written:
refreshing or auditing it is a deliberate maintenance step (see
``docs/runbooks/ci-red-triage.md``), never a side effect of running the gate.
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASELINE_NAME = ".secrets.baseline"


def _git(root: Path | None, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def main() -> int:
    try:
        root = Path(_git(None, "rev-parse", "--show-toplevel").decode().strip())
        tracked = [p for p in _git(root, "ls-files", "-z").decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        print("secret gate: not inside a git work tree", file=sys.stderr)
        return 2

    baseline = root / BASELINE_NAME
    if not baseline.is_file():
        print(f"secret gate: missing {BASELINE_NAME}", file=sys.stderr)
        return 2
    files = [p for p in tracked if p != BASELINE_NAME and (root / p).is_file()]

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / BASELINE_NAME
        shutil.copyfile(baseline, scratch)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "detect_secrets.pre_commit_hook",
                "--baseline",
                str(scratch),
                *files,
            ],
            cwd=root,
            check=False,
        )
        stale = not filecmp.cmp(baseline, scratch, shallow=False)

    if result.returncode != 0 or stale:
        if stale:
            print(
                f"secret gate: {BASELINE_NAME} is stale; refresh and audit it deliberately "
                "(docs/runbooks/ci-red-triage.md)",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
