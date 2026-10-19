"""A tenant may route only the hostnames the platform granted its namespace, at
no raised priority, and may not route TCP or UDP at all. No CEL engine ships
here, so `allowed()` mirrors the validations; the first tests pin their shape."""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-traefik-hostnames"
WRITES = ["CREATE", "UPDATE"]
# The leading matcher, and every Host matcher, in a tenant route rule.
LEADING_HOST = "^Host[(]`[^`]+`[)]( && .*)?$"
ANY_HOST = "Host[(]`[^`]*`[)]"
# The Traefik Ingress annotations that name the domains a router's certificate covers.
TLS_DOMAINS = "traefik.ingress.kubernetes.io/router.tls.domains."
VARIABLES = [
    {"name": "isRoute", "expression": "request.resource.resource == 'ingressroutes'"},
    {
        "name": "hosts",
        "expression": "params.data.hosts.split('\\n').map(h, h.trim()).filter(h, h != '')",
    },
    {"name": "hostMatchers", "expression": "variables.hosts.map(h, 'Host(`' + h + '`)')"},
]
VALIDATIONS = [
    {
        "expression": (
            "!variables.isRoute || !has(object.spec.routes) || object.spec.routes.all(r,"
            f" r.match.matches('{LEADING_HOST}') && !r.match.contains('||')"
            " && !r.match.contains('HostRegexp(')"
            f" && r.match.findAll('{ANY_HOST}').all(h, h in variables.hostMatchers))"
        ),
        "message": (
            "a tenant route must open with Host() on a hostname the platform granted"
            " this namespace, and may not use ||, HostRegexp or any other Host"
        ),
    },
    {
        "expression": (
            "!variables.isRoute || !has(object.spec.routes) || object.spec.routes.all(r,"
            " !has(r.priority) || ('maxPriority' in params.data"
            " && r.priority <= int(params.data.maxPriority)))"
        ),
        "message": (
            "a tenant route may set a priority only up to the maxPriority the platform"
            " granted this namespace"
        ),
    },
    {
        "expression": (
            "!variables.isRoute || !has(object.spec.tls) || !has(object.spec.tls.domains)"
            " || object.spec.tls.domains.all(d, (!has(d.main) || d.main in variables.hosts)"
            " && (!has(d.sans) || d.sans.all(s, s in variables.hosts)))"
        ),
        "message": (
            "a tenant route may list only TLS domains the platform granted this namespace"
        ),
    },
    {
        "expression": (
            "variables.isRoute || !has(object.spec) || !has(object.spec.defaultBackend)"
        ),
        "message": "a tenant Ingress may not set a default backend (it answers every hostname)",
    },
    {
        "expression": (
            "variables.isRoute || !has(object.spec) || !has(object.spec.rules)"
            " || object.spec.rules.all(r, has(r.host) && r.host in variables.hosts)"
        ),
        "message": "every tenant Ingress rule must name a host the platform granted this namespace",
    },
    {
        "expression": (
            "variables.isRoute || !has(object.spec) || !has(object.spec.tls)"
            " || object.spec.tls.all(t, !has(t.hosts) || t.hosts.all(h, h in variables.hosts))"
        ),
        "message": "a tenant Ingress may list only TLS hosts the platform granted this namespace",
    },
    {
        "expression": (
            "variables.isRoute || !has(object.metadata.annotations)"
            " || object.metadata.annotations.all(k,"
            f" !k.startsWith('{TLS_DOMAINS}')"
            " || object.metadata.annotations[k].split(',').all(d, d.trim() in variables.hosts))"
        ),
        "message": (
            "a tenant Ingress may list only router.tls.domains the platform granted this namespace"
        ),
    },
]
GRANT = {"hosts": "demo.apps.test\n  demo.home.test \n\n"}


def _docs(kind: str, name: str) -> list[dict]:
    return [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == kind and d["metadata"]["name"] == name
    ]


def _cel(text: str) -> str:
    return " ".join(text.split())


