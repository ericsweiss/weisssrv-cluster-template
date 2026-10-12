#!/usr/bin/env python3
"""Assert the scrape NetworkPolicy admits Prometheus on the port the monitor scrapes.

Port granularity (check-scrape-netpol.py is the namespace half), over a single
tree or a rendered cluster `--corpus`. Shapes and exit codes: docs/SCRIPTS.md.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import NamedTuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_OBSERVABILITY_NS = "observability"
DEFAULT_TREE = "kubernetes/flux"
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
ENDPOINT_KEYS = {"PodMonitor": "podMetricsEndpoints", "ServiceMonitor": "endpoints"}

# kustomize's own documents are build inputs, not resources, and share a kind
# name with the namespaced Flux CR, so they are recognised by API group.
KUSTOMIZE_GROUP = "kustomize.config.k8s.io"

# A Flux postBuild placeholder, the one scalar that may legally stand where this
# gate reads a list of addresses or ports.
SUBSTITUTION = re.compile(r"\$\{[^{}]+\}")

# Kinds that exist outside any namespace. They carry none by definition, so they
# are not grouped and cannot make a tree's namespaces ambiguous.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "APIService",
        "ClusterExternalSecret",
        "ClusterIssuer",
        "ClusterRole",
        "ClusterRoleBinding",
        "ClusterSecretStore",
        "CSIDriver",
        "CustomResourceDefinition",
        "IngressClass",
        "MutatingWebhookConfiguration",
        "Namespace",
        "Node",
        "PersistentVolume",
        "PriorityClass",
        "ProxyClass",
        "RuntimeClass",
        "StorageClass",
        "ValidatingAdmissionPolicy",
        "ValidatingWebhookConfiguration",
        "VolumeSnapshotClass",
    }
)

# Policy shapes this gate only partly models, named in the failure message.
UNMODELLED: list[str] = []


class GateError(Exception):
    """An operator error: the gate could not run, so it proves nothing."""


class Violation(Exception):
    """The scrape wiring in this repo is wrong."""


class Workload(NamedTuple):
    """One selected workload: its pod labels and the container ports it declares.

    A port name is unique within a pod, so each name here holds one number.
    """

    labels: dict
    names: dict[str, int]
    numbers: set[int]
    subject: str


class Target(NamedTuple):
    """One scrape target: the pods reached, the port number, and its spellings."""

    namespace: str | None
    labels: dict
    number: int
    spellings: set[int | str]
    subject: str


def note(message: str) -> None:
    if message not in UNMODELLED:
        UNMODELLED.append(message)


def describe(document: dict) -> str:
    """The document as a failure message names it."""
    metadata = document.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return f"{document.get('kind')} {name or '<unnamed>'}"


def located(parts: list[str]) -> str:
    """The bracketed `(Service x, Deployment y)` a failure message carries."""
    return f" ({', '.join(parts)})" if parts else ""


def mapping(value: object, subject: str, field: str) -> dict:
    """A field the gate reads as a mapping; a scalar or list there cannot be checked."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise GateError(f"{subject}: {field} is not a mapping")
    return value


def load_stream(handle, source: str) -> tuple[list[dict], list[str]]:
    """Every kinded document in one stream, else why it would not read."""
    try:
        loaded = list(yaml.safe_load_all(handle))
    except (OSError, UnicodeDecodeError) as error:
        return [], [f"{source}: unreadable: {error}"]
    except yaml.YAMLError as error:
        return [], [f"{source}: unparseable YAML: {error}"]
    return [d for d in loaded if isinstance(d, dict) and d.get("kind")], []


def load_documents(base: pathlib.Path) -> tuple[list[dict], list[str]]:
    """Every kinded document under the directory, and the files that would not read.

    kustomize accepts a JSON resource, so JSON is read as well as YAML.
    """
    documents: list[dict] = []
    errors: list[str] = []
    for extension in ("*.yaml", "*.yml", "*.json"):
        for path in sorted(base.rglob(extension)):
            # The handle, not the text: PyYAML names the stream in its mark,
            # so a parse error points at the file, not at "<unicode string>".
            try:
                with path.open(encoding="utf-8") as handle:
                    loaded, problems = load_stream(handle, str(path))
            except OSError as error:
                loaded, problems = [], [f"{path}: unreadable: {error}"]
            documents += loaded
            errors += problems
    return documents, errors


