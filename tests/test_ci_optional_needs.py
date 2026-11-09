"""This repository's own pipeline: every `needs: optional: true` resolves.

GitLab ignores an optional need that names no job, so a renamed job turns the
dependency off with no error. Driven with the library gate the template vendors.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import pinned_lib

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "template" / "scripts" / "check-ci-include-job-names.py"


# An include naming a file outside both checkouts hides the jobs AND the needs
# inside it, so the gate stops until the blind spot is declared with a reason.
UNREADABLE_INCLUDES = {
    "Jobs/Secret-Detection.gitlab-ci.yml": (
        "GitLab's managed scanner template, a file in neither checkout"
    )
}


def _run(*extra: str, repo_root: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(repo_root), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_vendored_gate_ships_in_the_template() -> None:
    """Without it this suite has nothing to drive, and the render has no gate."""
    assert GATE.is_file(), f"the template ships no {GATE.name}"


def test_every_optional_need_in_this_pipeline_resolves(tmp_path: Path) -> None:
    """The library includes are read from the tree at this pipeline's declared
    ref, whatever branch the checkout sits on."""
    pinned = pinned_lib(tmp_path, "optional-needs gate", "check-ci-include-job-names.py")
    extra = ["--lib-path", str(pinned), "--require-optional-needs"]
    for pattern, reason in UNREADABLE_INCLUDES.items():
        extra += ["--allow-unreadable-include", f"{pattern}={reason}"]
    result = _run(*extra)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_a_dangling_optional_need_is_reported(tmp_path: Path) -> None:
    """Mutation case: a need naming a job nothing creates has to be a finding,
    or the case above would pass on a half-finished rename."""
    (tmp_path / ".gitlab-ci.yml").write_text(
        'real-job:\n'
        '  script: ["true"]\n'
        'waiting-job:\n'
        '  script: ["true"]\n'
        '  needs:\n'
        '    - job: renamed-away\n'
        '      optional: true\n',
        encoding="utf-8",
    )
    result = _run("--require-optional-needs", repo_root=tmp_path)
    assert result.returncode == 1, f"{result.stdout}{result.stderr}"
    assert "renamed-away" in result.stderr
