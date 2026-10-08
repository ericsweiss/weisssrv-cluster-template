#!/usr/bin/env python3
"""Assert every scraped namespace admits Prometheus through its NetworkPolicies.

A `ports` rule is modelled by the sibling check-scrape-wiring.py, not here.
Exits 0 clean, 1 on a finding, 2 on an operator error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
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
        policy_types,
        selects_all_pods,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

MONITOR_KINDS = {"ServiceMonitor", "PodMonitor"}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
ENDPOINT_KEYS = {"ServiceMonitor": "endpoints", "PodMonitor": "podMetricsEndpoints"}
# Endpoint fields naming the scrape port, in the order prometheus-operator
# resolves them. `portNumber` is a PodMonitor-only spelling of a container port.
ENDPOINT_PORT_FIELDS = {
    "ServiceMonitor": ("port", "targetPort"),
    "PodMonitor": ("port", "portNumber", "targetPort"),
}
DEFAULT_OBSERVABILITY_NS = "observability"
NS_NAME_LABEL = "kubernetes.io/metadata.name"

# Policy shapes declined for want of modelling, per namespace. Printed with that
# namespace's failure: without them the message asserts no policy admits the
# scrape where the gate merely could not read one.
UNMODELLED: dict[str, list[str]] = {}


def note(namespace: str, message: str) -> None:
    """Record a shape this gate declined for want of modelling, not for cause."""
    entries = UNMODELLED.setdefault(namespace, [])
    if message not in entries:
        entries.append(message)


def _selects_observability(
    peer: dict,
    observability_ns: str,
    scraper_labels: dict | None = None,
    namespace: str = "",
    policy: str = "",
) -> bool:
    """True if a `from` peer provably matches the observability namespace.

    Only `kubernetes.io/metadata.name` is credited; ipBlock peers are judged by
    the caller. A shape the gate cannot read is declined with a note naming it.
    """

    def decline(reason: str) -> bool:
        if namespace:
            note(namespace, f"{policy}: {reason}")
        return False

    # Peer-level keys first: a typo like `podSelecter:` leaves the recognised
    # fields absent-or-empty, and the shortcut below would credit a peer
    # server-side apply rejects.
    if not isinstance(peer, dict) or set(peer) - {"ipBlock", "namespaceSelector", "podSelector"}:
        return decline("a `from` peer carries keys the API does not define")
    # A peer combining ipBlock with a selector is API-invalid — it must not
    # be credited through the selector path either.
    if peer.get("ipBlock") is not None:
        return decline(
            "its scrape peer is an ipBlock, credited only when the rule's unexcepted "
            "blocks span both address families; this gate resolves no CIDR against the "
            f"{observability_ns} pod range"
        )
    nssel = peer.get("namespaceSelector")
    if nssel is None:
        # A podSelector-only peer selects pods in the policy's own namespace.
        return False
    if not isinstance(nssel, dict):
        return decline("its scrape peer's namespaceSelector is not a mapping")
    # Unknown keys never credit: a `matchLables:` typo empties the recognised
    # terms and would otherwise ride the empty-selector shortcut.
    if set(nssel) - {"matchLabels", "matchExpressions"}:
        return decline("its scrape peer's namespaceSelector carries keys the API does not define")
    labels = nssel.get("matchLabels")
    exprs = nssel.get("matchExpressions")
    # Typed before walked: wrong-typed terms belong to a policy the API
    # rejects, which proves nothing about the scraper.
    if labels is not None and not isinstance(labels, dict):
        return decline("its scrape peer's namespaceSelector matchLabels is not a mapping")
    if exprs is not None and not isinstance(exprs, list):
        return decline("its scrape peer's namespaceSelector matchExpressions is not a list")
    labels = labels or {}
    exprs = exprs or []
    # An empty namespaceSelector matches every namespace, but the API ANDs the
    # peer's two selectors, so it credits only while the podSelector admits the
    # scraper as well.
    if not labels and not exprs:
        if _admits_scraper(peer.get("podSelector"), scraper_labels):
            return True
        return decline(
            "its scrape peer combines an empty namespaceSelector with a podSelector; "
            "declare the scraper pod labels with --prometheus-pod-label to have it judged"
        )
    name_matched = labels.get(NS_NAME_LABEL) == observability_ns
    extra_requirements = any(k != NS_NAME_LABEL for k in labels)
    for expr in exprs:
        # The credited requirement must be fully valid: known fields only,
        # and `values` a real list — a STRING would do substring membership
        # and credit an expression the API rejects.
        if not isinstance(expr, dict) or set(expr) - {"key", "operator", "values"}:
            return decline(
                "its scrape peer's namespaceSelector matchExpression is not an "
                "API-valid requirement"
            )
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
    if extra_requirements:
        return decline(
            f"its scrape peer selects the namespace by requirements beyond {NS_NAME_LABEL}, "
            "which this gate does not resolve to a namespace name"
        )
    if not name_matched:
        # The peer names some other namespace: read, and provably not the scraper's.
        return False
    # The peer names the namespace; its podSelector narrows which pods there may
    # connect, so only declared scraper labels can prove Prometheus is among them.
    if scraper_labels is None or _admits_scraper(peer.get("podSelector"), scraper_labels):
        return True
    return decline(
        f"its scrape peer scopes the {observability_ns} namespace with a podSelector that "
        "the declared --prometheus-pod-label values do not match"
    )


def _selector_matches(selector: object, labels: dict) -> bool:
    """Whether an API-valid LabelSelector matches one label set."""
    if not isinstance(selector, dict):
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key")
        operator = expr.get("operator")
        values = expr.get("values") or []
        present = key in labels
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and present and labels.get(key) in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def _admits_scraper(pod_selector: object, scraper_labels: dict | None) -> bool:
    """Whether a peer's podSelector lets the scraper pods through.

    An all-pods selector always does; a narrowing one only against the labels
    declared with --prometheus-pod-label.
    """
    if selects_all_pods(pod_selector):
        return True
    if scraper_labels is None or not _label_selector_is_api_valid(pod_selector):
        return False
    return _selector_matches(pod_selector, scraper_labels)


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
    """Structural validity of a LabelSelector, matching the apiserver: known
    keys and operators, typed terms, valid label keys/values, and the
    values-cardinality rules. Absent is valid."""
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
    """True when the rule's unexcepted zero-prefix ipBlock peers span IPv4 and
    IPv6. Together they admit every address whatever family the scraper
    speaks; one family alone proves nothing.
    """
    families: set[int] = set()
    for peer in peers or []:
        # ATOMICITY: an invalid shape anywhere in the rule rejects the whole
        # policy, so it disqualifies the credit; a valid non-contributing peer
        # is skipped.
        if not isinstance(peer, dict) or set(peer) - {"ipBlock", "namespaceSelector", "podSelector"}:
            return False
        ip_block = peer.get("ipBlock")
        if ip_block is None:
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
            # Wrong type, unknown keys (`exept:`) or the ipBlock+selector
            # combination: API-invalid, so poison rather than skip.
            return False
        excepts = ip_block.get("except")
        if excepts is not None and not isinstance(excepts, list):
            # `except: {}` and friends are invalid shapes, not narrowing.
            return False
        # A real parse, not a `:` sniff: only a cidr the API itself would
        # accept may credit, so `garbage/0` must not pass as IPv4.
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


def _modelled_match_labels(selector) -> dict | None:
    """A selector's matchLabels, or None when the gate cannot model the shape."""
    if selector is None:
        return {}
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        return None
    if selector.get("matchExpressions"):
        return None
    labels = selector.get("matchLabels") or {}
    return labels if isinstance(labels, dict) else None