def load_corpus(target: str) -> tuple[list[dict], list[str]]:
    """A rendered corpus: a multi-document stream, '-' for stdin, or a directory."""
    if target == "-":
        return load_stream(sys.stdin, "<stdin>")
    path = pathlib.Path(target)
    if path.is_dir():
        return load_documents(path)
    try:
        with path.open(encoding="utf-8") as handle:
            return load_stream(handle, str(path))
    except OSError as error:
        return [], [f"{path}: unreadable: {error}"]


def selected_workloads(documents: list[dict], pods: dict) -> list[Workload]:
    """Every workload whose pod labels carry `pods`, one record each.

    Per workload, not merged: pods behind one Service may declare the same port
    name at different numbers, and a policy matches pod labels, not a selector.
    """
    selected: list[Workload] = []
    for document in documents:
        if document.get("kind") not in WORKLOAD_KINDS:
            continue
        subject = describe(document)
        spec = mapping(document.get("spec"), subject, "spec")
        template = mapping(spec.get("template"), subject, "spec.template")
        labels = mapping(
            mapping(template.get("metadata"), subject, "spec.template.metadata").get("labels"),
            subject,
            "spec.template.metadata.labels",
        )
        if not pods.items() <= labels.items():
            continue
        pod = mapping(template.get("spec"), subject, "spec.template.spec")
        names: dict[str, int] = {}
        numbers: set[int] = set()
        for container in pod.get("containers") or []:
            for port in container.get("ports") or []:
                number = port.get("containerPort")
                if not number:
                    continue
                numbers.add(number)
                if port.get("name"):
                    names[port["name"]] = number
        selected.append(Workload(dict(labels), names, numbers, subject))
    return selected


def spellings(number: int, names: dict[str, int]) -> set[int | str]:
    """A port as a NetworkPolicy may legally name it: the number and its names."""
    return {number} | {name for name, value in names.items() if value == number}


def resolve_target(
    documents: list[dict],
    namespace: str | None,
    pods: dict,
    port: int | str,
    name: str,
    where: list[str],
) -> list[Target]:
    """One target per selected workload that DECLARES the port, by name or number.

    A numeric `targetPort` needs no containerPort for the Service to be valid,
    but a scrape is credited only where a selected workload declares the number.
    """
    workloads = selected_workloads(documents, pods)
    if port is None:
        raise Violation(
            f"{name}{located(where)}: a Service port names no pod port, so nothing "
            "is scraped"
        )
    if isinstance(port, str):
        if not any(port in workload.names for workload in workloads):
            raise Violation(
                f"{name}{located(where)}: no workload carrying {pods} declares a port "
                f"named {port!r}"
            )
    elif not any(port in workload.numbers for workload in workloads):
        raise Violation(
            f"{name}{located(where)}: no workload carrying {pods} declares a port {port}"
        )
    targets = []
    for workload in workloads:
        number = workload.names.get(port) if isinstance(port, str) else port
        # A name resolves to the number this workload gives it; a number is a
        # port of these pods only where this workload declares it as well.
        if number not in workload.numbers:
            continue
        targets.append(
            Target(
                namespace,
                workload.labels,
                number,
                spellings(number, workload.names),
                located([*where, workload.subject]),
            )
        )
    return targets


def services_for(documents: list[dict], selector: dict) -> list[dict]:
    """Every Service carrying the monitor's labels.

    Matched on labels only; whether one has a `spec.selector` the gate can
    follow to a workload is `pod_port`'s to refuse.
    """
    matched = []
    for document in documents:
        if document.get("kind") != "Service":
            continue
        metadata = mapping(document.get("metadata"), describe(document), "metadata")
        labels = mapping(metadata.get("labels"), describe(document), "metadata.labels")
        if selector.items() <= labels.items():
            matched.append(document)
    return matched


