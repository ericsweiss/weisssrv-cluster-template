#!/usr/bin/env python3
"""Every deploy job that runs a playbook must also trigger on the collection pin.

`ansible/requirements.yml` is the only in-repo signal that a role changed, so a
job missing it keeps deploying old roles. Zero deploy jobs inspected exits 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from ci_playbook_invocations import parse_invocations
    from ci_yaml import Reference, load_ci, script_lines
except ImportError:
    print(
        "ERROR: ci_yaml.py and ci_playbook_invocations.py must sit next to this "
        "script; vendor them from weisssrv-lib.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
CI_FILE = REPO / ".gitlab-ci.yml"
PIN = "ansible/requirements.yml"


def _load_ci(path: Path) -> dict:
    """The jobs document of a pipeline file, `!reference` nodes kept as data."""
    return load_ci(path)


def _changes_of(rule, doc: dict) -> list:
    """The `changes:` value of one rule, following a `!reference` and the
    `{paths: [...]}` long form."""
    if isinstance(rule, Reference):
        rule = rule.resolve(doc)
    if isinstance(rule, list):
        # `rules: !reference [...]` resolves to a whole rule list.
        return [item for sub in rule for item in _changes_of(sub, doc)]
    if not isinstance(rule, dict):
        return []
    changes = rule.get("changes")
    if isinstance(changes, Reference):
        changes = changes.resolve(doc)
    if isinstance(changes, dict):
        changes = changes.get("paths")
    return changes if isinstance(changes, list) else []


def rule_paths(job: dict, doc: dict | None = None) -> set[str]:
    """Every literal path a job's `rules:changes:` triggers on, references
    resolved."""
    doc = doc if doc is not None else {}
    rules = job.get("rules")
    if isinstance(rules, Reference):
        rules = rules.resolve(doc)
    paths: set[str] = set()
    for rule in rules or []:
        paths.update(p for p in _changes_of(rule, doc) if isinstance(p, str))
    return paths


def deploy_jobs(ci: dict) -> dict[str, dict]:
    """Jobs named `deploy-*`. The name prefix is the whole convention; the
    stage is not consulted, so an inherited stage cannot hide a job."""
    return {
        name: job
        for name, job in ci.items()
        if isinstance(job, dict) and name.startswith("deploy-") and not name.startswith(".")
    }


def _effective(name: str, ci: dict, key: str, seen: tuple[str, ...] = ()):
    """A job's `key` after `extends:` resolution, last-parent-wins.

    A job's own key wins outright; otherwise parents are searched in reverse
    declaration order. None means no ancestor declares the key."""
    if name in seen:
        return None
    job = ci.get(name)
    if not isinstance(job, dict):
        return None
    if key in job:
        return job.get(key) or []
    parents = job.get("extends") or []
    if isinstance(parents, str):
        parents = [parents]
    for parent in reversed(parents):
        inherited = _effective(parent, ci, key, (*seen, name))
        if inherited is not None:
            return inherited
    return None


def runs_a_playbook(name: str, ci: dict) -> bool:
    """Whether the job's effective script or before_script calls ansible-playbook."""
    lines: list[str] = []
    for key in ("before_script", "script"):
        lines += script_lines({key: _effective(name, ci, key) or []}, ci, key)
    return bool(parse_invocations("\n".join(lines)))


def jobs_missing_the_pin(ci: dict) -> list[str]:
    """Deploy jobs that run a playbook, by script or by trigger path, without the pin."""
    missing = []
    for name, job in deploy_jobs(ci).items():
        paths = rule_paths({**job, "rules": _effective(name, ci, "rules") or []}, ci)
        playbook = runs_a_playbook(name, ci) or any(
            p.startswith("ansible/playbooks/") for p in paths
        )
        if playbook and PIN not in paths:
            missing.append(name)
    return sorted(missing)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ci_file = Path(argv[0]) if argv else CI_FILE
    try:
        ci = _load_ci(ci_file)
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot read {ci_file}: {exc}", file=sys.stderr)
        return 2

    inspected = deploy_jobs(ci)
    if not inspected:
        print(
            f"ERROR: {ci_file} has no `deploy-*` job — this gate inspected nothing. "
            "If the job naming changed, update this script rather than leaving it green.",
            file=sys.stderr,
        )
        return 2

    missing = jobs_missing_the_pin(ci)
    if missing:
        print("deploy jobs that run a playbook but do not trigger on the collection")
        print(f"pin ({PIN}): " + ", ".join(missing))
        print("A weisssrv.infra bump would leave those targets on the old roles.")
        return 1
    print(
        f"every playbook-running deploy job triggers on {PIN} "
        f"({len(inspected)} deploy job(s) inspected)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