def test_the_hostname_policy_is_the_shape_the_mirror_evaluates():
    found = _docs("ValidatingAdmissionPolicy", NAME)
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["paramKind"] == {"apiVersion": "v1", "kind": "ConfigMap"}
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": ["traefik.io"], "apiVersions": ["*"], "operations": WRITES,
             "resources": ["ingressroutes"]},
            {"apiGroups": ["networking.k8s.io"], "apiVersions": ["*"], "operations": WRITES,
             "resources": ["ingresses"]},
        ]
    }
    assert [
        {"name": v["name"], "expression": _cel(v["expression"])} for v in spec["variables"]
    ] == VARIABLES
    assert [
        {"expression": _cel(v["expression"]), "message": v["message"]}
        for v in spec["validations"]
    ] == VALIDATIONS


def test_the_hostname_policy_has_no_shared_binding():
    """Each tenant's binding names its own grant, so none ships here."""
    assert not [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and d["spec"]["policyName"] == NAME
    ]


def hosts(grant: dict) -> list[str]:
    return [h.strip() for h in grant["hosts"].split("\n") if h.strip()]


def route_allowed(spec: dict, grant: dict = GRANT) -> bool:
    """The three IngressRoute validations, evaluated on `grant`."""
    matchers = [f"Host(`{h}`)" for h in hosts(grant)]
    for domain in spec.get("tls", {}).get("domains", []):
        if "main" in domain and domain["main"] not in hosts(grant):
            return False
        if not all(s in hosts(grant) for s in domain.get("sans", [])):
            return False
    for route in spec.get("routes", []):
        rule = route["match"]
        # CEL matches() is an unanchored RE2 search; the pattern anchors itself.
        if not re.search(LEADING_HOST, rule) or "||" in rule or "HostRegexp(" in rule:
            return False
        if not all(h in matchers for h in re.findall(ANY_HOST, rule)):
            return False
        if "priority" in route and not (
            "maxPriority" in grant and route["priority"] <= int(grant["maxPriority"])
        ):
            return False
    return True


def ingress_allowed(spec: dict, grant: dict = GRANT, annotations: dict | None = None) -> bool:
    """The four Ingress validations, evaluated on `grant`."""
    granted = hosts(grant)
    return (
        all(
            d.strip() in granted
            for k, v in (annotations or {}).items() if k.startswith(TLS_DOMAINS)
            for d in v.split(",")
        )
        and "defaultBackend" not in spec
        and all("host" in r and r["host"] in granted for r in spec.get("rules", []))
        and all(h in granted for t in spec.get("tls", []) for h in t.get("hosts", []))
    )


@pytest.mark.parametrize(
    "rule",
    [
        "Host(`demo.apps.test`)",
        "Host(`demo.home.test`)",
        "Host(`demo.apps.test`) && PathPrefix(`/api`)",
        "Host(`demo.apps.test`) && !PathPrefix(`/metrics`)",
    ],
)
def test_a_granted_host_route_is_admitted(rule):
    assert route_allowed({"routes": [{"match": rule}]})


@pytest.mark.parametrize(
    "rule",
    [
        "Host(`grafana.apps.test`)",
        "PathPrefix(`/`)",
        "ClientIP(`0.0.0.0/0`)",
        "HostRegexp(`.+`)",
        "Host(`demo.apps.test`) || PathPrefix(`/`)",
        "Host(`demo.apps.test`) || Host(`grafana.apps.test`)",
        "Host(`demo.apps.test`) && HostRegexp(`.+`)",
        "Host(`demo.apps.test`) && Host(`grafana.apps.test`)",
        "Host(`demo.apps.test`, `grafana.apps.test`)",
        "!Host(`demo.apps.test`)",
        "PathPrefix(`/`) && Host(`demo.apps.test`)",
        "(Host(`demo.apps.test`))",
        "Host(`DEMO.apps.test`)",
        "Host(`demo.apps.test.evil.test`)",
        "Host(``)",
    ],
)
def test_an_ungranted_or_hostless_route_is_refused(rule):
    assert not route_allowed({"routes": [{"match": rule}]})


