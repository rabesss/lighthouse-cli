"""Import boundaries for command discovery and legacy command exports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_clean_python(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_legacy_command_module_does_not_load_write_engines_on_import() -> None:
    result = _run_clean_python(
        """
import sys
import lighthouse_cli.commands

heavy = {
    'lighthouse_cli.api',
    'lighthouse_cli.assignments',
    'lighthouse_cli.config',
    'lighthouse_cli.course_config',
    'lighthouse_cli.manifest',
    'lighthouse_cli.show',
    'lighthouse_cli.submit',
    'lighthouse_cli.sync_engine',
}
loaded = sorted(heavy.intersection(sys.modules))
assert not loaded, loaded
""",
    )
    assert result.returncode == 0, result.stderr


def test_role_help_discovers_commands_without_loading_assessment_transport() -> None:
    result = _run_clean_python(
        """
import sys
from lighthouse_cli.cli import cli

assert cli.main(args=['student', '--help'], standalone_mode=False) == 0
assert 'lighthouse_cli.api' not in sys.modules
assert 'lighthouse_cli.assessment_api' not in sys.modules
""",
    )
    assert result.returncode == 0, result.stderr


def test_legacy_dependency_exports_still_resolve_the_real_implementations() -> None:
    result = _run_clean_python(
        """
import sys
from lighthouse_cli import commands

assert 'lighthouse_cli.api' not in sys.modules
assert commands.LighthouseClient.__module__ == 'lighthouse_cli.api'
assert 'lighthouse_cli.api' in sys.modules
assert commands.cmd_submit.__module__ == 'lighthouse_cli.submit'
""",
    )
    assert result.returncode == 0, result.stderr
