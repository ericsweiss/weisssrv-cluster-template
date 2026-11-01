#!/usr/bin/env python3
"""Assert every hand-written LAN address is a real inventory host.

An endpoint address or export client inside a declared host CIDR must cover an
`ansible_host`, and an export admits a scoped group whole. docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Both companions are reported at once: a consumer vendoring the gate alone
# otherwise fixes one name, re-runs, and is told about the next.
_MISSING = []
try:
    import gate_common  # noqa: E402
except ImportError:
    _MISSING.append("gate_common.py")
try:
    import inventory_tree  # noqa: E402
except ImportError:
    _MISSING.append("inventory_tree.py")
if _MISSING:
    print(
        f"ERROR: {' and '.join(_MISSING)} must sit next to this script — vendor "
        "them (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2)

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

DEFAULT_INVENTORY = "ansible/inventories/prod"
DEFAULT_MANIFEST_TREE = "kubernetes"
# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# left out takes its endpoints with it, uncompared and unreported.
MANIFEST_GLOBS = ("*.yaml", "*.yml")
EXPORTS_KEY = "nas_storage_exports"
# What Ansible itself skips in a vars directory. Everything else there is
# parsed, an extensionless `group_vars/all` included.
VARS_IGNORED_SUFFIXES = (".orig", ".bak", ".ini", ".cfg", ".retry", ".pyc", ".pyo")
DEFAULT_LAN_CIDR_KEY = "cluster_lan_cidr"
GATEWAY_KEY = "cluster_lan_gateway"
# Groups an export either admits in full or not at all. A group of mixed roles
# (`all`) would report every export that legitimately admits one role.
DEFAULT_SCOPED_GROUPS = ("k3s_servers", "k3s_agents")


class Vacuous(Exception):
    """The gate could not inspect its subject — exit 2, never a silent pass."""


def inventory(root: Path, hosts_yml: str, scoped_groups: Tuple[str, ...]) -> Tuple[
    Dict[str, str], Dict[str, Set[str]]
]:
    """({ansible_host: inventory_hostname}, {scoped group: {ansible_host, ...}}).

    A membership question is asked of addresses, not host names, because an
    export admits CIDRs and /32s.
    """
    try:
        doc = inventory_tree.load_inventory(root / hosts_yml)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Vacuous(f"{hosts_yml} unreadable: {exc}") from exc
    found = inventory_tree.hosts_by_address(doc)
    if not found:
        raise Vacuous(f"{hosts_yml} declares no ansible_host values")
    addresses = inventory_tree.addresses_by_host(doc)
    index = inventory_tree.group_index(doc)
    groups: Dict[str, Set[str]] = {}
    for name in scoped_groups:
        if name not in index:
            continue
        try:
            resolved = inventory_tree.resolve_hosts(name, index, strict=True)
        except inventory_tree.InventoryCycle as exc:
            raise Vacuous(str(exc)) from exc
        members = {addresses[host] for host in resolved if host in addresses}
        if members:
            groups[name] = members
    return found, groups


def inventory_var_files(root: Path, inventory_dir: str) -> List[Path]:
    """Every group_vars and host_vars file Ansible would read, nested dirs too.

    A group's vars live in `<group>.yml`, `<group>.yaml`, a bare `<group>` or a
    `<group>/` directory; one spelling alone passes over the rest.
    """
    found: List[Path] = []
    for tree in ("group_vars", "host_vars"):
        directory = root / inventory_dir / tree
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name.endswith("~") or path.suffix in VARS_IGNORED_SUFFIXES:
                continue
            found.append(path)
    return sorted(set(found))


def nfs_exports(root: Path, inventory_dir: str) -> Tuple[
    List[Tuple[str, List[str], str]], List[str]
]:
    """((export path, client specs, the file it came from), unreadable files)."""
    found: List[Tuple[str, List[str], str]] = []
    skipped: List[str] = []
    for path in inventory_var_files(root, inventory_dir):
        rel = path.relative_to(root).as_posix()
        try:
            with path.open(encoding="utf-8") as handle:
                doc = yaml.safe_load(handle) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            skipped.append(
                f"{rel}: unreadable ({exc.__class__.__name__}: {exc}) — "
                "its NFS exports were not checked"
            )
            continue
        if not isinstance(doc, dict):
            continue
        for export in doc.get(EXPORTS_KEY) or []:
            if not isinstance(export, dict):
                continue
            specs = [
                str(client["spec"])
                for client in export.get("clients") or []
                if isinstance(client, dict) and client.get("spec")
            ]
            found.append((str(export.get("path", "?")), specs, rel))
    return found, skipped


def host_addresses(values) -> Tuple[Dict[str, object], List[str]]:
    """({value: parsed address}, the values that are names instead).

    An `ansible_host` is ordinarily an address but legally a name, and a name
    cannot be placed in a CIDR, so containment reads the parsed map alone.
    """
    parsed: Dict[str, object] = {}
    unparsed: List[str] = []
    for value in values:
        try:
            parsed[str(value)] = ipaddress.ip_address(str(value))
        except ValueError:
            unparsed.append(str(value))
    return parsed, sorted(unparsed)


def _name_caveat(names: List[str]) -> str:
    """The clause a containment finding carries when an ansible_host is a name.

    A host behind a name is neither matched nor reported, so the finding may
    name an address that is in fact live.
    """
    if not names:
        return ""
    return (
        f" (ansible_host {', '.join(names)} is not an address, so no endpoint "
        "can be matched against it)"
    )


def _networks(specs: List[str]) -> List[Tuple[str, object]]:
    """(spec, network) for every client spec that is a CIDR or a bare address.

    A hostname, netgroup or wildcard spec is a legal export client this gate
    cannot resolve to an address, so it is left out rather than reported.
    """
    found = []
    for spec in specs:
        try:
            found.append((spec, ipaddress.ip_network(spec, strict=False)))
        except ValueError:
            continue
    return found


def partial_group_exports(
    exports: List[Tuple[str, List[str], str]], groups: Dict[str, Set[str]]
) -> List[str]:
    """Exports admitting some of a scoped group's nodes but not all of them.

    A client list is frozen when it is written, so a node added or renumbered
    later loses the mount on itself alone while the rest keep it.
    """
    problems = []
    for export, specs, rel in exports:
        networks = [network for _spec, network in _networks(specs)]
        if not networks:
            continue
        for group, members in sorted(groups.items()):
            placeable, named = host_addresses(members)
            admitted = {
                address
                for address, parsed in placeable.items()
                if any(parsed in net for net in networks)
            }
            if not admitted or admitted == set(placeable):
                continue
            problems.append(
                f"{rel}: export {export} admits part of {group} but not "
                f"{', '.join(sorted(set(placeable) - admitted))} — that node "
                "mounts nothing while the rest do" + _name_caveat(named)
            )
    return problems


def lan_networks(config: Dict[str, str], keys: List[str], extra: List[str]) -> List:
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
            raise Vacuous(f"{gate_common.CLUSTER_CONFIG} {key}={value!r}: {exc}") from exc
    for value in extra:
        try:
            found.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise Vacuous(f"--extra-lan-cidr {value!r}: {exc}") from exc
    if not found:
        raise Vacuous(
            f"{gate_common.CLUSTER_CONFIG} declares none of {', '.join(keys)} and no "
            "--extra-lan-cidr was passed — the gate has no LAN to scope to"
        )
    return found


def substitute(address: str, config: Dict[str, str]) -> str:
    """Resolve a `${cluster_*}` placeholder the way Flux's postBuild does.

    An unknown placeholder is left alone and fails the IP parse below.
    """
    match = _PLACEHOLDER.fullmatch(address.strip())
    if match and match.group(1) in config:
        return config[match.group(1)]
    return address


def _endpoint_list(value, config: Dict[str, str]) -> Tuple[List, str]:
    """(entries, reason it could not be read), resolving a whole-list roster key.

    A slice may spell its endpoints as one `${cluster_*}` placeholder holding a
    JSON list; a reason, not a bare empty list, keeps a dropped slice visible.
    """
    if isinstance(value, str):
        resolved = substitute(value, config)
        if resolved == value.strip():
            return [], f"{value.strip()!r} names no cluster-config key"
        try:
            value = yaml.safe_load(resolved)
        except yaml.YAMLError as exc:
            return [], f"{value!r} resolves to unparsable YAML ({exc.__class__.__name__})"
    entries = [entry for entry in (value or []) if isinstance(entry, dict)]
    if value and not entries:
        return [], f"{value!r} resolves to no list of endpoint entries"
    return entries, ""


def endpoint_addresses(root: Path, tree_name: str, config: Dict[str, str]) -> Tuple[
    List[Tuple[str, str, str]], List[str]
]:
    """((address, resource, file) per hand-written endpoint, unreadable files)."""
    found: List[Tuple[str, str, str]] = []
    skipped: List[str] = []
    tree = root / tree_name
    for path in sorted({p for glob in MANIFEST_GLOBS for p in tree.rglob(glob)}):
        rel = path.relative_to(root).as_posix()
        try:
            with path.open(encoding="utf-8") as handle:
                docs = list(yaml.safe_load_all(handle))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            skipped.append(
                f"{rel}: unreadable ({exc.__class__.__name__}: {exc}) — "
                "this gate could not inspect it"
            )
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            name = (doc.get("metadata") or {}).get("name", "?")
            if doc.get("kind") == "EndpointSlice":
                entries, reason = _endpoint_list(doc.get("endpoints"), config)
                if reason:
                    skipped.append(
                        f"{rel}: EndpointSlice/{name} endpoints {reason} — "
                        "this gate could not inspect it"
                    )
                for endpoint in entries:
                    for address in endpoint.get("addresses") or []:
                        found.append((str(address), f"EndpointSlice/{name}", rel))
            elif doc.get("kind") == "Endpoints":
                entries, reason = _endpoint_list(doc.get("subsets"), config)
                if reason:
                    skipped.append(
                        f"{rel}: Endpoints/{name} subsets {reason} — "
                        "this gate could not inspect it"
                    )
                for subset in entries:
                    for address in subset.get("addresses") or []:
                        if not isinstance(address, dict):
                            continue
                        if address.get("ip"):
                            found.append((str(address["ip"]), f"Endpoints/{name}", rel))
    return found, skipped


def check(
    root: Path,
    lan_cidr_keys: Tuple[str, ...] = (DEFAULT_LAN_CIDR_KEY,),
    extra_lan_cidrs: Tuple[str, ...] = (),
    inventory_dir: str = DEFAULT_INVENTORY,
    manifest_tree: str = DEFAULT_MANIFEST_TREE,
    scoped_groups: Tuple[str, ...] = DEFAULT_SCOPED_GROUPS,
) -> Tuple[List[str], int]:
    """(problems, number of in-LAN addresses compared against the inventory)."""
    hosts_yml = f"{inventory_dir}/hosts.yml"
    known, groups = inventory(root, hosts_yml, scoped_groups)
    hosts, named_hosts = host_addresses(known)
    if not hosts:
        raise Vacuous(
            f"every ansible_host in {hosts_yml} is a name, not an address, so no "
            "endpoint or export client can be matched against one"
        )
    try:
        config = gate_common.load_cluster_config(root)
    except gate_common.OperatorError as exc:
        raise Vacuous(str(exc)) from exc
    lans = lan_networks(config, list(lan_cidr_keys), list(extra_lan_cidrs))
    # A cluster that declares no gateway key simply gets no gateway allowance.
    gateway = config.get(GATEWAY_KEY, "")
    raw, skipped = endpoint_addresses(root, manifest_tree, config)
    addresses = [(substitute(a, config), resource, rel) for a, resource, rel in raw]
    if not addresses:
        raise Vacuous(f"no EndpointSlice/Endpoints address found under {manifest_tree}/")

    problems = list(skipped)
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
                f"in {hosts_yml} — the guest was renumbered on one side only"
                + _name_caveat(named_hosts)
            )

    exports, export_skipped = nfs_exports(root, inventory_dir)
    problems.extend(export_skipped)
    for export, specs, rel in exports:
        for spec, network in _networks(specs):
            # An overlap either way is in scope: a spec CONTAINING a host CIDR
            # is the broadest client list an exports file can carry, and
            # containment in one direction alone never saw it.
            in_lan = [
                lan for lan in lans
                if lan.version == network.version
                and (network.subnet_of(lan) or network.supernet_of(lan))
            ]
            if not in_lan:
                continue
            checked += 1
            wider = [lan for lan in in_lan if lan.subnet_of(network) and lan != network]
            if wider:
                problems.append(
                    f"{rel}: export {export} admits {spec}, which is wider than "
                    f"the host CIDR {wider[0]} — it admits every address in that "
                    "CIDR and beyond, whatever the inventory holds"
                )
                continue
            if not any(host in network for host in hosts.values()):
                problems.append(
                    f"{rel}: export {export} admits {spec}, which covers no "
                    f"ansible_host in {hosts_yml} — the client list was written "
                    "for an address range the inventory no longer uses"
                    + _name_caveat(named_hosts)
                )
    problems.extend(partial_group_exports(exports, groups))

    if not checked:
        scope = ", ".join(str(lan) for lan in lans)
        raise Vacuous(f"no address inside {scope} reached the comparison")
    return problems, checked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Guest endpoint addresses and NFS export clients vs the inventory"
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument(
        "--inventory", default=DEFAULT_INVENTORY, metavar="DIR",
        help="inventory directory holding hosts.yml, group_vars/ and host_vars/ "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--manifest-tree", default=DEFAULT_MANIFEST_TREE, metavar="DIR",
        help="tree walked for EndpointSlice/Endpoints objects (default: %(default)s)",
    )
    parser.add_argument(
        "--lan-cidr-key", action="append", default=None, metavar="KEY",
        help="cluster-config key holding a host CIDR; repeatable for a cluster "
             f"with several host VLANs (default: {DEFAULT_LAN_CIDR_KEY})",
    )
    parser.add_argument(
        "--extra-lan-cidr", action="append", default=None, metavar="CIDR",
        help="host CIDR cluster-config does not name; repeatable",
    )
    parser.add_argument(
        "--scoped-group", action="append", default=None, metavar="GROUP",
        help="inventory group an export must admit whole; repeatable "
             f"(default: {', '.join(DEFAULT_SCOPED_GROUPS)})",
    )
    args = parser.parse_args(argv)

    try:
        problems, total = check(
            Path(args.repo_root),
            lan_cidr_keys=tuple(args.lan_cidr_key or (DEFAULT_LAN_CIDR_KEY,)),
            extra_lan_cidrs=tuple(args.extra_lan_cidr or ()),
            inventory_dir=args.inventory,
            manifest_tree=args.manifest_tree,
            scoped_groups=tuple(args.scoped_group or DEFAULT_SCOPED_GROUPS),
        )
    except (Vacuous, OSError, UnicodeDecodeError) as exc:
        print(f"check-guest-endpoint-parity inspected nothing: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Hand-written LAN addresses have drifted from the inventory:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        "Guest endpoint addresses and NFS export clients agree with the inventory "
        f"({total} LAN address(es) checked)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
