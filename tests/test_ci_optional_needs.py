"""This repository's own pipeline: every `needs: optional: true` resolves.

GitLab ignores an optional need that names no job, so a renamed job turns the
dependency off with no error. Driven with the library gate the template vendors.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "template" / "scripts" / "check-ci-include-job-names.py"


def _lib_checkout() -> Path | None:
    """A weisssrv-lib checkout this machine can reach, or None.

    Found the way the render's own gates find one: `$WEISSSRV_LIB_PATH` first,
    then a sibling clone.
    """
    candidates = []
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(REPO.parent / "weisssrv-lib")
    for candidate in candidates:
        if (candidate / "scripts" / "check-ci-include-job-names.py").is_file():
            return candidate
    return None


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


def test_every_optional_need_in_this_pipeline_resolves() -> None:
    """The library includes are resolved at the pinned ref's checkout, so a
    `job_name` input this repository passes is read rather than assumed."""
    lib = _lib_checkout()
    if lib is None:
        # A permanent skip in CI is this gate reporting success forever, and the
        # job already clones the library for the ruleset parity gate.
        if os.environ.get("CI"):
            pytest.fail(
                "CI ships no weisssrv-lib checkout — the optional-needs gate would "
                "silently skip; set WEISSSRV_LIB_PATH in the python-tests job"
            )
        pytest.skip("no weisssrv-lib checkout to resolve the library includes")
    result = _run("--lib-path", str(lib), "--require-optional-needs")
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
