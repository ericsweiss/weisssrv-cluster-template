#!/usr/bin/env python3
"""Shared loader, selector and CIDR helpers for the corpus and live-input gates.

Exit contract: 0 clean, 1 violations, 2 unreadable input or invocation.
Vendor this file alongside any gate that imports it.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_NAMESPACE = "default"

# The cluster-identity ConfigMap every post-sources Flux stage substitutes from.
CLUSTER_CONFIG = "kubernetes/infrastructure/sources/cluster-config.yaml"

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

EMPTY_CORPUS_MESSAGE = (
    "empty corpus — no manifests on stdin. A gate that passes on nothing is not "
    "a gate; check the pipe and the `kustomize build` paths feeding it."
)

EMPTY_LIVE_INPUT_MESSAGE = (
    "no {what} on stdin. A gate that checks nothing is not a gate; check the "
    "kubectl on the left of the pipe (a wrong context or namespace selector "
    "returns a valid, empty item list)."
)

SELECTOR_KEYS = {"matchLabels", "matchExpressions"}


class OperatorError(RuntimeError):
    """Input or invocation the gate cannot act on — exit 2, never exit 1."""


def load_docs(stream) -> list[dict]:
    """Every mapping document on `stream`, flattening `kind: List` and bare lists."""
    docs: list[dict] = []
    try:
        for raw in yaml.safe_load_all(stream):
            if isinstance(raw, dict):
                if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
                    docs.extend(i for i in raw["items"] if isinstance(i, dict))
                else:
                    docs.append(raw)
            elif isinstance(raw, list):
                docs.extend(i for i in raw if isinstance(i, dict))
    except yaml.YAMLError as exc:
        raise OperatorError(f"failed to parse YAML input: {exc}") from exc
    return docs


def load_corpus(stream=None) -> list[dict]:
    """Documents on `stream`, or exit 2 on a parse failure or an empty corpus."""
    try:
        docs = load_docs(sys.stdin if stream is None else stream)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not docs:
        print(f"ERROR: {EMPTY_CORPUS_MESSAGE}", file=sys.stderr)
        raise SystemExit(2)
    return docs


def load_live_items(stream=None, what: str = "objects") -> list[dict]:
    """Items from a `kubectl get -o json` payload on `stream`, or exit 2.

    A valid, empty item list is an operator error, not a clean cluster: the
    wrong context or selector returns one and every check then reports nothing.
    """
    try:
        payload = json.load(sys.stdin if stream is None else stream)
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse `kubectl get -o json` input: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        print(
            f"ERROR: input is not a {what} list (expected `kubectl get ... -o json`)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        print(f"ERROR: {EMPTY_LIVE_INPUT_MESSAGE.format(what=what)}", file=sys.stderr)
        raise SystemExit(2)
    return items


def load_cluster_config(root: Path | str = ".", path: str = CLUSTER_CONFIG) -> dict[str, str]:
    """The cluster-config ConfigMap's `data:` map, every value coerced to str.

    Raises OperatorError on an unreadable file or an empty map, so a gate whose
    key set came from here exits 2 rather than passing having checked nothing.
    """
    config_path = Path(root) / path
    try:
        docs = load_docs(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{path} could not be read: {exc}") from exc
    except OperatorError as exc:
        raise OperatorError(f"{path}: {exc}") from exc
    for doc in docs:
        if doc.get("kind") not in (None, "ConfigMap"):
            continue
        data = doc.get("data")
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    raise OperatorError(f"no data keys in {path} — the gate has no key set to check")


def substitute(value: object, config: dict[str, str]) -> str:
    """Resolve a whole-value `${name}` placeholder the way Flux's postBuild does.

    Only a value that is nothing but one placeholder resolves; an unknown name
    and any partial spelling are returned untouched, to fail the caller's parse.
    """
    text = str(value if value is not None else "")
    match = _PLACEHOLDER.fullmatch(text.strip())
    if match and match.group(1) in config:
        return config[match.group(1)]
    return text


def doc_namespace(doc: dict) -> str:
    """The namespace a document lands in. An omitted one IS `default` to the API."""
    return (doc.get("metadata") or {}).get("namespace") or DEFAULT_NAMESPACE


def doc_key(doc: dict) -> str:
    """`namespace/Kind/name` — the one spelling every gate's allowlists use."""
    meta = doc.get("metadata") or {}
    return f"{doc_namespace(doc)}/{doc.get('kind')}/{meta.get('name', '?')}"


