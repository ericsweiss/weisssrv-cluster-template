"""check-generated-files reports every failed check through the vendored driver.

scripts/ci-run-check.sh is weisssrv-lib's, and the library tests its behaviour.
This suite holds the site half: the job sources it rather than defining its own.
"""

from __future__ import annotations

from conftest import REPO, load_script

ci_yaml = load_script("ci_yaml.py")

CI_FILE = REPO / ".gitlab-ci.yml"
JOB = "check-generated-files"


def test_the_gate_job_sources_the_vendored_driver() -> None:
    doc = ci_yaml.load_ci(CI_FILE)
    script = "\n".join(ci_yaml.script_lines(doc[JOB], doc))
    assert ". scripts/ci-run-check.sh" in script, f"{JOB} no longer sources the driver"
    assert "run_check()" not in script, (
        f"{JOB} defines its own run_check, which shadows the vendored driver"
    )
    assert "run_check_summary" in script, f"{JOB} never reports the failed checks"
