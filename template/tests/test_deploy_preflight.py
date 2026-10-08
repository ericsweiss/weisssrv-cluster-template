"""Coverage for deploy-preflight.py.

The gate runs against fixture repositories. Each mutation case must FAIL, which
proves the gate does not report clean on a pipeline it never read.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import require_tool

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "deploy-preflight.py"

PLAYBOOK = textwrap.dedent(
    """\
    ---
    - name: Fixture play
      hosts: localhost
      gather_facts: false
      tasks:
        - name: A tagged task
          ansible.builtin.debug:
            msg: tagged
          tags: [real]
    """
)


# A play whose pattern matches nothing in the inventory. `--list-hosts` prints
# `hosts (0):` and exits 0, so the run reads green without the host check.
ORPHAN_PLAYBOOK = textwrap.dedent(
    """\
    ---
    - name: Fixture play against a group that does not exist
      hosts: renamed_group
      gather_facts: false
      tasks:
        - name: A tagged task
          ansible.builtin.debug:
            msg: tagged
          tags: [real]
    """
)


# A play against an inventory GROUP. Ansible invents an implicit localhost, so
# only a group proves the `-i` the job wrote actually reached the play.
GROUP_PLAYBOOK = textwrap.dedent(
    """\
    ---
    - name: Fixture play against an inventory group
      hosts: deploy_targets
      gather_facts: false
      tasks:
        - name: A tagged task
          ansible.builtin.debug:
            msg: tagged
          tags: [real]
    """
)


def build(tmp_path: Path, jobs: str, playbook_name: str = "site.yml") -> Path:
    """A minimal repository the gate can walk: one pipeline, one playbook."""
    (tmp_path / "ansible" / "playbooks").mkdir(parents=True)
    (tmp_path / "ansible" / "playbooks" / playbook_name).write_text(PLAYBOOK)
    (tmp_path / "ansible" / "ansible.cfg").write_text("[defaults]\ninventory = inventory.ini\n")
    (tmp_path / "ansible" / "inventory.ini").write_text("localhost ansible_connection=local\n")
    (tmp_path / ".gitlab-ci.yml").write_text(jobs)
    return tmp_path


def run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)], cwd=repo, capture_output=True, text=True
    )


def job(script: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        .deploy-base:
          stage: deploy

        deploy-fixture:
          extends: .deploy-base
          script:
            - {script}
        """
    )


def require_ansible() -> None:
    """The gate shells out to `ansible-playbook --list-tasks`, so the cases below
    check nothing without it. Under $CI that is a job that never installed
    ansible-core, and the tag-selection coverage would evaporate silently."""
    require_tool(
        "ansible-playbook",
        "deploy-preflight tag selection",
        "Install ansible-core in the job that runs this suite.",
    )


def test_the_gate_exists():
    assert GATE.is_file(), "deploy-preflight.py is gone but the CI job still calls it"


def test_a_missing_playbook_fails(tmp_path):
    require_ansible()
    repo = build(tmp_path, job("ansible-playbook -i inventory.ini playbooks/gone.yml"))
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "does not exist" in result.stderr


def test_a_pipeline_with_no_deploy_job_fails(tmp_path):
    """Selection that stopped matching must not read as a clean run."""
    require_ansible()
    repo = build(
        tmp_path,
        textwrap.dedent(
            """\
            ---
            lint:
              script:
                - echo nothing to deploy
            """
        ),
    )
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "resolved 0 playbooks" in result.stderr


def test_an_unparseable_invocation_fails(tmp_path):
    """A job shape the parser cannot read loses coverage silently otherwise."""
    require_ansible()
    repo = build(tmp_path, job("ansible-playbook --help"))
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "parser does not understand" in result.stderr


def test_a_tag_selecting_no_task_fails(tmp_path):
    require_ansible()
    repo = build(
        tmp_path,
        job("ansible-playbook -i inventory.ini playbooks/site.yml --tags nosuchtag"),
    )
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "selects NO task" in result.stderr


