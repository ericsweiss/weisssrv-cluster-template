#!/usr/bin/env python3
"""Assert every namespace that owns a workload carries an ingress default-deny.

The invariant is that no namespace is reachable by every pod in the cluster;
the usual mechanism is a `netpol-baseline`-style component adding a
namespace-wide deny. Its sibling `scripts/check-scrape-netpol.py` cannot cover
it: that gate only inspects namespaces which ALREADY run an ingress-deny policy,
so an unfenced namespace is invisible to it rather than a failure — which is
also why handing that gate an `--exempt` for an unfenced namespace is inert.

Reads the rendered corpus (`kustomize build | envsubst`) on stdin.

A namespace OWNS A WORKLOAD when the corpus puts a Deployment / StatefulSet /
DaemonSet / ReplicaSet / Job / CronJob / Pod in it, or a HelmRelease targets it
(a chart's own workloads never appear in a kustomize corpus, so the release is
the only visible proxy).

A document with NO `metadata.namespace` is read the way the API reads it: it
lands in the implicit `default` namespace, which is a real namespace and is
fenced like any other. Treating "absent" as "no namespace" instead would drop
those documents on the floor — a namespace-less Deployment would own nothing,
and `default` would be the one namespace the mandate never reached. A
HelmRelease still honours `spec.targetNamespace`, falling back to that same
defaulted namespace.

A namespace is FENCED when it carries a NetworkPolicy with `Ingress` in
policyTypes and a podSelector that selects every pod — absent, `{}`, or the
equivalent empty-termed spellings `{matchLabels: {}}` / `{matchExpressions: []}`
— AND no namespace-wide policy that ALLOWS all ingress (see
`_allows_all_ingress`).
An app-scoped policy is deliberately not enough: it fences its own pods and
leaves every other pod in the namespace open. Neither is a namespace-wide policy
whose `ingress:` rule names no ports and admits every peer — either by carrying
neither `from` nor `ports` (`{}`, the API's "from anywhere, on any port") or by
listing a peer that selects everything (`{}`, `namespaceSelector: {}`,
`podSelector: {}`, or a `/0` `ipBlock` whose except list leaves any address
admitted). Those
shapes satisfy a bare "has an Ingress policyType" test while granting exactly
what the mandate forbids.
NetworkPolicies are additive, so one such policy defeats every default-deny
beside it — hence the namespace is reported unfenced even when a real
`default-deny-ingress` is present too.

EXEMPT namespaces are the ones a repo does not fully own. `flux-system` is the
one built in, because it is universal to a Flux cluster; everything else is site
state and comes in through `--exempt NS=REASON`, never through an edit to this
file (which is vendored, so a local edit is reverted by the next re-vendor).
Unused exemptions are reported, never fatal: a namespace can drop out of the
corpus without the exemption becoming wrong.

Exit codes: 0 clean, 1 an unfenced namespace, 2 the gate could not inspect its
subject (empty corpus, or a corpus with no workload namespace at all — the shape
a render loop that never reached the app stages produces).

Usage (wired into flux:lint, on the accumulated full corpus):
  python3 scripts/check-default-deny-coverage.py [--exempt NS=REASON ...] < corpus
"""
from __future__ import annotations

import argparse
import ipaddress
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Pod",
}

# The one exemption every Flux cluster needs, machine-readable so it is visible
# rather than assumed. A consumer's own exemptions are `--exempt` flags: this
# file is vendored byte-identical, so an exemption added here would be reverted
# on the next re-vendor and would leak one repo's policy into every other.
EXEMPT_NAMESPACES = {
    "flux-system": (
        "the gotk-components manifest ships its own policies and is regenerated "
        "verbatim by Flux's own install/bootstrap; a policy added there would be "
        "reverted."
    ),
}


class OperatorError(RuntimeError):
    """A broken invocation or an uninspectable corpus — exit 2, never exit 1."""


def _policy_types(spec: dict) -> set[str]:
    """policyTypes, applying the API default (inferred from which rules exist)."""
    declared = spec.get("policyTypes")
    if declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def _zero_prefix(cidr: object) -> bool:
    """Whether a CIDR's prefix length is 0 — numerically, so `/00` counts."""
    _address, separator, prefix_length = str(cidr or "").strip().rpartition("/")
    try:
        return separator == "/" and int(prefix_length, 10) == 0
    except ValueError:
        return False