def endpoints_by_hand(documents: list[dict], service: str | None) -> list[dict]:
    """The objects declaring this Service's endpoints by hand.

    An EndpointSlice is tied to its Service by label and an Endpoints object by
    name; either way the addresses are the operator's, not a selector's.
    """
    if not service:
        return []
    found: list[dict] = []
    for document in documents:
        kind = document.get("kind")
        if kind not in ("Endpoints", "EndpointSlice"):
            continue
        metadata = mapping(document.get("metadata"), describe(document), "metadata")
        if kind == "Endpoints":
            if metadata.get("name") == service:
                found.append(document)
            continue
        labels = mapping(metadata.get("labels"), describe(document), "metadata.labels")
        if labels.get("kubernetes.io/service-name") == service:
            found.append(document)
    return found


def _subsets(document: dict) -> list[tuple[int, list]]:
    """[(addresses, port entries)] for either kind; a slice is one subset.

    An Endpoints object pairs addresses with ports per subset, so a port in one
    subset says nothing about the addresses in another.
    """
    if document.get("kind") == "Endpoints":
        return [
            (len(subset.get("addresses") or []), subset.get("ports") or [])
            for subset in document.get("subsets") or []
            if isinstance(subset, dict)
        ]
    addresses = sum(
        len(entry.get("addresses") or [])
        for entry in document.get("endpoints") or []
        if isinstance(entry, dict)
    )
    return [(addresses, document.get("ports") or [])]


def _substituted(document: dict) -> bool:
    """Are this object's addresses or ports a substitution rather than a list?

    A `${...}` placeholder parses as a scalar, so what it holds is unknown here
    and the object is credited. Any other scalar is a shape nothing can read.
    """
    field = "subsets" if document.get("kind") == "Endpoints" else "endpoints"
    deferred = False
    for key in (field, "ports"):
        value = document.get(key)
        if value is None or isinstance(value, list):
            continue
        if isinstance(value, str) and SUBSTITUTION.fullmatch(value.strip()):
            note(f"{describe(document)}: its {key} is a substitution, not a list here")
            deferred = True
            continue
        raise GateError(
            f"{describe(document)}: {key} is {value!r}, which is neither a list nor a "
            "${...} substitution, so the addresses it holds cannot be read"
        )
    return deferred


def endpoint_serves(document: dict, named: object, target: object) -> bool:
    """Does this hand-written object hold an address on the scraped port?

    Prometheus keeps an endpoint target by the port's NAME, so a monitor's
    `port:` must be named here; a `targetPort:` matches the number instead.
    """
    if _substituted(document):
        return True
    for addresses, ports in _subsets(document):
        if not addresses:
            continue
        for port in ports:
            if not isinstance(port, dict):
                continue
            # A `port:` monitor keeps the target by name, whatever the number.
            if named is not None:
                if port.get("name") == named:
                    return True
                continue
            # Unnamed: an entry carrying no number is read as every port.
            if port.get("port") is None or target in (port.get("name"), port.get("port")):
                return True
    return False


