#!/usr/bin/env python3
"""Assert every role a CI-deployed playbook declares reaches every host it is
declared for. Per role, the hosts its plays declare minus the hosts the deploy
jobs reach must be empty. Static parse: hosts.yml, playbooks and .gitlab-ci.yml.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# PYTHONSAFEPATH, and any invocation that is not a direct script run, keeps this
# directory off sys.path, so the companion module is placed there explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import inventory_tree  # noqa: E402
    from ci_playbook_invocations import parse_invocations  # noqa: E402
    from ci_yaml import load_ci, script_lines  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment guard
    print(f"ERROR: {exc.name}.py must sit next to this script", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
HOSTS_YML = "ansible/inventories/prod/hosts.yml"

# Roles whose host-set gap is a deliberate operating decision, keyed by SHORT
# role name while playbooks declare `weisssrv.infra.<role>`. Every entry names
# what deploys it instead, and must name a role some CI-deployed play declares.
ACKNOWLEDGED_GAPS: dict[str, str] = {}

# Runtime-only ledger groups, filled by _reachability-probe.yml and subtracted by
# the deploy plays (`base_managed:!deploy_skipped`). Excluding one must NOT shrink
# the declared set: a host one run skipped still needs a deploy job covering it.
RUNTIME_LEDGER_GROUPS = frozenset({"deploy_skipped", "deploy_reached", "deploy_lost"})


def die(message: str) -> None:
    """Exit 2 — the input could not be analysed, which is never a pass."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_yaml(path: Path):
    """Parse one input file; an unreadable or malformed one exits 2."""
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        die(f"ERROR: {path} could not be parsed ({exc.__class__.__name__}: {exc})")


def build_inventory(root: Path) -> dict[str, set[str]]:
    """Map every group AND host name to the set of hosts it expands to."""
    index = inventory_tree.group_index(load_yaml(root / HOSTS_YML) or {})
    try:
        groups = {
            name: set(inventory_tree.resolve_hosts(name, index, strict=True)) for name in index
        }
    except inventory_tree.InventoryCycle as exc:
        die(f"ERROR: {exc}")
    for host in groups.get("all", set()):
        groups.setdefault(host, {host})
    return groups


def expand(pattern: str | None, groups: dict[str, set[str]]) -> set[str]:
    """Expand an Ansible host pattern. Only the forms this repo uses."""
    if pattern is None:
        return set(groups["all"])
    hosts: set[str] = set()
    for token in re.split(r"[:,]", pattern):
        token = token.strip()
        if not token:
            continue
        if token == "localhost":
            # A play that runs on the controller and delegates from there.
            hosts.add("localhost")
            continue
        if token.startswith("!") and token[1:] in RUNTIME_LEDGER_GROUPS:
            continue
        if token.startswith("!") or token.startswith("&"):
            die(
                f"ERROR: host pattern {pattern!r} uses an exclusion/intersection "
                f"this gate does not model. Teach it the form or split the play."
            )
        if token not in groups:
            die(f"ERROR: host pattern {pattern!r} references unknown group/host {token!r}")
        hosts |= groups[token]
    return hosts


IMPORT_PLAYBOOK = ("import_playbook", "ansible.builtin.import_playbook")
IMPORT_ROLE = ("import_role", "ansible.builtin.import_role")
INCLUDE_ROLE = ("include_role", "ansible.builtin.include_role")
IMPORT_TASKS = ("import_tasks", "ansible.builtin.import_tasks")
INCLUDE_TASKS = ("include_tasks", "ansible.builtin.include_tasks")
TASK_LISTS = ("pre_tasks", "tasks", "post_tasks", "handlers")
BLOCK_KEYS = ("block", "rescue", "always")


def applied_tags(spec) -> set[str]:
    """The `apply: {tags: ...}` of a dynamic include, the only tags its contents get."""
    apply = spec.get("apply") if isinstance(spec, dict) else None
    return set(as_list(apply.get("tags"))) if isinstance(apply, dict) else set()


