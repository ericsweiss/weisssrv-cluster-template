"""Every supervised apply task runs scripts/supervised-apply-guard.sh first.

weisssrv-lib tests the guard's refusals; this suite holds the Taskfile wiring.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from conftest import load_script

REPO = Path(__file__).resolve().parent.parent
taskfile_tree = load_script("taskfile_tree.py")
GUARD_REF = "scripts/supervised-apply-guard.sh"
ROOT_ANCHOR = "{{.ROOT_DIR}}/"
APPLY_CMD = "terraform apply"
# The DNS zone is the one apply CI runs on merge, so it carries no guard.
UNGUARDED_APPLY = {"terraform:apply"}


# A script path in exec position: a line start or `$(`, optionally root-anchored.
EXEC_PATH = re.compile(
    r"(?:^|\$\()\s*['\"]?(?:\{\{\.ROOT_DIR\}\}/|\./)?(scripts/[\w.-]+\.sh)\b",
    re.MULTILINE,
)


def _cmd_strings(task: dict) -> list[str]:
    """Each `cmds` entry as text, so a `task:`/`cmd:` mapping is searchable too."""
    return [
        cmd if isinstance(cmd, str) else yaml.safe_dump(cmd, default_flow_style=True)
        for cmd in (task.get("cmds") or [])
    ]


def _apply_tasks(tasks: dict[str, dict]) -> dict[str, list[str]]:
    """Task name -> command strings, for every task running `terraform apply`."""
    found: dict[str, list[str]] = {}
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


def _exec_paths(tasks: dict[str, dict]) -> dict[str, set[str]]:
    """Script path -> task names, for every scripts/*.sh a task runs by path."""
    found: dict[str, set[str]] = {}
    for name, body in tasks.items():
        for cmd in _cmd_strings(body):
            for rel in EXEC_PATH.findall(cmd):
                found.setdefault(rel, set()).add(name)
    return found


def _not_executable(repo: Path, paths: dict[str, set[str]]) -> list[str]:
    """Scripts run by path whose owner x bit is clear; go-task cannot exec them."""
    return [
        f"{rel} (run by {', '.join(sorted(names))}) is not executable"
        for rel, names in sorted(paths.items())
        if not (repo / rel).stat().st_mode & 0o100
    ]


def test_every_supervised_apply_runs_the_guard_first():
    tasks = taskfile_tree.load_tasks(REPO)
    assert tasks, "no Taskfile tasks parsed — this gate is examining nothing"
    applies = _apply_tasks(tasks)
    assert applies, "no task runs `terraform apply` — this gate examines nothing"
    assert set(applies) - UNGUARDED_APPLY, (
        "every `terraform apply` task is exempt — this gate examines nothing"
    )
    violations = _guard_violations(tasks)
    assert not violations, (
        "a Terraform apply can run unsupervised:\n  " + "\n  ".join(violations)
    )


def test_every_guard_call_is_anchored_to_the_repo_root():
    tasks = taskfile_tree.load_tasks(REPO)
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


def test_the_supervised_descriptions_match_the_guarded_tasks():
    """A new apply task is either marked SUPERVISED and guarded, or listed in
    UNGUARDED_APPLY on purpose."""
    tasks = taskfile_tree.load_tasks(REPO)
    applies = set(_apply_tasks(tasks))
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
    """Mutation case: a guard that is absent or too late is reported."""
    assert _guard_violations({"terraform:unifi-apply": {"cmds": cmds}})


def test_the_root_anchor_check_can_fail():
    """Mutation case: the anchor matters only for a task that changes dir."""
    relative = {"cmds": [f"{GUARD_REF} terraform:x y"], "dir": "terraform/x"}
    anchored = {"cmds": [f"{ROOT_ANCHOR}{GUARD_REF} terraform:x y"],
                "dir": "terraform/x"}
    assert _root_anchor_violations({"terraform:x-apply": relative})
    assert not _root_anchor_violations({"terraform:x-apply": anchored})
    assert not _root_anchor_violations({"terraform:x-apply": {"cmds": relative["cmds"]}})


def test_every_script_a_task_runs_by_path_is_executable():
    paths = _exec_paths(taskfile_tree.load_tasks(REPO))
    assert GUARD_REF in paths, "the guard is not run by path — this gate examines nothing"
    missing = [rel for rel in paths if not (REPO / rel).is_file()]
    assert not missing, f"tasks run scripts that do not exist: {missing}"
    violations = _not_executable(REPO, paths)
    assert not violations, "a task cannot start:\n  " + "\n  ".join(violations)


def test_the_exec_bit_check_can_fail(tmp_path):
    """Mutation case: a script run by path without its x bit is reported, and
    one run through `bash` is not collected at all."""
    script = tmp_path / GUARD_REF
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    os.chmod(script, 0o644)
    tasks = {"t:apply": {"cmds": [f"'{ROOT_ANCHOR}{GUARD_REF} t:apply x'"]},
             "t:other": {"cmds": ["bash scripts/other.sh"]}}
    paths = _exec_paths(tasks)
    assert set(paths) == {GUARD_REF}
    assert _not_executable(tmp_path, paths)
    os.chmod(script, 0o755)
    assert not _not_executable(tmp_path, paths)