def pod_port(
    documents: list[dict],
    namespace: str | None,
    where: list[str],
    selector: dict,
    endpoint: dict,
    kind: str,
    name: str,
) -> tuple[list[Target], int]:
    """The targets one endpoint creates in one namespace, and its hand-written count.

    prometheus-operator scrapes EVERY Service a ServiceMonitor selects, so a
    policy proven for one of them says nothing about the rest.
    """
    named = endpoint.get("port")
    target = endpoint.get("targetPort")
    # `portNumber` is a PodMonitor-only spelling of the container port.
    number = endpoint.get("portNumber") if kind == "PodMonitor" else None
    if named is None and target is None and number is None:
        if kind == "PodMonitor":
            raise GateError(
                f"{name}: a PodMonitor endpoint names none of `port`, `portNumber` "
                "or `targetPort`"
            )
        raise GateError(f"{name}: an endpoint names neither `port` nor `targetPort`")

    if kind == "PodMonitor":
        # A PodMonitor names a container port directly: there is no Service hop.
        direct = named if named is not None else (number if number is not None else target)
        return resolve_target(documents, namespace, selector, direct, name, where), 0

    # A ServiceMonitor's selector picks SERVICES, so the pods are the ones each
    # chosen Service selects, not whatever carries the monitor labels.
    services = services_for(documents, selector)
    if not services:
        raise Violation(
            f"{name}{located(where)}: no Service carries the monitor's labels {selector}: "
            "the scrape has no target"
        )
    targets: list[Target] = []
    by_hand = 0
    for service in services:
        service_name = (service.get("metadata") or {}).get("name")
        here = [*where, f"Service {service_name}"]
        service_spec = mapping(service.get("spec"), describe(service), "spec")
        pods = mapping(service_spec.get("selector"), describe(service), "spec.selector")
        if not pods:
            declared = endpoints_by_hand(documents, service_name)
            if declared:
                if not any(endpoint_serves(e, named, target) for e in declared):
                    raise Violation(
                        f"{name}{located(here)}: the Service declares no spec.selector and its "
                        f"hand-written endpoints hold no address on {named or target!r}: the "
                        "scrape has no target. Name the scraped port in the EndpointSlice and "
                        "give it an address."
                    )
                # Endpoints written here rather than derived from a selector: the
                # addresses are the operator's and no pod here serves the port.
                by_hand += 1
                continue
            raise Violation(
                f"{name}{located(here)}: the Service declares no spec.selector and nothing "
                "here declares its endpoints, so no workload serves the scraped port. "
                "Crediting a policy against it certifies nothing — give the Service a "
                "selector, or ship the EndpointSlice that names its addresses."
            )
        if named is None:
            # `targetPort` names a pod port directly, by name or by number.
            targets += resolve_target(documents, namespace, pods, target, name, here)
            continue
        for port in service_spec.get("ports") or []:
            if port.get("name") == named:
                # An absent or null targetPort defaults to the Service port.
                resolved = port.get("targetPort") or port.get("port")
                targets += resolve_target(documents, namespace, pods, resolved, name, here)
                break
    # A Service without the named port is no target, so only a monitor whose
    # every selected Service lacks it has nothing to scrape.
    if not targets and not by_hand:
        raise Violation(
            f"{name}{located(where)}: no Service carrying {selector} declares a port "
            f"named {named!r}"
        )
    return targets, by_hand


def scrape_allow(documents: list[dict], scrape_ns: str) -> str | None:
    """The name of a NetworkPolicy admitting the observability namespace, if any.

    It is the repo's own statement that something here is scraped, so it stands
    in for the answer that rendered the monitor.
    """
    for document in documents:
        if document.get("kind") != "NetworkPolicy":
            continue
        spec = mapping(document.get("spec"), describe(document), "spec")
        for rule in spec.get("ingress") or []:
            for peer in rule.get("from") or []:
                namespaces = peer.get("namespaceSelector") or {}
                labels = namespaces.get("matchLabels") or {}
                if labels.get("kubernetes.io/metadata.name") == scrape_ns:
                    return (document.get("metadata") or {}).get("name")
    return None


# Prometheus's own pod labels, declared by the caller (--prometheus-pod-label):
# a peer narrowing the observability namespace resolves against these.
SCRAPER_LABELS: dict[str, str] = {}


