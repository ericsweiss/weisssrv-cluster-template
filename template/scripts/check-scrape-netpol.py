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
import ipaddress
import re
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

    (`ipBlock` peers are judged by the CALLER across the whole rule: a single
    `/0` covers one address family only, and the corpus cannot establish the
    scraper's family — crediting it alone would be the silent false-allow
    this gate's bias forbids. A rule whose peers cover BOTH families is the
    explicit spelling of the omitted-`from` rule and is credited there.)
    """
    # Peer-level keys first: a typo like `podSelecter:` leaves the recognised
    # fields absent-or-empty, and the shortcut below would credit a peer
    # server-side apply rejects.
    if not isinstance(peer, dict) or set(peer) - {"ipBlock", "namespaceSelector", "podSelector"}:
        return False
    # A peer combining ipBlock with a selector is API-invalid — it must not
    # be credited through the selector path either.
    if peer.get("ipBlock") is not None:
        return False
    nssel = peer.get("namespaceSelector")
    if nssel is None or not isinstance(nssel, dict):
        return False
    # Unknown keys never credit: a `matchLables:` typo empties the recognised
    # terms and would otherwise ride the empty-selector shortcut.
    if set(nssel) - {"matchLabels", "matchExpressions"}:
        return False
    labels = nssel.get("matchLabels")
    exprs = nssel.get("matchExpressions")
    # Typed before walked: wrong-typed terms belong to a policy the API
    # rejects, which proves nothing about the scraper.
    if labels is not None and not isinstance(labels, dict):
        return False
    if exprs is not None and not isinstance(exprs, list):
        return False
    labels = labels or {}
    exprs = exprs or []
    # An EMPTY namespaceSelector needs no corpus knowledge to prove: `{}` (and
    # the equivalent `{matchLabels: {}}`) matches every namespace by API
    # semantics, observability included. But the API ANDs a peer's TWO
    # selectors, so the shortcut only holds while the peer's podSelector also
    # narrows nothing — `{namespaceSelector: {}, podSelector: {matchLabels:
    # {app: other}}}` is an unrelated cross-namespace allow, not proof the
    # scraper's pods are admitted. (A NAMED-namespace peer with a podSelector
    # is different: an allow that names observability restricts to the pods
    # its author means to admit, and whether the scraper carries those labels
    # is unknowable from the corpus — refusing to credit it would false-block
    # every tight real-world allow, so the label walk below keeps crediting
    # those.)
    if not labels and not exprs:
        return _selects_all_pods(peer.get("podSelector"))
    name_matched = labels.get(NS_NAME_LABEL) == observability_ns
    extra_requirements = any(k != NS_NAME_LABEL for k in labels)
    for expr in exprs:
        # The credited requirement must be fully valid: known fields only,
        # and `values` a real list — a STRING would do substring membership
        # and credit an expression the API rejects.
        if not isinstance(expr, dict) or set(expr) - {"key", "operator", "values"}:
            return False
        values = expr.get("values")
        if (
            expr.get("key") == NS_NAME_LABEL
            and expr.get("operator") == "In"
            and isinstance(values, list)
            and observability_ns in values
        ):
            name_matched = True
        else:
            extra_requirements = True
    return name_matched and not extra_requirements


# The apiserver's own label validation (validation.IsQualifiedName /
# IsValidLabelValue): an optional DNS-subdomain prefix, then a 63-char
# alphanumeric-bounded name; values are 63-char alphanumeric-bounded or empty.
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def _label_key_is_valid(key: object) -> bool:
    if not isinstance(key, str) or not key:
        return False
    prefix, slash, name = key.rpartition("/")
    if slash and (not prefix or len(prefix) > 253 or not _LABEL_PREFIX.fullmatch(prefix)):
        return False
    return bool(_LABEL_NAME.fullmatch(name))


def _label_value_is_valid(value: object) -> bool:
    return isinstance(value, str) and (value == "" or bool(_LABEL_NAME.fullmatch(value)))


def _label_selector_is_api_valid(selector: object) -> bool:
    """Structural validity of a LabelSelector — the COMPLETE check the
    apiserver applies, so the atomicity rule below cannot be dodged at any
    level: known keys, typed terms, syntactically valid label keys and
    values, known operators, and the operator's values-cardinality rules
    (In/NotIn require non-empty values; Exists/DoesNotExist forbid them).
    Absent (None) is valid; the API rejects everything else malformed."""
    if selector is None:
        return True
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        return False
    labels = selector.get("matchLabels")
    if labels is not None:
        if not isinstance(labels, dict):
            return False
        if not all(_label_key_is_valid(k) and _label_value_is_valid(v) for k, v in labels.items()):
            return False
    exprs = selector.get("matchExpressions")
    if exprs is not None:
        if not isinstance(exprs, list):
            return False
        for expr in exprs:
            if not isinstance(expr, dict) or set(expr) - {"key", "operator", "values"}:
                return False
            if not _label_key_is_valid(expr.get("key")):
                return False
            operator = expr.get("operator")
            values = expr.get("values")
            if operator in ("In", "NotIn"):
                if not (
                    isinstance(values, list)
                    and values
                    and all(_label_value_is_valid(v) for v in values)
                ):
                    return False
            elif operator in ("Exists", "DoesNotExist"):
                if values not in (None, []):
                    return False
            else:
                return False
    return True


def _rule_ipblocks_cover_both_families(peers: list) -> bool:
    """True when the rule's unexcepted zero-prefix ipBlock peers span IPv4 AND
    IPv6 — together they admit every address, the scraper's included,
    whatever family it speaks. One family alone proves nothing about a
    scraper whose family the corpus does not carry.
    """
    families: set[int] = set()
    for peer in peers or []:
        # ATOMICITY: any invalid shape anywhere in the rule rejects the WHOLE
        # policy at the API, so it disqualifies the credit outright — only a
        # peer that is VALID but simply not contributing (a selector peer, a
        # narrowing block) is skipped.
        if not isinstance(peer, dict) or set(peer) - {"ipBlock", "namespaceSelector", "podSelector"}:
            return False
        ip_block = peer.get("ipBlock")
        if ip_block is None:
            # A selector peer contributes nothing here — but only a VALID one
            # may be skipped: a malformed selector rejects the whole policy,
            # and skipping it would let sibling /0 peers credit a rule that
            # never applies.
            if not _label_selector_is_api_valid(
                peer.get("namespaceSelector")
            ) or not _label_selector_is_api_valid(peer.get("podSelector")):
                return False
            continue
        if (
            not isinstance(ip_block, dict)
            or set(ip_block) - {"cidr", "except"}
            or peer.get("namespaceSelector") is not None
            or peer.get("podSelector") is not None
        ):
            # Wrong type, unknown keys (`exept:`), or the ipBlock+selector
            # combination — all API-invalid: poison, not skip.
            return False
        excepts = ip_block.get("except")
        if excepts is not None and not isinstance(excepts, list):
            # `except: {}` and friends are invalid shapes, not narrowing.
            return False
        # A real parse, not a `:` sniff: crediting is the fail-OPEN direction
        # here, so only a cidr the API itself would accept may count —
        # `garbage/0` must not pass as IPv4.
        try:
            net = ipaddress.ip_network(str(ip_block.get("cidr") or "").strip(), strict=False)
        except ValueError:
            return False
        if excepts:
            # A valid, non-empty except list narrows — skip, don't poison.
            continue
        if net.prefixlen == 0:
            families.add(net.version)
    return {4, 6} <= families


def _policy_types(spec: dict) -> set[str]:
    """policyTypes, applying the API default (inferred from which rules exist)."""
    declared = spec.get("policyTypes")
    if declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def _selects_all_pods(selector: object) -> bool:
    """True when a podSelector selects every pod in the namespace.

    Absent and `{}` are the API's spellings of "all pods", but so are
    `{matchLabels: {}}` and `{matchExpressions: []}` — an empty selector term
    matches everything. The truthiness test this replaces read those
    namespace-wide policies as app-scoped, so a default-deny spelled that way
    never registered as restricting the namespace.
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
    # API-invalid, and crediting an invalid policy is the silent false-allow
    # this gate's bias forbids.
    if labels is not None and not isinstance(labels, dict):
        return False
    if exprs is not None and not isinstance(exprs, list):
        return False
    return not (labels or exprs)


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
            selector = spec.get("namespaceSelector")
            selector = selector if isinstance(selector, dict) else {}
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
            # By SELECTION, not truthiness: `{matchLabels: {}}` is namespace-wide.
            # spec.podSelector is a REQUIRED field — an absent one is not the
            # all-pods default but a policy the API rejects.
            namespace_wide = isinstance(pod_selector, dict) and _selects_all_pods(pod_selector)
            if namespace_wide:
                restricted.add(ns)
            for rule in spec.get("ingress") or []:
                # Rule-level keys too: `form:` reads as an omitted `from` —
                # the allow-all spelling — on a rule the API rejects.
                if not isinstance(rule, dict) or set(rule) - {"from", "ports"}:
                    continue
                peers = rule.get("from")
                if not peers and namespace_wide:
                    # Omitted `from` and empty `from: []` both mean "every
                    # source" in the API, so the scrape gets through.
                    allowed.add(ns)
                    continue
                if _rule_ipblocks_cover_both_families(peers):
                    allowed.add(ns)
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