def monitor_namespaces(spec: dict, own_ns: str) -> list[str] | None:
    """Namespaces a monitor selects, or None when the corpus cannot say."""
    selector = spec.get("namespaceSelector")
    selector = selector if isinstance(selector, dict) else {}
    match_names = selector.get("matchNames") or []
    if match_names:
        return [str(n) for n in match_names]
    if selector.get("any"):
        return None
    return [own_ns]


def _declared_ports(pod: dict) -> tuple[set[str], set[int]]:
    """A pod spec's named container ports, and every port number it declares."""
    names: set[str] = set()
    numbers: set[int] = set()
    for container in pod.get("containers") or []:
        if not isinstance(container, dict):
            continue
        for port in container.get("ports") or []:
            if not isinstance(port, dict):
                continue
            if isinstance(port.get("name"), str):
                names.add(port["name"])
            number = port.get("containerPort")
            if isinstance(number, int) and not isinstance(number, bool):
                numbers.add(number)
    return names, numbers


def _workload_index(docs: list[dict]) -> dict:
    """(namespace -> [(name, pod labels, port names, port numbers)]) per workload."""
    index: dict[str, list[tuple[str, dict, set[str], set[int]]]] = {}
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        template = (doc.get("spec") or {}).get("template")
        if not isinstance(template, dict):
            continue
        labels = (template.get("metadata") or {}).get("labels") or {}
        pod = template.get("spec") or {}
        if not isinstance(labels, dict) or not isinstance(pod, dict):
            continue
        names, numbers = _declared_ports(pod)
        index.setdefault(doc_namespace(doc), []).append(
            (str((doc.get("metadata") or {}).get("name", "?")), labels, names, numbers)
        )
    return index


