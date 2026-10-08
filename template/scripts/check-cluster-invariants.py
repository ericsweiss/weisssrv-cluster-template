#!/usr/bin/env python3
"""Assert the Ansible inventory's addresses are internally consistent.

Flags a duplicate vmid, a shared address, a host on a VIP or outside the LAN
CIDR, and an alert-rule address no host holds. Usage/exits: docs/SCRIPTS.md.
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

try:
    from inventory_tree import host_vars, load_inventory  # noqa: E402
except ImportError as exc:
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script — vendor it (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# Well-known VIP key names. The live check uses every cluster_*_vip key the
# config declares; this floor only names candidates when it declares none.
DEFAULT_VIP_KEYS = (
    "cluster_api_vip",
    "cluster_k3s_api_vip",
    "cluster_metallb_public_vip",
    "cluster_metallb_internal_vip",
)
DEFAULT_LAN_CIDR_KEY = "cluster_lan_cidr"

# An exact `instance="host[:port]"` selector in an alert rule. `instance=~` is a
# regex alternation the operator maintains deliberately, so only the exact form
# is held to the inventory.
INSTANCE_LITERAL = re.compile(r'instance\s*=\s*"([^"\s]+)"')
DEFAULT_RULES_GLOB = "**/*.y*ml"


def vip_keys(data: dict) -> tuple:
    """Every VIP key to check: the documented floor plus any `cluster_*_vip`."""
    declared = {key for key in data if key.startswith("cluster_") and key.endswith("_vip")}
    return tuple(sorted(set(DEFAULT_VIP_KEYS) | declared))


class OperatorError(Exception):
    """A bad invocation or a scan that inspected nothing: exit 2, not a finding."""


def inventory_hosts(hosts_file: Path) -> Dict[str, dict]:
    """host name -> merged vars, read through the shared inventory walker.

    Merged rather than collected per group: a host listed in two groups is ONE
    machine, and comparing its two entries would report it a duplicate of itself.
    """
    try:
        return host_vars(load_inventory(hosts_file) or {})
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorError("%s: %s" % (hosts_file, exc)) from exc


def cluster_config(path: Path) -> dict:
    """The `data:` mapping of the cluster-config ConfigMap."""
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorError("%s: %s" % (path, exc)) from exc
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data")
            if isinstance(data, dict):
                return data
    raise OperatorError("%s holds no ConfigMap with a `data:` mapping" % path)


def duplicates(hosts: Dict[str, dict], field: str) -> Tuple[List[str], int]:
    """Clashes on `field`, and how many hosts declared it at all."""
    owners: Dict[str, List[str]] = {}
    for name, host_vars in sorted(hosts.items()):
        value = host_vars.get(field)
        if value is not None:
            owners.setdefault(str(value), []).append(name)
    clashes = [
        "%s %s is claimed by %s" % (field, value, ", ".join(names))
        for value, names in sorted(owners.items())
        if len(names) > 1
    ]
    return clashes, sum(len(names) for names in owners.values())


def vip_collisions(hosts: Dict[str, dict], data: dict, keys) -> List[str]:
    claimed = {str(data[key]): key for key in keys if data.get(key)}
    if not claimed:
        raise OperatorError(
            "the cluster config declares none of %s, so the VIP arm is examining "
            "nothing; pass --allow-missing-vip if this consumer declares no VIP"
            % ", ".join(keys)
        )
    out = []
    for name, host_vars in sorted(hosts.items()):
        address = str(host_vars.get("ansible_host") or "")
        if address in claimed:
            out.append("%s is on %s, which is %s" % (name, address, claimed[address]))
    return out


def outside_lan(hosts: Dict[str, dict], cidrs) -> Tuple[List[str], int]:
    """Hosts addressed outside every declared LAN CIDR, and how many were compared."""
    networks = [ipaddress.ip_network(str(c), strict=False) for c in cidrs]
    out = []
    compared = 0
    for name, host_vars in sorted(hosts.items()):
        raw = host_vars.get("ansible_host")
        if raw is None:
            continue
        try:
            address = ipaddress.ip_address(str(raw))
        except ValueError:
            continue  # a name rather than an address; DNS resolves it
        compared += 1
        if not any(address in network for network in networks):
            out.append("%s: %s" % (name, raw))
    return out, compared


def rule_files(paths, glob: str) -> List[Path]:
    """Every rule file under the given files and directories, deduplicated."""
    found: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(p for p in path.glob(glob) if p.is_file())
        else:
            raise OperatorError("no such rules path: %s" % path)
        for candidate in candidates:
            if candidate not in found:
                found.append(candidate)
    return found


def instance_literals(files) -> Dict[str, List[str]]:
    """{address: files that pin it}, for every dotted-quad `instance=` literal.

    A hostname or a placeholder is left to DNS and to the substitution gates;
    only a literal address silently survives a renumber.
    """
    found: Dict[str, List[str]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError("%s: %s" % (path, exc)) from exc
        for value in INSTANCE_LITERAL.findall(text):
            address = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version != 4:
                continue
            owners = found.setdefault(address, [])
            if str(path) not in owners:
                owners.append(str(path))
    return found


def host_addresses(hosts: Dict[str, dict]) -> Set[str]:
    """Every `ansible_host` the inventory declares, as a string set."""
    return {
        str(host_vars_["ansible_host"])
        for host_vars_ in hosts.values()
        if host_vars_.get("ansible_host")
    }


def unknown_instances(literals: Dict[str, List[str]], known: Set[str]) -> List[str]:
    """Pinned addresses no inventory host, cluster VIP or allowlist entry claims."""
    return [
        "%s is pinned in %s but is no inventory ansible_host"
        % (address, ", ".join(owners))
        for address, owners in sorted(literals.items())
        if address not in known
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hosts", type=Path,
        default=Path("ansible/inventories/prod/hosts.yml"),
    )
    parser.add_argument(
        "--cluster-config", type=Path,
        default=Path("kubernetes/infrastructure/sources/cluster-config.yaml"),
    )
    parser.add_argument(
        "--vip-key", action="append", default=None,
        help="cluster-config key holding a VIP; repeatable (default: every "
             "cluster_*_vip key the config declares)",
    )
    parser.add_argument(
        "--lan-cidr-key", action="append", default=None,
        help="cluster-config key holding a LAN CIDR; repeatable (default: %s)"
             % DEFAULT_LAN_CIDR_KEY,
    )
    parser.add_argument(
        "--rules-dir", action="append", default=None, metavar="PATH",
        help="alert-rule file or directory whose exact `instance=\"<ipv4>\"` "
             "literals must be inventory addresses; repeatable",
    )
    parser.add_argument(
        "--rules-glob", default=DEFAULT_RULES_GLOB,
        help="glob applied inside each --rules-dir directory (default: %s)"
             % DEFAULT_RULES_GLOB,
    )
    parser.add_argument(
        "--allow-instance", action="append", default=None, metavar="ADDRESS",
        help="address an alert rule may pin although no inventory host holds "
             "it; repeatable, and a stale entry is an error",
    )
    parser.add_argument(
        "--allow-missing-cluster-config", action="store_true",
        help="the VIP and LAN arms are a pass for a consumer with no cluster config",
    )
    parser.add_argument(
        "--allow-missing-lan-cidr", action="store_true",
        help="the LAN arm is a pass for a consumer whose config declares no LAN CIDR",
    )
    parser.add_argument(
        "--allow-missing-lan-addresses", action="store_true",
        help="the LAN arm is a pass for a consumer that addresses hosts by name",
    )
    parser.add_argument(
        "--allow-missing-vip", action="store_true",
        help="the VIP arm is a pass for a consumer whose config declares no VIP",
    )
    parser.add_argument(
        "--allow-missing-vmid", action="store_true",
        help="the duplicate-vmid arm is a pass for an inventory that keeps "
             "vmid in host_vars rather than hosts.yml",
    )
    parser.add_argument(
        "--allow-missing-ansible-host", action="store_true",
        help="the duplicate-ansible_host arm is a pass for an inventory that "
             "resolves hosts by name",
    )
    args = parser.parse_args(argv)

    if not args.hosts.is_file():
        print("no such inventory file: %s" % args.hosts, file=sys.stderr)
        return 2

    findings: List[str] = []
    skipped_arms: List[str] = []
    data: dict = {}
    try:
        hosts = inventory_hosts(args.hosts)
        if not hosts:
            raise OperatorError("%s declares no hosts" % args.hosts)

        for field, tail in (
            ("vmid", "`pct` and `qm` share one vmid namespace"),
            ("ansible_host", "whichever is provisioned second takes the address"),
        ):
            clashes, declared = duplicates(hosts, field)
            if not declared:
                if not getattr(args, "allow_missing_%s" % field):
                    raise OperatorError(
                        "no inventory host declares %s, so the duplicate-%s arm "
                        "is examining nothing; pass --allow-missing-%s if this "
                        "consumer has none"
                        % (field, field, field.replace("_", "-"))
                    )
                skipped_arms.append(field)
                continue
            if clashes:
                findings.append(
                    "duplicate %s in the inventory (%s):\n  %s"
                    % (field, tail, "\n  ".join(clashes))
                )

        if args.cluster_config.is_file():
            data = cluster_config(args.cluster_config)
            keys = args.vip_key or vip_keys(data)
            if not any(data.get(key) for key in keys) and args.allow_missing_vip:
                skipped_arms.append("VIP")
            else:
                collisions = vip_collisions(hosts, data, keys)
                if collisions:
                    findings.append(
                        "inventory hosts configured on a cluster VIP:\n  %s"
                        % "\n  ".join(collisions)
                    )
            lan_keys = args.lan_cidr_key or [DEFAULT_LAN_CIDR_KEY]
            cidrs = [data[key] for key in lan_keys if data.get(key)]
            if not cidrs and not args.allow_missing_lan_cidr:
                raise OperatorError(
                    "the cluster config declares none of %s, so the LAN arm is "
                    "examining nothing; pass --allow-missing-lan-cidr if this "
                    "consumer has none" % ", ".join(lan_keys)
                )
            if cidrs:
                outside, compared = outside_lan(hosts, cidrs)
                if not compared and "ansible_host" not in skipped_arms:
                    if not args.allow_missing_lan_addresses:
                        raise OperatorError(
                            "no inventory host declares an IP-literal "
                            "ansible_host, so the LAN arm is examining nothing; "
                            "pass --allow-missing-lan-addresses if this consumer "
                            "addresses hosts by name"
                        )
                    skipped_arms.append("LAN addresses")
                if outside:
                    findings.append(
                        "hosts addressed outside %s (%s):\n  %s"
                        % (", ".join(lan_keys),
                           ", ".join(str(c) for c in cidrs),
                           "\n  ".join(outside))
                    )
            else:
                skipped_arms.append("LAN")
        elif args.allow_missing_cluster_config:
            skipped_arms += ["VIP", "LAN"]
        else:
            raise OperatorError(
                "no cluster config at %s, so the VIP and LAN arms cannot run; "
                "pass --allow-missing-cluster-config if this consumer has none"
                % args.cluster_config
            )

        if args.rules_dir:
            allowed = {str(a) for a in args.allow_instance or []}
            literals = instance_literals(rule_files(args.rules_dir, args.rules_glob))
            if not literals:
                raise OperatorError(
                    "no `instance=\"<ipv4>\"` literal under %s (glob %s), so the "
                    "instance-parity arm is examining nothing; drop --rules-dir "
                    "if this consumer pins no address"
                    % (", ".join(str(p) for p in args.rules_dir), args.rules_glob)
                )
            stale = sorted(allowed - set(literals))
            if stale:
                raise OperatorError(
                    "--allow-instance entries no rule pins any more: %s; drop "
                    "them so the allowlist keeps naming real exceptions"
                    % ", ".join(stale)
                )
            # Cluster VIPs are legitimate scrape targets and live in the config,
            # not the inventory.
            known = {
                str(v) for v in host_addresses(hosts) | set(data.values()) | allowed
            }
            unknown = unknown_instances(literals, known)
            if unknown:
                findings.append(
                    "alert rules pin an address the inventory does not declare "
                    "(a renumber leaves the rule silently dead):\n  %s"
                    % "\n  ".join(unknown)
                )
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print("malformed cluster config value: %s" % exc, file=sys.stderr)
        return 2

    if findings:
        for finding in findings:
            print("FAIL %s" % finding, file=sys.stderr)
        return 1
    if skipped_arms:
        print("inventory invariants OK (%d hosts; %s arm(s) skipped by request)"
              % (len(hosts), " and ".join(skipped_arms)))
    else:
        print("inventory invariants OK (%d hosts)" % len(hosts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
