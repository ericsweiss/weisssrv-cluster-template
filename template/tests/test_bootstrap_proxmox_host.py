"""scripts/bootstrap-proxmox-host.sh is the only admin path onto a fresh host.

It runs against a machine with root SSH and nothing else, so the destructive
shapes it must never take are asserted here rather than discovered in the field.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "bootstrap-proxmox-host.sh"
TASKFILE = REPO / "Taskfile.yml"


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_ships_executable():
    assert SCRIPT.is_file(), "bootstrap-proxmox-host.sh is missing"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT.name} is not executable"


def test_no_arguments_prints_usage_and_fails():
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Usage:" in result.stdout


def test_a_key_only_call_still_fails():
    """Mutation case: a host with no key would create a passworded account and
    leave no way in."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "192.0.2.11"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Usage:" in result.stdout


def test_root_is_refused_as_the_admin_user():
    result = subprocess.run(
        ["bash", str(SCRIPT), "192.0.2.11", "ssh-ed25519 AAAA key", "root"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout
    assert "not a usable admin username" in result.stderr


def test_authorized_keys_is_appended_never_truncated():
    """A single `>` here wipes every other admin key on the host."""
    text = source()
    assert re.search(r'>> "\\?\$AUTH"', text), "the key is not appended to authorized_keys"
    assert not re.search(r'[^>]> "\\?\$AUTH"', text), (
        "authorized_keys is written with a truncating redirect"
    )


def test_sudoers_is_validated_before_it_is_installed():
    """visudo -cf has to run on the candidate file first: a bad sudoers rule
    locks every sudo user out of the host, and this is the only admin path in."""
    text = source()
    validate = text.index("visudo -cf")
    install = text.index("install -m 0440")
    assert validate < install, "the sudoers rule is installed before it is validated"


def test_the_ssh_directory_and_key_file_get_restrictive_modes():
    text = source()
    assert "-m 0700" in text, ".ssh is not created mode 0700, so sshd ignores the key"
    assert "chmod 0600" in text, "authorized_keys is not restricted to 0600"


def test_the_default_user_is_this_clusters_admin_user():
    """The default has to match the admin_user the inventory and Taskfile use, or
    the bootstrapped account is one Ansible never connects as."""
    match = re.search(r"DEFAULT_ADMIN_USER='([^']+)'", source())
    assert match, "the script no longer declares a default admin user"
    taskfile = yaml.safe_load(TASKFILE.read_text(encoding="utf-8")) or {}
    expected = (taskfile.get("vars") or {}).get("ADMIN_USER")
    assert expected, "Taskfile.yml declares no ADMIN_USER var to compare against"
    assert match.group(1) == str(expected)
