"""Only the control plane may write tenant Endpoints or EndpointSlices: hand-written
ones point Traefik at another namespace's pods. No CEL engine ships here, so
`allowed()` mirrors the validation; the first test pins its shape."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-no-manual-endpoints"
EXPRESSION = (
    "request.userInfo.username == 'system:kube-controller-manager' || "
    "request.userInfo.username.startsWith('system:serviceaccount:kube-system:')"
)
MESSAGE = "tenant Endpoints and EndpointSlices are managed by the control plane only"
TENANT_MATCH = {
    "namespaceSelector": {
        "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
    }
}


def _docs(kind: str) -> list[dict]:
    return [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == kind
        and d["metadata"]["name"] == NAME
    ]


def allowed(username: str) -> bool:
    return username == "system:kube-controller-manager" or username.startswith(
        "system:serviceaccount:kube-system:"
    )


def test_the_policy_is_the_shape_the_mirror_evaluates():
    found = _docs("ValidatingAdmissionPolicy")
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["endpoints"],
        },
        {
            "apiGroups": ["discovery.k8s.io"],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["endpointslices"],
        },
    ]
    assert [v["expression"] for v in spec["validations"]] == [EXPRESSION]
    assert [v["message"] for v in spec["validations"]] == [MESSAGE]


def test_the_binding_denies_on_every_tenant_namespace():
    found = _docs("ValidatingAdmissionPolicyBinding")
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["policyName"] == NAME
    assert spec["validationActions"] == ["Deny"]
    assert spec["matchResources"] == TENANT_MATCH


# k3s runs kube-controller-manager with use-service-account-credentials, so
# each controller writes as its own kube-system ServiceAccount.
@pytest.mark.parametrize(
    "username",
    [
        "system:serviceaccount:kube-system:endpoint-controller",
        "system:serviceaccount:kube-system:endpointslice-controller",
        "system:serviceaccount:kube-system:endpointslicemirroring-controller",
        "system:kube-controller-manager",
    ],
)
def test_the_control_plane_is_admitted(username):
    assert allowed(username)


@pytest.mark.parametrize(
    "username",
    [
        "system:serviceaccount:demo:flux-reconciler",
        "system:serviceaccount:demo:default",
        "system:serviceaccount:kube-systemx:endpoint-controller",
        "tenant-admin@example.com",
        "system:kube-controller-manager-evil",
    ],
)
def test_anyone_else_is_refused(username):
    assert not allowed(username)