def admits(policy: dict, pods: dict, accepted: set[int | str], scrape_ns: str) -> bool:
    """Does this NetworkPolicy let the observability namespace in on the port?"""
    spec = mapping(policy.get("spec"), describe(policy), "spec")
    rules = spec.get("ingress") or []
    declared = spec.get("policyTypes")
    # The API infers Ingress from the presence of `ingress:` when policyTypes is
    # absent, and an empty list round-trips as absent, so it infers too.
    if not ("Ingress" in declared if declared else bool(rules)):
        return False
    selected = mapping(spec.get("podSelector"), describe(policy), "spec.podSelector")
    name = (policy.get("metadata") or {}).get("name")
    # An empty podSelector selects every pod; matchExpressions is not modelled,
    # so a policy spelled that way is not credited with admitting the scrape.
    if set(selected) - {"matchLabels"}:
        note(f"{name}: its podSelector uses matchExpressions, which this gate does not model")
        return False
    if not (selected.get("matchLabels") or {}).items() <= pods.items():
        return False
    for rule in rules:
        peers = rule.get("from") or []
        # An absent or empty `from` matches every source, observability included.
        scoped = not peers
        for peer in peers:
            namespaces = peer.get("namespaceSelector")
            if namespaces is None:
                # An ingress ipBlock covering the observability pod CIDR does admit
                # the scrape, but this gate resolves no CIDRs.
                if peer.get("ipBlock"):
                    note(
                        f"{name}: its scrape peer is an ipBlock, which this gate does not "
                        "resolve against the observability pod CIDR"
                    )
                continue
            labels = namespaces.get("matchLabels") or {}
            if labels.get("kubernetes.io/metadata.name") == scrape_ns:
                # Prometheus is chart-installed, so its pod labels are in no
                # manifest: a podSelector here resolves only against the labels
                # the caller declares, and any other narrowing is not credited.
                narrowing = [f"namespaceSelector.{t}" for t in sorted(set(namespaces) - {"matchLabels"})]
                narrowing += [
                    f"namespaceSelector.matchLabels.{k}"
                    for k in sorted(set(labels) - {"kubernetes.io/metadata.name"})
                ]
                pod_sel = peer.get("podSelector")
                if pod_sel is not None and not isinstance(pod_sel, dict):
                    narrowing.append("podSelector")
                elif isinstance(pod_sel, dict) and pod_sel:
                    wanted = pod_sel.get("matchLabels") or {}
                    if (
                        set(pod_sel) - {"matchLabels"}
                        or not isinstance(wanted, dict)
                        or not wanted
                    ):
                        narrowing.append("podSelector")
                    elif any(SCRAPER_LABELS.get(k) != v for k, v in wanted.items()):
                        note(
                            f"{name}: its peer for namespace {scrape_ns} selects pods by "
                            f"{wanted}, which the declared scraper labels do not carry "
                            f"({SCRAPER_LABELS or 'none declared'}), so the peer is not credited"
                        )
                        continue
                if narrowing:
                    note(
                        f"{name}: its peer for namespace {scrape_ns} narrows it with "
                        f"{', '.join(narrowing)}, which this gate cannot resolve against "
                        "Prometheus's chart-installed pods, so the peer is not credited"
                    )
                else:
                    scoped = True
            # An empty namespaceSelector selects every namespace, observability
            # included, but only a peer with no podSelector admits every pod.
            elif not namespaces:
                if peer.get("podSelector"):
                    note(
                        f"{name}: its scrape peer combines an empty namespaceSelector with "
                        "a podSelector, which this gate does not model"
                    )
                else:
                    scoped = True
            elif set(namespaces) - {"matchLabels"}:
                note(
                    f"{name}: its scrape peer selects the namespace with matchExpressions, "
                    "which this gate does not model"
                )
            elif "kubernetes.io/metadata.name" not in labels:
                note(
                    f"{name}: its scrape peer selects the namespace by labels other than "
                    "kubernetes.io/metadata.name, which this gate does not resolve to a name"
                )
        if not scoped:
            continue
        ports = rule.get("ports")
        # An absent or empty `ports` matches every port.
        if not ports:
            return True
        for entry in ports:
            if not isinstance(entry, dict):
                continue
            if (entry.get("protocol") or "TCP") != "TCP":
                continue
            # An entry with no `port` matches every port for the protocol.
            if "port" not in entry:
                return True
            low = entry.get("port")
            high = entry.get("endPort")
            if low in accepted:
                return True
            # `endPort` makes a numeric `port` the low end of a range.
            if isinstance(low, int) and isinstance(high, int) and any(
                isinstance(value, int) and low <= value <= high for value in accepted
            ):
                return True
    return False


def tree_namespace(documents: list[dict]) -> tuple[str | None, str | None]:
    """The namespace a Namespace manifest in the tree names, else why not.

    Derivation is only consulted when a monitor declares matchNames, so a tree
    that ships no Namespace (the operator owns it) is unaffected.
    """
    named = sorted(
        {
            (document.get("metadata") or {}).get("name")
            for document in documents
            if document.get("kind") == "Namespace"
        }
        - {None, ""}
    )
    if len(named) == 1:
        return named[0], None
    if not named:
        return None, (
            "without a namespace: --namespace-from-tree found no named Namespace "
            "manifest here, so ship the Namespace this tree deploys into, or pass "
            "--namespace."
        )
    return None, (
        f"without a namespace: --namespace-from-tree found {len(named)} of them here "
        f"({', '.join(named)}), and the policies here cover one namespace only. Split "
        "the tree, or pass --namespace."
    )


def unnamespaced(document: dict) -> bool:
    """Can this document carry no namespace at all?"""
    group = str(document.get("apiVersion") or "").split("/", 1)[0]
    return group == KUSTOMIZE_GROUP or document.get("kind") in CLUSTER_SCOPED_KINDS


