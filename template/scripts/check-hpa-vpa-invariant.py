#!/usr/bin/env python3
"""Assert no workload has both an HPA and a CPU-controlling VPA.

Reads the rendered corpus on stdin with consumer data from --policy-config;
exits 0 clean, 1 on a violation, 2 on an error. Contract: docs/SCRIPTS.md.
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
        doc_key,
        doc_namespace,
        load_corpus,
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

HPA_KIND = "HorizontalPodAutoscaler"
VPA_KIND = "VerticalPodAutoscaler"
PDB_KIND = "PodDisruptionBudget"


class Policy:
    """Consumer data from --policy-config. A value, not module state, so a
    second load replaces rather than accumulates. A plain class, not a
    dataclass: importlib path-loading cannot resolve the annotations.
    """

    def __init__(
        self,
        chart_native_hpa_targets=None,
        cpu_limit_allowlist=None,
        vpa_cap_allowlist=None,
        memory_ratio_allowlist=None,
        vpa_cap_declared_limits=None,
    ) -> None:
        # (namespace, target-kind, target-name) -> where the chart-native HPA comes from.
        self.chart_native_hpa_targets: dict[tuple[str, str, str], str] = dict(
            chart_native_hpa_targets or {}
        )
        # "namespace/Kind/name" workloads intentionally permitted a CPU limit.
        self.cpu_limit_allowlist: set[str] = set(cpu_limit_allowlist or ())
        # "namespace/VerticalPodAutoscaler/name" policies whose memory cap has
        # not been re-derived against its limit yet.
        self.vpa_cap_allowlist: set[str] = set(vpa_cap_allowlist or ())
        # "namespace/Kind/name" workloads allowed requests.memory == limits.memory
        # under a limit-rewriting VPA.
        self.memory_ratio_allowlist: set[str] = set(memory_ratio_allowlist or ())
        # Memory limits for targets the corpus does not render (a chart-rendered
        # workload, or one patched by a postRenderer): {"ns/Kind/name": {container: limit}}.
        self.vpa_cap_declared_limits: dict[str, dict[str, str]] = {
            str(k): {str(c): str(v) for c, v in (limits or {}).items()}
            for k, limits in (vpa_cap_declared_limits or {}).items()
        }


def _target_key(ns: str, ref: dict) -> tuple[str, str, str]:
    """(namespace, target-kind, target-name) — the join key between HPA and VPA."""
    return (ns or "default", ref.get("kind", ""), ref.get("name", ""))


def _hpa_metrics(spec: dict) -> set[str]:
    """Resource names an HPA scales on (cpu/memory).

    An absent or empty `metrics` yields {"cpu"}, the autoscaling/v2 default; a
    `metrics` holding only non-Resource entries yields the empty set.
    """
    metrics = spec.get("metrics") or []
    if not metrics:
        return {"cpu"}
    resources: set[str] = set()
    for metric in metrics:
        mtype = metric.get("type")
        if mtype == "Resource":
            name = (metric.get("resource") or {}).get("name")
        elif mtype == "ContainerResource":
            # Per-container CPU/memory target — still a cpu/memory HPA.
            name = (metric.get("containerResource") or {}).get("name")
        else:
            continue
        if name:
            resources.add(str(name).lower())
    return resources


def _vpa_resources(spec: dict) -> set[str]:
    """Resources a VPA controls. Default (no controlledResources) is cpu+memory."""
    controlled: set[str] = set()
    policies = (spec.get("resourcePolicy") or {}).get("containerPolicies", []) or []
    if not policies:
        # No policy means the VPA controls everything by default.
        return {"cpu", "memory"}
    for p in policies:
        if (p.get("mode") or "").lower() == "off":
            # Per-container Off policy is recommend-only — not mutating.
            continue
        cr = p.get("controlledResources")
        if cr is None:
            controlled |= {"cpu", "memory"}
        else:
            controlled |= {str(r).lower() for r in cr}
    # Without a '*' catch-all policy the unnamed containers keep the default
    # cpu+memory control, so fail closed and count those defaults.
    if not any(p.get("containerName") == "*" for p in policies):
        controlled |= {"cpu", "memory"}
    return controlled


# --- "no CPU limits" policy ---------------------------------------------------
POD_SPEC_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "Pod"}


def _allowlist(doc: dict, path, key: str, kind: str | None = None) -> set[str]:
    """Read one allowlist: a mapping `{"ns/Kind/name": "reason"}`.

    Every entry carries a reason structurally. A bare string is refused - an
    unexplained exemption is a hole, and a YAML comment survives no refactor.
    """
    raw = doc.get(key)
    if raw is None:
        return set()
    shape = f"namespace/{kind or 'Kind'}/name"
    if not isinstance(raw, dict):
        raise ValueError(
            f'{path}: {key} must be a mapping of "{shape}": reason'
        )
    out: set[str] = set()
    for target, reason in raw.items():
        if not str(reason or "").strip():
            raise ValueError(f"{path}: {key}[{target}] has no reason")
        # CRITICAL: a key becomes a set member verbatim, so a mis-shaped one
        # exempts nothing and the exemption silently does not apply.
        segments = str(target).split("/")
        if len(segments) != 3 or not all(s.strip() for s in segments):
            raise ValueError(
                f'{path}: {key}[{target}] is not of the form "{shape}"'
            )
        if kind and segments[1] != kind:
            raise ValueError(
                f'{path}: {key}[{target}] names kind "{segments[1]}", but this '
                f'allowlist exempts {kind} objects ("{shape}")'
            )
        out.add(str(target))
    return out


def _declared_limits(
    doc: dict, path, key: str = "vpa_cap_declared_limits"
) -> dict[str, dict[str, str]]:
    """Read `{"ns/Kind/name": {container: memory limit}}` for unrendered targets.

    Supplies the fact the corpus is missing rather than exempting the target, so
    the cap is still compared against a real limit.
    """
    raw = doc.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f'{path}: {key} must be a mapping of "namespace/Kind/name": '
            "{container: memory limit}"
        )
    out: dict[str, dict[str, str]] = {}
    for target, limits in raw.items():
        if not isinstance(limits, dict) or not limits:
            raise ValueError(
                f"{path}: {key}[{target}] must be a non-empty mapping of "
                "container name to memory limit"
            )
        for container, limit in limits.items():
            if parse_quantity(limit) is None:
                raise ValueError(
                    f"{path}: {key}[{target}][{container}] is not a memory "
                    f"quantity: {limit!r}"
                )
        out[str(target)] = {str(c): str(v) for c, v in limits.items()}
    return out


def load_policy(path) -> Policy:
    """Read a --policy-config file and return it. Mutates nothing.

    SHARED: validate-helm-values.py imports this (and `cpu_limit_violations`)
    so the kustomize-side and helm-rendered-side checks honor one allowlist.
    """
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    policy = Policy()
    for entry in doc.get("chart_native_hpa_targets") or []:
        missing = [k for k in ("namespace", "kind", "name") if not entry.get(k)]
        if missing:
            raise ValueError(f"{path}: chart-native target {entry!r} is missing {missing}")
        policy.chart_native_hpa_targets[(entry["namespace"], entry["kind"], entry["name"])] = str(
            entry.get("source", "chart-native HPA")
        )
    policy.cpu_limit_allowlist = _allowlist(doc, path, "cpu_limit_allowlist")
    policy.vpa_cap_allowlist = _allowlist(
        doc, path, "vpa_cap_allowlist", kind="VerticalPodAutoscaler"
    )
    policy.memory_ratio_allowlist = _allowlist(doc, path, "memory_ratio_allowlist")
    policy.vpa_cap_declared_limits = _declared_limits(doc, path)
    return policy


def _containers_of(doc: dict) -> list[dict]:
    """All containers (init + regular + ephemeral) of a pod-spec workload, else []."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        pod = spec
    elif kind == "CronJob":
        pod = ((((spec.get("jobTemplate") or {}).get("spec") or {})
                .get("template") or {}).get("spec") or {})
    elif kind in POD_SPEC_KINDS:
        pod = (spec.get("template") or {}).get("spec") or {}
    else:
        return []
    out: list[dict] = []
    for key in ("initContainers", "containers", "ephemeralContainers"):
        v = pod.get(key)
        if isinstance(v, list):
            out.extend(c for c in v if isinstance(c, dict))
    return out


