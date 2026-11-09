"""Only the control plane may write tenant Endpoints or EndpointSlices: hand-written
ones point Traefik at another namespace's pods. `allowed()` reads the usernames
out of the shipped validation, and the first test pins its shape."""

from __future__ import annotations

import re

import pytest
from conftest import REPO, assert_tenant_deny_binding, policy_spec

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-no-manual-endpoints"
CONTROLLERS = [
    "system:serviceaccount:kube-system:endpoint-controller",
    "system:serviceaccount:kube-system:endpointslice-controller",
    "system:serviceaccount:kube-system:endpointslicemirroring-controller",
]
EXPRESSION = (
    "request.userInfo.username == 'system:kube-controller-manager' || "
    "request.userInfo.username in ["
    + ", ".join(f"'{c}'" for c in CONTROLLERS)
    + "]"
)
MESSAGE = "tenant Endpoints and EndpointSlices are managed by the control plane only"


# A disjunction of `username == '<name>'` and `username in ['<name>', ...]`.
_USERNAME = r"request\.userInfo\.username"
_NAMES = r"'[^']+'(?:, '[^']+')*"
_CLAUSE = rf"{_USERNAME} (?:== '[^']+'|in \[{_NAMES}\])"
_ADMIT_ONLY = re.compile(rf"{_CLAUSE}(?: \|\| {_CLAUSE})*")


def admitted(expression: str) -> set[str]:
    """The exact usernames an admit-list expression lets through."""
    text = " ".join(expression.split())
    assert _ADMIT_ONLY.fullmatch(text), f"not an exact-username admit list: {text}"
    return set(re.findall(r"'([^']+)'", text))


def allowed(username: str) -> bool:
    """The shipped validation, evaluated on one request username."""
    [validation] = policy_spec(POLICY, "ValidatingAdmissionPolicy", NAME)["validations"]
    return username in admitted(validation["expression"])


def test_a_widened_expression_is_not_read_as_an_admit_list():
    """Mutation case: a prefix match would admit `<controller>-evil`."""
    with pytest.raises(AssertionError, match="not an exact-username admit list"):
        admitted("request.userInfo.username.startsWith('system:kube-controller-manager')")


def test_the_policy_is_the_shape_the_mirror_evaluates():
    spec = policy_spec(POLICY, "ValidatingAdmissionPolicy", NAME)
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
    assert_tenant_deny_binding(POLICY, NAME)


# k3s runs kube-controller-manager with use-service-account-credentials, so
# each controller writes as its own kube-system ServiceAccount; only these three.
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
        "system:serviceaccount:kube-system:default",
        "system:serviceaccount:kube-system:coredns",
        "system:serviceaccount:kube-system:endpoint-controller-evil",
        "tenant-admin@example.com",
        "system:kube-controller-manager-evil",
    ],
)
def test_anyone_else_is_refused(username):
    assert not allowed(username)
