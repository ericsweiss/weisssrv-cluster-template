"""scripts/supervised-apply-guard.sh refuses every unsupervised path, and every
supervised apply task runs it first. The guard is the only thing between a stray
flag or a pipeline and a live Terraform apply, so each refusal is asserted.
"""

from __future__ import annotations

import os
import pty
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "supervised-apply-guard.sh"
TASKFILE = REPO_ROOT / "Taskfile.yml"
TASKFILES_DIR = REPO_ROOT / "taskfiles"
GUARD_REF = "scripts/supervised-apply-guard.sh"
ROOT_ANCHOR = "{{.ROOT_DIR}}/"
APPLY_CMD = "terraform apply"
# The DNS zone is the one apply CI runs on merge, so it carries no guard.
UNGUARDED_APPLY = {"terraform:apply"}


def run_without_tty(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GUARD), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def run_with_tty(args: list[str], typed: str) -> int:
    """Drive the guard over a pty so its `[ -t 0 ]` check sees a terminal."""
    parent, child = pty.openpty()
    proc = subprocess.Popen(
        ["bash", str(GUARD), *args],
        stdin=child,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # The slave fd stays open until the child exits, or the write below races the
    # refusal and raises EIO. A refusal that never reads stdin is a pass here.
    try:
        os.write(parent, typed.encode())
    except OSError:
        pass
    proc.communicate(timeout=30)
    os.close(parent)
    os.close(child)
    return proc.returncode


def test_the_guard_ships_executable():
    assert GUARD.is_file(), "the supervised-apply guard is missing"
    assert os.access(GUARD, os.X_OK), f"{GUARD.name} is not executable"


def test_a_pipeline_cannot_apply():
    result = run_without_tty("terraform:unifi-apply", "live VLANs")
    assert result.returncode == 2, result.stderr
    assert "supervised operator step" in result.stderr


def test_missing_arguments_are_a_usage_error():
    assert run_without_tty("terraform:unifi-apply").returncode == 2


@pytest.mark.parametrize("spelling", ["-auto-approve", "-auto-approve=true", "--auto-approve"])
def test_auto_approve_is_refused_even_from_a_terminal(spelling):
    """Every spelling terraform accepts, refused as an operator error. A pattern
    anchored to the bare flag passes the other two straight through to apply."""
    assert run_with_tty(["terraform:unifi-apply", "live VLANs", spelling], "apply\n") == 2


@pytest.mark.parametrize("typed", ["\n", "yes\n", "APPLY\n"])
def test_only_the_exact_word_applies(typed):
    assert run_with_tty(["terraform:unifi-apply", "live VLANs"], typed) == 1


def test_the_typed_confirmation_passes():
    assert run_with_tty(["terraform:unifi-apply", "live VLANs"], "apply\n") == 0


def _taskfiles() -> list[Path]:
    """Taskfile.yml plus every namespace file, when the tree splits them."""
    files = [TASKFILE] if TASKFILE.is_file() else []
    if TASKFILES_DIR.is_dir():
        files.extend(sorted(TASKFILES_DIR.glob("*.yml")))
    return files


def _tasks() -> dict[str, dict]:
    """Fully qualified task name -> task body, across the Taskfile tree.

    A task in taskfiles/<ns>.yml is addressed as `<ns>:<task>`, so the file stem
    is the namespace prefix.
    """
    tasks: dict[str, dict] = {}
    for path in _taskfiles():
        doc = yaml.safe_load(path.read_text()) or {}
        prefix = "" if path == TASKFILE else f"{path.stem}:"
        for name, body in (doc.get("tasks") or {}).items():
            if isinstance(body, dict):
                tasks[f"{prefix}{name}"] = body
    return tasks


def _cmd_strings(task: dict) -> list[str]:
    """Each `cmds` entry as text, so a `task:`/`cmd:` mapping is searchable too."""
    return [
        cmd if isinstance(cmd, str) else yaml.safe_dump(cmd, default_flow_style=True)
        for cmd in (task.get("cmds") or [])
    ]


def _apply_tasks(tasks: dict[str, dict]) -> dict[str, list[str]]:
    """Task name -> command strings, for every task running `terraform apply`."""
    found = {}
    for name, body in tasks.items():
        cmds = _cmd_strings(body)
        if any(APPLY_CMD in cmd for cmd in cmds):
            found[name] = cmds
    return found


def _supervised_descs(tasks: dict[str, dict]) -> set[str]:
    return {
        name
        for name, body in tasks.items()
        if str(body.get("desc") or "").startswith("SUPERVISED")
    }


def _guard_violations(tasks: dict[str, dict]) -> list[str]:
    """Apply tasks outside UNGUARDED_APPLY whose first command is not the guard."""
    violations = []
    for name, cmds in sorted(_apply_tasks(tasks).items()):
        if name in UNGUARDED_APPLY:
            continue
        guarded = [i for i, cmd in enumerate(cmds) if GUARD_REF in cmd]
        applied = [i for i, cmd in enumerate(cmds) if APPLY_CMD in cmd]
        if not guarded:
            violations.append(f"{name} applies without calling {GUARD_REF}")
        elif guarded[0] != 0:
            violations.append(f"{name} runs {cmds[0]!r} before the guard")
        elif applied and guarded[0] > applied[0]:
            violations.append(f"{name} applies before the guard runs")
    return violations


def test_every_supervised_apply_runs_the_guard_first():
    tasks = _tasks()
    assert tasks, "no Taskfile tasks parsed — this gate is examining nothing"
    applies = _apply_tasks(tasks)
    assert applies, "no task runs `terraform apply` — this gate is examining nothing"
    assert set(applies) - UNGUARDED_APPLY, (
        "every `terraform apply` task is exempt — this gate is examining nothing"
    )
    violations = _guard_violations(tasks)
    assert not violations, (
        "a Terraform apply can run unsupervised:\n  " + "\n  ".join(violations)
    )


def _root_anchor_violations(tasks: dict[str, dict]) -> list[str]:
    """Guard calls in a task that sets `dir:` and does not anchor the path.

    go-task runs every cmd with `dir:` as cwd, so a repo-relative guard path
    exits 127 and the apply task dies before it can ask anything.
    """
    violations = []
    for name, body in sorted(tasks.items()):
        if not body.get("dir"):
            continue
        for cmd in _cmd_strings(body):
            if GUARD_REF in cmd and ROOT_ANCHOR + GUARD_REF not in cmd:
                violations.append(f"{name} calls {GUARD_REF} relative to its dir:")
    return violations


def test_every_guard_call_is_anchored_to_the_repo_root():
    tasks = _tasks()
    callers = [
        name
        for name, body in tasks.items()
        if any(GUARD_REF in cmd for cmd in _cmd_strings(body))
    ]
    assert callers, "no task calls the guard — this gate is examining nothing"
    violations = _root_anchor_violations(tasks)
    assert not violations, (
        "a supervised apply cannot reach its guard:\n  " + "\n  ".join(violations)
    )


def test_the_root_anchor_check_can_fail():
    """Mutation case: the anchor matters only for a task that changes directory."""
    relative = {"cmds": [f"{GUARD_REF} terraform:x y"], "dir": "terraform/x"}
    anchored = {"cmds": [f"{ROOT_ANCHOR}{GUARD_REF} terraform:x y"], "dir": "terraform/x"}
    assert _root_anchor_violations({"terraform:x-apply": relative})
    assert not _root_anchor_violations({"terraform:x-apply": anchored})
    assert not _root_anchor_violations({"terraform:x-apply": {"cmds": relative["cmds"]}})


def test_the_supervised_descriptions_match_the_guarded_tasks():
    """A new apply task is either marked SUPERVISED and guarded, or listed in
    UNGUARDED_APPLY on purpose."""
    tasks = _tasks()
    applies = set(_apply_tasks(tasks))
    assert applies, "no task runs `terraform apply` — this gate is examining nothing"
    expected = applies - UNGUARDED_APPLY
    assert expected, "no supervised apply task — this gate is examining nothing"
    assert _supervised_descs(tasks) & applies == expected, (
        "the apply tasks whose desc starts with SUPERVISED are "
        f"{sorted(_supervised_descs(tasks) & applies)} but the guarded set is "
        f"{sorted(expected)}"
    )


@pytest.mark.parametrize(
    "cmds",
    [
        pytest.param(["op run -- terraform apply"], id="guard-dropped"),
        pytest.param(
            ["op run -- terraform apply", f"{GUARD_REF} terraform:unifi-apply x"],
            id="guard-after-apply",
        ),
    ],
)
def test_the_wiring_check_can_fail(cmds):
    """Mutation case: the collector reports a guard that is absent or too late."""
    assert _guard_violations({"terraform:unifi-apply": {"cmds": cmds}})