def _find_values_cpu_limits(node, path: str = "") -> list[str]:
    """Recursively find `limits.cpu` inside a HelmRelease `.spec.values` tree."""
    hits: list[str] = []
    if isinstance(node, dict):
        lim = node.get("limits")
        # `cpu: null`/`""` clears a chart default rather than setting a limit
        # (k8s treats it as "no CPU limit"), so don't flag a merely-present key.
        if isinstance(lim, dict) and lim.get("cpu") not in (None, ""):
            key = f"{path}.limits.cpu" if path else "limits.cpu"
            hits.append(f"{key}={lim.get('cpu')}")
        for k, v in node.items():
            if k != "limits":
                hits.extend(_find_values_cpu_limits(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_find_values_cpu_limits(v, f"{path}[{i}]"))
    return hits


def cpu_limit_violations(docs: list[dict], allowlist: set[str] | None = None) -> list[str]:
    """Flag any pod-spec container or HelmRelease values that set a CPU limit."""
    allowed = allowlist or set()
    out: list[str] = []
    for d in docs:
        kind = d.get("kind")
        wlkey = doc_key(d)
        if wlkey in allowed:
            continue
        if kind == "HelmRelease":
            values = (d.get("spec") or {}).get("values") or {}
            for hit in _find_values_cpu_limits(values, "values"):
                out.append(f"  {wlkey}: HelmRelease sets a CPU limit ({hit})")
        else:
            for c in _containers_of(d):
                lim = (c.get("resources") or {}).get("limits") or {}
                if lim.get("cpu") not in (None, ""):
                    out.append(
                        f"  {wlkey}: container {c.get('name', '?')!r} sets "
                        f"limits.cpu={lim.get('cpu')}"
                    )
    return out


# --- VPA memory-cap rule ------------------------------------------------------
_BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL_SUFFIXES = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}