def effective_namespace(document: dict, default: str | None) -> str | None:
    """The namespace a document lands in: its own, else --namespace."""
    metadata = document.get("metadata")
    own = metadata.get("namespace") if isinstance(metadata, dict) else None
    return str(own) if own else default


def group_by_namespace(
    documents: list[dict], namespace: str | None
) -> dict[str | None, list[dict]]:
    """Namespaced documents keyed by effective namespace: their own, else the tree's.

    A policy admits only its own namespace's pods, so each group is judged
    alone; a tree stating ONE namespace lends it to the documents naming none.
    """
    # A document that can hold no namespace is neither monitor nor policy, and
    # reading its absent namespace as "unassigned" refuses a valid tree.
    namespaced = [d for d in documents if not unnamespaced(d)]
    stated = {effective_namespace(d, None) for d in namespaced} - {None}
    default = namespace
    if default is None and len(stated) == 1:
        default = next(iter(stated))
    groups: dict[str | None, list[dict]] = {}
    for document in namespaced:
        groups.setdefault(effective_namespace(document, default), []).append(document)
    return groups


def monitor_scope(
    monitor: dict,
    group_ns: str | None,
    groups: dict[str | None, list[dict]],
    corpus: bool,
    namespace: str | None,
    namespace_error: str | None,
) -> list[str | None]:
    """The namespaces whose pods this monitor scrapes.

    A tree covers one namespace, so a wider scope is refused there; a corpus
    carries them all, so `matchNames` and `any` resolve against it.
    """
    name = (monitor.get("metadata") or {}).get("name")
    spec = mapping(monitor.get("spec"), describe(monitor), "spec")
    scope = mapping(spec.get("namespaceSelector"), describe(monitor), "spec.namespaceSelector")
    names = scope.get("matchNames") or []
    if corpus:
        unknown = sorted(set(scope) - {"any", "matchNames"})
        if unknown:
            raise GateError(
                f"{name}: spec.namespaceSelector carries {', '.join(unknown)}, which is "
                "not part of a monitor's namespace selector, so the scrape's scope "
                "cannot be resolved."
            )
        if scope.get("any"):
            return sorted(groups, key=lambda ns: ns or "")
        if not names:
            return [group_ns]
        missing = [entry for entry in names if entry not in groups]
        if missing:
            raise GateError(
                f"{name}: spec.namespaceSelector.matchNames names {', '.join(missing)}, "
                "which this corpus holds no object in: either the name is stale or the "
                "render is partial, and a namespace absent from the corpus cannot be "
                "checked."
            )
        return list(names)
    # A matchNames entry must equal the namespace this group is judged in — the
    # documents' own, --namespace, or the tree's Namespace — else it is refused;
    # an unverified name would certify another namespace.
    if set(scope) - {"any", "matchNames"} or scope.get("any") or len(names) > 1:
        raise GateError(
            f"{name}: spec.namespaceSelector scopes the scrape outside this directory, "
            "whose NetworkPolicies cover one namespace only. Leave it out, scope it "
            "to this namespace with `any: false` or `matchNames: [<namespace>]` and "
            "`--namespace`, or judge the whole cluster with `--corpus`."
        )
    if names and names != [group_ns]:
        if namespace:
            detail = (f"against --namespace {namespace}: the policies here cover "
                      "that namespace only.")
        else:
            detail = namespace_error or (
                "without --namespace: pass the namespace this tree deploys into.")
        raise GateError(
            f"{name}: spec.namespaceSelector.matchNames {names} cannot be verified "
            + detail
        )
    return [group_ns]


