"""Tests for scripts/check-ansible-service-names.py.

Exit 0 clean, 1 a name that is not a systemd unit, 2 nothing scanned.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "check-ansible-service-names.py"

TASK = "- hosts: all\n  tasks:\n    - {}\n"

CASES = [
    ("bare_unit", TASK.format("systemd:\n        name: k3s-agent"), 0),
    ("full_suffix", TASK.format("service:\n        name: nfs-server.service"), 0),
    ("timer", TASK.format("systemd:\n        name: zfs-scrub.timer"), 0),
    ("templated", TASK.format('systemd:\n        name: "{{ svc }}"'), 0),
    ("include_role_is_not_a_unit",
     TASK.format("ansible.builtin.include_role:\n        name: weisssrv.infra.k3s"), 0),
    ("role_fqcn", TASK.format("ansible.builtin.systemd:\n        name: weisssrv.infra.k3s"), 1),
    ("masked_failed_when",
     TASK.format("systemd:\n        name: weisssrv.infra.k3s\n      failed_when: false"), 1),
    ("list_of_units", TASK.format(
        "systemd:\n        name:\n          - unbound.service\n          - weisssrv.infra.adguard"), 1),
    ("free_form", TASK.format("systemd: name=weisssrv.infra.k3s state=restarted"), 1),
    ("inside_a_block", TASK.format("block:\n        - service:\n            name: weisssrv.infra.k3s"), 1),
    ("inventory_data_named_like_the_module",
     "exporter:\n  service:\n    name: weisssrv.infra.node_exporter\n"
     "  systemd:\n    name: some.dotted.value\n", 0),
    ("vault_tag", "secret: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  6162\n", 0),
    ("unparseable", "- hosts: all\n  tasks:\n   - bad: [\n", 2),
]


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("body,rc", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_playbook_shape(tmp_path, body, rc):
    (tmp_path / "play.yml").write_text(body)
    result = _run(tmp_path)
    assert result.returncode == rc, result.stdout + result.stderr
    if rc:
        assert "play.yml" in result.stderr and "ERROR" not in result.stdout


@pytest.mark.parametrize("sub", ["empty", "missing"])
def test_an_empty_or_missing_tree_is_an_error(tmp_path, sub):
    if sub == "empty":
        (tmp_path / sub).mkdir()
    assert _run(tmp_path / sub).returncode == 2


def test_the_real_tree_is_clean():
    result = _run(GATE.parent.parent / "ansible")
    assert result.returncode == 0, result.stdout + result.stderr


def _gate():
    spec = importlib.util.spec_from_file_location("service_names", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,bad", [
    ("weisssrv.infra.k3s", True),
    ("k3s", False),
    ("nfs-server.service", False),
    ("systemd-networkd.socket", False),
    ("home-eric.mount", False),
    ("{{ svc_name }}", False),
])
def test_is_bad_unit(name, bad):
    assert _gate().is_bad_unit(name) is bad
