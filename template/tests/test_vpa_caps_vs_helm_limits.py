"""A chart container's VPA caps memory at its limit, and an applying policy keeps
that limit fixed with RequestsOnly. check-hpa-vpa-invariant.py reads pod-spec
kinds only, so these chart-rendered targets are matched here by container name."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from conftest import REPO, assert_all_parsed, k8s_documents

MANIFESTS = REPO / "kubernetes"

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
_SUFFIXES = (
    ("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("Ti", 2**40),
    ("K", 10**3), ("M", 10**6), ("G", 10**9), ("T", 10**12),
)

# Resolved container policies below this are a broken resolver, not a shrunk
# estate: the platform controllers alone land here under every answer set.
RESOLVED_FLOOR = 6
# Modes in which the admission controller writes the recommendation into pods.
APPLYING_MODES = {"Auto", "Initial", "Recreate", "InPlaceOrRecreate"}


def _quantity(value) -> float:
    text = str(value)
    for suffix, multiplier in _SUFFIXES:
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * multiplier
    return float(text)


def _norm(value) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def load_manifests(root: Path = MANIFESTS):
    """(raw (namespace, kind, name) keys, (namespace, release) -> values, VPAs).

    Keys carry the namespace, so same-named releases in two namespaces stay apart.
    """
    raw: set[tuple[str | None, str, str]] = set()
    releases: dict[tuple[str | None, str], dict] = {}
    vpas: list[dict] = []
    documents, unreadable = k8s_documents(root)
    assert_all_parsed(unreadable, "workload, HelmRelease or VPA")
    for _path, doc in documents:
        meta = doc.get("metadata") or {}
        name, namespace = meta.get("name"), meta.get("namespace")
        kind = doc.get("kind")
        spec = doc.get("spec") or {}
        if kind in WORKLOAD_KINDS:
            raw.add((namespace, kind, name))
        elif kind == "HelmRelease":
            home = spec.get("targetNamespace") or namespace
            releases[(home, name)] = spec.get("values") or {}
        elif kind == "VerticalPodAutoscaler":
            vpas.append(doc)
    return raw, releases, vpas


def declared_limits(values, prefix: tuple = ()) -> dict[tuple, str]:
    """Every `resources.limits.memory` in a values tree, by its parent path."""
    found: dict[tuple, str] = {}
    if isinstance(values, dict):
        for key, value in values.items():
            if key == "resources" and isinstance(value, dict):
                memory = (value.get("limits") or {}).get("memory")
                if memory:
                    found[prefix] = memory
            found.update(declared_limits(value, prefix + (str(key),)))
    return found


def owning_release(
    target: str, namespace: str | None, releases: dict[tuple[str | None, str], dict]
) -> tuple[str | None, str] | None:
    """The release whose name is the longest prefix of the workload, in the same
    namespace. A namespace a kustomization sets is absent here and matches any;
    two same-named candidates are then ambiguous and resolve to none."""
    candidates = [
        key for key in releases
        if (None in (key[0], namespace) or key[0] == namespace)
        and _norm(target).startswith(_norm(key[1]))
    ]
    if not candidates:
        return None
    longest = max(len(_norm(key[1])) for key in candidates)
    best = [key for key in candidates if len(_norm(key[1])) == longest]
    return best[0] if len(best) == 1 else None


def container_limit(values: dict, container: str) -> str | None:
    """The limit a values tree declares for that container, if unambiguous."""
    limits = declared_limits(values)
    if container == "*":
        return next(iter(limits.values())) if len(limits) == 1 else None
    keyed = [v for path, v in limits.items() if path and _norm(path[-1]) == _norm(container)]
    return keyed[0] if len(keyed) == 1 else None


def cap_violations(raw, releases, vpas) -> tuple[list[str], int]:
    """(messages, number of container policies actually compared)."""
    problems: list[str] = []
    resolved = 0
    for vpa in vpas:
        spec = vpa.get("spec") or {}
        target = spec.get("targetRef") or {}
        namespace = (vpa.get("metadata") or {}).get("namespace")
        if any(
            (kind, wname) == (target.get("kind"), target.get("name"))
            and (None in (wns, namespace) or wns == namespace)
            for wns, kind, wname in raw
        ):
            continue
        release = owning_release(str(target.get("name")), namespace, releases)
        if release is None:
            continue
        mode = (spec.get("updatePolicy") or {}).get("updateMode", "Auto")
        name = (vpa.get("metadata") or {}).get("name")
        policies = (spec.get("resourcePolicy") or {}).get("containerPolicies") or []
        for policy in policies:
            container = policy.get("containerName")
            cap = (policy.get("maxAllowed") or {}).get("memory")
            limit = container_limit(releases[release], str(container))
            if not cap or not limit:
                continue
            resolved += 1
            where = f"{name}/{container} (chart {release[0]}/{release[1]})"
            if _quantity(cap) > _quantity(limit):
                problems.append(
                    f"{where}: maxAllowed.memory {cap} is above the container's "
                    f"{limit} limit — the updater would recommend an unschedulable request"
                )
            elif policy.get("controlledValues") == "RequestsOnly" and cap != limit:
                problems.append(
                    f"{where}: RequestsOnly keeps {limit} as the OOM ceiling, so "
                    f"maxAllowed.memory must equal it, not {cap}"
                )
            if mode in APPLYING_MODES and policy.get("controlledValues") != "RequestsOnly":
                problems.append(
                    f"{where}: updateMode {mode} under RequestsAndLimits rescales the "
                    f"{limit} limit with every recommendation, so the ceiling moves "
                    "and the pod is evicted again; set controlledValues: RequestsOnly"
                )
    return problems, resolved


@pytest.fixture(scope="module")
def manifests():
    return load_manifests()


def test_no_cap_exceeds_its_chart_container_limit(manifests):
    problems, resolved = cap_violations(*manifests)
    assert not problems, "\n  ".join(["VPA caps above a chart limit:", *problems])
    assert resolved >= RESOLVED_FLOOR, (
        f"only {resolved} chart-rendered container policies resolved to a declared "
        f"limit (floor {RESOLVED_FLOOR}) — the resolver stopped finding them"
    )


def test_a_cap_raised_above_its_limit_is_reported(manifests):
    """Mutation case: the live tree with one cap pushed over its chart limit."""
    raw, releases, vpas = manifests
    mutated = copy.deepcopy(vpas)
    for vpa in mutated:
        policies = ((vpa.get("spec") or {}).get("resourcePolicy") or {}).get(
            "containerPolicies"
        ) or []
        for policy in policies:
            if (policy.get("maxAllowed") or {}).get("memory"):
                policy["maxAllowed"]["memory"] = "64Gi"
    problems, _ = cap_violations(raw, releases, mutated)
    assert any("is above the container's" in p for p in problems)


def test_a_requests_only_cap_below_its_limit_is_reported():
    """RequestsOnly caps must equal the limit; a lower one is a silent throttle."""
    releases = {(None, "demo"): {"resources": {"limits": {"memory": "512Mi"}}}}
    vpas = [{
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "demo"},
        "spec": {
            "targetRef": {"kind": "Deployment", "name": "demo"},
            "resourcePolicy": {"containerPolicies": [{
                "containerName": "*",
                "maxAllowed": {"memory": "256Mi"},
                "controlledValues": "RequestsOnly",
            }]},
        },
    }]
    problems, resolved = cap_violations(set(), releases, vpas)
    assert resolved == 1
    assert any("must equal it" in p for p in problems)


@pytest.mark.parametrize("mode,flagged", [("Auto", True), ("Initial", True), ("Off", False)])
def test_an_applying_policy_without_requests_only_is_reported(mode, flagged):
    """Mutation case: the cap equals the limit, but the limit itself moves."""
    releases = {(None, "demo"): {"resources": {"limits": {"memory": "512Mi"}}}}
    vpas = [{
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "demo"},
        "spec": {
            "targetRef": {"kind": "Deployment", "name": "demo"},
            "updatePolicy": {"updateMode": mode},
            "resourcePolicy": {"containerPolicies": [{
                "containerName": "*",
                "maxAllowed": {"memory": "512Mi"},
            }]},
        },
    }]
    problems, _ = cap_violations(set(), releases, vpas)
    assert any("set controlledValues: RequestsOnly" in p for p in problems) is flagged


def test_a_raw_workload_target_is_left_to_the_vendored_gate():
    """check-hpa-vpa-invariant.py owns those; a second opinion here would drift."""
    releases = {(None, "demo"): {"resources": {"limits": {"memory": "512Mi"}}}}
    vpas = [{
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "demo"},
        "spec": {
            "targetRef": {"kind": "Deployment", "name": "demo"},
            "resourcePolicy": {"containerPolicies": [{
                "containerName": "*",
                "maxAllowed": {"memory": "64Gi"},
            }]},
        },
    }]
    assert cap_violations({(None, "Deployment", "demo")}, releases, vpas) == ([], 0)


def test_a_raw_workload_in_another_namespace_does_not_hide_a_chart_vpa():
    """Mutation case: only a same-namespace raw workload takes the VPA away."""
    releases = {("apps", "demo"): {"resources": {"limits": {"memory": "512Mi"}}}}
    vpas = [{
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "demo", "namespace": "apps"},
        "spec": {
            "targetRef": {"kind": "Deployment", "name": "demo"},
            "updatePolicy": {"updateMode": "Off"},
            "resourcePolicy": {"containerPolicies": [{
                "containerName": "*",
                "maxAllowed": {"memory": "64Gi"},
            }]},
        },
    }]
    problems, resolved = cap_violations({("other", "Deployment", "demo")}, releases, vpas)
    assert resolved == 1
    assert len(problems) == 1 and "is above the container's" in problems[0]


def test_a_same_named_release_in_another_namespace_is_not_the_owner():
    """Mutation case: the VPA's own namespace decides which chart it caps."""
    releases = {
        ("a", "runner"): {"resources": {"limits": {"memory": "512Mi"}}},
        ("b", "runner"): {"resources": {"limits": {"memory": "8Gi"}}},
    }
    assert owning_release("runner-x", "a", releases) == ("a", "runner")
    assert owning_release("runner-x", "c", releases) is None
    assert owning_release("runner-x", None, releases) is None
