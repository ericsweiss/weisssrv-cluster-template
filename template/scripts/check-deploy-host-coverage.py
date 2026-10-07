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
    sys.exit(f"{exc.name}.py must sit next to this script")

REPO = Path(__file__).resolve().parent.parent
HOSTS_YML = "ansible/inventories/prod/hosts.yml"

# Roles whose host-set gap is a deliberate operating decision, keyed by SHORT
# role name while playbooks declare `weisssrv.infra.<role>`. Same contract as
# check-deploy-coverage.sh: every entry names what deploys it instead.
ACKNOWLEDGED_GAPS = {
    "k3s": "k3s node lifecycle is manual (task k3s:deploy)",
    "proxmox_vm": "VM provisioning is manual (task k3s:provision-vms and friends)",
    "proxmox_lxc": "LXC provisioning is manual (same reasoning as proxmox_vm)",
    "proxmox_ha": "HA rules / replication are manual (task proxmox:ha)",
    "zfs_encryption": "ZFS passphrase activation is a manual cold-boot operation",
}

# Runtime-only ledger groups, filled by _reachability-probe.yml and subtracted by
# the deploy plays (`base_managed:!deploy_skipped`). Excluding one must NOT shrink
# the declared set: a host one run skipped still needs a deploy job covering it.
RUNTIME_LEDGER_GROUPS = frozenset({"deploy_skipped", "deploy_reached", "deploy_lost"})


def die(message: str) -> None:
    """Exit 2 — the input could not be analysed, which is never a pass."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_yaml(path: Path):
    with path.open() as handle:
        return yaml.safe_load(handle)


def build_inventory(root: Path) -> dict[str, set[str]]:
    """Map every group AND host name to the set of hosts it expands to."""
    index = inventory_tree.group_index(inventory_tree.load_inventory(root / HOSTS_YML))
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


def parse_playbook(rel_path: str, root: Path) -> list[dict]:
    """Return [{hosts, roles: {role: tags}}] for one playbook."""
    path = root / "ansible/playbooks" / rel_path
    if not path.exists():
        die(f"ERROR: deploy job references {path}, which does not exist")
    plays = []
    for play in load_yaml(path) or []:
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        play_tags = set(as_list(play.get("tags")))
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
    ci = load_ci(root / ".gitlab-ci.yml")

    invocations = []
    for job_name, job in ci.items():
        if not isinstance(job, dict) or not job_name.startswith("deploy-"):
            continue
        if job.get("stage") != "deploy":
            continue
        changes: set[str] = set()
        for rule in job.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rule_changes = rule.get("changes") or []
            if isinstance(rule_changes, dict):
                rule_changes = rule_changes.get("paths") or []
            changes |= {c for c in rule_changes if isinstance(c, str)}
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
                        "changes": changes,
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
    for role in sorted(declared):
        if short_name(role) in ACKNOWLEDGED_GAPS:
            continue
        gap = declared[role] - covered.get(role, set())
        if gap:
            failures.append((role, gap))

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
        return 1

    print(f"All {len(declared)} roles in CI-deployed playbooks reach every host they are declared for.")
    declared_short = {short_name(role) for role in declared}
    for role, reason in sorted(ACKNOWLEDGED_GAPS.items()):
        if role in declared_short:
            print(f"  (skipped {role}: {reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
