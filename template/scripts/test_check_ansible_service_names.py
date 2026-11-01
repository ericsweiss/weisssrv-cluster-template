"""Unit tests for scripts/check-ansible-service-names.py.

Each test writes one playbook into a throwaway tree and asserts the exit code:
0 clean, 1 a name that is not a systemd unit, 2 nothing scanned.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent / "check-ansible-service-names.py"


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _playbook(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "play.yml"
    path.write_text(body)
    return path


def test_a_real_unit_name_passes(tmp_path):
    _playbook(tmp_path, "- hosts: all\n  tasks:\n    - systemd:\n        name: k3s-agent\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout


def test_a_full_unit_name_passes(tmp_path):
    _playbook(tmp_path, "- hosts: all\n  tasks:\n    - service:\n        name: nfs-server.service\n")
    assert _run(tmp_path).returncode == 0


def test_a_role_fqcn_fails(tmp_path):
    """The mutation case: a role FQCN resolves to no unit, and `failed_when:
    false` would make that a permanent silent no-op."""
    _playbook(
        tmp_path,
        "- hosts: all\n  tasks:\n    - ansible.builtin.systemd:\n        name: weisssrv.infra.unbound\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "weisssrv.infra.unbound" in result.stderr
    assert "weisssrv.infra.unbound" not in result.stdout


def test_a_templated_name_is_skipped(tmp_path):
    _playbook(tmp_path, "- hosts: all\n  tasks:\n    - systemd:\n        name: \"{{ svc }}\"\n")
    assert _run(tmp_path).returncode == 0


def test_an_empty_tree_is_an_error(tmp_path):
    """Scanning nothing must not read as a clean tree."""
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 2


def test_an_unparseable_file_is_an_error(tmp_path):
    _playbook(tmp_path, "- hosts: all\n  tasks:\n   - bad: [\n")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "could not parse" in result.stderr


def test_a_task_inside_a_block_is_inspected(tmp_path):
    _playbook(
        tmp_path,
        "- hosts: all\n  tasks:\n    - block:\n        - service:\n"
        "            name: weisssrv.infra.k3s\n",
    )
    assert _run(tmp_path).returncode == 1


def test_a_list_of_units_is_inspected(tmp_path):
    _playbook(
        tmp_path,
        "- hosts: all\n  tasks:\n    - systemd:\n        name:\n"
        "          - unbound.service\n          - weisssrv.infra.adguard\n",
    )
    assert _run(tmp_path).returncode == 1


def test_free_form_args_are_inspected(tmp_path):
    _playbook(tmp_path, "- hosts: all\n  tasks:\n    - systemd: name=weisssrv.infra.k3s\n")
    assert _run(tmp_path).returncode == 1


def test_inventory_data_named_like_the_module_is_not_a_task(tmp_path):
    """A `service:` mapping in vars is data, not a module call."""
    _playbook(
        tmp_path,
        "exporter:\n  service:\n    name: weisssrv.infra.node_exporter\n"
        "  systemd:\n    name: some.dotted.value\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_vault_tag_does_not_stop_the_scan(tmp_path):
    _playbook(tmp_path, "secret: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  6162\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_real_tree_is_clean():
    result = _run(GATE.parent.parent / "ansible")
    assert result.returncode == 0, result.stdout + result.stderr
