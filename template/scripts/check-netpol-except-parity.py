#!/usr/bin/env python3
"""Assert no fenced pod has unrestricted egress.

Checks /0 except-list parity, fence containment and allow-everything rules, over
the tree as written or a rendered --corpus. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (same directory)
        doc_namespace,
        exclude_nets,
        policy_types,
        zero_prefix,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent

# Full reserved-space list. Used by the platform policies whose egress is a
# narrow public-API call (Cloudflare, ACME, 1Password cloud).
RESERVED_FULL = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
]

# LAN-fence list. Used by app policies that need broad internet egress and only
# have to be kept off the LAN, Tailscale and the metadata address.
LAN_FENCE = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
]

CANONICAL = {"reserved-full": RESERVED_FULL, "lan-fence": LAN_FENCE}

# The constraints a NetworkPolicyPeer can carry. A peer with none of them is the
# empty peer, which matches every destination.
PEER_KEYS = ("ipBlock", "podSelector", "namespaceSelector")

# The ranges no egress rule may reach IN FULL. LAN_FENCE is the v4 half — exactly
# what every canonical list fences off; the v6 entries are its analogue, so an
# `::/0` egress cannot slip past a v4-only test.
FENCE_NETS = [ipaddress.ip_network(c) for c in LAN_FENCE] + [
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Egress rules that deliberately have no peers, i.e. allow egress everywhere,
# keyed "<namespace>/<name>" with the reason. Site data: populated from --config.
UNRESTRICTED_EGRESS_OK: dict[str, str] = {}


class ConfigError(ValueError):
    """A malformed --config file — an operator error (exit 2), not a violation."""


class Policy:
    """Consumer data from --config. A value, not module state, so a second load
    replaces rather than accumulates. A plain class, not a dataclass: the module
    is loaded by path with importlib, which cannot resolve the annotations.
    """

    def __init__(self, canonical=None, fence_nets=None, unrestricted_ok=None) -> None:
        self.canonical = dict(CANONICAL if canonical is None else canonical)
        self.fence_nets = list(FENCE_NETS if fence_nets is None else fence_nets)
        self.unrestricted_ok = dict(
            UNRESTRICTED_EGRESS_OK if unrestricted_ok is None else unrestricted_ok
        )


def load_config(path) -> Policy:
    """Read a --config file and return the Policy it describes."""
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ConfigError("top-level must be a mapping")
    policy = Policy()
    lists = doc.get("canonical_except_lists")
    if lists:
        if not isinstance(lists, dict) or not all(isinstance(v, list) for v in lists.values()):
            raise ConfigError("canonical_except_lists must map name -> [cidr]")
        policy.canonical = {str(k): [str(c) for c in v] for k, v in lists.items()}
    fences = doc.get("fence_networks")
    if fences:
        try:
            policy.fence_nets = [ipaddress.ip_network(str(c)) for c in fences]
        except ValueError as exc:
            raise ConfigError(f"fence_networks: {exc}") from exc
    allow = doc.get("unrestricted_egress_ok") or {}
    if not isinstance(allow, dict):
        raise ConfigError("unrestricted_egress_ok must map key -> reason")
    for key, reason in allow.items():
        if not str(reason or "").strip():
            raise ConfigError(f"unrestricted_egress_ok[{key}] has no reason")
    policy.unrestricted_ok = {str(k): str(v) for k, v in allow.items()}
    return policy


def _policy_key(doc) -> str:
    """`namespace/name` — no Kind, unlike gate_common.doc_key. An omitted
    namespace reads as `default`, the spelling every sibling gate uses."""
    meta = doc.get("metadata") or {}
    return f"{doc_namespace(doc)}/{meta.get('name', '<unnamed>')}"


def has_egress(spec) -> bool:
    """Whether the policy actually restricts egress.

    Mirrors the API's own derivation: an omitted policyTypes is inferred from
    the rules present, and an explicit empty list restricts nothing.
    """
    return "Egress" in policy_types(spec)


def _nets(cidrs, rendered=False):
    """Parse cidrs, returning (parsed, unparseable).

    A `${name}` placeholder is skipped in an unrendered tree, which the consumer
    substitutes before the API sees it, and reported in a rendered corpus.
    """
    parsed, bad = [], []
    for cidr in cidrs:
        if not rendered and isinstance(cidr, str) and "${" in cidr:
            continue
        try:
            parsed.append(ipaddress.ip_network(cidr, strict=False))
        except (ValueError, TypeError):
            bad.append(cidr)
    return parsed, bad


def unfenced_reach(blocks, policy=None, rendered=False):
    """The fence ranges an egress rule can still reach; [] when properly fenced.

    `blocks` is [(cidr, [except, ...]), ...] and an except narrows only its own
    peer. A rule is unfenced when a whole fence range fits inside it.
    """
    policy = policy or Policy()
    allowed = []
    for cidr, excepts in blocks:
        net, _bad = _nets([cidr], rendered)
        cuts, _bad_cuts = _nets(excepts, rendered)
        allowed.extend(exclude_nets(net, cuts))
    # Collapse before the fence check: a fenced range assembled from smaller
    # peers (two /17s covering a fenced /16) must not slip past a per-block
    # subnet test.
    collapsed = []
    for version in (4, 6):
        collapsed.extend(
            ipaddress.collapse_addresses([net for net in allowed if net.version == version])
        )
    return sorted(
        {
            str(fence)
            for fence in policy.fence_nets
            for net in collapsed
            if net.version == fence.version and fence.subnet_of(net)
        }
    )


def iter_egress_rules(doc):
    """Yield (key, index, rule) for every egress rule of an Egress policy."""
    if not isinstance(doc, dict) or doc.get("kind") != "NetworkPolicy":
        return
    spec = doc.get("spec") or {}
    if not has_egress(spec):
        return
    key = _policy_key(doc)
    rules = spec.get("egress")
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        if isinstance(rule, dict):
            yield key, index, rule


def iter_ip_blocks(doc):
    """Yield (policy_name, direction, cidr, except_list) for every ipBlock peer.

    except_list is [] when the key is absent or empty, which is the deletion
    case. Malformed rules are skipped here and reported by `malformed_rules`.
    """
    if not isinstance(doc, dict) or doc.get("kind") != "NetworkPolicy":
        return
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    spec = doc.get("spec") or {}
    for direction, key in (("egress", "to"), ("ingress", "from")):
        rules = spec.get(direction)
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict):
                continue
            peers = rule.get(key)
            for peer in peers if isinstance(peers, list) else []:
                if not isinstance(peer, dict):
                    continue
                block = peer.get("ipBlock")
                if not isinstance(block, dict):
                    continue
                yield name, direction, block.get("cidr"), list(block.get("except") or [])


def malformed_rules(doc):
    """Yield a message for every rule shape the apiserver would reject.

    Operator errors, not fence findings: a rule that is not a mapping, or a
    peer list that is not a list, says nothing about any except-list.
    """
    if not isinstance(doc, dict) or doc.get("kind") != "NetworkPolicy":
        return
    key = _policy_key(doc)
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return
    for direction, peer_key in (("egress", "to"), ("ingress", "from")):
        rules = spec.get(direction)
        if rules is None:
            continue
        if not isinstance(rules, list):
            yield f"NetworkPolicy {key}: spec.{direction} is not a list"
            continue
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                yield (
                    f"NetworkPolicy {key}: {direction} rule [{index}] is "
                    f"{rule!r}, not a mapping"
                )
                continue
            peers = rule.get(peer_key)
            if peers is not None and not isinstance(peers, list):
                yield (
                    f"NetworkPolicy {key}: {direction} rule [{index}] has a "
                    f"`{peer_key}:` that is not a list"
                )


def peerless_egress(rule):
    """Why an egress rule allows EVERY destination, or None.

    A rule with no `to:` carries no peers; a peer mapping with none of
    ipBlock/podSelector/namespaceSelector is the empty peer, read the same way.
    """
    peers = rule.get("to")
    if not peers:
        return "has no `to:` peers"
    for index, peer in enumerate(peers if isinstance(peers, list) else []):
        if isinstance(peer, dict) and not any(peer.get(k) is not None for k in PEER_KEYS):
            return (
                f"has an EMPTY `to:` peer [{index}] — no ipBlock, podSelector "
                f"or namespaceSelector"
            )
    return None


def classify(except_list, policy=None):
    """Return the canonical list name this matches, or None."""
    policy = policy or Policy()
    for label, canonical in policy.canonical.items():
        if except_list == canonical:
            return label
    return None


def _json_manifest_docs(payload):
    """The Kubernetes documents in a parsed JSON file, or None if it holds none.

    A JSON file under a manifest tree is as likely to be a Grafana dashboard,
    so the shape decides whether this gate owns the file at all.
    """
    docs = payload if isinstance(payload, list) else [payload]
    if docs and all(
        isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc
        for doc in docs
    ):
        return docs
    return None


def looks_like_json_manifest(path):
    """Whether an UNPARSEABLE JSON file was meant to be a manifest.

    Its text is all that is left to go on, and a dashboard that lost a comma
    must not read as the NetworkPolicy corpus failing to load.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return True
    return '"apiVersion"' in text and '"kind"' in text


