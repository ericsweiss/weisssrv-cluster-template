#!/usr/bin/env python3
"""Assert every scraped namespace admits Prometheus through its NetworkPolicies.

A namespace carrying any NetworkPolicy with `Ingress` in policyTypes is
ingress-default-deny. Enabling a ServiceMonitor/PodMonitor without the paired
allow leaves the pod healthy — kubelet probes originate in the host netns and
bypass the CNI policy chain — while the scrape is REJECTed, so the only symptom
is TargetDown. A namespace counts as scraped via a ServiceMonitor/PodMonitor
(its own namespace or `spec.namespaceSelector.matchNames`) or via a HelmRelease
enabling a chart-native monitor in `.spec.values`; chart-rendered monitors never
appear in the kustomize corpus.

Namespace-level reachability only — port and pod-selector granularity resolve at
chart-render time (docs/SCRIPTS.md). Exit 0 clean, 1 on a finding, 2 on an
operator error including an empty corpus or one holding no scrape target.

Usage (wired into flux:lint, on the accumulated full corpus):
  kustomize build <path> | envsubst >> corpus
  python3 scripts/check-scrape-netpol.py [--observability-namespace NS]
                                         [--exempt NS=REASON ...] < corpus
"""
from __future__ import annotations

import argparse
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

MONITOR_KINDS = {"ServiceMonitor", "PodMonitor"}
DEFAULT_OBSERVABILITY_NS = "observability"
NS_NAME_LABEL = "kubernetes.io/metadata.name"


def _selects_observability(peer: dict, observability_ns: str) -> bool:
    """True if a NetworkPolicy `from` peer provably matches the observability
    namespace.

    Only the `kubernetes.io/metadata.name` label is guaranteed on a namespace;
    any OTHER requirement in the selector depends on namespace labels the
    corpus does not carry, so a selector that adds one cannot be proven to
    match. Refusing to credit it errs toward a visible false-block rather
    than a silent false-allow.
    """
    nssel = peer.get("namespaceSelector")
    if nssel is None:
        return False
    labels = nssel.get("matchLabels") or {}
    exprs = nssel.get("matchExpressions") or []
    name_matched = labels.get(NS_NAME_LABEL) == observability_ns
    extra_requirements = any(k != NS_NAME_LABEL for k in labels)
    for expr in exprs:
        if (
            expr.get("key") == NS_NAME_LABEL
            and expr.get("operator") == "In"
            and observability_ns in (expr.get("values") or [])
        ):
            name_matched = True
        else:
            extra_requirements = True
    return name_matched and not extra_requirements


def _policy_types(spec: dict) -> set[str]:
    """policyTypes, applying the API default (inferred from which rules exist)."""
    declared = spec.get("policyTypes")
    if declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def _find_monitor_toggles(node, path: str = "") -> list[str]:
    """Paths of truthy `serviceMonitor.enabled` / `podMonitor.enabled` in values.

    Case-insensitive: charts spell it both `serviceMonitor` (traefik, ESO) and
    `servicemonitor` (cert-manager).
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if (
                str(key).lower() in ("servicemonitor", "podmonitor")
                and isinstance(value, dict)
                and value.get("enabled") is True
            ):
                hits.append(f"{child}.enabled")
            hits.extend(_find_monitor_toggles(value, child))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(_find_monitor_toggles(value, f"{path}[{i}]"))
    return hits


class OperatorError(RuntimeError):
    """A broken invocation — exit 2, never exit 1 (which means "a scrape is blocked")."""


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


def analyze(
    docs: list[dict], observability_ns: str = DEFAULT_OBSERVABILITY_NS
) -> tuple[dict[str, list[str]], set[str], set[str]]:
    """-> (scraped namespace -> reasons, ingress-restricted namespaces, allowed)."""
    scraped: dict[str, list[str]] = {}
    restricted: set[str] = set()
    allowed: set[str] = set()

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        ns = meta.get("namespace") or ""
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind in MONITOR_KINDS:
            selector = spec.get("namespaceSelector") or {}
            match_names = selector.get("matchNames") or []
            if match_names:
                targets = [str(n) for n in match_names]
            elif selector.get("any"):
                # Cluster-wide discovery: which namespaces actually hold a
                # matching Service is unknowable from the corpus, so it is not
                # attributable to any one namespace.
                continue
            else:
                # No namespaceSelector means the monitor's own namespace.
                targets = [ns]
            for target in targets:
                scraped.setdefault(target, []).append(f"{kind} {ns}/{name}")

        elif kind == "HelmRelease":
            target = spec.get("targetNamespace") or ns
            for hit in _find_monitor_toggles(spec.get("values") or {}, "values"):
                scraped.setdefault(target, []).append(
                    f"HelmRelease {ns}/{name} ({hit}: true)"
                )

        elif kind == "NetworkPolicy":
            if "Ingress" not in _policy_types(spec):
                continue
            pod_selector = spec.get("podSelector")
            namespace_wide = not pod_selector  # {} or absent selects every pod
            if namespace_wide:
                restricted.add(ns)
            for rule in spec.get("ingress") or []:
                peers = rule.get("from")
                if not peers and namespace_wide:
                    # Omitted `from` and empty `from: []` both mean "every
                    # source" in the API, so the scrape gets through.
                    allowed.add(ns)
                    continue
                for peer in peers or []:
                    if _selects_observability(peer, observability_ns):
                        allowed.add(ns)

    return scraped, restricted, allowed


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
        description="Scraped namespaces must admit Prometheus through their NetworkPolicies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--observability-namespace",
        default=DEFAULT_OBSERVABILITY_NS,
        help="namespace Prometheus scrapes from (default: %(default)s)",
    )
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        metavar="NS=REASON",
        help="namespace exempt from the invariant, with its reason (repeatable)",
    )
    args = parser.parse_args(argv)
    observability_ns = args.observability_namespace
    try:
        exempt = _parse_exempt(args.exempt)
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

    scraped, restricted, allowed = analyze(docs, observability_ns)

    violations: list[str] = []
    checked = 0
    for ns in sorted(scraped):
        if ns not in restricted or ns in exempt:
            continue
        checked += 1
        if ns in allowed:
            continue
        reasons = ", ".join(sorted(set(scraped[ns])))
        violations.append(
            f"  {ns}: scraped ({reasons}) and ingress-restricted, but no "
            f"NetworkPolicy admits the {observability_ns} namespace — the scrape "
            f"is REJECTed at the CNI and TargetDown fires. Add an "
            f"`allow-metrics-ingress` policy (namespaceSelector "
            f"{NS_NAME_LABEL}: {observability_ns}) on the monitored port."
        )

    if violations:
        print(
            "Scrape/NetworkPolicy invariant violated — monitored namespaces that "
            "block Prometheus:",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
        return 1

    if not scraped:
        # A non-empty corpus holding no scrape target is the wiring failure an
        # empty one cannot be: the render loop produced documents but never
        # reached the stage that defines the monitors, so every namespace here
        # went unexamined. Same arm as check-secretstore-scope.py's store-less
        # corpus.
        print(
            f"ERROR: inspected 0 scrape targets in {len(docs)} document(s) — a gate "
            "that checks nothing is not a gate. Check that the `kustomize build` "
            "paths feeding stdin cover the stage that defines the "
            "ServiceMonitors/PodMonitors.",
            file=sys.stderr,
        )
        return 2

    print(
        f"Scrape/NetworkPolicy invariant OK ({checked} scraped ingress-restricted "
        f"namespaces checked, {len(scraped)} scraped namespaces seen in "
        f"{len(docs)} document(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
