"""Every host-touching maintenance task runs through the verify wrapper.

scripts/maintenance-run-with-verify.sh runs the health report whatever the
playbook's outcome, so a bare ansible-playbook call reports success unverified.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MAINTENANCE_TASKFILE = REPO / "taskfiles" / "maintenance.yml"
WRAPPER = "scripts/maintenance-run-with-verify.sh"


def unwrapped(taskfile: str) -> list[str]:
    """`<task>: <cmd>` for every maintenance command that skips the wrapper."""
    tasks = (yaml.safe_load(taskfile) or {}).get("tasks") or {}
    offenders = []
    for name, task in tasks.items():
        if not isinstance(task, dict):
            continue
        for cmd in task.get("cmds") or []:
            if not isinstance(cmd, str) or "ansible-playbook" not in cmd:
                continue
            if WRAPPER not in cmd:
                offenders.append(f"{name}: {cmd.strip()}")
    return offenders


def test_the_taskfile_runs_playbooks_and_the_wrapper_exists():
    """A renamed file or wrapper would make the assertion below vacuous."""
    assert MAINTENANCE_TASKFILE.is_file(), "taskfiles/maintenance.yml is missing"
    assert (REPO / WRAPPER).is_file(), f"{WRAPPER} is missing"
    text = MAINTENANCE_TASKFILE.read_text(encoding="utf-8")
    assert "ansible-playbook" in text, "no maintenance task invokes ansible-playbook"


def test_every_maintenance_playbook_runs_through_the_verify_wrapper():
    offenders = unwrapped(MAINTENANCE_TASKFILE.read_text(encoding="utf-8"))
    assert not offenders, (
        "these maintenance commands invoke ansible-playbook without "
        f"{WRAPPER}, so they report success unverified:\n  " + "\n  ".join(offenders)
    )


def test_a_bare_playbook_invocation_is_caught():
    """Mutation case: the shape this gate exists to fail."""
    mutated = yaml.safe_dump(
        {"tasks": {"update-packages": {"cmds": ["ansible-playbook -i inv play.yml"]}}}
    )
    assert unwrapped(mutated) == ["update-packages: ansible-playbook -i inv play.yml"]
