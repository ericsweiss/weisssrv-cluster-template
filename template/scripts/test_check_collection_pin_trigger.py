"""Unit tests for scripts/check-collection-pin-trigger.py.

Each test builds a pipeline file in a throwaway directory and asserts the exit
code: 0 clean, 1 a deploy job missing the pin, 2 nothing inspected.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent / "check-collection-pin-trigger.py"

CLEAN = """\
deploy-ansible-dns:
  stage: deploy
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
      changes:
        - ansible/playbooks/dns.yml
        - ansible/requirements.yml
  script:
    - ansible-playbook playbooks/dns.yml
"""

MISSING = CLEAN.replace("        - ansible/requirements.yml\n", "")

NO_DEPLOY_JOBS = """\
lint:
  stage: lint
  script:
    - task lint
"""


def _run(tmp_path: Path, ci_text: str) -> subprocess.CompletedProcess:
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(ci_text)
    return subprocess.run(
        [sys.executable, str(GATE), str(ci)], capture_output=True, text=True, check=False
    )


def test_a_job_that_triggers_on_the_pin_passes(tmp_path):
    result = _run(tmp_path, CLEAN)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "triggers on ansible/requirements.yml" in result.stdout


def test_a_job_missing_the_pin_fails(tmp_path):
    """The mutation case: drop the pin from the changes list and the gate reds."""
    result = _run(tmp_path, MISSING)
    assert result.returncode == 1
    assert "deploy-ansible-dns" in result.stdout


def test_a_pipeline_with_no_deploy_job_is_an_error(tmp_path):
    """Inspecting nothing must not read as a clean pipeline."""
    result = _run(tmp_path, NO_DEPLOY_JOBS)
    assert result.returncode == 2
    assert "inspected nothing" in result.stderr


def test_an_unreadable_pipeline_file_is_an_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(GATE), str(tmp_path / "absent.yml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_the_pin_is_inherited_through_extends(tmp_path):
    """A job with no rules of its own takes the parent's changes list."""
    spec = importlib.util.spec_from_file_location("collection_pin", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ci = {
        ".deploy-base": {
            "rules": [{"changes": ["ansible/playbooks/dns.yml", "ansible/requirements.yml"]}]
        },
        "deploy-ansible-dns": {"extends": ".deploy-base"},
    }
    assert module.jobs_missing_the_pin(ci) == []
