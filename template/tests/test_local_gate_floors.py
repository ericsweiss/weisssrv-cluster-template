"""The local `task lint` gates that mirror a CI include must keep its floors.

A linter exits 0 on a target list that matches nothing, so the floor is the only
thing that tells "clean" apart from "linted nothing".
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
LINT_TASKFILE = REPO / "taskfiles" / "lint.yml"
FLUX_TASKFILE = REPO / "taskfiles" / "flux.yml"

_FLOOR = re.compile(r'ruff matched no Python file')
_EMPTY_RENDER_FLOOR = re.compile(r'produced no resources')


def ruff_task(taskfile: str) -> dict:
    """The `ruff` task mapping out of a Taskfile tree file."""
    doc = yaml.safe_load(taskfile) or {}
    return (doc.get("tasks") or {}).get("ruff") or {}


def floor_missing(task: dict) -> bool:
    """True when the ruff task runs ruff without first asserting it has work."""
    cmds = [c for c in (task.get("cmds") or []) if isinstance(c, str)]
    if not any("ruff check" in c for c in cmds):
        return False
    return not any(_FLOOR.search(c) for c in cmds)


def test_the_local_ruff_task_keeps_the_linted_nothing_floor():
    """The CI python-lint include fails a target list holding no Python file; the
    local task claims to mirror it, so it carries the same floor."""
    assert LINT_TASKFILE.is_file(), "taskfiles/lint.yml is missing"
    task = ruff_task(LINT_TASKFILE.read_text(encoding="utf-8"))
    assert task, "taskfiles/lint.yml declares no `ruff` task"
    assert not floor_missing(task), (
        "taskfiles/lint.yml's ruff task runs `ruff check` with no linted-nothing "
        "floor: a target list that matches no .py file would pass. Restore the "
        "loop that exits 1 with 'ruff matched no Python file'."
    )


def test_a_floorless_ruff_task_is_reported():
    """Mutation case: the collector, not just the shipped state."""
    bare = {"cmds": ["ruff check --no-cache scripts"]}
    assert floor_missing(bare)
    assert not floor_missing({"cmds": ["echo hi"]})
    assert not floor_missing(
        {
            "cmds": [
                'test -n "$x" || { echo "ERROR: ruff matched no Python file"; exit 1; }',
                "ruff check --no-cache scripts",
            ]
        }
    )


def flux_lint_task(taskfile: str) -> dict:
    """The `lint` task mapping out of taskfiles/flux.yml."""
    doc = yaml.safe_load(taskfile) or {}
    return (doc.get("tasks") or {}).get("lint") or {}


def empty_render_floor_missing(task: dict) -> bool:
    """True when the task builds a Kustomization without asserting it rendered."""
    cmds = [c for c in (task.get("cmds") or []) if isinstance(c, str)]
    if not any("kustomize build" in c for c in cmds):
        return False
    return not any(_EMPTY_RENDER_FLOOR.search(c) for c in cmds)


def test_the_local_flux_lint_task_keeps_the_empty_render_floor():
    """kubeconform exits 0 on empty input, so an emptied `resources:` list would
    lint clean while the reconcile prunes every object the stage applied."""
    assert FLUX_TASKFILE.is_file(), "taskfiles/flux.yml is missing"
    task = flux_lint_task(FLUX_TASKFILE.read_text(encoding="utf-8"))
    assert task, "taskfiles/flux.yml declares no `lint` task"
    assert not empty_render_floor_missing(task), (
        "taskfiles/flux.yml's lint task builds each Kustomization with no "
        "empty-render floor: an emptied resources: list would pass. Restore the "
        "guard that fails with 'produced no resources'."
    )


def test_a_floorless_flux_lint_task_is_reported():
    """Mutation case: the collector, not just the shipped state."""
    assert empty_render_floor_missing({"cmds": ['RAW=$(kustomize build "$SRCPATH")']})
    assert not empty_render_floor_missing({"cmds": ["echo hi"]})
    assert not empty_render_floor_missing(
        {
            "cmds": [
                'RAW=$(kustomize build "$SRCPATH")\n'
                'printf %s "$RAW" | grep -qE "^kind:" || { echo "produced no resources"; exit 1; }',
            ]
        }
    )