def test_a_tag_that_selects_a_task_passes(tmp_path):
    require_ansible()
    repo = build(
        tmp_path,
        job("ansible-playbook -i inventory.ini playbooks/site.yml --tags real"),
    )
    result = run(repo)
    assert result.returncode == 0, result.stderr
    # A run that inspected no selection reports "0 tag selection(s) checked",
    # which is green for the same reason a broken parser would be.
    assert "1 playbook(s), 1 tag selection(s) checked" in result.stdout


def test_skip_tags_is_not_read_as_a_selection(tmp_path):
    """--skip-tags excludes rather than selects, so an inert value there is not a
    no-op step: only the --tags selection is checked."""
    require_ansible()
    repo = build(
        tmp_path,
        job(
            "ansible-playbook -i inventory.ini playbooks/site.yml "
            "--tags real --skip-tags nosuchtag"
        ),
    )
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 playbook(s), 1 tag selection(s) checked" in result.stdout


def test_a_skip_tags_only_job_selects_nothing_to_check(tmp_path):
    """The playbook still resolves, and no tag selection is scored: a widened
    flag regex would check `nosuchtag` here and fail a healthy job."""
    require_ansible()
    repo = build(
        tmp_path,
        job("ansible-playbook -i inventory.ini playbooks/site.yml --skip-tags nosuchtag"),
    )
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 playbook(s), 0 tag selection(s) checked" in result.stdout


REFERENCED_SCRIPT = textwrap.dedent(
    """\
    ---
    .deploy-base:
      stage: deploy
      script:
        - ansible-playbook -i inventory.ini playbooks/site.yml --tags real

    deploy-referenced:
      extends: .deploy-base
      script: !reference [.deploy-base, script]

    deploy-elsewhere:
      extends: .deploy-base
      script: !reference [.from-an-include, script]

    deploy-inherited:
      extends: .deploy-base

    deploy-inline:
      extends: .deploy-base
      script:
        - ansible-playbook -i inventory.ini playbooks/site.yml --tags real
    """
)


def test_a_reference_in_this_file_is_expanded(tmp_path):
    """`!reference` to a template in the same pipeline is followed, so the job's
    real invocation is checked rather than reported unreadable."""
    require_ansible()
    repo = build(tmp_path, REFERENCED_SCRIPT)
    result = run(repo)
    assert "OK   deploy-referenced: playbooks/site.yml --tags real" in result.stdout
    assert "deploy-referenced" not in result.stderr


def test_an_unresolvable_reference_or_inherited_script_fails(tmp_path):
    """A job whose script the gate cannot read must not vanish behind a sibling
    that still carries an inline invocation."""
    require_ansible()
    repo = build(tmp_path, REFERENCED_SCRIPT)
    result = run(repo)
    assert result.returncode == 1, result.stdout
    for name in ("deploy-elsewhere", "deploy-inherited"):
        assert f"{name}: script: was not inspected" in result.stderr
    assert ".from-an-include script" in result.stderr
    assert "deploy-inline" not in result.stderr


def test_a_pipeline_with_an_inputs_spec_header_is_still_read(tmp_path):
    """GitLab's inputs syntax makes a pipeline two documents. Reading only the
    first leaves the gate with no jobs, and it fails on a healthy pipeline."""
    require_ansible()
    spec_header = textwrap.dedent(
        """\
        ---
        spec:
          inputs:
            environment:
              default: prod
        ---
        """
    )
    repo = build(
        tmp_path,
        spec_header
        + job("ansible-playbook -i inventory.ini playbooks/site.yml --tags real").removeprefix(
            "---\n"
        ),
    )
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 playbook(s), 1 tag selection(s) checked" in result.stdout


def test_a_limit_matching_no_host_fails(tmp_path):
    """A `--limit` that intersects a play to zero hosts leaves the deploy job
    green: ansible exits 0 with "skipping: no hosts matched"."""
    require_ansible()
    repo = build(
        tmp_path,
        job(
            "ansible-playbook -i inventory.ini playbooks/site.yml "
            "--limit nosuchgroup --tags real"
        ),
    )
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "nosuchgroup" in result.stderr


