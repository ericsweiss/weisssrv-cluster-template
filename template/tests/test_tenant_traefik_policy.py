"""Tenant forwardAuth, failover, highestRandomWeight, per-service middleware and
parentRefs stay in the tenant's namespace, the platform allowlist or absent.
No CEL engine ships here, so the mirrors are pinned to the expressions they assume."""

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


REDIS_MESSAGE = (
    "a tenant rateLimit middleware may not name a Redis backend"
    " (Traefik would connect to it with its own egress)"
)
REDIS_EXPRESSION = "!has(object.spec.rateLimit) || !has(object.spec.rateLimit.redis)"


def test_the_redis_refusal_is_the_shape_the_mirror_evaluates():
    found = [v for v in _object_refs()["validations"] if v["message"] == REDIS_MESSAGE]
    assert len(found) == 1
    assert " ".join(found[0]["expression"].split()) == REDIS_EXPRESSION


def redis_allowed(spec: dict) -> bool:
    return "rateLimit" not in spec or "redis" not in spec["rateLimit"]


def test_a_rate_limit_with_a_redis_backend_is_refused():
    """Traefik, not the tenant, opens the Redis connection, with its own egress."""
    assert not redis_allowed({"rateLimit": {"average": 10, "redis": {"endpoints": ["10.0.0.5:6379"]}}})


def test_an_in_memory_rate_limit_is_untouched():
    assert redis_allowed({"rateLimit": {"average": 10, "burst": 20}})


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


# Every per-service middlewares[] list the pinned Traefik CRDs (v3.7) define, and
# highestRandomWeight.services[], held to the route-level rule.
MIDDLEWARE_REF = (
    "!m.name.contains('@') && (!has(m.namespace) || m.namespace == variables.ns"
    " || (m.namespace + '/' + m.name) in variables.platformMiddlewares)"
)
HRW_EXPRESSION = (
    "!has(object.spec.highestRandomWeight) || !has(object.spec.highestRandomWeight.services)"
    " || object.spec.highestRandomWeight.services.all(s,"
    " (!has(s.namespace) || s.namespace == variables.ns) && !s.name.contains('@')"
    " && (!has(s.serversTransport) || !s.serversTransport.contains('@')))"
)
HRW_MESSAGE = (
    "a tenant highestRandomWeight service may reference only its own namespace,"
    " never a name@provider service or transport"
)
SERVICE_MIDDLEWARES_EXPRESSION = (
    "(!has(object.spec.errors) || !has(object.spec.errors.service)"
    " || !has(object.spec.errors.service.middlewares)"
    f" || object.spec.errors.service.middlewares.all(m, {MIDDLEWARE_REF}))"
    " && (!has(object.spec.weighted) || !has(object.spec.weighted.services)"
    " || object.spec.weighted.services.all(s, !has(s.middlewares)"
    f" || s.middlewares.all(m, {MIDDLEWARE_REF})))"
    " && (!has(object.spec.highestRandomWeight) || !has(object.spec.highestRandomWeight.services)"
    " || object.spec.highestRandomWeight.services.all(s, !has(s.middlewares)"
    f" || s.middlewares.all(m, {MIDDLEWARE_REF})))"
    " && (!has(object.spec.mirroring) || ((!has(object.spec.mirroring.middlewares)"
    f" || object.spec.mirroring.middlewares.all(m, {MIDDLEWARE_REF}))"
    " && (!has(object.spec.mirroring.mirrors) || object.spec.mirroring.mirrors.all(s,"
    f" !has(s.middlewares) || s.middlewares.all(m, {MIDDLEWARE_REF})))))"
)
SERVICE_MIDDLEWARES_MESSAGE = (
    "a tenant errors, weighted, highestRandomWeight or mirroring service may apply"
    " only its own middlewares or the platform allowlist, never a name@provider one"
)
ROUTE_SERVICE_MIDDLEWARES_EXPRESSION = (
    "!has(object.spec.routes) || object.spec.routes.all(r, !has(r.services)"
    " || r.services.all(s, !has(s.middlewares)"
    f" || s.middlewares.all(m, {MIDDLEWARE_REF})))"
)
ROUTE_SERVICE_MIDDLEWARES_MESSAGE = (
    "a tenant route service may apply only its own middlewares or the platform"
    " allowlist, never a name@provider one"
)
PLATFORM_MW = "traefik/security-headers"