def _service_index(docs: list[dict]) -> dict:
    """(namespace -> [(name, labels, pod selector, port name -> pod port)]) per Service.

    The pod port is the entry's `targetPort`, which defaults to its `port`.
    """
    index: dict[str, list[tuple[str, dict, dict, dict]]] = {}
    for doc in docs:
        if doc.get("kind") != "Service":
            continue
        meta = doc.get("metadata") or {}
        labels = meta.get("labels") or {}
        spec = doc.get("spec") or {}
        if not isinstance(labels, dict) or not isinstance(spec, dict):
            continue
        selector = spec.get("selector")
        ports: dict[str, object] = {}
        for port in spec.get("ports") or []:
            if not isinstance(port, dict) or not isinstance(port.get("name"), str):
                continue
            target = port.get("targetPort")
            ports[port["name"]] = port.get("port") if target is None else target
        index.setdefault(doc_namespace(doc), []).append((
            str(meta.get("name", "?")),
            labels,
            selector if isinstance(selector, dict) else {},
            ports,
        ))
    return index


def _selected(entries: list, labels: dict) -> list:
    """Entries whose own labels carry every label in `labels`."""
    return [entry for entry in entries if labels.items() <= entry[1].items()]


def _endpoint_port(endpoint: dict, kind: str) -> tuple[str | None, object]:
    """The field an endpoint resolves its scrape port from, and its value."""
    for field in ENDPOINT_PORT_FIELDS[kind]:
        value = endpoint.get(field)
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        return field, value
    return None, None


def _undeclared(value: object, names: set[str], numbers: set[int]) -> bool:
    """Whether a port spelling names nothing the selected objects declare."""
    return value not in (names if isinstance(value, str) else numbers)


def _port_message(
    monitor: str, field: str, value: object, subject: str, selector: dict,
    target: str, objects: list[str], declared: list,
) -> str:
    """One failure line: the spelling, what declares nothing, and the effect."""
    return (
        f"  {monitor}: {field} {value!r} is declared by no {subject} its selector "
        f"{selector or '{}'} matches in {target} ({', '.join(sorted(objects))} "
        f"declare {declared or 'no named port'}) — the scrape resolves zero "
        f"targets, so no `up` series appears and an `up == 0` alert arm can never "
        f"fire. Needs an absent() arm as well as the renamed port."
    )