def _excepts_cover_cidr(excepts: object, cidr: object) -> bool:
    """True only when the except list leaves NO address of `cidr` admitted.

    Computed by exact subtraction, not by a well-known-ranges heuristic: which
    space pods live in is a property of the cluster, and this file is vendored
    into clusters whose pod CIDRs may be CGNAT, public, or IPv6. Unparseable
    entries do not count toward coverage — an except the API would reject
    cannot be what closes the namespace, and guessing in its favour would
    fail open.
    """
    try:
        net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
    except ValueError:
        return False
    remaining = [net]
    for raw in excepts if isinstance(excepts, list) else []:
        try:
            exc = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        # Only an except the API would accept may subtract: Kubernetes
        # requires each entry to be a STRICT subnet of the cidr, so an equal
        # or out-of-range entry belongs to a policy that never admits — and
        # letting it erase the network would certify a fence the (rejected)
        # policy does not provide.
        if exc.version != net.version or exc == net or not exc.subnet_of(net):
            continue
        surviving = []
        for part in remaining:
            # CIDRs nest or are disjoint — no partial overlap exists.
            if part.subnet_of(exc):
                continue
            if exc.subnet_of(part):
                surviving.extend(part.address_exclude(exc))
            else:
                surviving.append(part)
        remaining = surviving
        if not remaining:
            return True
    return not remaining


def _selects_all_pods(selector: object) -> bool:
    """True when a podSelector selects every pod in the namespace.

    Absent and `{}` are the API's spellings of "all pods", but so are
    `{matchLabels: {}}` and `{matchExpressions: []}` — an EMPTY selector term
    matches everything, it does not match nothing. A truthiness test on the
    selector mapping reads those namespace-wide policies as app-scoped, which
    flips both verdicts this gate hands out: a real default-deny written that
    way stops counting as a fence, and a wide-open allow written that way
    stops counting as the policy that defeats one.
    """
    if selector is None or selector == {}:
        return True
    if not isinstance(selector, dict):
        return False
    # Unknown keys before empty terms: a typo like `matchLables:` leaves the
    # recognised terms empty, and reading that as select-all would let a
    # policy server-side apply REJECTS act on the verdict.
    if set(selector) - {"matchLabels", "matchExpressions"}:
        return False
    labels = selector.get("matchLabels")
    exprs = selector.get("matchExpressions")
    # Typed, not truthy: `matchLabels: []` / `matchExpressions: {}` are
    # API-invalid — a policy carrying them never applies, so it must neither
    # count as a fence nor as the wide-open peer that defeats one.
    if labels is not None and not isinstance(labels, dict):
        return False
    if exprs is not None and not isinstance(exprs, list):
        return False
    return not (labels or exprs)


def _peer_selects_everything(peer: object) -> bool:
    """True when one `from` peer narrows nothing.

    A `NetworkPolicyPeer` narrows either by CIDR (`ipBlock`) or by label
    selector, and an EMPTY label selector is not an absent one: `{}` matches
    every object in its scope, so `namespaceSelector: {}` means every namespace
    and `podSelector: {}` every pod. A peer built only from empty selectors —
    or the bare `{}` — therefore admits everything the rule could name, exactly
    like a rule with no `from` at all.

    An `ipBlock` usually narrows, but a zero-length prefix admits every
    address there is — the spelling the earlier "any ipBlock narrows" reading
    waved through as a fence-compatible peer. Judged by the PREFIX LENGTH,
    not by matching `0.0.0.0/0`/`::/0` textually: `/0` covers the whole
    family whatever the address half spells (`0:0:0:0:0:0:0:0/0`,
    `10.0.0.0/0`). An `except` list only closes it when it leaves NO address
    admitted — judged by exact subtraction, never by assuming which ranges a
    cluster's pods occupy (this file is vendored into clusters whose pod
    CIDRs may be CGNAT, public, or IPv6). In practice a `/0` ingress peer is
    therefore wide open unless its excepts reconstruct the whole family.
    """
    if not isinstance(peer, dict):
        return False
    ip_block = peer.get("ipBlock")
    if ip_block is not None:
        if not isinstance(ip_block, dict):
            return False
        if not _zero_prefix(ip_block.get("cidr")):
            return False
        return not _excepts_cover_cidr(ip_block.get("except"), ip_block.get("cidr"))
    for key in ("namespaceSelector", "podSelector"):
        if not _selects_all_pods(peer.get(key)):
            return False
    return True


def _allows_all_ingress(spec: dict) -> bool:
    """True when an ingress rule admits everything.

    Two shapes qualify, and a bare "has an Ingress policyType" test counts both
    as a fence:

    * an `ingress:` entry with neither `from` nor `ports` — the API's "allow
      from any source, on any port";
    * an entry with no `ports` whose `from` list holds a peer that selects
      everything (see `_peer_selects_everything`). Peers within one rule are
      OR'd, so a single such peer opens the whole rule.

    A policy carrying either fences nothing — the gate is named for the mandate,
    not for the mere presence of an Ingress policyType.
    A rule that names ports is NOT treated as wide open here, whatever its
    peers: it still narrows the surface, and calling it a violation would be a
    different (port-level) mandate than the one this gate enforces.
    """
    for rule in spec.get("ingress") or []:
        if not isinstance(rule, dict) or rule.get("ports"):
            continue
        peers = rule.get("from")
        if not peers:
            return True
        if isinstance(peers, list) and any(_peer_selects_everything(p) for p in peers):
            return True
    return False


