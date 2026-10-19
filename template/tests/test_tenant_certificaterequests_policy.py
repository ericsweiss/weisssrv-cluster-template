"""Only cert-manager's controller may send a tenant CertificateRequest to a
ClusterIssuer (admission cannot read the CSR) or write an ACME Order or
Challenge. `allowed()` mirrors the validations; the first tests pin its shape."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
CERT_MANAGER = REPO / "kubernetes" / "infrastructure" / "controllers" / "cert-manager"
NAME = "tenant-certificaterequests"
CONTROLLER = "system:serviceaccount:cert-manager:cert-manager"
VALIDATIONS = [
    (
        "request.resource.group == 'acme.cert-manager.io'"
        " || !has(object.spec.issuerRef.kind) || object.spec.issuerRef.kind != 'ClusterIssuer'"
        " || request.userInfo.username == 'system:serviceaccount:cert-manager:cert-manager'",
        "tenant CertificateRequests to platform ClusterIssuers are created by cert-manager"
        " from a validated Certificate only",
    ),
    (
        "request.resource.group != 'acme.cert-manager.io'"
        " || request.userInfo.username == 'system:serviceaccount:cert-manager:cert-manager'",
        "tenant ACME Orders and Challenges are written by cert-manager's controller only",
    ),
]
TENANT_MATCH = {
    "namespaceSelector": {
        "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
    }
}


def _docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(POLICY.read_text()) if isinstance(d, dict)]


def _policy() -> dict:
    found = [
        d for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == NAME
    ]
    assert len(found) == 1
    return found[0]["spec"]


def test_the_policy_is_the_shape_the_mirror_evaluates():
    spec = _policy()
    assert spec["failurePolicy"] == "Fail"
    assert "paramKind" not in spec
    assert "variables" not in spec
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {
                "apiGroups": ["cert-manager.io"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["certificaterequests"],
            },
            {
                "apiGroups": ["acme.cert-manager.io"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["orders", "challenges"],
            },
        ]
    }
    assert [
        (" ".join(v["expression"].split()), v["message"]) for v in spec["validations"]
    ] == VALIDATIONS


def test_the_policy_is_deny_bound_on_every_tenant_namespace():
    bindings = [
        d["spec"] for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and d["spec"]["policyName"] == NAME
    ]
    assert bindings == [
        {"policyName": NAME, "validationActions": ["Deny"], "matchResources": TENANT_MATCH}
    ]


def test_the_controller_identity_is_the_release_the_platform_ships():
    """The chart names the controller's ServiceAccount after the release
    (templates/deployment.yaml, serviceAccountName) in the release namespace,
    so an override of either changes the identity the policy admits."""
    release = next(
        d for d in yaml.safe_load_all((CERT_MANAGER / "release.yaml").read_text())
        if isinstance(d, dict) and d.get("kind") == "HelmRelease"
    )
    assert release["metadata"]["namespace"] == "cert-manager"
    assert release["spec"]["releaseName"] == "cert-manager"
    values = release["spec"]["values"]
    assert "serviceAccount" not in values
    assert "fullnameOverride" not in values and "nameOverride" not in values
    assert "namespace" not in values
    assert CONTROLLER == "system:serviceaccount:cert-manager:cert-manager"


def allowed(issuer_ref: dict, username: str, group: str = "cert-manager.io") -> bool:
    """The validations, evaluated on a request's API group, issuerRef and requester."""
    acme = group == "acme.cert-manager.io"
    return (acme or issuer_ref.get("kind") != "ClusterIssuer" or username == CONTROLLER) and (
        not acme or username == CONTROLLER
    )


TENANT = "system:serviceaccount:example-app:example-app-flux"
PROD = {"name": "letsencrypt-prod", "kind": "ClusterIssuer", "group": "cert-manager.io"}


def test_a_tenant_authored_request_to_a_cluster_issuer_is_refused():
    """Tenants hold `admin`, which cert-manager's default aggregateClusterRoles
    widens with certificaterequests write, so authorization does not stop this."""
    assert not allowed(PROD, TENANT)
    assert not allowed({**PROD, "name": "letsencrypt-staging"}, "alice")


def test_the_controllers_request_to_a_cluster_issuer_is_admitted():
    assert allowed(PROD, CONTROLLER)


@pytest.mark.parametrize(
    "issuer_ref",
    [
        {"name": "own-ca", "kind": "Issuer", "group": "cert-manager.io"},
        {"name": "own-ca"},
    ],
    ids=["issuer", "default-kind"],
)
def test_a_tenant_request_to_its_own_issuer_is_admitted(issuer_ref):
    assert allowed(issuer_ref, TENANT)


ACME = "acme.cert-manager.io"


@pytest.mark.parametrize(
    "issuer_ref",
    [PROD, {"name": "own-ca", "kind": "Issuer", "group": "cert-manager.io"}, {"name": "own-ca"}],
    ids=["cluster-issuer", "issuer", "default-kind"],
)
def test_a_tenant_authored_acme_order_or_challenge_is_refused(issuer_ref):
    """Defence in depth: cert-manager-edit grants no create on orders and the
    cert-manager webhook refuses Challenge spec changes, so this is not a live
    bypass; the policy holds if a future RBAC change widens either."""
    assert not allowed(issuer_ref, TENANT, ACME)


def test_the_controllers_acme_order_and_challenge_writes_are_admitted():
    assert allowed(PROD, CONTROLLER, ACME)
    assert allowed({"name": "own-ca", "kind": "Issuer"}, CONTROLLER, ACME)