def _pods_declare(
    monitor: str, field: str, value: object, target: str, selector: dict,
    workloads: dict, subject: str = "workload",
) -> list[str]:
    """Findings for a pod-port spelling the workloads a selector picks lack.

    An empty selector picks every workload in the namespace, and no matched one
    means a chart renders it: neither proves anything about the port.
    """
    if not selector:
        return []
    matched = _selected(workloads.get(target, []), selector)
    if not matched:
        return []
    names: set[str] = set()
    numbers: set[int] = set()
    for entry in matched:
        names |= entry[2]
        numbers |= entry[3]
    if not _undeclared(value, names, numbers):
        return []
    declared = sorted(names) if isinstance(value, str) else sorted(numbers)
    return [_port_message(
        monitor, field, value, subject, selector, target,
        [entry[0] for entry in matched], declared,
    )]


def _service_hop(
    monitor: str, value: str, target: str, services: list, workloads: dict
) -> list[str]:
    """Findings for a Service port whose NAMED targetPort no pod declares.

    A numeric targetPort needs no container `ports:` entry, so only a name can
    fail to resolve; a selectorless Service has no pods in the corpus to check.
    """
    problems: list[str] = []
    for name, _, selector, ports in services:
        resolved = ports.get(value)
        if not isinstance(resolved, str) or not selector:
            continue
        problems += _pods_declare(
            monitor, f"port {value!r} -> Service {name} targetPort",
            resolved, target, selector, workloads,
        )
    return problems


def check_monitor_ports(docs: list[dict]) -> tuple[list[str], int]:
    """Every monitor endpoint must resolve to a port the objects it selects declare.

    A renamed port yields zero targets, so no `up` series appears and an
    `up == 0` alert arm has nothing to fire on.
    """
    workloads = _workload_index(docs)
    services = _service_index(docs)
    problems: list[str] = []
    checked = 0
    for doc in docs:
        kind = doc.get("kind")
        if kind not in MONITOR_KINDS:
            continue
        spec = doc.get("spec") or {}
        selector = _modelled_match_labels(spec.get("selector"))
        targets = monitor_namespaces(spec, doc_namespace(doc))
        if selector is None or targets is None:
            continue
        monitor = f"{kind} {doc_namespace(doc)}/{(doc.get('metadata') or {}).get('name', '?')}"
        for target in targets:
            for endpoint in spec.get(ENDPOINT_KEYS[kind]) or []:
                if not isinstance(endpoint, dict):
                    continue
                field, value = _endpoint_port(endpoint, kind)
                if field is None:
                    continue
                checked += 1
                if kind == "PodMonitor":
                    # A PodMonitor names a container port directly: no Service hop.
                    problems += _pods_declare(
                        monitor, field, value, target, selector, workloads
                    )
                    continue
                matched = _selected(services.get(target, []), selector)
                if not matched:
                    continue
                if field == "targetPort":
                    # A ServiceMonitor targetPort is matched against the pod's
                    # container ports, so it resolves past the Service.
                    for _, _, pods, _ in matched:
                        problems += _pods_declare(
                            monitor, field, value, target, pods, workloads
                        )
                    continue
                declared = set().union(*(set(entry[3]) for entry in matched))
                if _undeclared(value, declared, set()):
                    problems.append(_port_message(
                        monitor, field, value, "Service", selector, target,
                        [entry[0] for entry in matched], sorted(declared),
                    ))
                    continue
                problems += _service_hop(monitor, value, target, matched, workloads)
    return problems, checked