def load_manifest(path):
    """Documents in one manifest file, or raise an OSError / parse error.

    A `.json` manifest holds one document, or a top-level list of them; a YAML
    stream carries several. A JSON file of another shape yields nothing.
    """
    path = Path(path)
    # The handle, not the text: PyYAML names the stream in its mark, so the
    # parse error points at the file instead of at "<unicode string>".
    with path.open() as fh:
        if path.suffix == ".json":
            return _json_manifest_docs(json.load(fh)) or []
        return list(yaml.safe_load_all(fh))


def scan_docs(docs, source, policy=None, rendered=False):
    """Return (violations, policies_scanned, errors) for one document stream.

    `source` prefixes every message. `rendered` says the placeholders are
    already substituted, so a leftover `${...}` CIDR is an operator error.
    """
    policy = policy or Policy()
    violations = []
    errors = []
    scanned = sum(
        1 for doc in docs if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy"
    )
    for doc in docs:
        for message in malformed_rules(doc):
            errors.append(f"{source}: {message}")
        # (a) A rule allowing every destination leaves no ipBlock behind for the
        # per-peer arms to inspect.
        for key, index, rule in iter_egress_rules(doc):
            reason = peerless_egress(rule)
            if reason:
                if key not in policy.unrestricted_ok:
                    violations.append(
                        f"{source}: NetworkPolicy {key} egress rule "
                        f"[{index}] {reason} — that allows "
                        f"egress to EVERY destination, LAN included, "
                        f"which is strictly more open than a /0 ipBlock "
                        f"with no except-list. Add peers, or declare the "
                        f"exemption under `unrestricted_egress_ok` in the "
                        f"--config file, with its reason."
                    )
                continue
            # (b) The peers may still reach a whole fenced range without any
            # single one of them being a /0.
            blocks = []
            for peer in rule.get("to") or []:
                block = peer.get("ipBlock") if isinstance(peer, dict) else None
                if isinstance(block, dict):
                    blocks.append((block.get("cidr"), list(block.get("except") or [])))
            cidrs = [cidr for cidr, _ in blocks]
            # Suppressed when the per-peer arm already names this rule, to keep
            # one message per defect.
            all_default = cidrs and all(zero_prefix(c) for c in cidrs)
            per_peer_reports = all_default and any(
                classify(exc, policy) is None for _, exc in blocks
            )
            reachable = (
                [] if per_peer_reports else unfenced_reach(blocks, policy, rendered)
            )
            if reachable and key not in policy.unrestricted_ok:
                violations.append(
                    f"{source}: NetworkPolicy {key} egress rule [{index}] "
                    f"reaches all of {', '.join(reachable)} via {cidrs}. "
                    f"A fenced range reached in full is a LAN escape "
                    f"however it is spelled — one narrower block or a /0 "
                    f"split into halves — so fence it with the canonical "
                    f"lan-fence (or reserved-full) except-list, or narrow "
                    f"the peer to the addresses actually needed."
                )
        for name, direction, cidr, except_list in iter_ip_blocks(doc):
            _parsed, bad = _nets([cidr] + list(except_list), rendered)
            for value in bad:
                errors.append(
                    f"{source}: NetworkPolicy {name} ({direction} ipBlock) has an "
                    f"unparseable CIDR {value!r} — the API would reject this "
                    f"policy, so it says nothing about the fence."
                )
            # The canonical lists are the egress /0 contract; an ingress peer or
            # a narrower egress block keeps whatever it excludes.
            if direction != "egress" or not zero_prefix(cidr):
                continue
            if not except_list:
                violations.append(
                    f"{source}: NetworkPolicy {name} has an EGRESS "
                    f"ipBlock {cidr} with no except-list — that is "
                    f"unrestricted egress to the LAN, loopback and "
                    f"cloud-metadata ranges. Add the canonical "
                    f"lan-fence (or reserved-full) list."
                )
                continue
            if classify(except_list, policy):
                continue
            violations.append(
                f"{source}: NetworkPolicy {name} (egress ipBlock {cidr}) "
                f"has a non-canonical except-list: {except_list}"
            )
    return violations, scanned, errors