def task_roles(tasks, inherited: set[str], base: Path | None, seen: tuple = ()) -> dict[str, set[str]]:
    """Roles the tasks pull in, with the tags that select them. A dynamic
    include's own tags select only the include, so its contents keep the
    inherited tags plus `apply` tags. With no `base`, task files are not followed."""
    roles: dict[str, set[str]] = {}

    def credit(found: dict[str, set[str]]) -> None:
        for name, tags in found.items():
            roles.setdefault(name, set()).update(tags)

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tags = inherited | set(as_list(task.get("tags")))
        for key in BLOCK_KEYS:
            credit(task_roles(task.get(key), tags, base, seen))
        for key in IMPORT_ROLE + INCLUDE_ROLE:
            spec = task.get(key)
            name = spec.get("name") if isinstance(spec, dict) else None
            if isinstance(name, str):
                credit({name: tags if key in IMPORT_ROLE else inherited | applied_tags(spec)})
        for key in IMPORT_TASKS + INCLUDE_TASKS:
            if key in task and base is not None:
                spec = task[key]
                inner = tags if key in IMPORT_TASKS else inherited | applied_tags(spec)
                credit(task_file_roles(spec, inner, base, seen))
    return roles


def task_file_roles(spec, tags: set[str], base: Path, seen: tuple) -> dict[str, set[str]]:
    """Follow an include_tasks/import_tasks file; one this gate cannot resolve exits 2."""
    file = spec.get("file") if isinstance(spec, dict) else spec
    if not isinstance(file, str) or "{{" in file:
        die(f"ERROR: task file {file!r} included from {base} is not a literal path, so "
            "the roles it pulls in cannot be scored. Name the file literally.")
    path = next((p for p in (base / file, base / "tasks" / file) if p.is_file()), None)
    if path is None:
        die(f"ERROR: task file {file!r} included from {base} does not exist")
    path = path.resolve()
    if path in seen:
        die(f"ERROR: include_tasks/import_tasks cycle through {path}")
    return task_roles(load_yaml(path), tags, path.parent, (*seen, path))


def parse_playbook(
    rel_path: str,
    root: Path,
    inherited: frozenset[str] = frozenset(),
    seen: tuple = (),
    *,
    follow_task_files: bool = True,
) -> list[dict]:
    """Return [{hosts, roles: {role: tags}}] for one playbook and its imports."""
    path = root / "ansible/playbooks" / rel_path
    if not path.exists():
        die(f"ERROR: deploy job references {path}, which does not exist")
    if rel_path in seen:
        die(f"ERROR: import_playbook cycle through {rel_path}")
    plays = []
    for play in load_yaml(path) or []:
        if not isinstance(play, dict):
            continue
        play_tags = set(inherited) | set(as_list(play.get("tags")))
        imported = next((play[k] for k in IMPORT_PLAYBOOK if k in play), None)
        if isinstance(imported, str):
            child = (Path(rel_path).parent / imported).as_posix()
            plays += parse_playbook(
                child, root, frozenset(play_tags), (*seen, rel_path), follow_task_files=follow_task_files
            )
            continue
        if "hosts" not in play:
            continue
        roles: dict[str, set[str]] = {}
        for entry in play.get("roles") or []:
            if isinstance(entry, str):
                name, tags = entry, set()
            elif isinstance(entry, dict) and "role" in entry:
                name, tags = entry["role"], set(as_list(entry.get("tags")))
            else:
                continue
            roles.setdefault(name, set())
            roles[name] |= tags | play_tags
        for key in TASK_LISTS:
            base = path.parent if follow_task_files else None
            for name, tags in task_roles(play.get(key), play_tags, base).items():
                roles.setdefault(name, set()).update(tags)
        if roles:
            plays.append({"hosts": str(play["hosts"]), "roles": roles})
    return plays


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def short_name(role: str) -> str:
    """Playbooks declare `weisssrv.infra.<role>`; ACKNOWLEDGED_GAPS is keyed short."""
    return role.rsplit(".", 1)[-1]


def relative_playbook(token: str | None) -> str | None:
    """CI spells playbooks relative to ansible/; parse_playbook wants the part
    under playbooks/. A call to anything else is not this gate's subject."""
    if not token:
        return None
    _, marker, rel = token.rpartition("playbooks/")
    return rel if marker else None


