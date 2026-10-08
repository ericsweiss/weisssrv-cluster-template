"""Runs the vendored `scripts/check-ci-include-job-names.py` against this
repository's real pipeline: every `needs: optional: true` resolves to a job some
include creates. The checker's own unit suite lives in the lib."""

from __future__ import annotations

import subprocess
import sys

from conftest import REPO, SCRIPTS, _lib_root

GATE = SCRIPTS / "check-ci-include-job-names.py"

# Jobs created by a source this gate cannot read. GitLab's managed scanner
# arrives through `template:`, which is a file in neither checkout.
EXTRA_JOBS = {"secret_detection": "GitLab's managed Secret-Detection.gitlab-ci.yml"}


def _run(*extra: str, repo_root: str | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(GATE), "--repo-root", repo_root or str(REPO), *extra]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_every_optional_need_resolves_to_a_job() -> None:
    """A library rename or a non-default `job_name` input turns an optional need
    off with no error, so the gate the pipeline waits for stops blocking it."""
    extra = ["--lib-path", str(_lib_root()), "--require-optional-needs"]
    for job, reason in EXTRA_JOBS.items():
        extra += ["--extra-job", f"{job}={reason}"]
    result = _run(*extra)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


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