def scan_corpus(stream, policy=None, source="<corpus>"):
    """Scan an already-substituted multi-document stream.

    The placeholder skip is off here, so the fence arms judge real CIDRs — in an
    unrendered tree they see `${...}` and examine nothing.
    """
    try:
        docs = list(yaml.safe_load_all(stream))
    except yaml.YAMLError as e:
        return [], 0, [f"{source}: unparseable YAML: {e}"]
    return scan_docs(docs, source, policy, rendered=True)


def scan_paths(paths, policy=None):
    """Return (violations, policies_scanned, errors).

    `policies_scanned` stops a renamed manifest subtree turning the gate
    vacuously green; `errors` carries operator mistakes, which exit 2.
    """
    policy = policy or Policy()
    violations = []
    errors = []
    scanned = 0
    for root in paths:
        root = Path(root)
        if root.is_dir():
            files = [
                (p, True)
                for p in sorted(
                    p for ext in ("*.yaml", "*.yml", "*.json") for p in root.rglob(ext)
                )
            ]
        elif root.is_file():
            files = [(root, False)]
        else:
            errors.append(f"{root}: no such file or directory")
            continue
        for path, discovered in files:
            try:
                docs = load_manifest(path)
            except OSError as e:
                errors.append(f"{path}: unreadable: {e}")
                continue
            except json.JSONDecodeError as e:
                # A file this gate was POINTED at is its subject whatever it
                # holds; one it merely walked onto has to look like a manifest.
                if not discovered or looks_like_json_manifest(path):
                    errors.append(f"{path}: unparseable JSON: {e}")
                continue
            except yaml.YAMLError as e:
                errors.append(f"{path}: unparseable YAML: {e}")
                continue
            doc_violations, doc_scanned, doc_errors = scan_docs(docs, path, policy)
            violations += doc_violations
            scanned += doc_scanned
            errors += doc_errors
    return violations, scanned, errors


