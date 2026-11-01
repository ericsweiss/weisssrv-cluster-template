"""The drift-plan jobs detect a console edit; they never reconcile one.

The library include owns the job shape and its suite tests it; this file checks
what each include here passes it.
"""

from __future__ import annotations

import pytest
from conftest import REPO, load_script

ci_yaml = load_script("ci_yaml.py")

CI_FILE = REPO / ".gitlab-ci.yml"
DRIFT_INCLUDE = "/ci/validate/terraform-drift-plan.yml"

needs_pipeline = pytest.mark.skipif(not CI_FILE.is_file(), reason="no .gitlab-ci.yml")


def _drift_includes() -> list[dict]:
    if not CI_FILE.is_file():
        return []
    return [
        entry
        for entry in ci_yaml.load_ci(CI_FILE).get("include") or []
        if isinstance(entry, dict) and entry.get("file") == DRIFT_INCLUDE
    ]


DRIFT = {(e.get("inputs") or {}).get("job_name"): e for e in _drift_includes()}


@needs_pipeline
def test_the_pipeline_includes_the_drift_plans():
    """A renamed include would make every parametrised case below vacuous."""
    assert "authentik-drift-plan" in DRIFT, (
        f"no authentik-drift-plan include of {DRIFT_INCLUDE} — the supervised-apply "
        "roots then have nothing watching them for a console edit"
    )


@pytest.mark.parametrize("name", sorted(DRIFT))
def test_a_drift_include_reads_the_declared_pin_and_a_real_module(name):
    entry = DRIFT[name]
    assert entry.get("ref") == "$[[ inputs.lib_ref ]]", f"{name} is off the declared lib_ref"
    module_dir = entry["inputs"].get("module_dir")
    assert module_dir and (REPO / module_dir).is_dir(), (
        f"{name} plans {module_dir!r}, which the repository lacks"
    )


@pytest.mark.parametrize("name", sorted(DRIFT))
def test_the_scheduled_detector_carries_no_credential_guard(name):
    """A falsy secrets_guard REMOVES the job, so a revoked token would hide
    drift behind a green pipeline instead of reddening the plan."""
    assert "secrets_guard" not in DRIFT[name]["inputs"], (
        f"{name} passes secrets_guard; the library default keeps the detector"
    )