def test_a_limit_the_inventory_satisfies_passes(tmp_path):
    require_ansible()
    repo = build(
        tmp_path,
        job(
            "ansible-playbook -i inventory.ini playbooks/site.yml "
            "--limit localhost --tags real"
        ),
    )
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


STUB_LIST_HOSTS = """\
#!/bin/sh
# --list-hosts with no `hosts (N):` banner, and a selected task so only the
# host arm can fire.
case "$*" in
  *--list-hosts*) echo "playbook: playbooks/site.yml"; exit 0 ;;
  *) echo "      tasks:"; echo "        A tagged task  TAGS: [real]"; exit 0 ;;
esac
"""


def test_unreadable_list_hosts_output_is_not_a_pass(tmp_path):
    """Mutation case: no `hosts (N):` line means the host check was skipped, so
    reporting clean would hide every limit that matches nothing."""
    repo = build(
        tmp_path,
        job("ansible-playbook -i inventory.ini playbooks/site.yml --limit localhost"),
    )
    stub_bin = repo / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "ansible-playbook"
    stub.write_text(STUB_LIST_HOSTS)
    stub.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1, result.stdout
    assert "printed no" in result.stderr


def test_a_malformed_pipeline_is_an_input_error_not_a_finding(tmp_path):
    """Exit 2, not 1: a pipeline the gate could not parse inspected no deploy
    job at all, and a findings exit code invites an exemption instead of a fix."""
    repo = build(tmp_path, "deploy-fixture: [unclosed\n")
    result = run(repo)
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "no deploy job was inspected" in result.stderr


def test_a_missing_pipeline_is_an_input_error(tmp_path):
    repo = build(tmp_path, job("ansible-playbook -i inventory.ini playbooks/site.yml"))
    (repo / ".gitlab-ci.yml").unlink()
    result = run(repo)
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "no deploy job was inspected" in result.stderr


def test_a_limitless_play_matching_no_host_fails(tmp_path):
    """A job with no --limit is in scope too: an inventory group that was renamed
    leaves the whole deploy step a no-op that still exits 0."""
    require_ansible()
    repo = build(tmp_path, job("ansible-playbook -i inventory.ini playbooks/orphan.yml"))
    (repo / "ansible" / "playbooks" / "orphan.yml").write_text(ORPHAN_PLAYBOOK)
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "matches NO host in any play" in result.stderr
    assert "its own hosts: patterns" in result.stderr, (
        "the finding must name the scope it checked, so a limitless job is "
        "distinguishable from a --limit one"
    )


@pytest.mark.parametrize("flag", ["-igroups.ini", "--inventory=groups.ini"])
def test_an_attached_inventory_flag_reaches_the_play(tmp_path, flag):
    """Unparsed, these spellings leave `inventory` None and the gate runs the
    play with no `-i` at all, so every host check it makes is vacuous."""
    require_ansible()
    repo = build(
        tmp_path,
        job(f"ansible-playbook {flag} playbooks/groups.yml --tags real"),
        playbook_name="groups.yml",
    )
    (repo / "ansible" / "playbooks" / "groups.yml").write_text(GROUP_PLAYBOOK)
    (repo / "ansible" / "groups.ini").write_text(
        "[deploy_targets]\nlocalhost ansible_connection=local\n"
    )
    result = run(repo)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_a_missing_pyyaml_is_an_operator_error(tmp_path):
    """Exit 2, not 1: a broken dependency install is not a policy finding."""
    repo = build(tmp_path, job("ansible-playbook -i inventory.ini playbooks/site.yml"))
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "yaml.py").write_text('raise ImportError("poisoned")\n')
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(poison)},
    )
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "PyYAML required" in result.stderr


def test_a_missing_ansible_playbook_is_an_operator_error(tmp_path):
    """Every check shells out to it, so a job that never installed ansible-core
    must not read as a pipeline with no no-op deploy step."""
    repo = build(tmp_path, job("ansible-playbook -i inventory.ini playbooks/site.yml"))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(empty)},
    )
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "ansible-playbook is not on PATH" in result.stderr
