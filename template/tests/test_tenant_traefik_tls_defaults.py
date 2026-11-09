"""Traefik's crd provider (v3.7 buildTLSOptions/buildTLSStores) drops a `default`
TLSOption or TLSStore defined in two namespaces, so tenants may not name one so."""

from __future__ import annotations

import yaml
from conftest import REPO, assert_tenant_deny_binding, policy_spec

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
PLATFORM_TLS = REPO / "kubernetes" / "infrastructure" / "configs" / "traefik-tls-options.yaml"
NAME = "tenant-traefik-tls-defaults"
MESSAGE = "the Traefik default TLSOption/TLSStore is platform-owned"
EXPRESSION = "object.metadata.name != 'default'"


def _policy() -> dict:
    return policy_spec(POLICY, "ValidatingAdmissionPolicy", NAME)


def test_the_validation_refuses_exactly_the_default_name():
    """An exact match, so a tenant's own `default-tls` option is never caught."""
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
    assert_tenant_deny_binding(POLICY, NAME)


def test_the_platform_default_tls_option_is_what_the_policy_protects():
    platform = [
        d for d in yaml.safe_load_all(PLATFORM_TLS.read_text())
        if isinstance(d, dict) and d.get("kind") == "TLSOption"
    ]
    assert [(d["metadata"]["name"], d["metadata"]["namespace"]) for d in platform] == [
        ("default", "traefik")
    ]