def _policy(name: str) -> dict:
    docs = [d for d in yaml.safe_load_all(POLICY.read_text()) if isinstance(d, dict)]
    found = [d for d in docs if d.get("kind") == "ValidatingAdmissionPolicy"
             and d["metadata"]["name"] == name]
    assert len(found) == 1
    return found[0]["spec"]


def _pinned(policy: str, message: str) -> str:
    found = [v for v in _policy(policy)["validations"] if v["message"] == message]
    assert len(found) == 1, message
    return " ".join(found[0]["expression"].split())


def _platform_middlewares(policy: str) -> list[str]:
    variables = {v["name"]: v["expression"] for v in _policy(policy).get("variables") or []}
    return ast.literal_eval(" ".join(variables["platformMiddlewares"].split()))


def _mw_ok(m: dict, ns: str) -> bool:
    if "@" in m["name"]:
        return False
    if "namespace" not in m or m["namespace"] == ns:
        return True
    return f"{m['namespace']}/{m['name']}" in _platform_middlewares("tenant-traefik-object-refs")


def _service_lists(spec: dict) -> list[list[dict]]:
    """Each middlewares list the object-refs mirror walks, as the CEL does."""
    lists = [(spec.get("errors", {}).get("service") or {}).get("middlewares") or []]
    for key in ("weighted", "highestRandomWeight"):
        lists += [s.get("middlewares") or [] for s in (spec.get(key) or {}).get("services") or []]
    mirroring = spec.get("mirroring") or {}
    lists.append(mirroring.get("middlewares") or [])
    lists += [s.get("middlewares") or [] for s in mirroring.get("mirrors") or []]
    return lists


def service_middlewares_allowed(spec: dict, ns: str) -> bool:
    return all(_mw_ok(m, ns) for mws in _service_lists(spec) for m in mws)


def route_service_middlewares_allowed(spec: dict, ns: str) -> bool:
    return all(_mw_ok(m, ns) for r in spec.get("routes") or []
               for s in r.get("services") or [] for m in s.get("middlewares") or [])


def hrw_allowed(spec: dict, ns: str) -> bool:
    return all(
        s.get("namespace", ns) == ns and "@" not in s["name"]
        and "@" not in s.get("serversTransport", "")
        for s in (spec.get("highestRandomWeight") or {}).get("services") or []
    )


def test_the_service_reference_validations_are_the_shapes_the_mirrors_evaluate():
    assert _pinned("tenant-traefik-object-refs", HRW_MESSAGE) == HRW_EXPRESSION
    assert (_pinned("tenant-traefik-object-refs", SERVICE_MIDDLEWARES_MESSAGE)
            == SERVICE_MIDDLEWARES_EXPRESSION)
    assert (_pinned("tenant-traefik-route-refs", ROUTE_SERVICE_MIDDLEWARES_MESSAGE)
            == ROUTE_SERVICE_MIDDLEWARES_EXPRESSION)


def test_both_policies_allow_the_same_platform_middlewares():
    assert (_platform_middlewares("tenant-traefik-object-refs")
            == _platform_middlewares("tenant-traefik-route-refs"))
    assert PLATFORM_MW in _platform_middlewares("tenant-traefik-object-refs")


@pytest.mark.parametrize(
    "service",
    [{"name": "web", "namespace": "observability"}, {"name": "api@internal"},
     {"name": "web", "serversTransport": "insecure@file"}],
    ids=["cross-namespace", "provider-service", "provider-transport"],
)
def test_a_foreign_highest_random_weight_service_is_refused(service):
    assert not hrw_allowed({"highestRandomWeight": {"services": [service]}}, "demo")


def test_an_own_highest_random_weight_service_is_admitted():
    assert hrw_allowed({"highestRandomWeight": {"services": [
        {"name": "web", "port": 80}, {"name": "web-b", "namespace": "demo"}]}}, "demo")


def _placed(where: str, middlewares: list[dict]) -> dict:
    """A TraefikService or Middleware spec carrying `middlewares` at `where`."""
    service = {"name": "web", "port": 80, "middlewares": middlewares}
    return {
        "errors": {"errors": {"service": service}},
        "weighted": {"weighted": {"services": [service]}},
        "highestRandomWeight": {"highestRandomWeight": {"services": [service]}},
        "mirroring": {"mirroring": {"name": "web", "middlewares": middlewares}},
        "mirrors": {"mirroring": {"name": "web", "mirrors": [service]}},
    }[where]


