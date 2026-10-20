"""Only cert-manager's controller may send a tenant CertificateRequest past a core
Issuer or create an ACME Order or Challenge; Orders alone match UPDATE, where the
garbage collector is admitted too. `allowed()` mirrors the policy."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
CERT_MANAGER = REPO / "kubernetes" / "infrastructure" / "controllers" / "cert-manager"
NAME = "tenant-certificaterequests"
CONTROLLER = "system:serviceaccount:cert-manager:cert-manager"
GARBAGE_COLLECTOR = "system:serviceaccount:kube-system:generic-garbage-collector"
VALIDATIONS = [
    (
        "request.resource.group == 'acme.cert-manager.io'"
        " || request.userInfo.username == 'system:serviceaccount:cert-manager:cert-manager'"
        " || ((!has(object.spec.issuerRef.group)"
        " || object.spec.issuerRef.group in ['', 'cert-manager.io'])"
        " && (!has(object.spec.issuerRef.kind) || object.spec.issuerRef.kind in ['', 'Issuer']))",
        "tenant certificate requests may use a platform ClusterIssuer (via a validated"
        " Certificate) or a cert-manager.io Issuer in their own namespace only",
    ),
    (
        "request.resource.group != 'acme.cert-manager.io'"
        " || request.userInfo.username == 'system:serviceaccount:cert-manager:cert-manager'"
        " || (request.operation == 'UPDATE' && request.userInfo.username"
        " == 'system:serviceaccount:kube-system:generic-garbage-collector')",
        "tenant ACME Orders and Challenges are created by cert-manager's controller only",
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
                "operations": ["CREATE"],
                "resources": ["certificaterequests"],
            },
            {
                "apiGroups": ["acme.cert-manager.io"],
                "apiVersions": ["*"],
                "operations": ["CREATE"],
                "resources": ["challenges"],
            },
            {
                "apiGroups": ["acme.cert-manager.io"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["orders"],
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


MATCHED = {
    ("cert-manager.io", "certificaterequests"): {"CREATE"},
    ("acme.cert-manager.io", "challenges"): {"CREATE"},
    ("acme.cert-manager.io", "orders"): {"CREATE", "UPDATE"},
}


def allowed(
    issuer_ref: dict,
    username: str,
    group: str = "cert-manager.io",
    resource: str | None = None,
    operation: str = "CREATE",
) -> bool:
    """The policy, evaluated on a request's API group, resource, operation,
    issuerRef and requester; an unmatched operation is admitted untouched."""
    acme = group == "acme.cert-manager.io"
    resource = resource or ("orders" if acme else "certificaterequests")
    if operation not in MATCHED[(group, resource)]:
        return True
    core_issuer = issuer_ref.get("group", "") in ("", "cert-manager.io") and issuer_ref.get(
        "kind", ""
    ) in ("", "Issuer")
    return (acme or username == CONTROLLER or core_issuer) and (
        not acme
        or username == CONTROLLER
        or (operation == "UPDATE" and username == GARBAGE_COLLECTOR)
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
        {"name": "pca", "kind": "AWSPCAClusterIssuer", "group": "awspca.cert-manager.io"},
        {"name": "pca", "kind": "AWSPCAIssuer", "group": "awspca.cert-manager.io"},
        {"name": "own-ca", "kind": "Issuer", "group": "example.io"},
        {"name": "x", "kind": "SomeClusterIssuer"},
    ],
    ids=["external-cluster-issuer", "external-issuer", "external-group", "other-kind"],
)
def test_a_tenant_request_to_an_external_issuer_kind_is_refused(issuer_ref):
    """An external issuer plugin's own kinds, cluster-scoped ones in particular,
    would sign a tenant CSR outside its hostname grant, so only cert-manager's
    core namespaced Issuer is open to a tenant-authored request."""
    assert not allowed(issuer_ref, TENANT)


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
    assert not allowed(issuer_ref, TENANT, ACME, "orders")
    assert not allowed(issuer_ref, TENANT, ACME, "challenges")


def test_a_tenant_order_update_is_refused():
    """Order spec is frozen only in spec.request by the cert-manager webhook
    (internal/apis/acme/validation/order.go), so Order UPDATE stays matched."""
    assert not allowed(PROD, TENANT, ACME, "orders", "UPDATE")


@pytest.mark.parametrize(
    "group,resource",
    [("cert-manager.io", "certificaterequests"), (ACME, "challenges")],
    ids=["certificaterequest", "challenge"],
)
def test_a_non_controller_update_of_a_frozen_spec_kind_is_admitted(group, resource):
    """The cert-manager webhook refuses spec changes on CertificateRequests and
    Challenges after creation, so the policy matches CREATE only and a
    finalizer removal by the garbage collector or a tenant is not refused."""
    for username in (TENANT, GARBAGE_COLLECTOR):
        assert allowed(PROD, username, group, resource, "UPDATE")


def test_the_garbage_collector_may_update_an_order():
    """foregroundDeletion finalizer removal must not wedge an Order's deletion."""
    assert allowed(PROD, GARBAGE_COLLECTOR, ACME, "orders", "UPDATE")


@pytest.mark.parametrize("resource", ["orders", "challenges"])
def test_the_garbage_collector_may_not_create_an_acme_object(resource):
    """Its exception covers the finalizer UPDATE only, never a CREATE."""
    assert not allowed(PROD, GARBAGE_COLLECTOR, ACME, resource, "CREATE")


def test_the_controllers_acme_order_and_challenge_writes_are_admitted():
    assert allowed(PROD, CONTROLLER, ACME)
    assert allowed({"name": "own-ca", "kind": "Issuer"}, CONTROLLER, ACME)
