"""Every `needs: optional: true` resolves to a job something creates.

GitLab ignores an optional need naming no job, so a moved `job_name` default
turns a dependency off silently. Runs the vendored gate at the pinned ref.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO, SCRIPTS, _lib_root, load_script, pinned_lib_ref, require_ref

GATE = SCRIPTS / "check-ci-include-job-names.py"
LINT_TASK = "lint:ci-policy"
# GitLab's managed scanner arrives through `template:`, a file in neither
# checkout, so its job and its include are declared with a reason.
ALLOWANCES = (
    "--extra-job",
    "secret_detection=GitLab's managed Secret-Detection.gitlab-ci.yml",
    "--allow-unreadable-include",
    "Jobs/Secret-Detection.gitlab-ci.yml=GitLab's managed scanner template, a file in neither checkout",
)


@pytest.fixture(scope="module")
def lib_tree(tmp_path_factory) -> Path:
    """The library's `ci/` tree at the pinned ref, so a job the pin lacks cannot resolve."""
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
    run = subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(REPO), "--lib-path", str(lib_tree),
         "--require-optional-needs", *ALLOWANCES],
        capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, f"{run.stdout}{run.stderr}"
    assert "optional need(s) resolve" in run.stdout, (
        f"the gate resolved no optional need at all:\n{run.stdout}{run.stderr}"
    )


def test_the_lint_task_passes_the_same_allowances() -> None:
    """`task lint:ci-policy` and this suite run the gate with one set of allowances."""
    task = load_script("taskfile_tree.py").load_tasks(REPO)[LINT_TASK]
    text = " ".join(
        " ".join(str(step if isinstance(step, str) else step.get("cmd", "")).split())
        for step in task.get("cmds") or []
    )
    for flag, value in zip(ALLOWANCES[::2], ALLOWANCES[1::2]):
        assert f'{flag} "{value}"' in text, f"{LINT_TASK} lacks {flag} {value}"