def parse_quantity(value) -> float | None:
    """A Kubernetes quantity in bytes, or None when it cannot be read.

    Unparseable is None rather than 0: a value this cannot compare must not
    become a violation invented out of a parse failure.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        # Plain and decimal-exponent forms ("1", "1.5", "1e6") — tried first so
        # the exponent form is never mistaken for the "E" (exa) suffix.
        return float(text)
    except ValueError:
        pass
    for suffix, mult in _BINARY_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * mult
            except ValueError:
                return None
    mult = _DECIMAL_SUFFIXES.get(text[-1])
    if mult is None:
        return None
    try:
        return float(text[:-1]) * mult
    except ValueError:
        return None


def _workload_memory_limits(docs: list[dict]) -> dict[tuple[str, str, str], dict[str, str]]:
    """(namespace, kind, name) -> {container name: its memory limit}."""
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    for d in docs:
        kind = d.get("kind")
        if kind not in POD_SPEC_KINDS and kind != "CronJob":
            continue
        meta = d.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        limits: dict[str, str] = {}
        for c in _containers_of(d):
            mem = ((c.get("resources") or {}).get("limits") or {}).get("memory")
            if mem not in (None, ""):
                limits[str(c.get("name") or "?")] = str(mem)
        out[(doc_namespace(d), kind, str(name))] = limits
    return out


def vpa_cap_violations(
    docs: list[dict],
    allowlist: set[str] | None = None,
    declared_limits: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Flag `maxAllowed.memory` caps that cannot bind (see docs/SCRIPTS.md).

    A target the corpus does not render carries no limit to compare against;
    `declared_limits` supplies it, and vpa_cap_unjudged() names the rest.
    """
    return _vpa_cap_scan(docs, allowlist, declared_limits)[0]


