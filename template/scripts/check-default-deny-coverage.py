#!/usr/bin/env python3
"""Assert every namespace that owns a workload carries an ingress default-deny.

Reads the rendered corpus on stdin, exemptions via --exempt NS=REASON and an
egress default-deny per --require-egress NS. Exit codes: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        OperatorError,
        doc_namespace,
        load_corpus,
        parse_exempt,
        peer_selects_everything,
        policy_types,
        selects_all_pods,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Pod",
}

# Vendored file: a consumer's own exemptions are --exempt flags, so the one
# exemption every Flux cluster needs is machine-readable here rather than assumed.
EXEMPT_NAMESPACES = {
    "flux-system": (
        "the gotk-components manifest ships its own policies and is regenerated "
        "verbatim by Flux's own install/bootstrap; a policy added there would be "
        "reverted."
    ),
}


def _allows_all(spec: dict, direction: str, peer_key: str) -> bool:
    """True when a rule in one direction admits everything.

    An entry with neither peers nor `ports`, or a port-less entry whose peer
    selects everything. A rule naming ports still narrows the surface.
    """
    for rule in spec.get(direction) or []:
        if not isinstance(rule, dict) or rule.get("ports"):
            continue
        peers = rule.get(peer_key)
        if not peers:
            return True
        if isinstance(peers, list) and any(peer_selects_everything(p) for p in peers):
            return True
    return False


def analyze(docs: list[dict]) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """-> (namespace -> the workloads in scope, ingress-fenced, egress-fenced)."""
    workloads: dict[str, set[str]] = {}
    fenced: set[str] = set()
    egress_fenced: set[str] = set()
    # Additive: one wide-open allow re-opens the namespace, so subtract at the end.
    wide_open: set[str] = set()
    egress_wide_open: set[str] = set()

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        ns = doc_namespace(doc)
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind in WORKLOAD_KINDS:
            workloads.setdefault(ns, set()).add(f"{kind}/{name}")
        elif kind == "HelmRelease":
            target = spec.get("targetNamespace") or ns
            workloads.setdefault(target, set()).add(f"HelmRelease/{name}")
        elif (
            kind == "NetworkPolicy"
            # podSelector is required at spec level, so absent is API-invalid,
            # not all-pods: it can neither fence nor defeat a fence.
            and isinstance(spec.get("podSelector"), dict)
            and selects_all_pods(spec.get("podSelector"))
        ):
            declared = policy_types(spec)
            if "Ingress" in declared:
                target = wide_open if _allows_all(spec, "ingress", "from") else fenced
                target.add(ns)
            if "Egress" in declared:
                target = (
                    egress_wide_open if _allows_all(spec, "egress", "to") else egress_fenced
                )
                target.add(ns)

    return workloads, fenced - wide_open, egress_fenced - egress_wide_open


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
    parser.add_argument(
        "--require-egress",
        action="append",
        default=[],
        metavar="NS",
        help="namespace that must also carry an egress default-deny (repeatable). "
             "Opt-in: the ingress mandate is cluster-wide, egress is per namespace",
    )
    args = parser.parse_args(argv)

    try:
        exempt = dict(EXEMPT_NAMESPACES)
        exempt.update(parse_exempt(args.exempt))
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    docs = load_corpus()

    workloads, fenced, egress_fenced = analyze(docs)

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

    for ns in sorted(set(args.require_egress)):
        if ns in egress_fenced:
            continue
        violations.append(
            f"  {ns}: --require-egress names it, but no namespace-wide NetworkPolicy "
            f"denies egress by default — a policy whose podSelector is empty must "
            f"list Egress in policyTypes, and an egress rule with neither `to` nor "
            f"`ports` re-opens it. Add the egress default-deny to the namespace's "
            f"kustomization, or drop --require-egress {ns}."
        )

    if violations:
        print(
            "Default-deny mandate violated — namespaces open to every pod "
            "in the cluster:",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
        return 1

    unused = sorted(ns for ns in exempt if ns not in workloads)
    if unused:
        print(f"(exemptions declared but not exercised by this corpus: {', '.join(unused)})")
    egress = len(set(args.require_egress))
    print(
        f"Ingress default-deny OK ({len(workloads)} workload namespaces, "
        f"{len([n for n in workloads if n in fenced])} fenced, "
        f"{len([n for n in workloads if n in exempt])} exempt"
        + (f", {egress} egress-fenced" if egress else "")
        + f") in {len(docs)} document(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
