#!/usr/bin/env python3
"""Generate a shell-sourceable host roster (`hosts.env`) from an Ansible inventory.

Export value kinds: names, ips, ip, hostvar and groupvar (both with `var:`).
Idempotent; pair it with a CI regenerate-and-diff job. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from inventory_tree import (  # noqa: E402  (resolved from this script's own directory)
        declared_groups,
        group_index,
        host_vars,
        resolve_hosts,
    )
except ImportError as exc:
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script — vendor it (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

VALUE_KINDS = ("names", "ips", "ip", "hostvar", "groupvar")


def _inventory_group_vars(data: dict, group: str) -> dict:
    """A group's own `vars:` block from the inventory tree, merged across occurrences."""
    found: dict = {}

    def walk(name: str, defn) -> None:
        if not isinstance(defn, dict):
            return
        if str(name) == group and isinstance(defn.get("vars"), dict):
            found.update(defn["vars"])
        for child, child_defn in (defn.get("children") or {}).items():
            walk(child, child_defn)

    walk("all", data.get("all") or {})
    return found


def _group_vars_files(group_vars_dir: Path | None, group: str) -> dict:
    """A group's vars from `group_vars/<group>.yml` and `group_vars/<group>/*.yml`."""
    merged: dict = {}
    if group_vars_dir is None:
        return merged
    candidates = [group_vars_dir / f"{group}{suffix}" for suffix in (".yml", ".yaml")]
    nested = group_vars_dir / group
    if nested.is_dir():
        candidates += sorted(nested.glob("*.yml")) + sorted(nested.glob("*.yaml"))
    for path in candidates:
        if not path.is_file():
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            merged.update(doc)
    return merged


def _group_var(data: dict, group_vars_dir: Path | None, group: str, var: str) -> list[str]:
    """One group-level variable as env-file tokens. A list space-joins.

    Loud on a missing or empty value: a silently empty export reaches a script
    as an unset roster, which is the failure this kind exists to remove.
    """
    merged = dict(_group_vars_files(group_vars_dir, group))
    merged.update(_inventory_group_vars(data, group))
    if var not in merged:
        raise ValueError(
            f"group {group!r} declares no {var!r} in its inventory `vars:` or in "
            f"group_vars/{group}.yml"
        )
    value = merged[var]
    items = value if isinstance(value, list) else [value]
    tokens = [str(item) for item in items if item is not None and str(item) != ""]
    if not tokens:
        raise ValueError(f"group {group!r} has an empty {var!r}")
    return tokens


def _group_hosts(data: dict, group: str) -> dict:
    """{name: hostvars} for a group, every nested child group included.

    A group-of-groups resolves to the union of its descendants, depth-first in
    declaration order.
    """
    hostvars = host_vars(data)
    return {name: hostvars.get(name) for name in resolve_hosts(group, group_index(data))}


def _host_ip(name: str, hostvars: dict | None) -> str:
    ip = (hostvars or {}).get("ansible_host")
    if not ip:
        raise ValueError(f"host {name!r} has no ansible_host")
    return str(ip)


def _host_var(name: str, hostvars: dict | None, var: str) -> str:
    value = (hostvars or {}).get(var)
    if value is None or value == "":
        raise ValueError(f"host {name!r} has no {var!r}")
    return str(value)


def _resolve(data: dict, spec: dict, group_vars_dir: Path | None = None) -> list[str]:
    group = spec.get("group")
    if not group:
        raise ValueError(f"export {spec.get('key')!r} has no group")
    kind = spec.get("value", "ips")
    host = spec.get("host")
    if kind == "groupvar":
        var = spec.get("var")
        if not var:
            raise ValueError(f"export {spec.get('key')!r} is a groupvar with no `var:`")
        try:
            if group not in declared_groups(data):
                raise ValueError(f"export {spec.get('key')!r}: {_why_empty(data, spec)}")
            return _group_var(data, group_vars_dir, group, var)
        except ValueError:
            if spec.get("required", True):
                raise
            return []
    hosts = _group_hosts(data, group)
    if kind == "hostvar":
        var = spec.get("var")
        if not var:
            raise ValueError(f"export {spec.get('key')!r} is a hostvar with no `var:`")
        if host is not None:
            hostvars = hosts.get(host)
            return [] if hostvars is None else [_host_var(host, hostvars, var)]
        return [_host_var(name, hv, var) for name, hv in hosts.items()]
    if host is not None:
        hostvars = hosts.get(host)
        if hostvars is None:
            return []
        return [host] if kind == "names" else [_host_ip(host, hostvars)]
    if kind == "names":
        return list(hosts.keys())
    return [_host_ip(name, hv) for name, hv in hosts.items()]


def _why_empty(data: dict, spec: dict) -> str:
    """Explain an empty resolution: missing group, missing host, or empty group."""
    group = spec.get("group")
    if group not in declared_groups(data):
        return f"group {group!r} is not in the inventory (renamed/removed?)"
    host = spec.get("host")
    if host is not None:
        return f"host {host!r} is not in group {group!r} (renamed/removed?)"
    return f"group {group!r} contains no hosts, directly or through its children"


def build(data: dict, exports: list[dict],
          group_vars_dir: Path | None = None) -> list[tuple[str, str]]:
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
            resolved = _resolve(data, spec, group_vars_dir)
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
    parser.add_argument(
        "--group-vars", type=Path,
        help="group_vars directory the `groupvar` kind reads "
             "(default: group_vars/ beside the inventory)",
    )
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
    group_vars_dir = args.group_vars or (args.inventory.parent / "group_vars")
    try:
        pairs = build(data, exports, group_vars_dir)
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