def analyze(
    docs: list[dict],
    observability_ns: str = DEFAULT_OBSERVABILITY_NS,
    scraper_labels: dict | None = None,
) -> tuple[dict[str, list[str]], set[str], set[str]]:
    """-> (scraped namespace -> reasons, ingress-restricted namespaces, allowed)."""
    scraped: dict[str, list[str]] = {}
    restricted: set[str] = set()
    allowed: set[str] = set()
    UNMODELLED.clear()

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        # The API's own defaulting: an omitted namespace IS `default`, not
        # "nowhere". Dropping such documents would exempt a whole namespace.
        ns = doc_namespace(doc)
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind in MONITOR_KINDS:
            # None is cluster-wide discovery: which namespaces hold a matching
            # Service is unknowable from the corpus, so it is attributable to
            # no single namespace.
            targets = monitor_namespaces(spec, ns)
            if targets is None:
                continue
            for target in targets:
                scraped.setdefault(target, []).append(f"{kind} {ns}/{name}")

        elif kind == "HelmRelease":
            target = spec.get("targetNamespace") or ns
            for hit in _find_monitor_toggles(spec.get("values") or {}, "values"):
                scraped.setdefault(target, []).append(
                    f"HelmRelease {ns}/{name} ({hit}: true)"
                )

        elif kind == "NetworkPolicy":
            if "Ingress" not in policy_types(spec):
                continue
            pod_selector = spec.get("podSelector")
            # By SELECTION, not truthiness: `{matchLabels: {}}` is namespace-wide.
            # spec.podSelector is a REQUIRED field — an absent one is not the
            # all-pods default but a policy the API rejects.
            namespace_wide = isinstance(pod_selector, dict) and selects_all_pods(pod_selector)
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
                    if _selects_observability(
                        peer, observability_ns, scraper_labels, ns, f"NetworkPolicy {ns}/{name}"
                    ):
                        allowed.add(ns)

    return scraped, restricted, allowed


def _scraper_labels(pairs: list[str]) -> dict | None:
    """`KEY=VALUE` scraper labels, or None when none were declared."""
    labels: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = str(pair).partition("=")
        if not separator or not key.strip():
            raise OperatorError(
                f"--prometheus-pod-label {pair!r} must be KEY=VALUE"
            )
        labels[key.strip()] = value.strip()
    return labels or None


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
    parser.add_argument(
        "--prometheus-pod-label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a label the scraper pods carry (repeatable). Declaring them lets a "
             "peer that narrows the observability namespace with a podSelector be "
             "judged; without them such a peer is credited unexamined",
    )
    args = parser.parse_args(argv)
    observability_ns = args.observability_namespace
    try:
        exempt = parse_exempt(args.exempt)
        scraper_labels = _scraper_labels(args.prometheus_pod_label)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    docs = load_corpus()

    scraped, restricted, allowed = analyze(docs, observability_ns, scraper_labels)
    port_violations, ports_checked = check_monitor_ports(docs)

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
        # A shape the gate declined for want of modelling may already admit the
        # scrape, so the operator is told which policy went unread.
        violations += [f"    not modelled: {entry}" for entry in UNMODELLED.get(ns, ())]

    if violations:
        print(
            "Scrape/NetworkPolicy invariant violated — monitored namespaces that "
            "block Prometheus:",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
    if port_violations:
        print(
            "Scrape port invariant violated — monitors whose endpoint port is "
            "declared nowhere:",
            file=sys.stderr,
        )
        print("\n".join(port_violations), file=sys.stderr)
    if violations or port_violations:
        return 1

    if not scraped:
        # Documents rendered but no scrape target found: the render never
        # reached the stage defining the monitors, so nothing was examined.
        print(
            f"ERROR: inspected 0 scrape targets in {len(docs)} document(s) — a gate "
            "that checks nothing is not a gate. Check that the `kustomize build` "
            "paths feeding stdin cover the stage that defines the "
            "ServiceMonitors/PodMonitors.",
            file=sys.stderr,
        )
        return 2

    unused = sorted(ns for ns in exempt if ns not in scraped or ns not in restricted)
    if unused:
        print(f"(exemptions declared but not exercised by this corpus: {', '.join(unused)})")
    print(
        f"Scrape/NetworkPolicy invariant OK ({checked} scraped ingress-restricted "
        f"namespaces checked, {len(scraped)} scraped namespaces seen, "
        f"{ports_checked} monitor endpoint port(s) examined in "
        f"{len(docs)} document(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
