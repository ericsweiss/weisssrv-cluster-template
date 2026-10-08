#!/usr/bin/env python3
"""Assert every hand-written LAN address is a real inventory host.

EndpointSlice/Endpoints addresses and per-host NFS /32s must equal an
`ansible_host` (`cluster_lan_gateway` aside), and an export admits a k3s group whole.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import gate_common  # noqa: E402
    import inventory_tree  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script - re-vendor it from weisssrv-lib.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

try:
    import yaml  # noqa: E402
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

MANIFEST_TREE = "kubernetes"
# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# left out takes its endpoints with it, uncompared and unreported.
MANIFEST_GLOBS = ("*.yaml", "*.yml")
HOSTS_YML = "ansible/inventories/prod/hosts.yml"
CLUSTER_CONFIG = gate_common.CLUSTER_CONFIG
GROUP_VARS = "ansible/inventories/prod/group_vars"
NAS_GROUP = "nas"
DEFAULT_LAN_CIDR_KEY = "cluster_lan_cidr"
GATEWAY_KEY = "cluster_lan_gateway"
# Groups whose members an export either admits in full or not at all.
K3S_SCOPED_GROUPS = ("k3s_servers", "k3s_agents")


class Vacuous(Exception):
    """The gate could not inspect its subject — exit 2, never a silent pass."""


def inventory(root: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """({ansible_host: inventory_hostname}, {group: {ansible_host, ...}}).

    A group carries the addresses of its own hosts and of every child group, so
    a membership question can be asked of `k3s_agents` or of `k3s`.
    """
    try:
        doc = inventory_tree.load_inventory(root / HOSTS_YML)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Vacuous(f"{HOSTS_YML} unreadable: {exc}") from exc
    found = inventory_tree.hosts_by_address(doc)
    addresses = inventory_tree.addresses_by_host(doc)
    index = inventory_tree.group_index(doc)
    groups: dict[str, set[str]] = {}
    for name in index:
        try:
            resolved = inventory_tree.resolve_hosts(name, index, strict=True)
        except inventory_tree.InventoryCycle as exc:
            raise Vacuous(str(exc)) from exc
        members = {addresses[host] for host in resolved if host in addresses}
        if members:
            groups[name] = members
    if not found:
        raise Vacuous(f"{HOSTS_YML} declares no ansible_host values")
    return found, groups


def substitute(address: str, config: dict[str, str]) -> str:
    """Resolve a `${cluster_*}` placeholder the way Flux's postBuild does.

    An unknown placeholder is left alone and fails the IP parse below.
    """
    match = _PLACEHOLDER.fullmatch(address.strip())
    if match and match.group(1) in config:
        return config[match.group(1)]
    return address


def _endpoint_list(
    value, config: dict[str, str], problems: list[str] | None = None, where: str = ""
) -> list:
    """The endpoints list, resolving a whole-list `${cluster_*}` roster key.

    A roster that does not resolve or does not parse is appended to `problems`:
    returning an empty list would drop every address it holds silently.
    """
    if isinstance(value, str):
        resolved = substitute(value, config)
        if resolved == value.strip():
            if problems is not None:
                problems.append(
                    f"{where} endpoints are {value.strip()!r}, which cluster-config "
                    "does not define — its addresses were not checked"
                )
            return []
        try:
            value = yaml.safe_load(resolved)
        except yaml.YAMLError as exc:
            if problems is not None:
                problems.append(f"{where} endpoints roster does not parse: {exc}")
            return []
    return [entry for entry in (value or []) if isinstance(entry, dict)]


def endpoint_addresses(
    root: Path, config: dict[str, str], unreadable: list[str] | None = None
) -> list[tuple[str, str, str]]:
    """(address, resource, file) for every address a hand-written endpoint names.

    An unparseable file or an unresolved roster placeholder goes to `unreadable`:
    dropping either leaves its addresses uncompared and unreported.
    """
    found = []
    tree = root / MANIFEST_TREE
    for path in sorted({p for glob in MANIFEST_GLOBS for p in tree.rglob(glob)}):
        rel = path.relative_to(root).as_posix()
        try:
            with path.open(encoding="utf-8") as handle:
                docs = list(yaml.safe_load_all(handle))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            if unreadable is not None:
                unreadable.append(f"{rel} unparseable, its objects were not checked: {exc}")
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            name = (doc.get("metadata") or {}).get("name", "?")
            if doc.get("kind") == "EndpointSlice":
                where = f"{rel}: EndpointSlice/{name}"
                for endpoint in _endpoint_list(doc.get("endpoints"), config, unreadable, where):
                    for address in endpoint.get("addresses") or []:
                        found.append((str(address), f"EndpointSlice/{name}", rel))
            elif doc.get("kind") == "Endpoints":
                where = f"{rel}: Endpoints/{name}"
                for subset in _endpoint_list(doc.get("subsets"), config, unreadable, where):
                    for address in subset.get("addresses") or []:
                        if not isinstance(address, dict):
                            continue
                        if address.get("ip"):
                            found.append((str(address["ip"]), f"Endpoints/{name}", rel))
    return found


def nas_group_var_files(root: Path) -> list[Path]:
    """Every file holding the nas group's vars, in Ansible's merge order.

    A group's vars live in `<group>.yml`, `<group>.yaml` or a `<group>/`
    directory; reading only the first spelling passes silently on the others.
    """
    base = root / GROUP_VARS
    found = [
        path
        for path in (base / f"{NAS_GROUP}.yml", base / f"{NAS_GROUP}.yaml")
        if path.is_file()
    ]
    directory = base / NAS_GROUP
    if directory.is_dir():
        found += sorted(p for p in directory.rglob("*.yml") if p.is_file())
        found += sorted(p for p in directory.rglob("*.yaml") if p.is_file())
    return found


def nfs_exports(
    root: Path, unreadable: list[str] | None = None
) -> list[tuple[str, list[str], str]]:
    """(export path, every client spec, the file it came from) per NFS export."""
    paths = nas_group_var_files(root)
    if not paths:
        raise Vacuous(
            f"no {NAS_GROUP} group vars under {GROUP_VARS}/ — expected "
            f"{NAS_GROUP}.yml, {NAS_GROUP}.yaml or {NAS_GROUP}/"
        )
    found = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            with path.open(encoding="utf-8") as handle:
                doc = yaml.safe_load(handle) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            if unreadable is not None:
                unreadable.append(f"{rel} unparseable, its exports were not checked: {exc}")
            continue
        if not isinstance(doc, dict):
            continue
        for export in doc.get("nas_storage_exports") or []:
            if not isinstance(export, dict):
                continue
            specs = [
                str(client.get("spec", ""))
                for client in export.get("clients") or []
                if isinstance(client, dict) and client.get("spec")
            ]
            found.append((str(export.get("path", "?")), specs, rel))
    return found


def partial_group_exports(
    exports: list[tuple[str, list[str], str]], groups: dict[str, set[str]]
) -> list[str]:
    """Exports that admit some of a k3s group's nodes but not all of them.

    A per-host client list is frozen when the inventory is written, so a node
    added later loses the mount on itself alone.
    """
    problems = []
    for export, specs, rel in exports:
        if not specs or not all(spec.endswith("/32") for spec in specs):
            continue
        admitted = {spec[: -len("/32")] for spec in specs}
        for group in K3S_SCOPED_GROUPS:
            members = groups.get(group) or set()
            missing = sorted(members - admitted)
            if not missing or not (members & admitted):
                continue
            problems.append(
                f"{rel}: export {export} admits part of {group} but not "
                f"{', '.join(missing)} — that node mounts nothing while the rest do"
            )
    return problems


def lan_networks(config: dict[str, str], keys: list[str], extra: list[str]) -> list:
    """Every network the gate scopes to, from cluster-config keys then literals.

    A multi-VLAN site splits management from storage or a DMZ, so membership is
    an ANY-match over this list rather than one flat CIDR.
    """
    found = []
    for key in keys:
        value = config.get(key)
        if not value:
            continue
        try:
            found.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise Vacuous(f"{CLUSTER_CONFIG} {key}={value!r}: {exc}") from exc
    for value in extra:
        try:
            found.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise Vacuous(f"--extra-lan-cidr {value!r}: {exc}") from exc
    if not found:
        raise Vacuous(
            f"{CLUSTER_CONFIG} declares none of {', '.join(keys)} and no "
            "--extra-lan-cidr was passed — the gate has no LAN to scope to"
        )
    return found


def check(
    root: Path,
    lan_cidr_keys: list[str] | None = None,
    extra_lan_cidrs: list[str] | None = None,
) -> list[str]:
    return check_detailed(root, lan_cidr_keys, extra_lan_cidrs)[0]


def check_detailed(
    root: Path,
    lan_cidr_keys: list[str] | None = None,
    extra_lan_cidrs: list[str] | None = None,
) -> tuple[list[str], int]:
    """(problems, addresses actually compared against the inventory)."""
    known, groups = inventory(root)
    try:
        config = gate_common.load_cluster_config(root)
    except gate_common.OperatorError as exc:
        raise Vacuous(str(exc)) from exc
    keys = list(lan_cidr_keys or [DEFAULT_LAN_CIDR_KEY])
    lans = lan_networks(config, keys, list(extra_lan_cidrs or []))
    # A cluster that declares no gateway key simply gets no gateway allowance.
    gateway = config.get(GATEWAY_KEY, "")
    unreadable: list[str] = []
    addresses = [
        (substitute(a, config), resource, rel)
        for a, resource, rel in endpoint_addresses(root, config, unreadable)
    ]
    if not addresses:
        raise Vacuous(f"no EndpointSlice/Endpoints address found under {MANIFEST_TREE}/")

    problems = list(unreadable)
    checked = 0
    for address, resource, rel in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            problems.append(f"{rel}: {resource} address {address!r} is not an IP address")
            continue
        if not any(parsed in lan for lan in lans) or address == gateway:
            continue
        checked += 1
        if address not in known:
            problems.append(
                f"{rel}: {resource} points at {address}, which is no ansible_host "
                f"in {HOSTS_YML} — the guest was renumbered on one side only"
            )
    exports = nfs_exports(root, problems)
    for export, specs, rel in exports:
        for spec in specs:
            if not spec.endswith("/32"):
                continue
            address = spec[: -len("/32")]
            checked += 1
            if address not in known:
                problems.append(
                    f"{rel}: export {export} allows {spec}, which is no "
                    f"ansible_host in {HOSTS_YML} — the guest was renumbered on one side only"
                )
    problems.extend(partial_group_exports(exports, groups))
    if not checked:
        scope = ", ".join(str(lan) for lan in lans)
        raise Vacuous(f"no address inside {scope} reached the comparison")
    return problems, checked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Guest endpoint addresses vs the inventory")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument(
        "--lan-cidr-key", action="append", default=None,
        help="cluster-config key holding a LAN CIDR; repeatable, one per network "
             f"this site declares (default: {DEFAULT_LAN_CIDR_KEY})",
    )
    parser.add_argument(
        "--extra-lan-cidr", action="append", default=None,
        help="a LAN CIDR cluster-config does not declare; repeatable",
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root)

    try:
        problems, checked = check_detailed(root, args.lan_cidr_key, args.extra_lan_cidr)
    except (Vacuous, OSError, UnicodeDecodeError) as exc:
        print(f"check-guest-endpoint-parity inspected nothing: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Guest endpoint addresses have drifted from the inventory:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"Guest endpoint addresses agree with the inventory ({checked} addresses checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