def _load(stream) -> list[dict]:
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


def analyze(docs: list[dict]) -> tuple[dict[str, set[str]], set[str]]:
    """-> (namespace -> the workloads that put it in scope, fenced namespaces)."""
    workloads: dict[str, set[str]] = {}
    fenced: set[str] = set()
    # Namespace-wide policies that ALLOW all ingress. Tracked separately and
    # subtracted at the end because NetworkPolicies are additive: one of these
    # re-opens the namespace no matter what else is declared beside it.
    wide_open: set[str] = set()

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        # The API's own defaulting: an omitted namespace IS `default`, not
        # "nowhere". Dropping such documents would exempt a whole namespace.
        ns = meta.get("namespace") or "default"
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind in WORKLOAD_KINDS:
            workloads.setdefault(ns, set()).add(f"{kind}/{name}")
        elif kind == "HelmRelease":
            target = spec.get("targetNamespace") or ns
            workloads.setdefault(target, set()).add(f"HelmRelease/{name}")
        elif (
            kind == "NetworkPolicy"
            and "Ingress" in _policy_types(spec)
            # Namespace-wide by SELECTION, not by truthiness: `{matchLabels: {}}`
            # selects every pod just as `{}` does (_selects_all_pods). At SPEC
            # level `podSelector` is a REQUIRED field, so absent is not the
            # all-pods default but a policy the API rejects — it can neither
            # fence nor defeat.
            and isinstance(spec.get("podSelector"), dict)
            and _selects_all_pods(spec.get("podSelector"))
        ):
            # Namespace-wide and ingress-typed: either the fence itself, or the
            # policy that re-opens the namespace despite one.
            if _allows_all_ingress(spec):
                wide_open.add(ns)
            else:
                fenced.add(ns)

    return workloads, fenced - wide_open


def _parse_exempt(values: list[str]) -> dict[str, str]:
    """`NS=REASON` pairs. A reason is mandatory: an unexplained exemption is a hole."""
    exempt: dict[str, str] = {}
    for raw in values or []:
        ns, sep, reason = raw.partition("=")
        if not sep or not ns.strip() or not reason.strip():
            raise OperatorError(f"--exempt takes NS=REASON, got {raw!r}")
        exempt[ns.strip()] = reason.strip()
    return exempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every workload-owning namespace must carry an ingress default-deny.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        metavar="NS=REASON",
        help="additional exempt namespace, with its reason (repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        exempt = dict(EXEMPT_NAMESPACES)
        exempt.update(_parse_exempt(args.exempt))
        docs = _load(sys.stdin)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not docs:
        print(
            "ERROR: empty corpus — no manifests on stdin. A gate that passes on nothing "
            "is not a gate; check the pipe and the `kustomize build` paths feeding it.",
            file=sys.stderr,
        )
        return 2

    workloads, fenced = analyze(docs)

    if not workloads:
        print(
            f"ERROR: inspected 0 workload namespaces in {len(docs)} document(s) — the "
            "render loop produced documents but reached no stage that deploys a "
            "workload, so every namespace went unexamined.",
            file=sys.stderr,
        )
        return 2

    violations = []
    for ns in sorted(workloads):
        if ns in fenced or ns in exempt:
            continue
        owners = ", ".join(sorted(workloads[ns])[:4])
        violations.append(
            f"  {ns}: owns workloads ({owners}) but no namespace-wide NetworkPolicy "
            f"that denies ingress by default — a policy carrying an empty ingress "
            f"rule (`ingress: [{{}}]`) allows everything and does not count. Add the "
            f"netpol-baseline component to the namespace's kustomization, or declare "
            f"the exemption where this gate is invoked (--exempt {ns}=REASON)."
        )

    if violations:
        print(
            "Ingress default-deny mandate violated — namespaces open to every pod "
            "in the cluster:",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
        return 1

    unused = sorted(ns for ns in exempt if ns not in workloads)
    if unused:
        print(f"(exemptions declared but not exercised by this corpus: {', '.join(unused)})")
    print(
        f"Ingress default-deny OK ({len(workloads)} workload namespaces, "
        f"{len([n for n in workloads if n in fenced])} fenced, "
        f"{len([n for n in workloads if n in exempt])} exempt) in {len(docs)} document(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
