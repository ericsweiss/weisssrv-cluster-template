"""A VPA memory cap never sits above the chart container's own memory limit.

check-hpa-vpa-invariant.py reads pod-spec kinds only, so it skips every VPA whose
target a HelmRelease renders. This covers those, matched by container name.
"""

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


def _quantity(value) -> float:
    text = str(value)
    for suffix, multiplier in _SUFFIXES:
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * multiplier
    return float(text)


def _norm(value) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def load_manifests(root: Path = MANIFESTS):
    """(raw workload keys, release name -> values, VPA docs)."""
    documents, unreadable = k8s_documents(root)
    assert_all_parsed(unreadable, "VPA policy or chart limit")
    raw: set = set()
    releases: dict = {}
    vpas: list = []
    for _path, doc in documents:
        name = (doc.get("metadata") or {}).get("name")
        kind = doc.get("kind")
        if kind in WORKLOAD_KINDS:
            raw.add((kind, name))
        elif kind == "HelmRelease":
            releases[name] = (doc.get("spec") or {}).get("values") or {}
        elif kind == "VerticalPodAutoscaler":
            vpas.append(doc)
    return raw, releases, vpas


def declared_limits(values, prefix: tuple = ()) -> dict:
    """Every `resources.limits.memory` in a values tree, by its parent path."""
    found: dict = {}
    if isinstance(values, dict):
        for key, value in values.items():
            if key == "resources" and isinstance(value, dict):
                memory = (value.get("limits") or {}).get("memory")
                if memory:
                    found[prefix] = memory
            found.update(declared_limits(value, prefix + (str(key),)))
    return found


def owning_release(target: str, releases: dict):
    """The release whose name is the longest prefix of the workload name."""
    candidates = [n for n in releases if _norm(target).startswith(_norm(n))]
    if not candidates:
        return None
    longest = max(len(_norm(n)) for n in candidates)
    best = [n for n in candidates if len(_norm(n)) == longest]
    return best[0] if len(best) == 1 else None


def container_limit(values: dict, container: str):
    """The limit a values tree declares for that container, if unambiguous."""
    limits = declared_limits(values)
    if container == "*":
        return next(iter(limits.values())) if len(limits) == 1 else None
    keyed = [v for path, v in limits.items() if path and _norm(path[-1]) == _norm(container)]
    return keyed[0] if len(keyed) == 1 else None


def cap_violations(raw, releases, vpas) -> tuple:
    """(messages, number of container policies actually compared)."""
    problems: list = []
    resolved = 0
    for vpa in vpas:
        spec = vpa.get("spec") or {}
        target = spec.get("targetRef") or {}
        if (target.get("kind"), target.get("name")) in raw:
            continue
        release = owning_release(str(target.get("name")), releases)
        if release is None:
            continue
        name = (vpa.get("metadata") or {}).get("name")
        policies = (spec.get("resourcePolicy") or {}).get("containerPolicies") or []
        for policy in policies:
            container = policy.get("containerName")
            cap = (policy.get("maxAllowed") or {}).get("memory")
            limit = container_limit(releases[release], str(container))
            if not cap or not limit:
                continue
            resolved += 1
            where = f"{name}/{container} (chart {release})"
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
    releases = {"demo": {"resources": {"limits": {"memory": "512Mi"}}}}
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


def test_a_raw_workload_target_is_left_to_the_vendored_gate():
    """check-hpa-vpa-invariant.py owns those; a second opinion here would drift."""
    releases = {"demo": {"resources": {"limits": {"memory": "512Mi"}}}}
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
    assert cap_violations({("Deployment", "demo")}, releases, vpas) == ([], 0)
