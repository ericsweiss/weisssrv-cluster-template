"""Unit tests for scripts/check-deploy-host-coverage.py.

Each test builds a throwaway repository root and asserts the exit code: 0 full
coverage, 1 an unreached host, 2 an input the gate cannot score.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

GATE = Path(__file__).resolve().parent / "check-deploy-host-coverage.py"

HOSTS = textwrap.dedent(
    """\
    all:
      children:
        web:
          hosts:
            web-01:
            web-02:
    """
)

PLAYBOOK = textwrap.dedent(
    """\
    - hosts: web
      roles:
        - role: weisssrv.infra.base
    """
)


def _repo(tmp_path: Path, ci_script: str) -> Path:
    root = tmp_path / "repo"
    (root / "ansible/inventories/prod").mkdir(parents=True)
    (root / "ansible/playbooks").mkdir(parents=True)
    (root / "ansible/inventories/prod/hosts.yml").write_text(HOSTS)
    (root / "ansible/playbooks/site.yml").write_text(PLAYBOOK)
    (root / ".gitlab-ci.yml").write_text(
        textwrap.dedent(
            f"""\
            deploy-ansible-base:
              stage: deploy
              rules:
                - changes:
                    - ansible/playbooks/site.yml
              script:
                - {ci_script}
            """
        )
    )
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--repo", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_job_that_reaches_every_host_passes(tmp_path):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_limit_that_misses_a_host_fails(tmp_path):
    """The mutation case: a --limit narrower than the play leaves a host that
    nothing in the deploy stage ever converges."""
    root = _repo(
        tmp_path,
        "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01",
    )
    result = _run(root)
    assert result.returncode == 1
    assert "web-02" in result.stderr


def test_a_pipeline_with_no_deploy_invocation_is_an_error(tmp_path):
    """Scoring nothing must not read as full coverage."""
    root = _repo(tmp_path, "echo nothing to deploy")
    result = _run(root)
    assert result.returncode == 2
    assert "no deploy-stage ansible-playbook invocations" in result.stderr


def test_skip_tags_is_refused_rather_than_scored(tmp_path):
    root = _repo(
        tmp_path,
        "ansible-playbook -i inventories/prod playbooks/site.yml --skip-tags slow",
    )
    result = _run(root)
    assert result.returncode == 2
    assert "not modelled" in result.stderr