def vpa_cap_unjudged(
    docs: list[dict],
    allowlist: set[str] | None = None,
    declared_limits: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """VPA memory caps no limit could be compared against.

    A chart-rendered DaemonSet, or one whose limit comes from a postRenderer
    patch, is absent from the corpus, so the cap arm would skip it in silence.
    """
    return _vpa_cap_scan(docs, allowlist, declared_limits)[1]


def _vpa_cap_scan(
    docs: list[dict],
    allowlist: set[str] | None = None,
    declared_limits: dict[str, dict[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
    """(cap violations, caps that could not be judged)."""
    allowed = allowlist or set()
    declared = declared_limits or {}
    workloads = _workload_memory_limits(docs)
    unjudged: list[str] = []
    out: list[str] = []
    for d in docs:
        if d.get("kind") != VPA_KIND:
            continue
        ns = doc_namespace(d)
        vpakey = doc_key(d)
        if vpakey in allowed:
            continue
        spec = d.get("spec") or {}
        ref = spec.get("targetRef") or {}
        if not ref.get("name"):
            continue
        target = _target_key(ns, ref)
        tns, tkind, tname = target
        # A declared limit fills a gap; a rendered one is the truth and wins.
        limits = {**declared.get("%s/%s/%s" % target, {}), **(workloads.get(target) or {})}
        vpa_off = str((spec.get("updatePolicy") or {}).get("updateMode", "Auto")).lower() == "off"
        policies = (spec.get("resourcePolicy") or {}).get("containerPolicies", []) or []
        if not limits:
            if any(
                parse_quantity((p.get("maxAllowed") or {}).get("memory")) is not None
                for p in policies
            ):
                unjudged.append(
                    f"  {vpakey}: maxAllowed.memory is set, but the corpus renders no "
                    f"memory limit for {tns}/{tkind}/{tname}, so the cap was not "
                    f"compared against anything"
                )
            continue
        # An exact-named containerPolicy overrides "*" for that container, so
        # the wildcard's cap must not be judged against explicitly-covered ones.
        explicit = {p.get("containerName") for p in policies
                    if p.get("containerName") not in (None, "*")}
        for p in policies:
            cap_raw = (p.get("maxAllowed") or {}).get("memory")
            cap = parse_quantity(cap_raw)
            if cap is None:
                continue
            off = vpa_off or str(p.get("mode") or "").lower() == "off"
            # Unset controlledValues defaults to RequestsAndLimits, i.e. the
            # policy moves the limit too.
            controls_limits = (
                str(p.get("controlledValues") or "RequestsAndLimits").lower() != "requestsonly"
            )
            cname = p.get("containerName")
            targets = (
                {k: v for k, v in limits.items() if k not in explicit}
                if cname in (None, "*")
                else {k: v for k, v in limits.items() if k == cname}
            )
            for container, limit_raw in sorted(targets.items()):
                limit = parse_quantity(limit_raw)
                if limit is None or limit <= 0:
                    continue
                if cap > limit * (1 + 1e-9):
                    out.append(
                        f"  {vpakey}: maxAllowed.memory {cap_raw} is above the {limit_raw} "
                        f"limit of container {container!r} in {tns}/{tkind}/{tname} — a "
                        f"recommendation the kubelet would reject; lower the cap or raise "
                        f"the limit in the same commit"
                    )
                elif controls_limits and not off and abs(cap - limit) <= limit * 1e-9:
                    out.append(
                        f"  {vpakey}: maxAllowed.memory {cap_raw} equals the limit of "
                        f"container {container!r} in {tns}/{tkind}/{tname} while the policy "
                        f"also controls limits (controlledValues: "
                        f"{p.get('controlledValues') or 'unset'}) — the updater rescales the "
                        f"limit with the request, so that ceiling never binds; cap below the "
                        f"limit, or set controlledValues: RequestsOnly with a hand-set limit"
                    )
    return out, unjudged


def _container_policy_for(policies: list[dict], container: str) -> dict | None:
    """The containerPolicy the VPA applies to `container`, or None for defaults."""
    for p in policies:
        if p.get("containerName") == container:
            return p
    for p in policies:
        if p.get("containerName") in (None, "*"):
            return p
    return None


def _rewrites_memory_limit(policies: list[dict], container: str) -> bool:
    """Whether a VPA's policy set moves this container's memory LIMIT."""
    policy = _container_policy_for(policies, container)
    if policy is None:
        # No policy covers the container: the VPA controls it with the
        # defaults, which are cpu+memory and RequestsAndLimits.
        return True
    if str(policy.get("mode") or "").lower() == "off":
        return False
    controlled = policy.get("controlledResources")
    if controlled is not None and "memory" not in {str(r).lower() for r in controlled}:
        return False
    return str(policy.get("controlledValues") or "RequestsAndLimits").lower() != "requestsonly"


def memory_ratio_violations(docs: list[dict], allowlist: set[str] | None = None) -> list[str]:
    """Flag requests.memory == limits.memory under a limit-rewriting VPA.

    The updater preserves the ratio, so a 1:1 container never gains headroom.
    """
    allowed = allowlist or set()
    by_target: dict[tuple[str, str, str], list[tuple[str, list[dict]]]] = {}
    for d in docs:
        if d.get("kind") != VPA_KIND:
            continue
        spec = d.get("spec") or {}
        ref = spec.get("targetRef") or {}
        if not ref.get("name"):
            continue
        if str((spec.get("updatePolicy") or {}).get("updateMode", "Auto")).lower() == "off":
            continue
        policies = (spec.get("resourcePolicy") or {}).get("containerPolicies", []) or []
        by_target.setdefault(_target_key(doc_namespace(d), ref), []).append(
            (doc_key(d), [p for p in policies if isinstance(p, dict)])
        )

    out: list[str] = []
    for d in docs:
        kind = d.get("kind")
        if kind not in POD_SPEC_KINDS and kind != "CronJob":
            continue
        name = (d.get("metadata") or {}).get("name")
        if not name:
            continue
        wlkey = doc_key(d)
        targeting = by_target.get((doc_namespace(d), kind, str(name)))
        if not targeting or wlkey in allowed:
            continue
        for container in _containers_of(d):
            resources = container.get("resources") or {}
            request = parse_quantity((resources.get("requests") or {}).get("memory"))
            limit = parse_quantity((resources.get("limits") or {}).get("memory"))
            if request is None or limit is None or limit <= 0:
                continue
            if abs(request - limit) > limit * 1e-9:
                continue
            cname = str(container.get("name") or "?")
            for vpakey, policies in targeting:
                if not _rewrites_memory_limit(policies, cname):
                    continue
                out.append(
                    f"  {wlkey}: container {cname!r} sets requests.memory == "
                    f"limits.memory while {vpakey} rewrites its memory limit — the "
                    f"updater preserves the ratio, so the limit tracks the request "
                    f"up forever and the container never gains headroom. Set a "
                    f"limit above the request, or set controlledValues: "
                    f"RequestsOnly on the VPA with a hand-set limit"
                )
    return out


def _pod_labels(docs: list[dict]) -> dict[tuple[str, str, str], dict]:
    """(namespace, kind, name) -> pod template labels, for every rendered workload."""
    out: dict[tuple[str, str, str], dict] = {}
    for d in docs:
        kind = d.get("kind")
        if kind not in POD_SPEC_KINDS and kind != "CronJob":
            continue
        name = (d.get("metadata") or {}).get("name")
        if not name:
            continue
        spec = d.get("spec") or {}
        if kind == "Pod":
            labels = (d.get("metadata") or {}).get("labels") or {}
        elif kind == "CronJob":
            labels = ((((spec.get("jobTemplate") or {}).get("spec") or {})
                       .get("template") or {}).get("metadata") or {}).get("labels") or {}
        else:
            labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        out[(doc_namespace(d), kind, str(name))] = labels if isinstance(labels, dict) else {}
    return out


def _blocks_last_eviction(pdb_spec: dict, replicas: int) -> bool:
    """Whether a budget leaves zero allowed disruptions at `replicas` pods.

    minAvailable >= replicas, or maxUnavailable rounding down to zero, pins the
    pod in place and the node never drains.
    """
    minimum = pdb_spec.get("minAvailable")
    maximum = pdb_spec.get("maxUnavailable")
    for value, is_minimum in ((minimum, True), (maximum, False)):
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            resolved = value
        elif isinstance(value, str) and value.endswith("%") and value[:-1].isdigit():
            percent = int(value[:-1])
            # The API rounds minAvailable up and maxUnavailable down.
            resolved = (
                -(-replicas * percent // 100) if is_minimum else replicas * percent // 100
            )
        else:
            continue
        if (resolved >= replicas) if is_minimum else (resolved <= 0):
            return True
    return False


def _selector_matches(selector: object, labels: dict) -> bool:
    """Whether a PDB's label selector matches a pod label set. matchExpressions
    are not modelled, so a selector using them matches nothing here."""
    if not isinstance(selector, dict) or selector.get("matchExpressions"):
        return False
    match_labels = selector.get("matchLabels")
    if not isinstance(match_labels, dict) or not match_labels:
        return False
    return match_labels.items() <= labels.items()


def pdb_floor_violations(docs: list[dict]) -> list[str]:
    """Flag an HPA whose floor is one replica beside a PDB that blocks its eviction.

    The node then never drains: the budget forbids removing the only pod and the
    autoscaler never adds a second.
    """
    labels_of = _pod_labels(docs)
    budgets: dict[str, list[tuple[str, dict]]] = {}
    for d in docs:
        if d.get("kind") != PDB_KIND:
            continue
        name = (d.get("metadata") or {}).get("name") or "?"
        budgets.setdefault(doc_namespace(d), []).append((str(name), d.get("spec") or {}))

    out: list[str] = []
    for d in docs:
        if d.get("kind") != HPA_KIND:
            continue
        spec = d.get("spec") or {}
        ref = spec.get("scaleTargetRef") or {}
        if not ref.get("name"):
            continue
        floor = spec.get("minReplicas", 1)
        if not isinstance(floor, int) or isinstance(floor, bool) or floor >= 2:
            continue
        key = _target_key(doc_namespace(d), ref)
        # No rendered target means a chart owns the pod template, so the budget
        # cannot be matched to it from this corpus.
        if key not in labels_of:
            continue
        for pdb_name, pdb_spec in budgets.get(key[0], []):
            if not _selector_matches(pdb_spec.get("selector"), labels_of[key]):
                continue
            if not _blocks_last_eviction(pdb_spec, floor):
                continue
            out.append(
                f"  {doc_key(d)}: minReplicas {floor} while {PDB_KIND} "
                f"{key[0]}/{pdb_name} allows no disruption at {floor} replica(s) — "
                f"a drain of the node holding it blocks forever. Raise minReplicas "
                f"to 2, or relax the budget (maxUnavailable: 1)"
            )
    return out


def request_above_limit_violations(docs: list[dict]) -> list[str]:
    """Flag a container whose resource request exceeds its own limit.

    The API server rejects such a pod, so `kustomize build` and kubeconform
    both pass it and the Kustomization applying it never becomes ready.
    """
    out: list[str] = []
    for d in docs:
        if not (d.get("metadata") or {}).get("name"):
            continue
        for container in _containers_of(d):
            resources = container.get("resources")
            if not isinstance(resources, dict):
                continue
            requests = resources.get("requests")
            limits = resources.get("limits")
            if not isinstance(requests, dict) or not isinstance(limits, dict):
                continue
            cname = str(container.get("name") or "?")
            for resource in sorted(set(requests) & set(limits)):
                request = parse_quantity(requests[resource])
                limit = parse_quantity(limits[resource])
                if request is None or limit is None:
                    continue
                if request <= limit * (1 + 1e-9):
                    continue
                out.append(
                    f"  {doc_key(d)}: container {cname!r} requests {resource} "
                    f"{requests[resource]} above its own limit {limits[resource]} — "
                    f"the API server rejects the pod, so the Kustomization applying "
                    f"it never becomes ready. Lower the request, or raise the limit"
                )
    return out


def dangling_target_violations(docs: list[dict], exempt: set[str]) -> list[str]:
    """Flag an autoscaler whose target no workload in the corpus renders.

    Only workload kinds are judged: a chart-rendered or CRD-backed target is
    invisible here, which is why this arm is opt-in.
    """
    labels_of = _pod_labels(docs)
    out: list[str] = []
    for d in docs:
        kind = d.get("kind")
        if kind not in (HPA_KIND, VPA_KIND):
            continue
        spec = d.get("spec") or {}
        field = "scaleTargetRef" if kind == HPA_KIND else "targetRef"
        ref = spec.get(field) or {}
        if not ref.get("name"):
            continue
        key = _target_key(doc_namespace(d), ref)
        if key[1] not in POD_SPEC_KINDS and key[1] != "CronJob":
            continue
        if key in labels_of or "/".join(key) in exempt:
            continue
        out.append(
            f"  {doc_key(d)}: {field} names {key[1]}/{key[2]} in {key[0]}, which no "
            f"workload in the corpus renders — the autoscaler applies and scales "
            f"nothing. Fix the name, or declare the target in the policy file "
            f"(chart_native_hpa_targets / vpa_cap_declared_limits)"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HPA/VPA + CPU-limit policy gate.")
    parser.add_argument("--require-chart-native-vpas", action="store_true")
    parser.add_argument(
        "--require-rendered-targets", action="store_true",
        help="fail an HPA/VPA whose workload target the corpus does not render "
             "(leave off where charts render the targets)",
    )
    parser.add_argument("--policy-config", help="YAML/JSON policy data (see module docstring)")
    parser.add_argument(
        "--allow-unjudged-vpa-caps", action="store_true",
        help="report, rather than fail on, memory caps whose target the corpus "
             "renders no limit for",
    )
    args = parser.parse_args(argv)
    policy = Policy()
    if args.policy_config:
        try:
            policy = load_policy(args.policy_config)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"ERROR: --policy-config {args.policy_config}: {exc}", file=sys.stderr)
            return 2

    docs = load_corpus()

    # Multiple HPAs or VPAs can target one workload, so aggregate per key rather
    # than last-wins: union the resource sets so a memory-only VPA can never mask
    # a cpu-controlling one on the same target.
    hpas: dict[tuple[str, str, str], set[str]] = {}
    vpas: dict[tuple[str, str, str], set[str]] = {}  # mutating VPAs only (Off skipped)
    vpa_names: dict[tuple[str, str, str], list[str]] = {}

    for d in docs:
        kind = d.get("kind")
        meta = d.get("metadata") or {}
        ns = doc_namespace(d)
        spec = d.get("spec") or {}
        if kind == HPA_KIND:
            ref = spec.get("scaleTargetRef") or {}
            if not ref.get("name"):
                continue
            key = _target_key(ns, ref)
            hpas[key] = hpas.get(key, set()) | _hpa_metrics(spec)
        elif kind == VPA_KIND:
            ref = spec.get("targetRef") or {}
            if not ref.get("name"):
                continue
            key = _target_key(ns, ref)
            # updateMode "Off" is recommend-only: it never mutates pods, so it
            # cannot fight an HPA (this is how coredns pairs a min==max HPA pin
            # with a right-sizing VPA). Only mutating modes can conflict.
            mode = (spec.get("updatePolicy") or {}).get("updateMode", "Auto")
            if str(mode).lower() == "off":
                continue
            vpas[key] = vpas.get(key, set()) | _vpa_resources(spec)
            vpa_names.setdefault(key, []).append(meta.get("name", "?"))

    violations: list[str] = []
    for key in sorted(hpas):
        hpa_res = hpas[key]
        if key not in vpas:
            continue
        vpa_res = vpas[key]
        # hpa_res already encodes the autoscaling/v2 default: it is {"cpu"} when
        # no metrics were declared and empty when metrics held only non-Resource
        # (External/Object/Pods) entries, which can't clash with a VPA.
        clash = hpa_res & vpa_res
        if clash:
            ns, tkind, tname = key
            names = ", ".join(repr(n) for n in sorted(vpa_names[key]))
            violations.append(
                f"  {ns}/{tkind}/{tname}: HPA scales {sorted(hpa_res)} "
                f"but VPA(s) {names} also control {sorted(clash)} "
                f"(set the VPA to controlledResources excluding {sorted(clash)})"
            )

    # Static check for chart-native HPAs (their HPA isn't in the corpus, but their
    # VPA is). Opt-in: only meaningful on the full rendered corpus flux:lint builds.
    if args.require_chart_native_vpas:
        for key, source in sorted(policy.chart_native_hpa_targets.items()):
            ns, tkind, tname = key
            # `not vpas.get(key)` (vs `key not in vpas`) also catches a mutating
            # VPA whose every containerPolicy is mode:Off — it registers with an
            # empty controlled set but right-sizes nothing, so it must not count.
            if not vpas.get(key):
                violations.append(
                    f"  {ns}/{tkind}/{tname}: chart-native HPA ({source}) has no "
                    f"mutating (Auto/Initial) VPA in the rendered corpus — add a "
                    f"memory-only VPA (controlledResources: [memory]) so CPU stays "
                    f"HPA-owned and memory is actually right-sized (an Off VPA "
                    f"recommends but never resizes, so it does not satisfy this)"
                )
            elif "cpu" in vpas.get(key, set()):
                names = ", ".join(repr(n) for n in sorted(vpa_names.get(key, [])))
                violations.append(
                    f"  {ns}/{tkind}/{tname}: chart-native HPA ({source}) scales cpu "
                    f"but mutating VPA(s) {names} also control cpu — set "
                    f"controlledResources to exclude cpu (memory-only)"
                )

    cpu_violations = (
        cpu_limit_violations(docs, policy.cpu_limit_allowlist)
        if args.require_chart_native_vpas else []
    )
    cap_violations, cap_unjudged = (
        _vpa_cap_scan(docs, policy.vpa_cap_allowlist, policy.vpa_cap_declared_limits)
        if args.require_chart_native_vpas else ([], [])
    )
    ratio_violations = (
        memory_ratio_violations(docs, policy.memory_ratio_allowlist)
        if args.require_chart_native_vpas else []
    )
    floor_violations = pdb_floor_violations(docs)
    inverted_resources = request_above_limit_violations(docs)
    dangling = (
        dangling_target_violations(
            docs,
            {"/".join(k) for k in policy.chart_native_hpa_targets}
            | set(policy.vpa_cap_declared_limits),
        )
        if args.require_rendered_targets else []
    )

    failed = False
    if violations:
        print("HPA/VPA invariant violated — same resource driven by both:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        failed = True
    if cpu_violations:
        print(
            "CPU-limit policy violated — pods/HelmReleases must not set a CPU limit "
            "(compressible resource; CFS throttling hurts latency and distorts "
            "CPU-based HPAs). Offenders:",
            file=sys.stderr,
        )
        print("\n".join(cpu_violations), file=sys.stderr)
        failed = True
    if cap_violations:
        print(
            "VPA cap policy violated — maxAllowed.memory must stay under the container's "
            "memory limit, and may equal it only where the policy does not control limits "
            "(controlledValues: RequestsOnly). Offenders:",
            file=sys.stderr,
        )
        print("\n".join(cap_violations), file=sys.stderr)
        print(
            "  (a cap awaiting re-derivation goes in the policy file's vpa_cap_allowlist "
            "with its rationale, not left silently violating)",
            file=sys.stderr,
        )
        failed = True
    if cap_unjudged:
        print(
            "VPA memory caps NOT JUDGED — the corpus renders no memory limit for their "
            "target, so a cap above the real limit would pass review. Pin the limit in "
            "the policy file's vpa_cap_declared_limits, or acknowledge these with "
            "--allow-unjudged-vpa-caps. Targets:",
            file=sys.stderr,
        )
        print("\n".join(cap_unjudged), file=sys.stderr)
        if not args.allow_unjudged_vpa_caps:
            failed = True
    if ratio_violations:
        print(
            "Memory requests==limits under a limit-rewriting VPA — the updater "
            "preserves the ratio, so the container never gains headroom. Offenders:",
            file=sys.stderr,
        )
        print("\n".join(ratio_violations), file=sys.stderr)
        print(
            "  (a deliberate 1:1 pin goes in the policy file's memory_ratio_allowlist "
            "with its rationale)",
            file=sys.stderr,
        )
        failed = True
    if floor_violations:
        print(
            "Undrainable pairing — an HPA floor of one replica beside a budget that "
            "allows no disruption:",
            file=sys.stderr,
        )
        print("\n".join(floor_violations), file=sys.stderr)
        failed = True
    if inverted_resources:
        print(
            "Resource request above its own limit — the pod is rejected by the API "
            "server, not by any render-time gate:",
            file=sys.stderr,
        )
        print("\n".join(inverted_resources), file=sys.stderr)
        failed = True
    if dangling:
        print(
            "Autoscaler target missing — the ref names a workload the corpus does "
            "not render:",
            file=sys.stderr,
        )
        print("\n".join(dangling), file=sys.stderr)
        failed = True
    if failed:
        return 1

    print(
        f"HPA/VPA invariant OK ({len(hpas)} HPAs, {len(vpas)} VPAs checked"
        + (f", {len(policy.chart_native_hpa_targets)} chart-native targets asserted"
           ", CPU-limit policy OK, VPA cap policy OK, memory ratio policy OK"
           if args.require_chart_native_vpas else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
