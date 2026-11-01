"""Unit tests for scripts/check-deploy-host-coverage.py.

Each test builds a throwaway repo and asserts the exit code: 0 full coverage,
1 an unreached host or a stale acknowledgement, 2 an input it cannot score.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


def _write_playbook(root: Path, name: str, body: str) -> None:
    (root / "ansible/playbooks" / name).write_text(textwrap.dedent(body))


def test_a_role_in_an_imported_playbook_is_declared(tmp_path):
    """import_playbook pulls its plays in, so their roles must be reached too."""
    root = _repo(
        tmp_path,
        "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01",
    )
    _write_playbook(root, "site.yml", """\
        ---
        - ansible.builtin.import_playbook: extra.yml
        """)
    _write_playbook(root, "extra.yml", """\
        ---
        - hosts: web
          roles:
            - role: weisssrv.infra.base
        """)
    result = _run(root)
    assert result.returncode == 1
    assert "web-02" in result.stderr


def test_an_import_playbook_tag_reaches_the_imported_roles(tmp_path):
    """`--tags foo` selects every role in a playbook imported with `tags: [foo]`."""
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --tags foo")
    _write_playbook(root, "site.yml", """\
        ---
        - import_playbook: base.yml
          tags: [foo]
        """)
    _write_playbook(root, "base.yml", PLAYBOOK)
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_role_pulled_in_by_include_role_is_declared(tmp_path):
    root = _repo(
        tmp_path,
        "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01",
    )
    _write_playbook(root, "site.yml", """\
        ---
        - hosts: web
          tasks:
            - name: Grouped
              block:
                - ansible.builtin.include_role:
                    name: weisssrv.infra.base
        """)
    result = _run(root)
    assert result.returncode == 1
    assert "weisssrv.infra.base" in result.stderr


def test_an_import_cycle_is_an_error(tmp_path):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    _write_playbook(root, "site.yml", "---\n- import_playbook: site.yml\n")
    result = _run(root)
    assert result.returncode == 2
    assert "cycle" in result.stderr


def test_a_malformed_playbook_exits_2_not_a_traceback(tmp_path):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    _write_playbook(root, "site.yml", "- hosts: web\n  roles: [base\n")
    result = _run(root)
    assert result.returncode == 2, result.stderr
    assert "could not be parsed" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_acknowledgement_for_a_fully_reached_role_is_stale(tmp_path, monkeypatch):
    gate = _load_gate()
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    monkeypatch.setitem(gate.ACKNOWLEDGED_GAPS, "base", "reason")
    assert gate.main(["--repo", str(root)]) == 1


def test_an_acknowledgement_for_a_real_gap_is_honoured(tmp_path, monkeypatch):
    gate = _load_gate()
    root = _repo(
        tmp_path,
        "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01",
    )
    monkeypatch.setitem(gate.ACKNOWLEDGED_GAPS, "base", "reason")
    assert gate.main(["--repo", str(root)]) == 0


def test_an_acknowledgement_for_an_undeclared_role_is_orphaned(tmp_path, monkeypatch):
    """No deploy playbook declares the role, so the entry would hide its first gap."""
    gate = _load_gate()
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    monkeypatch.setitem(gate.ACKNOWLEDGED_GAPS, "never_declared", "reason")
    assert gate.main(["--repo", str(root)]) == 1


INCLUDE_ROLE_TAGGED = """\
    - hosts: web
      tasks:
        - name: Pull the role in
          ansible.builtin.include_role:
            name: weisssrv.infra.base
          tags: [foo]
    """


def test_a_tagged_include_role_is_not_covered_by_its_own_tag(tmp_path):
    """`--tags foo` runs the include but filters out the role's untagged tasks."""
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --tags foo")
    _write_playbook(root, "site.yml", INCLUDE_ROLE_TAGGED)
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "web-01" in result.stderr and "web-02" in result.stderr