def policy_types(spec: dict) -> set[str]:
    """A NetworkPolicy's policyTypes, inferring an absent field as the API does.

    The field is omitempty, so an empty list round-trips as an absent one and
    takes the same inferred default.
    """
    declared = spec.get("policyTypes")
    if isinstance(declared, list) and declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def selects_all_pods(selector: object) -> bool:
    """True when a label selector selects everything in its scope."""
    if selector is None or selector == {}:
        return True
    if not isinstance(selector, dict):
        return False
    # Unknown keys first: a `matchLables:` typo leaves the recognised terms empty.
    if set(selector) - SELECTOR_KEYS:
        return False
    labels = selector.get("matchLabels")
    exprs = selector.get("matchExpressions")
    # Typed, not truthy: API-invalid terms neither fence nor defeat a fence.
    if labels is not None and not isinstance(labels, dict):
        return False
    if exprs is not None and not isinstance(exprs, list):
        return False
    return not (labels or exprs)


def zero_prefix(cidr: object) -> bool:
    """Whether a CIDR's prefix length is 0 — numerically, so `/00` counts."""
    _address, separator, prefix_length = str(cidr or "").strip().rpartition("/")
    try:
        return separator == "/" and int(prefix_length, 10) == 0
    except ValueError:
        return False


def exclude_nets(nets: list, cuts: list) -> list:
    """The parts of `nets` no entry of `cuts` covers.

    CIDRs nest or are disjoint, so each net is kept, dropped, or split. A cut of
    another address family covers nothing and passes the net through.
    """
    remaining = list(nets)
    for cut in cuts:
        surviving = []
        for part in remaining:
            if part.version != cut.version:
                surviving.append(part)
            elif part.subnet_of(cut):
                continue
            elif cut.subnet_of(part):
                surviving.extend(part.address_exclude(cut))
            else:
                surviving.append(part)
        remaining = surviving
        if not remaining:
            return remaining
    return remaining


def excepts_cover_cidr(excepts: object, cidr: object) -> bool:
    """True only when the except list leaves no address of `cidr` admitted.

    Exact subtraction, not a well-known-ranges heuristic: pod CIDRs vary by
    cluster. An entry the API would reject never counts toward coverage.
    """
    try:
        net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
    except ValueError:
        return False
    cuts = []
    for raw in excepts if isinstance(excepts, list) else []:
        try:
            exc = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        # Each entry must be a strict subnet of the cidr, so an equal or
        # out-of-range one is API-invalid and credits no coverage.
        if exc.version != net.version or exc == net or not exc.subnet_of(net):
            continue
        cuts.append(exc)
    return not exclude_nets([net], cuts)


def peer_selects_everything(peer: object) -> bool:
    """True when one NetworkPolicy peer narrows nothing."""
    if not isinstance(peer, dict):
        return False
    ip_block = peer.get("ipBlock")
    if ip_block is not None:
        if not isinstance(ip_block, dict):
            return False
        if not zero_prefix(ip_block.get("cidr")):
            return False
        return not excepts_cover_cidr(ip_block.get("except"), ip_block.get("cidr"))
    # An omitted namespaceSelector scopes the peer to the policy's own namespace,
    # so it narrows even when the podSelector is empty.
    if peer.get("namespaceSelector") is None and "podSelector" in peer:
        return False
    for key in ("namespaceSelector", "podSelector"):
        if not selects_all_pods(peer.get(key)):
            return False
    return True


def parse_exempt(values: list[str]) -> dict[str, str]:
    """`NS=REASON` pairs. A reason is mandatory: an unexplained exemption is a hole."""
    exempt: dict[str, str] = {}
    for raw in values or []:
        ns, sep, reason = raw.partition("=")
        if not sep or not ns.strip() or not reason.strip():
            raise OperatorError(f"--exempt takes NS=REASON, got {raw!r}")
        exempt[ns.strip()] = reason.strip()
    return exempt