PLACES = ["errors", "weighted", "highestRandomWeight", "mirroring", "mirrors"]
FOREIGN = [{"name": "mw", "namespace": "other"}, {"name": "mw@file"},
           {"name": "mw@kubernetescrd", "namespace": "demo"},
           {"name": "lan-tailscale-only", "namespace": "authentik"}]
OWN_OR_PLATFORM = [{"name": "mw"}, {"name": "mw", "namespace": "demo"},
                   {"name": "security-headers", "namespace": "traefik"}]


@pytest.mark.parametrize("middleware", FOREIGN, ids=lambda m: m["name"] + "/" + m.get("namespace", "-"))
@pytest.mark.parametrize("where", PLACES)
def test_a_foreign_per_service_middleware_is_refused(where, middleware):
    assert not service_middlewares_allowed(_placed(where, [middleware]), "demo")


@pytest.mark.parametrize("where", PLACES)
def test_own_and_platform_per_service_middlewares_are_admitted(where):
    assert service_middlewares_allowed(_placed(where, OWN_OR_PLATFORM), "demo")


@pytest.mark.parametrize("middleware", FOREIGN, ids=lambda m: m["name"] + "/" + m.get("namespace", "-"))
def test_a_foreign_route_service_middleware_is_refused(middleware):
    spec = {"routes": [{"match": "Host(`demo.apps.test`)", "services": [
        {"name": "web", "port": 80, "middlewares": [middleware]}]}]}
    assert not route_service_middlewares_allowed(spec, "demo")


def test_own_and_platform_route_service_middlewares_are_admitted():
    spec = {"routes": [{"match": "Host(`demo.apps.test`)", "services": [
        {"name": "web", "port": 80, "middlewares": OWN_OR_PLATFORM}]}]}
    assert route_service_middlewares_allowed(spec, "demo")


# Traefik v3.7 IngressRoute spec.parentRefs attach a route under another one
# (multi-layer routing), so a tenant route may name none.
PARENT_REFS_EXPRESSION = "!has(object.spec.parentRefs) || size(object.spec.parentRefs) == 0"
PARENT_REFS_MESSAGE = (
    "tenant routes may not attach to parent routes; multi-layer routing is platform-owned"
)


def parent_refs_allowed(spec: dict) -> bool:
    """The parentRefs validation."""
    return "parentRefs" not in spec or len(spec["parentRefs"]) == 0


def test_the_parent_refs_refusal_is_the_shape_the_mirror_evaluates():
    assert _pinned("tenant-traefik-route-refs", PARENT_REFS_MESSAGE) == PARENT_REFS_EXPRESSION


@pytest.mark.parametrize(
    "refs",
    [[{"name": "edge", "namespace": "traefik"}], [{"name": "own"}],
     [{"name": "own", "namespace": "demo"}]],
    ids=["platform-parent", "own-parent", "own-namespace-parent"],
)
def test_a_tenant_route_with_parent_refs_is_refused_whatever_it_names(refs):
    assert not parent_refs_allowed({"routes": [], "parentRefs": refs})


def test_a_tenant_route_without_parent_refs_is_admitted():
    assert parent_refs_allowed({"routes": []})
    assert parent_refs_allowed({"routes": [], "parentRefs": []})


# Skipped: an UPDATE to a terminating object that leaves spec and annotations
# unchanged; the annotations clause keeps the name@provider check out of reach.
FINALIZING_EXPRESSION = (
    "request.operation != 'UPDATE' || !has(object.metadata.deletionTimestamp) || "
    "object.spec != oldObject.spec || (has(object.metadata.annotations) ? "
    "object.metadata.annotations : {}) != (has(oldObject.metadata.annotations) ? "
    "oldObject.metadata.annotations : {})"
)


def test_the_ingress_refs_policy_lets_a_terminating_object_shed_its_finalizer():
    spec = _policy("tenant-traefik-ingress-refs")
    assert list(spec)[:3] == ["failurePolicy", "matchConstraints", "matchConditions"]
    assert [(c["name"], " ".join(c["expression"].split())) for c in spec["matchConditions"]] == [
        ("enforce-unless-finalizing-unchanged", FINALIZING_EXPRESSION)
    ]
