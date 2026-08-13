#!/usr/bin/env python3
"""Generate a shell-sourceable host roster (`hosts.env`) from an Ansible inventory.

The inventory is the single source of truth for the cluster's host/IP roster.
This flattens the groups an operator tool actually needs into a small
shell-sourceable (and go-task `dotenv:`-loadable) env file, so the roster is
defined once instead of being hand-copied into a Taskfile, ops scripts and a CI
ssh-keyscan list.

WHICH groups become WHICH variables is consumer data, so it lives in an export
map (YAML), not here:

    output: scripts/hosts.env
    exports:
      - key: PVE_HOSTS
        group: proxmox
        value: names          # names | ips | ip
      - key: PVE_IPS
        group: proxmox
        value: ips
      - key: HOME_ASSISTANT_IP
        group: services
        host: home            # a single host inside the group
        value: ip
      - key: WINDOWS_IP
        group: windows_vms
        host: windows
        value: ip
        required: false       # empty string instead of an error when absent
      - key: ALL_SSH_IPS
        combine: [PVE_IPS, DNS_IPS]   # union of earlier keys, in order

A `group:` may be a group-of-groups: membership resolves depth-first through
`children:`, so `group: k3s` yields the union of k3s_servers and k3s_agents.

`required` defaults to true: a group that resolves to zero hosts fails loudly,
naming which of the three causes applies (group absent, host absent, group
empty) instead of emitting an empty roster value. A host with no `ansible_host`
always fails.

Idempotent — pair it with a CI job that regenerates and diffs the committed
output.

  generate-hosts-env.py --inventory <hosts.yml> --map <exports.yml>
                        [--output <hosts.env>] [--regen-command "<cmd>"]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

VALUE_KINDS = ("names", "ips", "ip")


def _all_groups(data: dict) -> dict:
    return (data.get("all") or {}).get("children") or {}


def _group_hosts(data: dict, group: str) -> dict:
    """Return {name: hostvars} for a group, including every nested child group.

    A group-of-groups (`children:` with no `hosts:`) resolves to the union of
    its descendants, depth-first in declaration order; a cycle is ignored via
    the visited set. First occurrence of a host wins.
    """
    groups = _all_groups(data)
    hosts: dict = {}
    seen: set[str] = set()

    def walk(name: str, inline: dict | None) -> None:
        if name in seen:
            return
        seen.add(name)
        # A child may be defined inline under its parent or at the top level.
        for node in (groups.get(name), inline):
            if not isinstance(node, dict):
                continue
            for host, hostvars in (node.get("hosts") or {}).items():
                hosts.setdefault(host, hostvars)
            for child, child_node in (node.get("children") or {}).items():
                walk(child, child_node)

    walk(group, None)
    return hosts


def _group_exists(data: dict, group: str) -> bool:
    """True if `group` is declared anywhere in the inventory tree."""
    groups = _all_groups(data)
    if group in groups:
        return True
    stack = [node for node in groups.values() if isinstance(node, dict)]
    seen_nodes: list[int] = []
    while stack:
        node = stack.pop()
        if id(node) in seen_nodes:
            continue
        seen_nodes.append(id(node))
        children = node.get("children") or {}
        if group in children:
            return True
        stack.extend(n for n in children.values() if isinstance(n, dict))
    return False


def _host_ip(name: str, hostvars: dict | None) -> str:
    ip = (hostvars or {}).get("ansible_host")
    if not ip:
        raise ValueError(f"host {name!r} has no ansible_host")
    return str(ip)


def _resolve(data: dict, spec: dict) -> list[str]:
    group = spec.get("group")
    if not group:
        raise ValueError(f"export {spec.get('key')!r} has no group")
    hosts = _group_hosts(data, group)
    host = spec.get("host")
    if host is not None:
        hostvars = hosts.get(host)
        if hostvars is None:
            return []
        return [host] if spec.get("value") == "names" else [_host_ip(host, hostvars)]
    if spec.get("value") == "names":
        return list(hosts.keys())
    return [_host_ip(name, hv) for name, hv in hosts.items()]


def _why_empty(data: dict, spec: dict) -> str:
    """Explain an empty resolution: missing group, missing host, or empty group."""
    group = spec.get("group")
    if not _group_exists(data, group):
        return f"group {group!r} is not in the inventory (renamed/removed?)"
    host = spec.get("host")
    if host is not None:
        return f"host {host!r} is not in group {group!r} (renamed/removed?)"
    return f"group {group!r} contains no hosts, directly or through its children"


def build(data: dict, exports: list[dict]) -> list[tuple[str, str]]:
    """Return ordered (KEY, space-joined-value) pairs for the env file."""
    values: dict[str, list[str]] = {}
    pairs: list[tuple[str, str]] = []
    for spec in exports:
        key = spec.get("key")
        if not key:
            raise ValueError(f"export entry has no key: {spec!r}")
        combine = spec.get("combine")
        if combine:
            resolved: list[str] = []
            for src in combine:
                if src not in values:
                    raise ValueError(
                        f"export {key!r} combines {src!r}, which is not defined above it"
                    )
                resolved.extend(values[src])
        else:
            kind = spec.get("value", "ips")
            if kind not in VALUE_KINDS:
                raise ValueError(f"export {key!r} has unknown value kind {kind!r}")
            resolved = _resolve(data, spec)
            if not resolved and spec.get("required", True):
                raise ValueError(f"required export {key!r} resolved to nothing: {_why_empty(data, spec)}")
        values[key] = resolved
        pairs.append((key, " ".join(resolved)))
    return pairs


def header(inventory: Path, regen_command: str) -> str:
    return (
        "# AUTO-GENERATED by generate-hosts-env.py from\n"
        f"# {inventory}. Do NOT edit by hand.\n"
        f"# Run `{regen_command}` to regenerate. CI fails if out of sync.\n"
        "#\n"
        "# Shell-sourceable and go-task `dotenv:`-loadable.\n"
    )


def render(pairs: list[tuple[str, str]], inventory: Path, regen_command: str) -> str:
    lines = [header(inventory, regen_command)]
    for key, value in pairs:
        lines.append(f'{key}="{value}"\n')
    return "".join(lines)


def load_map(path: Path) -> tuple[list[dict], str | None]:
    """Return (exports, output-path-from-map)."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path} top-level is not a mapping")
    exports = doc.get("exports")
    if not isinstance(exports, list) or not exports:
        raise ValueError(f"{path} has no non-empty `exports` list")
    return exports, doc.get("output")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a hosts.env from an Ansible inventory.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--map", dest="map_file", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="overrides `output:` in the map")
    parser.add_argument("--regen-command", default="generate-hosts-env.py")
    args = parser.parse_args(argv)

    if not args.inventory.exists():
        print(f"ERROR: {args.inventory} not found", file=sys.stderr)
        return 1
    try:
        exports, map_output = load_map(args.map_file)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    out = args.output or (Path(map_output) if map_output else None)
    if out is None:
        print("ERROR: no output path (pass --output or set `output:` in the map)", file=sys.stderr)
        return 1

    try:
        with args.inventory.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse {args.inventory}: {e}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"ERROR: {args.inventory} top-level is not a mapping", file=sys.stderr)
        return 1
    try:
        pairs = build(data, exports)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(pairs, args.inventory, args.regen_command))
    except OSError as e:
        print(f"ERROR: failed to write {out}: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {len(pairs)} keys to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
