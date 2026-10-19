"""A tenant Certificate may name only its granted hostnames, from a platform
ClusterIssuer or its own Issuer. No CEL engine ships here, so `allowed()`
mirrors the validations; the first tests pin their shape."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
ISSUERS = REPO / "kubernetes" / "infrastructure" / "configs" / "cluster-issuer.yaml"
NAME = "tenant-certificates"
WRITES = ["CREATE", "UPDATE"]
PLATFORM_ISSUERS = ["letsencrypt-prod", "letsencrypt-staging"]
VARIABLES = [
    {
        "name": "hosts",
        "expression": "params.data.hosts.split('\\n').map(h, h.trim()).filter(h, h != '')",
    },
    {"name": "platformIssuers", "expression": "['letsencrypt-prod', 'letsencrypt-staging']"},
]
VALIDATIONS = [
    {
        "expression": (
            "(!has(object.spec.dnsNames) || object.spec.dnsNames.all(n,"
            " !n.contains('*') && n in variables.hosts))"
            " && (!has(object.spec.commonName) || (!object.spec.commonName.contains('*')"
            " && object.spec.commonName in variables.hosts))"
        ),
        "message": "a tenant may request certificates only for hostnames granted this namespace",
    },
    {
        "expression": (
            "!has(object.spec.literalSubject) && !has(object.spec.ipAddresses)"
            " && !has(object.spec.uris) && !has(object.spec.emailAddresses)"
            " && !has(object.spec.otherNames)"
        ),
        "message": (
            "a tenant certificate may name only DNS hostnames"
            " (no literalSubject, IP, URI, email or otherName)"
        ),
    },
    {
        "expression": (
            "(!has(object.spec.issuerRef.group) || object.spec.issuerRef.group in"
            " ['', 'cert-manager.io']) && (has(object.spec.issuerRef.kind)"
            " && object.spec.issuerRef.kind == 'ClusterIssuer'"
            " ? object.spec.issuerRef.name in variables.platformIssuers"
            " : !has(object.spec.issuerRef.kind) || object.spec.issuerRef.kind in ['', 'Issuer'])"
        ),
        "message": (
            "a tenant certificate may use only a platform ClusterIssuer or an Issuer"
            " in its own namespace"
        ),
    },
]
GRANT = {"hosts": "demo.apps.test\n  demo.home.test \n\n"}
ISSUER = {"name": "letsencrypt-prod", "kind": "ClusterIssuer", "group": "cert-manager.io"}


def _docs(kind: str, name: str) -> list[dict]:
    return [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == kind and d["metadata"]["name"] == name
    ]


def _cel(text: str) -> str:
    return " ".join(text.split())


def test_the_certificate_policy_is_the_shape_the_mirror_evaluates():
    found = _docs("ValidatingAdmissionPolicy", NAME)
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["paramKind"] == {"apiVersion": "v1", "kind": "ConfigMap"}
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": ["cert-manager.io"], "apiVersions": ["*"], "operations": WRITES,
             "resources": ["certificates"]},
        ]
    }
    assert [
        {"name": v["name"], "expression": _cel(v["expression"])} for v in spec["variables"]
    ] == VARIABLES
    assert [
        {"expression": _cel(v["expression"]), "message": v["message"]}
        for v in spec["validations"]
    ] == VALIDATIONS


def test_the_certificate_policy_has_no_shared_binding():
    """Each tenant's binding names its own grant, so none ships here."""
    assert not [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and d["spec"]["policyName"] == NAME
    ]


def test_the_platform_issuers_are_the_cluster_issuers_shipped():
    shipped = sorted(
        d["metadata"]["name"] for d in yaml.safe_load_all(ISSUERS.read_text())
        if isinstance(d, dict) and d.get("kind") == "ClusterIssuer"
    )
    assert shipped == PLATFORM_ISSUERS


def allowed(spec: dict, grant: dict = GRANT) -> bool:
    """The three validations, evaluated on `grant`."""
    granted = [h.strip() for h in grant["hosts"].split("\n") if h.strip()]
    names = spec.get("dnsNames", []) + ([spec["commonName"]] if "commonName" in spec else [])
    if not all("*" not in n and n in granted for n in names):
        return False
    if any(k in spec for k in
           ("literalSubject", "ipAddresses", "uris", "emailAddresses", "otherNames")):
        return False
    ref = spec["issuerRef"]
    if ref.get("group", "") not in ("", "cert-manager.io"):
        return False
    if ref.get("kind") == "ClusterIssuer":
        return ref["name"] in PLATFORM_ISSUERS
    return ref.get("kind", "") in ("", "Issuer")


@pytest.mark.parametrize(
    "spec",
    [
        {"dnsNames": ["demo.apps.test"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test", "demo.home.test"], "commonName": "demo.apps.test",
         "issuerRef": {**ISSUER, "name": "letsencrypt-staging"}},
        {"dnsNames": ["demo.apps.test"], "issuerRef": {"name": "own-ca"}},
        {"dnsNames": ["demo.apps.test"], "issuerRef": {"name": "own-ca", "kind": "Issuer"}},
    ],
    ids=["prod", "staging-with-cn", "own-issuer-default-kind", "own-issuer"],
)
def test_a_granted_certificate_is_admitted(spec):
    assert allowed(spec)


@pytest.mark.parametrize(
    "spec",
    [
        {"dnsNames": ["auth.apps.test"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test", "auth.apps.test"], "issuerRef": ISSUER},
        {"dnsNames": ["*.apps.test"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "commonName": "auth.apps.test", "issuerRef": ISSUER},
        {"commonName": "*.apps.test", "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "literalSubject": "CN=auth.apps.test",
         "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "ipAddresses": ["10.0.0.1"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "uris": ["spiffe://x"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "emailAddresses": ["a@b"], "issuerRef": ISSUER},
        {"dnsNames": ["demo.apps.test"], "issuerRef": {**ISSUER, "name": "other-ca"}},
        {"dnsNames": ["demo.apps.test"], "issuerRef": {**ISSUER, "group": "example.io"}},
        {"dnsNames": ["demo.apps.test"], "issuerRef": {"name": "x", "kind": "AwsPCAIssuer"}},
    ],
    ids=[
        "ungranted", "one-ungranted", "wildcard", "ungranted-cn", "wildcard-cn",
        "literal-subject", "ip", "uri", "email", "other-cluster-issuer", "external-group",
        "other-kind",
    ],
)
def test_an_ungranted_certificate_is_refused(spec):
    assert not allowed(spec)