def test_one_shadowing_route_refuses_the_whole_object():
    assert not route_allowed(
        {"routes": [{"match": "Host(`demo.apps.test`)"}, {"match": "PathPrefix(`/`)"}]}
    )


ROUTE = {"match": "Host(`demo.apps.test`)"}


def test_granted_route_tls_domains_are_admitted():
    assert route_allowed(
        {"routes": [ROUTE],
         "tls": {"domains": [{"main": "demo.apps.test", "sans": ["demo.home.test"]}]}}
    )


@pytest.mark.parametrize(
    "domain",
    [
        {"main": "auth.apps.test"},
        {"main": "*.apps.test"},
        {"main": "demo.apps.test", "sans": ["auth.apps.test"]},
        {"sans": ["*.home.test"]},
    ],
    ids=["ungranted-main", "wildcard-main", "ungranted-san", "wildcard-san"],
)
def test_an_ungranted_route_tls_domain_is_refused(domain):
    """A domain on the route asks Traefik for a certificate on that name."""
    assert not route_allowed({"routes": [ROUTE], "tls": {"domains": [domain]}})


def test_a_priority_needs_a_granted_ceiling():
    route = {"match": "Host(`demo.apps.test`)", "priority": 10}
    assert not route_allowed({"routes": [route]})
    assert route_allowed({"routes": [route]}, {**GRANT, "maxPriority": "10"})
    assert not route_allowed({"routes": [route]}, {**GRANT, "maxPriority": "9"})


def test_a_granted_host_ingress_is_admitted():
    assert ingress_allowed(
        {
            "rules": [{"host": "demo.apps.test", "http": {}}],
            "tls": [{"hosts": ["demo.apps.test"], "secretName": "demo-tls"}],
        }
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"rules": [{"http": {}}]},
        {"rules": [{"host": "grafana.apps.test"}]},
        {"rules": [{"host": "*.apps.test"}]},
        {"defaultBackend": {"service": {"name": "demo", "port": {"number": 80}}}},
        {"rules": [{"host": "demo.apps.test"}], "tls": [{"hosts": ["grafana.apps.test"]}]},
    ],
    ids=["hostless-rule", "ungranted-host", "wildcard-host", "default-backend", "ungranted-tls"],
)
def test_an_ungranted_or_hostless_ingress_is_refused(spec):
    assert not ingress_allowed(spec)


def test_granted_ingress_tls_domain_annotations_are_admitted():
    assert ingress_allowed(
        {"rules": [{"host": "demo.apps.test"}]},
        annotations={
            f"{TLS_DOMAINS}0.main": "demo.apps.test",
            f"{TLS_DOMAINS}0.sans": "demo.home.test, demo.apps.test",
            "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
        },
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("0.main", "auth.apps.test"),
        ("0.main", "*.apps.test"),
        ("0.sans", "demo.home.test,auth.apps.test"),
        ("1.sans", ""),
    ],
    ids=["ungranted-main", "wildcard-main", "ungranted-san", "empty"],
)
def test_an_ungranted_ingress_tls_domain_annotation_is_refused(key, value):
    assert not ingress_allowed(
        {"rules": [{"host": "demo.apps.test"}]}, annotations={f"{TLS_DOMAINS}{key}": value}
    )


def test_tenant_tcp_and_udp_routes_are_refused_outright():
    found = _docs("ValidatingAdmissionPolicy", "tenant-no-tcp-udp-routes")
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": ["traefik.io"], "apiVersions": ["*"], "operations": WRITES,
             "resources": ["ingressroutetcps", "ingressrouteudps"]},
        ]
    }
    assert spec["validations"] == [
        {
            "expression": "false",
            "message": "tenants serve HTTP only; ask the platform for a TCP route",
        }
    ]
    bindings = _docs("ValidatingAdmissionPolicyBinding", "tenant-no-tcp-udp-routes")
    assert [b["spec"] for b in bindings] == [
        {
            "policyName": "tenant-no-tcp-udp-routes",
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
                }
            },
        }
    ]
