#!/usr/bin/env python3
"""Resolve an Ansible inventory: groups to hosts, hosts to their vars.

Gates and generators import it so inventory shape is read one way.
Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_INVENTORY = Path("ansible/inventories/prod/hosts.yml")


class InventoryCycle(ValueError):
    """A group reaches itself through its children, so resolution has no end."""


def load_inventory(path: Path | str | None = None) -> dict:
    """The parsed hosts.yml mapping; the default path is relative to the cwd."""
    return yaml.safe_load(
        Path(path if path is not None else DEFAULT_INVENTORY).read_text(encoding="utf-8")
    ) or {}


def group_index(inventory: dict) -> dict[str, dict]:
    """group name -> {"hosts": [...], "children": [...]}, merged across occurrences.

    Keyed on the KEY, not a truthy value: `hosts: {}` is a real empty group,
    while a bare `name:` reference never shadows the occurrence with content.
    """
    index: dict[str, dict] = {}

    def walk(name: str, defn) -> None:
        if not isinstance(defn, dict) or ("hosts" not in defn and "children" not in defn):
            return
        entry = index.setdefault(str(name), {"hosts": [], "children": []})
        for host in defn.get("hosts") or {}:
            if str(host) not in entry["hosts"]:
                entry["hosts"].append(str(host))
        for child, child_defn in (defn.get("children") or {}).items():
            if str(child) not in entry["children"]:
                entry["children"].append(str(child))
            walk(child, child_defn)

    walk("all", inventory.get("all") or {})
    return index


def declared_groups(inventory: dict) -> set[str]:
    """Every group name the inventory mentions, null-bodied placeholders included.

    Wider than group_index, whose keys are the occurrences carrying content.
    """
    names: set[str] = {"all"}

    def walk(defn) -> None:
        if not isinstance(defn, dict):
            return
        for child, child_defn in (defn.get("children") or {}).items():
            names.add(str(child))
            walk(child_defn)

    walk(inventory.get("all") or {})
    return names


def resolve_hosts(name: str, index: dict[str, dict], strict: bool = False) -> list[str]:
    """Hosts under a group, in inventory order, children expanded depth first.

    A cycle terminates; `strict=True` raises InventoryCycle. An undeclared name
    resolves to nothing, so a caller needing the difference tests `name in index`.
    """
    hosts: list[str] = []

    def walk(group: str, path: tuple[str, ...]) -> None:
        if group in path:
            if strict:
                raise InventoryCycle(f"inventory group cycle: {' -> '.join([*path, group])}")
            return
        entry = index.get(group) or {}
        for host in entry.get("hosts") or []:
            if host not in hosts:
                hosts.append(host)
        for child in entry.get("children") or []:
            walk(child, (*path, group))

    walk(name, ())
    return hosts


def all_hosts(inventory: dict) -> list[str]:
    """Every host in the inventory, in inventory order."""
    return resolve_hosts("all", group_index(inventory))


def host_vars(inventory: dict) -> dict[str, dict]:
    """host name -> vars merged across every group that lists the host."""
    merged: dict[str, dict] = {}

    def walk(defn) -> None:
        if not isinstance(defn, dict):
            return
        for host, hostvars in (defn.get("hosts") or {}).items():
            merged.setdefault(str(host), {}).update(hostvars or {})
        for child in (defn.get("children") or {}).values():
            walk(child)

    walk(inventory.get("all") or {})
    return merged


def hosts_by_address(inventory: dict) -> dict[str, str]:
    """{ansible_host: host name}; the first host to claim an address keeps it."""
    found: dict[str, str] = {}
    for host, hostvars in host_vars(inventory).items():
        address = hostvars.get("ansible_host")
        if address:
            found.setdefault(str(address), host)
    return found


def addresses_by_host(inventory: dict) -> dict[str, str]:
    """{host name: ansible_host} for every host that declares one."""
    return {
        host: str(hostvars["ansible_host"])
        for host, hostvars in host_vars(inventory).items()
        if hostvars.get("ansible_host")
    }
