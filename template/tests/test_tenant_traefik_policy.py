"""A tenant forwardAuth Middleware may call only its own namespace's Services or
the Authentik outpost, and a tenant TraefikService may not fail over. No CEL
engine ships here, so the mirrors are pinned to the expressions they assume."""

from __future__ import annotations

import ast
import re

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
AUTHENTIK = REPO / "kubernetes" / "apps" / "authentik" / "middleware.yaml"
TRAEFIK = REPO / "kubernetes" / "infrastructure" / "controllers" / "traefik" / "release.yaml"
MESSAGE = "a tenant forwardAuth middleware may call only a Service in its own namespace"
EXPRESSION = (
    "!has(object.spec.forwardAuth)"
    " || object.spec.forwardAuth.address in variables.platformForwardAuth"
    " || object.spec.forwardAuth.address.matches(variables.ownServiceAddress)"
)
# Traefik v3's failover service and fallback each carry a full service reference
# (name, namespace, serversTransport, middlewares), so tenants get none.
FAILOVER_EXPRESSION = "!has(object.spec.failover)"
FAILOVER_MESSAGE = "tenant failover services are not supported; use weighted services"
OUTPOST = "http://authentik-server.authentik.svc.cluster.local/outpost.goauthentik.io/auth/traefik"


def _object_refs() -> dict:
    docs = [d for d in yaml.safe_load_all(POLICY.read_text()) if isinstance(d, dict)]
    found = [
        d for d in docs
        if d.get("kind") == "ValidatingAdmissionPolicy"
        and d["metadata"]["name"] == "tenant-traefik-object-refs"
    ]
    assert len(found) == 1
    return found[0]["spec"]


def _variables() -> dict[str, str]:
    return {v["name"]: v["expression"] for v in _object_refs().get("variables") or []}


def allowed(spec: dict, ns: str) -> bool:
    """The validation, evaluated on the shipped variables."""
    if "forwardAuth" not in spec:
        return True
    variables = _variables()
    address = spec["forwardAuth"]["address"]
    if address in ast.literal_eval(variables["platformForwardAuth"]):
        return True
    parts = re.fullmatch(
        r"'([^']*)'\s*\+\s*variables\.ns\s*\+\s*'([^']*)'", variables["ownServiceAddress"]
    )
    assert parts, variables["ownServiceAddress"]
    # CEL matches() is an unanchored RE2 search; the pattern anchors itself.
    return re.search(parts.group(1) + ns + parts.group(2), address) is not None


def test_the_validation_is_the_shape_the_mirror_evaluates():
    found = [v for v in _object_refs()["validations"] if v["message"] == MESSAGE]
    assert len(found) == 1
    assert found[0]["expression"] == EXPRESSION


def test_the_outpost_allowlist_is_the_platform_middleware_address():
    """The allowlist names exactly what the platform authentik-auth calls."""
    assert ast.literal_eval(_variables()["platformForwardAuth"]) == [OUTPOST]
    addresses = {
        d["spec"]["forwardAuth"]["address"]
        for d in yaml.safe_load_all(AUTHENTIK.read_text())
        if isinstance(d, dict) and "forwardAuth" in (d.get("spec") or {})
    }
    assert addresses == {OUTPOST}


@pytest.mark.parametrize(
    "address",
    [
        "http://oauth2-proxy.demo.svc",
        "http://oauth2-proxy.demo.svc/oauth2/auth",
        "http://oauth2-proxy.demo.svc:4180/oauth2/auth",
        "https://oauth2-proxy.demo.svc.cluster.local/oauth2/auth",
        "http://oauth2-proxy.demo.svc.cluster.local:4180",
        OUTPOST,
    ],
)
def test_an_own_namespace_or_outpost_address_is_admitted(address):
    assert allowed({"forwardAuth": {"address": address}}, "demo")


@pytest.mark.parametrize(
    "address",
    [
        "http://grafana.observability.svc/api/admin",
        "http://grafana.observability.svc.cluster.local:3000/",
        "http://oauth2-proxy.demo.svc.observability.svc/",
        "http://oauth2-proxy.demo.svc@grafana.observability.svc/",
        "http://oauth2-proxy.demo.svc.evil.example/",
        "http://oauth2-proxy.demo-other.svc/",
        "http://10.43.0.10:8080/",
        "http://auth.example.com/verify",
        "ftp://oauth2-proxy.demo.svc/",
        "http://authentik-server.authentik.svc.cluster.local/api/v3/",
    ],
)
def test_another_namespace_or_outside_address_is_refused(address):
    assert not allowed({"forwardAuth": {"address": address}}, "demo")


def test_a_middleware_without_forward_auth_is_untouched():
    assert allowed({"headers": {"customResponseHeaders": {"X-Frame-Options": "DENY"}}}, "demo")


def test_traefik_reads_default_tls_objects_only_from_its_own_namespace():
    """Second layer behind tenant-traefik-tls-defaults: a `default` TLSOption or
    TLSStore outside `traefik` is ignored even if the policy goes missing."""
    release = yaml.safe_load(TRAEFIK.read_text())
    crd = release["spec"]["values"]["providers"]["kubernetesCRD"]
    assert crd.get("defaultTLSResourcesNamespace") == "traefik"


def failover_allowed(spec: dict) -> bool:
    """The failover validation."""
    return "failover" not in spec


def test_the_failover_refusal_is_the_shape_the_mirror_evaluates():
    found = [v for v in _object_refs()["validations"] if v["message"] == FAILOVER_MESSAGE]
    assert len(found) == 1
    assert " ".join(found[0]["expression"].split()) == FAILOVER_EXPRESSION


@pytest.mark.parametrize(
    "failover",
    [
        {"service": {"name": "web", "port": 80}, "fallback": {"name": "web-b", "port": 80}},
        {"service": {"name": "web", "port": 80},
         "fallback": {"name": "grafana", "namespace": "observability", "port": 3000}},
        {"service": {"name": "api@internal", "kind": "TraefikService"},
         "fallback": {"name": "web", "port": 80}},
        {"service": {"name": "web", "port": 80, "middlewares": [
            {"name": "mw", "namespace": "other"}]}, "fallback": {"name": "web", "port": 80}},
    ],
    ids=["own-namespace", "cross-namespace-fallback", "provider-service", "foreign-middleware"],
)
def test_a_tenant_failover_service_is_refused_whatever_it_names(failover):
    """Refused whole, so no field of either reference can reach outside."""
    assert not failover_allowed({"failover": failover})


def test_a_weighted_or_mirroring_service_is_untouched_by_the_failover_refusal():
    assert failover_allowed({"weighted": {"services": [{"name": "web", "port": 80}]}})
    assert failover_allowed({"mirroring": {"name": "web", "mirrors": [{"name": "web"}]}})