def check_paths(paths, policy=None):
    """Just the policy violations — the accounting arms belong to `main`."""
    return scan_paths(paths, policy)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Public-egress NetworkPolicies must carry a canonical reserved-CIDR fence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="consumer policy data (see module docstring)")
    parser.add_argument(
        "--corpus",
        metavar="FILE",
        help="rendered multi-document stream to scan instead of the default tree "
             "('-' for stdin); its placeholders are already substituted",
    )
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    args = parser.parse_args(argv)

    policy = Policy()
    if args.config:
        try:
            policy = load_config(args.config)
        except (OSError, ConfigError, yaml.YAMLError) as exc:
            # Same rule as a missing scan path: an operator error, not a
            # traceback and not exit 1 (which reads as "the fence drifted").
            detail = getattr(exc, "strerror", None) or exc
            print(f"ERROR: --config {args.config}: {detail}", file=sys.stderr)
            return 2
    # A corpus replaces the default tree, not an explicit path list: a consumer
    # pipes its substituted render in and the fence arms judge real CIDRs.
    paths = args.paths or ([] if args.corpus else [REPO / "kubernetes"])
    violations, scanned, errors = scan_paths(paths, policy)
    if args.corpus:
        try:
            if args.corpus == "-":
                corpus = scan_corpus(sys.stdin, policy, "<stdin>")
            else:
                with open(args.corpus) as fh:
                    corpus = scan_corpus(fh, policy, args.corpus)
        except OSError as exc:
            detail = getattr(exc, "strerror", None) or exc
            print(f"ERROR: --corpus {args.corpus}: {detail}", file=sys.stderr)
            return 2
        violations += corpus[0]
        scanned += corpus[1]
        errors += corpus[2]
    if errors:
        print("ERROR: the manifest corpus could not be read:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    if violations:
        print("ERROR: NetworkPolicy except-lists have drifted from the canonical sets:")
        for v in violations:
            print(f"  - {v}")
        print("Canonical sets: " + ", ".join(sorted(policy.canonical)))
        return 1
    if not scanned:
        # A renamed or moved manifest subtree would otherwise leave the LAN-fence
        # gate green with nothing behind it.
        inspected = [str(p) for p in paths] + ([args.corpus] if args.corpus else [])
        print(
            "ERROR: scanned 0 NetworkPolicy manifests under "
            f"{', '.join(inspected)} — a gate that inspects nothing "
            "is not a gate. Point it at the manifests, or drop the job.",
            file=sys.stderr,
        )
        return 2
    print(
        f"NetworkPolicy egress is fenced across {scanned} policy/policies: every /0 "
        "peer carries a canonical except-list, no egress rule reaches a fenced "
        f"range in full, and the {len(policy.unrestricted_ok)} peer-less rule(s) "
        "are declared."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
