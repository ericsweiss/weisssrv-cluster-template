"""Runs the vendored `scripts/check-ci-include-job-names.py` against this
repository's real pipeline: every `needs: optional: true` resolves to a job some
include creates. The checker's own unit suite lives in the lib."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO, SCRIPTS, _lib_root, load_script, pinned_lib_ref, require_ref

GATE = SCRIPTS / "check-ci-include-job-names.py"

# Jobs created by a source this gate cannot read. GitLab's managed scanner
# arrives through `template:`, which is a file in neither checkout.
EXTRA_JOBS = {"secret_detection": "GitLab's managed Secret-Detection.gitlab-ci.yml"}

# The same include as a blind spot: its own jobs and needs are both invisible,
# so the gate stops until the skip is declared with a reason.
UNREADABLE_INCLUDES = {
    "Jobs/Secret-Detection.gitlab-ci.yml": (
        "GitLab's managed scanner template, a file in neither checkout"
    )
}


def _run(*extra: str, repo_root: str | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(GATE), "--repo-root", repo_root or str(REPO), *extra]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _allowances() -> list[str]:
    extra = []
    for job, reason in EXTRA_JOBS.items():
        extra += ["--extra-job", f"{job}={reason}"]
    for pattern, reason in UNREADABLE_INCLUDES.items():
        extra += ["--allow-unreadable-include", f"{pattern}={reason}"]
    return extra


@pytest.fixture(scope="module")
def lib_tree(tmp_path_factory) -> Path:
    """The library's `ci/` tree at the pinned ref, not whatever branch the
    checkout is on, so a job the pin lacks cannot resolve."""
    lib = _lib_root()
    ref = require_ref(lib, pinned_lib_ref())
    out = tmp_path_factory.mktemp("lib-ci-at-pin")
    archive = subprocess.run(["git", "-C", str(lib), "archive", ref, "ci"], capture_output=True)
    assert archive.returncode == 0, f"could not archive ci/ at {ref}: {archive.stderr.decode()}"
    extract = subprocess.run(
        ["tar", "-x", "-C", str(out)], input=archive.stdout, capture_output=True
    )
    assert extract.returncode == 0, f"could not extract ci/ at {ref}: {extract.stderr.decode()}"
    return out


def test_every_optional_need_resolves_to_a_job(lib_tree) -> None:
    """A library rename or a non-default `job_name` input turns an optional need
    off with no error, so the gate the pipeline waits for stops blocking it."""
    result = _run("--lib-path", str(lib_tree), "--require-optional-needs", *_allowances())
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "optional need(s) resolve" in result.stdout, (
        f"the gate resolved no optional need at all:\n{result.stdout}{result.stderr}"
    )


def test_an_undeclared_unreadable_include_stops_the_gate(lib_tree) -> None:
    """Without the allowance the gate stops: a need hidden inside an unreadable
    include would otherwise pass as resolved."""
    result = _run("--lib-path", str(lib_tree))
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "Secret-Detection" in result.stdout + result.stderr


def test_the_lint_task_passes_the_same_allowances() -> None:
    """`task lint` and this suite run the gate with one set of allowances."""
    tree = load_script("taskfile_tree.py")
    task = tree.load_tasks(REPO)["lint:ci-policy"]
    text = " ".join(
        " ".join(str(step if isinstance(step, str) else step.get("cmd", "")).split())
        for step in task.get("cmds") or []
    )
    for job, reason in EXTRA_JOBS.items():
        assert f'--extra-job "{job}={reason}"' in text, job
    for pattern, reason in UNREADABLE_INCLUDES.items():
        assert f'--allow-unreadable-include "{pattern}={reason}"' in text, pattern


def test_a_dangling_optional_need_is_reported(tmp_path) -> None:
    """Mutation case: a need naming a job nothing creates has to be a finding,
    or this suite would pass on a half-finished rename."""
    (tmp_path / ".gitlab-ci.yml").write_text(
        "real-job:\n"
        "  script: [\"true\"]\n"
        "waiting-job:\n"
        "  script: [\"true\"]\n"
        "  needs:\n"
        "    - job: renamed-away\n"
        "      optional: true\n",
        encoding="utf-8",
    )
    result = _run("--require-optional-needs", repo_root=str(tmp_path))
    assert result.returncode == 1, f"{result.stdout}{result.stderr}"
    assert "renamed-away" in result.stderr
