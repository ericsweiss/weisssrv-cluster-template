"""Flatten the Taskfile.yml `includes:` tree into one {name: definition} map.

Task names are namespaced by the include key, so `flux:lint` is `lint` in
taskfiles/flux.yml. `- task:` refs in cmds are rewritten fully-qualified.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
ROOT_TASKFILE = "Taskfile.yml"


def _qualify(ref: str, namespace: str) -> str:
    """Resolve one `- task:` reference the way go-task does.

    A leading colon is root-relative; a bare name inside a namespace resolves
    within that namespace.
    """
    if ref.startswith(":"):
        return ref[1:]
    if namespace:
        return f"{namespace}:{ref}"
    return ref


def _rewrite_refs(task: dict, namespace: str) -> dict:
    task = copy.deepcopy(task)
    cmds = task.get("cmds")
    if isinstance(cmds, list):
        for cmd in cmds:
            if isinstance(cmd, dict) and isinstance(cmd.get("task"), str):
                cmd["task"] = _qualify(cmd["task"], namespace)
    deps = task.get("deps")
    if isinstance(deps, list):
        for i, dep in enumerate(deps):
            if isinstance(dep, str):
                deps[i] = _qualify(dep, namespace)
            elif isinstance(dep, dict) and isinstance(dep.get("task"), str):
                dep["task"] = _qualify(dep["task"], namespace)
    return task


def include_paths(repo: Path | None = None) -> dict[str, Path]:
    """{namespace: taskfile path} for every entry in the root `includes:`."""
    repo = repo or REPO
    doc = yaml.safe_load((repo / ROOT_TASKFILE).read_text(encoding="utf-8")) or {}
    paths: dict[str, Path] = {}
    for namespace, entry in (doc.get("includes") or {}).items():
        raw = entry.get("taskfile") if isinstance(entry, dict) else entry
        paths[namespace] = repo / raw
    return paths


def taskfile_paths(repo: Path | None = None) -> list[Path]:
    """The root Taskfile plus every included file, in include order."""
    repo = repo or REPO
    return [repo / ROOT_TASKFILE, *include_paths(repo).values()]


def load_tasks(repo: Path | None = None) -> dict[str, dict]:
    """Every task in the tree, keyed by the name `task <name>` would run."""
    repo = repo or REPO
    doc = yaml.safe_load((repo / ROOT_TASKFILE).read_text(encoding="utf-8")) or {}
    tasks: dict[str, dict] = {
        name: _rewrite_refs(body or {}, "") for name, body in (doc.get("tasks") or {}).items()
    }
    for namespace, path in include_paths(repo).items():
        included = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, body in (included.get("tasks") or {}).items():
            tasks[f"{namespace}:{name}"] = _rewrite_refs(body or {}, namespace)
    return tasks


def task_names(repo: Path | None = None) -> set[str]:
    """Names an operator can type, i.e. everything except internal helpers."""
    return {name for name, body in load_tasks(repo).items() if not body.get("internal")}