def check_monitor(
    monitor: dict,
    group_ns: str | None,
    namespaces: list[str | None],
    groups: dict[str | None, list[dict]],
    policies: dict[str | None, list[dict]],
    scrape_ns: str,
    corpus: bool = False,
) -> tuple[int, int]:
    """One monitor's endpoints against the policies where its pods live.

    Returns the count of hand-written-endpoint targets and of endpoints whose
    pods a chart renders: no pod here serves either, so neither has a policy.
    """
    kind = monitor["kind"]
    name = (monitor.get("metadata") or {}).get("name")
    spec = mapping(monitor.get("spec"), describe(monitor), "spec")
    chooses = mapping(spec.get("selector"), describe(monitor), "spec.selector")
    # Discarding a term that narrows the selection judges objects the monitor
    # does not select, and leaves the ones it does select unresolved.
    unsupported = sorted(set(chooses) - {"matchLabels"})
    if unsupported:
        raise GateError(
            f"{name}: spec.selector carries {', '.join(unsupported)}, which this gate "
            "does not model, so the objects the monitor selects cannot be resolved"
        )
    selector = mapping(
        chooses.get("matchLabels"), describe(monitor), "spec.selector.matchLabels"
    )
    if not selector:
        raise GateError(
            f"{name}: the monitor selects every pod, so no policy can be matched to it"
        )
    endpoints = spec.get(ENDPOINT_KEYS[kind]) or []
    if not endpoints:
        raise GateError(f"{name}: the monitor declares no endpoints, so nothing is checked")
    hand_written = 0
    chart_rendered = 0
    for endpoint in endpoints:
        targets: list[Target] = []
        by_hand = 0
        unrendered = 0
        problems: list[str] = []
        for target_ns in namespaces:
            members = groups.get(target_ns) or []
            where = [] if target_ns == group_ns else [f"namespace {target_ns}"]
            try:
                found, counted = pod_port(
                    members, target_ns, where, selector, endpoint, kind, name
                )
            except Violation as error:
                chosen = (
                    selected_workloads(members, selector)
                    if kind == "PodMonitor"
                    else services_for(members, selector)
                )
                # A chart's workloads reach no manifest, so a namespace the
                # corpus carries without one states nothing about these pods;
                # an object it does carry for the monitor is judged as written.
                if (
                    corpus
                    and not any(d.get("kind") in WORKLOAD_KINDS for d in members)
                    and not chosen
                ):
                    unrendered += 1
                    continue
                # One namespace: its answer is the monitor's. Several: only a
                # namespace holding an object the monitor selects speaks for it,
                # and a target elsewhere does not answer for this one.
                if len(namespaces) == 1 or chosen:
                    raise
                problems.append(str(error))
                continue
            targets += found
            by_hand += counted
        if not targets and not by_hand:
            # Only when NO namespace in scope renders a workload: one that does
            # is a namespace whose answer the monitor has to live with.
            if unrendered == len(namespaces):
                chart_rendered += 1
                continue
            raise Violation("\n".join(problems))
        hand_written += by_hand
        for target in targets:
            if any(
                admits(policy, target.labels, target.spellings, scrape_ns)
                for policy in policies.get(target.namespace) or []
            ):
                continue
            message = (
                f"{name}{target.subject}: no NetworkPolicy admits namespace {scrape_ns} "
                f"on port {target.number}, so Prometheus cannot reach this target"
            )
            raise Violation("\n".join([message, *UNMODELLED]))
    return hand_written, chart_rendered


def refuse_vacuous(
    groups: dict[str | None, list[dict]], targeted: set[str | None], scrape_ns: str
) -> None:
    """A namespace that declares a scrape while no monitor here targets it."""
    for group_ns in sorted(groups, key=lambda ns: ns or ""):
        if group_ns in targeted:
            continue
        declared = scrape_allow(groups[group_ns], scrape_ns)
        if declared:
            raise GateError(
                f"{declared} admits namespace {scrape_ns} but no ServiceMonitor "
                "or PodMonitor scrapes its namespace: a gate that checks nothing is not "
                "a gate. Restore the monitor, or remove the scrape allow with it."
            )


