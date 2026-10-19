"""Traefik's crd provider (v3.7 buildTLSOptions/buildTLSStores) drops a `default`
TLSOption or TLSStore defined in two namespaces, so tenants may not name one so.
`allowed()` mirrors the validation; the first test pins the shape it assumes."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
PLATFORM_TLS = REPO / "kubernetes" / "infrastructure" / "configs" / "traefik-tls-options.yaml"
NAME = "tenant-traefik-tls-defaults"
MESSAGE = "the Traefik default TLSOption/TLSStore is platform-owned"
EXPRESSION = "object.metadata.name != 'default'"


def _docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(POLICY.read_text()) if isinstance(d, dict)]


def _policy() -> dict:
    found = [
        d for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == NAME
    ]
    assert len(found) == 1
    return found[0]["spec"]


def allowed(name: str) -> bool:
    """The validation, evaluated on an object's metadata.name."""
    return name != "default"


def test_the_validation_is_the_shape_the_mirror_evaluates():
    validations = _policy()["validations"]
    assert [(v["expression"], v["message"]) for v in validations] == [(EXPRESSION, MESSAGE)]


def test_the_policy_matches_tls_options_and_stores_on_every_write():
    assert _policy()["matchConstraints"] == {
        "resourceRules": [
            {
                "apiGroups": ["traefik.io"],
                "apiVersions": ["*"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["tlsoptions", "tlsstores"],
            }
        ]
    }


def test_the_policy_is_deny_bound_on_every_tenant_namespace():
    bindings = [
        d["spec"] for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and d["spec"]["policyName"] == NAME
    ]
    assert bindings == [
        {
            "policyName": NAME,
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
                }
            },
        }
    ]


def test_the_platform_default_tls_option_is_what_the_policy_protects():
    platform = [
        d for d in yaml.safe_load_all(PLATFORM_TLS.read_text())
        if isinstance(d, dict) and d.get("kind") == "TLSOption"
    ]
    assert [(d["metadata"]["name"], d["metadata"]["namespace"]) for d in platform] == [
        ("default", "traefik")
    ]


def test_a_tenant_default_is_refused():
    assert not allowed("default")


@pytest.mark.parametrize("name", ["legacy-tls12", "demo", "default-tls", "my-default"])
def test_any_other_name_is_admitted(name):
    assert allowed(name)
