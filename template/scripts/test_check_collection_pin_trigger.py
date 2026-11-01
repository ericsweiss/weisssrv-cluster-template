"""Unit tests for scripts/check-collection-pin-trigger.py.

Each case writes a pipeline file and asserts the exit code: 0 clean, 1 a deploy
job that runs a playbook without triggering on the pin, 2 nothing inspected.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "check-collection-pin-trigger.py"
PIN = "ansible/requirements.yml"

CLEAN = f"""\
deploy-ansible-dns:
  stage: deploy
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
      changes:
        - ansible/playbooks/dns.yml
        - {PIN}
  script:
    - ansible-playbook playbooks/dns.yml
"""

REFERENCED_CHANGES = """\
.paths-ansible:
  changes:
    - ansible/playbooks/base.yml
{extra}deploy-ansible-base:
  stage: deploy
  rules:
    - changes: !reference [.paths-ansible, changes]
"""

CASES = [
    pytest.param(CLEAN, 0, f"triggers on {PIN}", id="clean"),
    pytest.param(CLEAN.replace(f"        - {PIN}\n", ""), 1, "deploy-ansible-dns",
                 id="pin-missing"),
    # The `{paths: [...]}` long form must not read as an empty rule set.
    pytest.param("""\
deploy-ansible-base:
  stage: deploy
  rules:
    - changes:
        paths: [ansible/playbooks/base.yml]
""", 1, "deploy-ansible-base", id="dict-form-changes"),
    pytest.param("""\
.deploy-base:
  stage: deploy
deploy-ansible-certs:
  extends: .deploy-base
  rules:
    - changes: [ansible/playbooks/certs.yml]
""", 1, "deploy-ansible-certs", id="stage-via-extends"),
    # The last parent's rules are the effective ones, as in GitLab.
    pytest.param(f"""\
.rules-pinless:
  rules:
    - changes: [ansible/playbooks/base.yml]
.rules-clean:
  rules:
    - changes: [ansible/playbooks/base.yml, {PIN}]
deploy-ansible-inherited:
  stage: deploy
  extends: [.rules-clean, .rules-pinless]
""", 1, "deploy-ansible-inherited", id="last-parent-rules"),
    pytest.param(f"""\
.deploy-base:
  rules:
    - changes: [ansible/playbooks/dns.yml, {PIN}]
  script:
    - ansible-playbook playbooks/dns.yml
deploy-ansible-dns:
  extends: .deploy-base
""", 0, f"triggers on {PIN}", id="pin-inherited-through-extends"),
    # The script, not the trigger paths, is what makes a job run a playbook.
    pytest.param("""\
.deploy-base:
  before_script: [cd ansible]
  script:
    - op run -- ansible-playbook -i inventories/prod x.yml
deploy-inventory-only:
  stage: deploy
  extends: .deploy-base
  rules:
    - changes: [ansible/inventories/prod/hosts.yml]
""", 1, "deploy-inventory-only", id="script-not-path-trigger"),
    pytest.param("""\
deploy-terraform:
  stage: deploy
  script: [terraform apply]
  rules:
    - changes: [terraform/cloudflare/main.tf]
""", 0, f"triggers on {PIN}", id="non-playbook-deploy-job"),
    pytest.param(REFERENCED_CHANGES.format(extra=""), 1, "deploy-ansible-base",
                 id="reference-changes-missing"),
    pytest.param(REFERENCED_CHANGES.format(extra=f"    - {PIN}\n"), 0,
                 f"triggers on {PIN}", id="reference-changes-present"),
    # GitLab's `spec:` inputs header makes the file two documents.
    pytest.param("""\
spec:
  inputs:
    lib_ref:
      default: v0.1.2
---
.acme-script:
  script:
    - ansible-playbook playbooks/acme.yml
deploy-acme:
  stage: deploy
  script:
    - !reference [.acme-script, script]
""", 1, "deploy-acme", id="spec-header"),
    pytest.param("lint:\n  stage: lint\n  script: [task lint]\n", 2, "inspected nothing",
                 id="nothing-inspected"),
]


def _gate(ci: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), str(ci)], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("ci_text,expected_rc,fragment", CASES)
def test_the_gate(tmp_path, ci_text, expected_rc, fragment):
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(ci_text)
    result = _gate(ci)
    output = result.stdout + result.stderr
    assert result.returncode == expected_rc, output
    assert fragment in output
    assert "Traceback" not in result.stderr


def test_an_unreadable_pipeline_file_is_an_error(tmp_path):
    result = _gate(tmp_path / "absent.yml")
    assert result.returncode == 2
    assert "cannot read" in result.stderr
    assert "Traceback" not in result.stderr