def test_an_include_role_apply_tag_covers_the_role(tmp_path):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --tags foo")
    _write_playbook(root, "site.yml", INCLUDE_ROLE_TAGGED.replace(
        "name: weisssrv.infra.base", "name: weisssrv.infra.base\n            apply:\n              tags: [foo]"))
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_tagged_import_role_is_covered_by_its_tag(tmp_path):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --tags foo")
    _write_playbook(root, "site.yml", INCLUDE_ROLE_TAGGED.replace("include_role", "import_role"))
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


def _task_file_repo(tmp_path, ci_script: str, include: str = "ansible.builtin.include_tasks") -> Path:
    root = _repo(tmp_path, ci_script)
    _write_playbook(root, "site.yml", f"""\
        - hosts: web
          tasks:
            - name: Shared steps
              {include}: tasks/_shared.yml
        """)
    (root / "ansible/playbooks/tasks").mkdir()
    _write_playbook(root, "tasks/_shared.yml", """\
        - name: Pull the role in
          ansible.builtin.include_role:
            name: weisssrv.infra.acme
        """)
    return root


@pytest.mark.parametrize("include", ["ansible.builtin.include_tasks", "import_tasks"])
def test_a_role_behind_a_task_file_is_declared(tmp_path, include):
    root = _task_file_repo(
        tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01", include
    )
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "weisssrv.infra.acme" in result.stderr and "web-02" in result.stderr


def test_a_role_behind_a_task_file_is_covered(tmp_path):
    root = _task_file_repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


TAGGED_TASK_FILE = """\
    - hosts: web
      tasks:
        - name: Shared steps
          {include}:
            file: tasks/_shared.yml{apply}
          tags: [foo]
    """


@pytest.mark.parametrize(
    ("include", "apply", "covered"),
    [
        ("ansible.builtin.include_tasks", "", False),
        ("import_tasks", "", True),
        ("ansible.builtin.include_tasks", "\n            apply:\n              tags: [foo]", True),
    ],
    ids=["include-own-tag", "import-own-tag", "include-apply-tag"],
)
def test_a_tagged_task_file_include_scores_its_roles_by_ansibles_tag_rule(
    tmp_path, include, apply, covered
):
    """A dynamic include's own tags select only the include, never the roles
    inside it; import_tasks and `apply` tags reach the contents."""
    root = _task_file_repo(
        tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --tags foo"
    )
    _write_playbook(root, "site.yml", TAGGED_TASK_FILE.format(include=include, apply=apply))
    result = _run(root)
    if covered:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "web-01" in result.stderr


def test_a_task_file_resolves_under_the_tasks_directory(tmp_path):
    """`include_tasks: _shared.yml` finds playbooks/tasks/_shared.yml, as Ansible does."""
    root = _task_file_repo(
        tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml --limit web-01"
    )
    _write_playbook(root, "site.yml", """\
        - hosts: web
          tasks:
            - ansible.builtin.include_tasks: _shared.yml
        """)
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "weisssrv.infra.acme" in result.stderr and "web-02" in result.stderr


@pytest.mark.parametrize(
    ("target", "message"),
    [("tasks/missing.yml", "does not exist"), ("tasks/{{ which }}.yml", "not a literal path")],
)
def test_a_task_file_the_gate_cannot_follow_exits_2(tmp_path, target, message):
    root = _task_file_repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    _write_playbook(root, "site.yml", f"""\
        - hosts: web
          tasks:
            - ansible.builtin.include_tasks: "{target}"
        """)
    result = _run(root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr


def test_a_task_file_cycle_exits_2(tmp_path):
    root = _task_file_repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    _write_playbook(root, "tasks/_shared.yml", "- ansible.builtin.import_tasks: _shared.yml\n")
    result = _run(root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cycle" in result.stderr


@pytest.mark.parametrize("target", ["ansible/inventories/prod/hosts.yml", ".gitlab-ci.yml"])
def test_a_malformed_inventory_or_pipeline_exits_2_not_a_traceback(tmp_path, target):
    root = _repo(tmp_path, "ansible-playbook -i inventories/prod playbooks/site.yml")
    (root / target).write_text("key: [unclosed\n")
    result = _run(root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "could not be parsed" in result.stderr


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_deploy_host_coverage", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_real_tree_passes():
    result = subprocess.run(
        [sys.executable, str(GATE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
