"""A tenant Flux Kustomization or HelmRelease names a ServiceAccount and a source
in its own namespace, so the controller never applies it as itself.
No CEL engine ships here, so `allowed()` mirrors the pinned validations."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import (
    REPO,
    TENANT_LABEL,
    assert_all_parsed,
    assert_tenant_deny_binding,
    cel,
    k8s_documents,
    policy_spec,
)

CONFIGS = REPO / "kubernetes" / "infrastructure" / "configs"
POLICY = CONFIGS / "tenant-flux-policy.yaml"
NAME = "tenant-flux-impersonation"
FINALIZING_UNCHANGED = (
    "request.operation != 'UPDATE' || !has(object.metadata.deletionTimestamp) || "
    "object.spec != oldObject.spec || (has(object.metadata.annotations) ? "
    "object.metadata.annotations : {}) != (has(oldObject.metadata.annotations) ? "
    "oldObject.metadata.annotations : {})"
)
VALIDATIONS = [
    {
        "expression": "has(object.spec.serviceAccountName) && object.spec.serviceAccountName != ''",
        "message": "a tenant Kustomization or HelmRelease must set spec.serviceAccountName",
    },
    {
        "expression": "!variables.isKustomization || !has(object.spec.sourceRef.namespace) || "
        "object.spec.sourceRef.namespace == variables.ns",
        "message": "a tenant Kustomization may reference a source only in its own namespace",
    },
    {
        "expression": "variables.isKustomization || ((!has(object.spec.chart) || "
        "!has(object.spec.chart.spec.sourceRef.namespace) || "
        "object.spec.chart.spec.sourceRef.namespace == variables.ns) && "
        "(!has(object.spec.chartRef) || !has(object.spec.chartRef.namespace) || "
        "object.spec.chartRef.namespace == variables.ns))",
        "message": "a tenant HelmRelease may reference a chart source only in its own namespace",
    },
]
FLUX_KINDS = {
    ("kustomize.toolkit.fluxcd.io", "Kustomization"),
    ("helm.toolkit.fluxcd.io", "HelmRelease"),
}


def allowed(kind: str, spec: dict, ns: str = "demo") -> bool:
    """The three VALIDATIONS, on one Kustomization or HelmRelease.

    test_the_validations_are_the_shape_the_mirror_evaluates pins them to the policy."""
    if not spec.get("serviceAccountName"):
        return False
    if kind == "Kustomization":
        return (spec.get("sourceRef") or {}).get("namespace", ns) == ns
    chart_ref = ((spec.get("chart") or {}).get("spec") or {}).get("sourceRef") or {}
    return chart_ref.get("namespace", ns) == ns and (
        (spec.get("chartRef") or {}).get("namespace", ns) == ns
    )


def test_the_configs_stage_applies_the_policy():
    kustomization = CONFIGS / "kustomization.yaml"
    listed = (yaml.safe_load(kustomization.read_text(encoding="utf-8")) or {}).get("resources") or []
    assert POLICY.name in listed


def _flux_system() -> Path:
    """The one cluster's flux-system directory, whatever the cluster is named."""
    found = sorted((REPO / "kubernetes" / "clusters").glob("*/flux-system"))
    assert len(found) == 1, f"expected one kubernetes/clusters/*/flux-system, found {found}"
    return found[0]


def _controller_args(name: str) -> list[str]:
    """Container args of a Flux controller after the flux-system JSON6902 add patches.

    gotk-components.yaml exists only after `flux bootstrap`; before it, the
    patches alone are the args this repository adds."""
    flux_system = _flux_system()
    components = flux_system / "gotk-components.yaml"
    args: list[str] = []
    if components.is_file():
        deploy = next(
            d for d in yaml.safe_load_all(components.read_text())
            if isinstance(d, dict) and d.get("kind") == "Deployment"
            and d["metadata"]["name"] == name
        )
        args = list(deploy["spec"]["template"]["spec"]["containers"][0].get("args", []))
    for patch in yaml.safe_load((flux_system / "kustomization.yaml").read_text())["patches"]:
        target = patch.get("target") or {}
        if target.get("kind") != "Deployment" or target.get("name") != name:
            continue
        ops = yaml.safe_load(patch["patch"])
        if not isinstance(ops, list):
            continue
        args += [op["value"] for op in ops if op.get("op") == "add"
                 and op.get("path") == "/spec/template/spec/containers/0/args/-"]
    return args


def test_no_kustomization_fetches_a_remote_base():
    """Every tree builds from its own reviewed source; the controller refuses a
    resources entry that names a URL."""
    assert "--no-remote-bases=true" in _controller_args("kustomize-controller")


def test_tenant_alerts_and_receivers_cannot_reach_platform_objects():
    """A tenant holds admin over notification.toolkit.fluxcd.io in its namespace;
    without the flag an Alert streams flux-system events and a Receiver triggers
    platform reconciles."""
    assert "--no-cross-namespace-refs=true" in _controller_args("notification-controller")


def test_the_policy_fails_closed_on_both_flux_kinds():
    spec = policy_spec(POLICY, "ValidatingAdmissionPolicy", NAME)
    assert spec["failurePolicy"] == "Fail"
    assert "paramKind" not in spec
    assert spec["matchConstraints"] == {"resourceRules": [
        {"apiGroups": ["kustomize.toolkit.fluxcd.io"], "apiVersions": ["v1"],
         "operations": ["CREATE", "UPDATE"], "resources": ["kustomizations"]},
        {"apiGroups": ["helm.toolkit.fluxcd.io"], "apiVersions": ["v2"],
         "operations": ["CREATE", "UPDATE"], "resources": ["helmreleases"]},
    ]}
    assert [(c["name"], cel(c["expression"])) for c in spec["matchConditions"]] == [
        ("enforce-unless-finalizing-unchanged", FINALIZING_UNCHANGED)
    ]
    assert [(v["name"], cel(v["expression"])) for v in spec["variables"]] == [
        ("ns", "object.metadata.namespace"),
        ("isKustomization", "request.resource.resource == 'kustomizations'"),
    ]


