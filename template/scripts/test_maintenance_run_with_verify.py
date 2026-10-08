"""Coverage for scripts/maintenance-run-with-verify.sh.

The wrapper must run the verify script whatever the command did, and report the
command's own exit status when it failed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "maintenance-run-with-verify.sh"


def _run(tmp_path: Path, command: list[str], verify_body: str,
         **env_over: str) -> subprocess.CompletedProcess:
    """Run the wrapper with a stub verify script, from a stub repo root."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "post-maintenance-verify.sh").write_text(verify_body)
    env = {**os.environ, "CI_PROJECT_DIR": str(tmp_path), **env_over}
    return subprocess.run(["bash", str(SCRIPT), *command], capture_output=True,
                          text=True, env=env, timeout=60)


OK_VERIFY = "#!/bin/sh\necho VERIFY_RAN\nexit 0\n"


def test_a_successful_command_still_runs_the_verify(tmp_path):
    res = _run(tmp_path, ["true"], OK_VERIFY)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "VERIFY_RAN" in res.stdout
    assert "SUMMARY: maintenance command OK; verify OK" in res.stdout


def test_a_failed_command_still_runs_the_verify_and_keeps_its_status(tmp_path):
    # The failing case is the one that most needs a health report, so the verify
    # must run; the wrapper then reports the command's status, not the verify's.
    res = _run(tmp_path, ["bash", "-c", "exit 7"], OK_VERIFY)
    assert res.returncode == 7, res.stdout + res.stderr
    assert "VERIFY_RAN" in res.stdout
    assert "maintenance command FAILED (rc=7)" in res.stdout


def test_a_failing_verify_fails_an_otherwise_clean_run(tmp_path):
    res = _run(tmp_path, ["true"], "#!/bin/sh\nexit 3\n")
    assert res.returncode == 3
    assert "verify FAILED (rc=3)" in res.stdout


def test_no_command_is_a_usage_error(tmp_path):
    res = _run(tmp_path, [], OK_VERIFY)
    assert res.returncode == 64
    assert "at least one argument" in res.stderr


def test_a_missing_verify_script_is_a_usage_error_not_a_silent_pass(tmp_path):
    res = _run(tmp_path, ["true"], OK_VERIFY,
               VERIFY_SCRIPT="scripts/not-a-verify-script.sh")
    assert res.returncode == 64
    assert "not found or not readable" in res.stderr


def test_verify_script_override_is_honoured(tmp_path):
    other = tmp_path / "scripts" / "other-verify.sh"
    other.parent.mkdir(exist_ok=True)
    other.write_text("#!/bin/sh\necho OTHER_VERIFY\nexit 0\n")
    res = _run(tmp_path, ["true"], "#!/bin/sh\nexit 3\n",
               VERIFY_SCRIPT="scripts/other-verify.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OTHER_VERIFY" in res.stdout