def parse_ci(root: Path) -> list[dict]:
    """Return one entry per deploy-stage ansible-playbook invocation."""
    try:
        ci = load_ci(root / ".gitlab-ci.yml")
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        die(f"ERROR: {root / '.gitlab-ci.yml'} could not be parsed ({exc.__class__.__name__}: {exc})")

    invocations = []
    for job_name, job in ci.items():
        if not isinstance(job, dict) or not job_name.startswith("deploy-"):
            continue
        if job.get("stage") != "deploy":
            continue
        # script_lines expands a `!reference [.anchor, script]` block, so a
        # referenced invocation counts toward coverage instead of vanishing.
        for line in script_lines(job, ci):
            for call in parse_invocations(line):
                playbook = relative_playbook(call["playbook"])
                if playbook is None:
                    continue
                invocations.append(
                    {
                        "job": job_name,
                        "playbook": playbook,
                        "limit": call["limit"],
                        "tags": call["tags"],
                        "skip_tags": call["skip_tags"],
                    }
                )
    return invocations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert CI deploy jobs reach every host each role is declared for.")
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO,
        help="repo root to inspect (the unit tests drive fixture trees through it)",
    )
    args = parser.parse_args(argv)
    root = args.repo.resolve()

    groups = build_inventory(root)
    invocations = parse_ci(root)
    if not invocations:
        print("ERROR: no deploy-stage ansible-playbook invocations found in .gitlab-ci.yml", file=sys.stderr)
        return 2

    playbooks = {inv["playbook"] for inv in invocations}
    parsed = {name: parse_playbook(name, root) for name in sorted(playbooks)}

    declared: dict[str, set[str]] = {}
    covered: dict[str, set[str]] = {}

    for plays in parsed.values():
        for play in plays:
            play_hosts = expand(play["hosts"], groups)
            for role in play["roles"]:
                declared.setdefault(role, set())
                declared[role] |= play_hosts

    for inv in invocations:
        for play in parsed[inv["playbook"]]:
            play_hosts = expand(play["hosts"], groups)
            # No --limit means the play runs on everything it targets, including
            # the controller-only `hosts: localhost` plays.
            reached = play_hosts if inv["limit"] is None else play_hosts & expand(inv["limit"], groups)
            for role, tags in play["roles"].items():
                if inv["tags"] is not None and not (inv["tags"] & tags):
                    continue
                covered.setdefault(role, set())
                covered[role] |= reached

    # `--skip-tags` subtracts roles this gate cannot attribute, so such a job
    # would be scored as covering everything its tags select.
    unreadable = [
        f"{inv['job']}: --skip-tags {','.join(sorted(inv['skip_tags']))} on {inv['playbook']} "
        "is not modelled, so this job's coverage was not scored. Teach "
        "ci_playbook_invocations.py and this gate rather than the job."
        for inv in invocations
        if inv["skip_tags"]
    ]
    if unreadable:
        print("ERROR: deploy invocations this gate cannot score:\n", file=sys.stderr)
        for line in unreadable:
            print(f"  {line}", file=sys.stderr)
        return 2

    failures = []
    gaps_by_short: dict[str, bool] = {}
    for role in sorted(declared):
        gap = declared[role] - covered.get(role, set())
        short = short_name(role)
        gaps_by_short[short] = gaps_by_short.get(short, False) or bool(gap)
        if short in ACKNOWLEDGED_GAPS:
            continue
        if gap:
            failures.append((role, gap))

    # An acknowledgement that hides no current gap would hide the next real one:
    # its role is reached everywhere, or no CI-deployed play declares it at all.
    stale = sorted(
        role for role in ACKNOWLEDGED_GAPS if role in gaps_by_short and not gaps_by_short[role]
    )
    orphaned = sorted(set(ACKNOWLEDGED_GAPS) - set(gaps_by_short))
    if stale:
        print("ERROR: ACKNOWLEDGED_GAPS entries for roles CI already deploys everywhere:\n", file=sys.stderr)
        for role in stale:
            print(f"  {role} — remove it from ACKNOWLEDGED_GAPS", file=sys.stderr)
    if orphaned:
        print("ERROR: ACKNOWLEDGED_GAPS entries for roles no CI-deployed playbook declares:\n", file=sys.stderr)
        for role in orphaned:
            print(f"  {role} — remove it from ACKNOWLEDGED_GAPS", file=sys.stderr)

    if failures:
        print("ERROR: roles that CI deploys to only part of the host set they are declared for:\n", file=sys.stderr)
        for role, gap in failures:
            print(f"  {role} — unreached hosts: {', '.join(sorted(gap))}", file=sys.stderr)
            print(
                "      (no deploy-stage invocation selects it for those hosts —"
                " check the job's --limit and --tags)",
                file=sys.stderr,
            )
        print(
            "\nResolution: extend the relevant deploy job's --tags/--limit,"
            "\nor add the role to ACKNOWLEDGED_GAPS in scripts/check-deploy-host-coverage.py with"
            "\na rationale naming what deploys it instead.",
            file=sys.stderr,
        )
    if stale or orphaned or failures:
        return 1

    print(f"All {len(declared)} roles in CI-deployed playbooks reach every host they are declared for.")
    declared_short = {short_name(role) for role in declared}
    for role, reason in sorted(ACKNOWLEDGED_GAPS.items()):
        if role in declared_short:
            print(f"  (skipped {role}: {reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