def test_the_validations_are_the_shape_the_mirror_evaluates():
    validations = [
        {"expression": cel(v["expression"]), "message": v["message"]}
        for v in policy_spec(POLICY, "ValidatingAdmissionPolicy", NAME)["validations"]
    ]
    assert validations == VALIDATIONS, "the policy changed; update allowed() and VALIDATIONS"


def test_the_binding_denies_in_every_tenant_namespace():
    assert_tenant_deny_binding(POLICY, NAME)


def flux_objects_in_tenant_namespaces(root: Path) -> tuple[set[str], list[dict], list[str]]:
    """One walk of `root`: the tenant-labelled Namespaces, the Flux objects that
    target one, and the Flux objects that sit inside one."""
    documents, unreadable = k8s_documents(root)
    assert_all_parsed(unreadable, "Flux object or tenant Namespace")
    tenants = {
        doc["metadata"]["name"] for _, doc in documents
        if doc.get("kind") == "Namespace"
        and TENANT_LABEL in ((doc.get("metadata") or {}).get("labels") or {})
    }
    flux = [
        (path, doc) for path, doc in documents
        if (str(doc.get("apiVersion", "")).split("/")[0], doc.get("kind")) in FLUX_KINDS
    ]
    wiring = [d for _, d in flux if (d.get("spec") or {}).get("targetNamespace") in tenants]
    inside = [
        f"{path.relative_to(root)}: {doc['metadata']['name']}"
        for path, doc in flux
        if (doc.get("metadata") or {}).get("namespace") in tenants
    ]
    return tenants, wiring, inside


def test_no_in_tree_flux_object_sits_in_a_tenant_namespace():
    """Each tenant's wiring reconciles from a namespace the binding never matches,
    and nothing else in this tree lands where the policy refuses later writes."""
    tenants, wiring, inside = flux_objects_in_tenant_namespaces(REPO / "kubernetes")
    if not tenants:
        pytest.skip("no tenant onboarded yet; no tenant namespace to check")
    assert wiring, "no Flux object targets a tenant namespace; this check covers nothing"
    assert inside == []


def _write_tree(root: Path, docs: list[dict]) -> Path:
    (root / "tenant.yaml").write_text(yaml.safe_dump_all(docs))
    return root


def test_the_in_tree_walk_finds_a_flux_object_inside_a_tenant_namespace(tmp_path):
    """Mutation case: the walk the in-tree check runs reports a Kustomization
    placed in a tenant-labelled Namespace, and passes its wiring outside it."""
    namespace = {"apiVersion": "v1", "kind": "Namespace",
                 "metadata": {"name": "demo", "labels": {TENANT_LABEL: "demo"}}}
    wiring = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
              "metadata": {"name": "demo", "namespace": "flux-system"},
              "spec": {"targetNamespace": "demo"}}
    stray = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
             "metadata": {"name": "stray", "namespace": "demo"}, "spec": {}}
    tenants, found, inside = flux_objects_in_tenant_namespaces(
        _write_tree(tmp_path, [namespace, wiring]))
    assert (tenants, found, inside) == ({"demo"}, [wiring], [])
    _, _, inside = flux_objects_in_tenant_namespaces(
        _write_tree(tmp_path, [namespace, wiring, stray]))
    assert inside == ["tenant.yaml: stray"]


SOURCE = {"kind": "GitRepository", "name": "app"}
CHART = {"spec": {"chart": "web", "sourceRef": {"kind": "HelmRepository", "name": "r"}}}


@pytest.mark.parametrize(
    ("kind", "spec", "admitted"),
    [
        ("Kustomization", {"serviceAccountName": "deployer", "sourceRef": SOURCE}, True),
        ("Kustomization", {"sourceRef": SOURCE}, False),
        ("Kustomization", {"serviceAccountName": "", "sourceRef": SOURCE}, False),
        ("Kustomization", {"serviceAccountName": "deployer",
                           "sourceRef": {**SOURCE, "namespace": "demo"}}, True),
        ("Kustomization", {"serviceAccountName": "deployer",
                           "sourceRef": {**SOURCE, "namespace": "flux-system"}}, False),
        ("HelmRelease", {"serviceAccountName": "deployer", "chart": CHART}, True),
        ("HelmRelease", {"chart": CHART}, False),
        ("HelmRelease", {"serviceAccountName": "deployer", "chart": {"spec": {
            "chart": "web", "sourceRef": {"kind": "HelmRepository", "name": "r",
                                          "namespace": "flux-system"}}}}, False),
        ("HelmRelease", {"serviceAccountName": "deployer", "chartRef": {
            "kind": "OCIRepository", "name": "web", "namespace": "demo"}}, True),
        ("HelmRelease", {"serviceAccountName": "deployer", "chartRef": {
            "kind": "OCIRepository", "name": "web", "namespace": "other"}}, False),
    ],
    ids=[
        "kustomization-own", "kustomization-no-sa", "kustomization-empty-sa",
        "kustomization-same-ns-source", "kustomization-foreign-source",
        "helmrelease-own", "helmrelease-no-sa", "helmrelease-foreign-chart-source",
        "helmrelease-own-chart-ref", "helmrelease-foreign-chart-ref",
    ],
)
def test_the_mirrored_policy_refuses_what_it_should(kind, spec, admitted):
    assert allowed(kind, spec) is admitted