def check(
    documents: list[dict],
    scrape_ns: str = DEFAULT_OBSERVABILITY_NS,
    namespace: str | None = None,
    namespace_error: str | None = None,
    corpus: bool = False,
) -> int:
    """Every monitor's scraped port, against the policies where its pods live."""
    UNMODELLED.clear()
    groups = group_by_namespace(documents, namespace)
    stated = sorted(ns for ns in groups if ns is not None)
    if namespace and [ns for ns in stated if ns != namespace]:
        raise GateError(
            f"--namespace {namespace} disagrees with the namespaces the tree states "
            f"({', '.join(stated)}): a document in another namespace cannot be judged "
            "against it. Point the gate at one namespace's manifests, or drop "
            "--namespace and let each document's own namespace stand."
        )
    if None in groups and len(groups) > 1:
        unassigned = [describe(d) for d in groups[None]]
        raise GateError(
            f"the tree states namespaces {', '.join(stated)} but names none for "
            f"{', '.join(unassigned)}: a document cannot be grouped by guess. Give "
            "each one a namespace, pass --namespace, or split the tree."
        )
    monitors = [
        (group_ns, document)
        for group_ns in sorted(groups, key=lambda ns: ns or "")
        for document in groups[group_ns]
        if document.get("kind") in ENDPOINT_KEYS
    ]
    if not monitors:
        refuse_vacuous(groups, set(), scrape_ns)
        print(
            f"{len(documents)} document(s), no ServiceMonitor or PodMonitor; "
            "nothing to check"
        )
        return 0
    policies = {
        group_ns: [d for d in members if d.get("kind") == "NetworkPolicy"]
        for group_ns, members in groups.items()
    }
    targeted: set[str | None] = set()
    hand_written = 0
    chart_rendered = 0
    for group_ns, monitor in monitors:
        namespaces = monitor_scope(
            monitor, group_ns, groups, corpus, namespace, namespace_error
        )
        targeted |= set(namespaces)
        counted, charted = check_monitor(
            monitor, group_ns, namespaces, groups, policies, scrape_ns, corpus
        )
        hand_written += counted
        chart_rendered += charted
    # Corpus-wide only: a chart renders monitors a corpus does not carry, so a
    # single namespace's allow cannot be paired with one the way a tree's is.
    if not corpus:
        refuse_vacuous(groups, targeted, scrape_ns)
    tail = f"; {hand_written} target(s) have hand-written endpoints" if hand_written else ""
    if chart_rendered:
        tail += f"; {chart_rendered} endpoint(s) scrape pods a chart renders"
    print(f"{len(monitors)} monitor(s); every scraped port is admitted from {scrape_ns}{tail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        help=f"manifest directory to check (default: {DEFAULT_TREE})",
    )
    parser.add_argument(
        "--observability-namespace",
        default=DEFAULT_OBSERVABILITY_NS,
        help="namespace Prometheus scrapes from (default: %(default)s)",
    )
    parser.add_argument(
        "--prometheus-pod-label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a label Prometheus's pods carry; a scrape peer narrowing the observability "
             "namespace with a podSelector is credited only when every label it asks for "
             "is declared here (repeatable)",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--namespace",
        help="namespace this tree deploys into; a monitor's matchNames must equal it",
    )
    scope.add_argument(
        "--namespace-from-tree",
        action="store_true",
        help="read that namespace from the one Namespace manifest under the directory",
    )
    scope.add_argument(
        "--corpus",
        metavar="PATH",
        help="rendered cluster corpus to check instead of a tree — a multi-document "
             "file, '-' for stdin, or a directory; every namespace in it is judged, "
             "so a monitor's matchNames resolves against it",
    )
    arguments = parser.parse_args(argv)
    for item in arguments.prometheus_pod_label:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--prometheus-pod-label expects KEY=VALUE, got {item!r}")
        SCRAPER_LABELS[key] = value

    if arguments.corpus and arguments.directory:
        print(
            "ERROR: --corpus replaces the manifest directory, and the two are read "
            "under different namespace rules: pass one or the other",
            file=sys.stderr,
        )
        return 2

    if arguments.corpus:
        inspected = arguments.corpus
        documents, errors = load_corpus(arguments.corpus)
    else:
        base = pathlib.Path(arguments.directory or DEFAULT_TREE)
        if not base.is_dir():
            print(
                f"ERROR: {base} is not a directory: the gate was pointed at a path that "
                "does not exist",
                file=sys.stderr,
            )
            return 2
        inspected = str(base)
        documents, errors = load_documents(base)

    if errors:
        print("ERROR: a manifest could not be read:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 2
    if not documents:
        print(
            f"ERROR: no manifest in {inspected} carries a `kind` — a gate that checks "
            "nothing is not a gate; check that the manifests are still there.",
            file=sys.stderr,
        )
        return 2

    namespace, namespace_error = arguments.namespace, None
    if arguments.namespace_from_tree:
        namespace, namespace_error = tree_namespace(documents)

    try:
        return check(
            documents,
            arguments.observability_namespace,
            namespace,
            namespace_error,
            bool(arguments.corpus),
        )
    except GateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except Violation as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
