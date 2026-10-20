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
# Traefik decodes annotation fields case-insensitively, so the policy lowercases keys.
# The Ingress annotations that would reshape the routing rule Traefik builds from
# spec.rules, or raise its priority over a platform route.
ROUTING = [
    "traefik.ingress.kubernetes.io/router.rule",
    "traefik.ingress.kubernetes.io/router.priority",
    "traefik.ingress.kubernetes.io/router.rulesyntax",
]
# Another ingress operator, such as Tailscale's, publishes an Ingress of its own
# class without Traefik, so a tenant Ingress is Traefik's or nobody's.
TRAEFIK_CLASS = "traefik"
LEGACY_CLASS = "kubernetes.io/ingress.class"
TAILSCALE = "tailscale.com/"
VARIABLES = [
    {"name": "isRoute", "expression": "request.resource.resource == 'ingressroutes'"},
    {
        "name": "hosts",
        "expression": "params.data.hosts.split('\\n').map(h, h.trim()).filter(h, h != '')",
    },
    {"name": "hostMatchers", "expression": "variables.hosts.map(h, 'Host(`' + h + '`)')"},
]
# A wildcard in the grant would let one Ingress rule or TLS host cover many names.
EXACT_GRANT = {
    "expression": "variables.hosts.all(h, !h.contains('*'))",
    "message": "the platform grant must list exact hostnames, no wildcards",
}
VALIDATIONS = [
    EXACT_GRANT,
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
            f" !k.lowerAscii().startsWith('{TLS_DOMAINS}')"
            " || object.metadata.annotations[k].split(',').all(d, d.trim() in variables.hosts))"
        ),
        "message": (
            "a tenant Ingress may list only router.tls.domains the platform granted this namespace"
        ),
    },
    {
        "expression": (
            "variables.isRoute || !has(object.metadata.annotations)"
            " || object.metadata.annotations.all(k,"
            " !(k.lowerAscii() in ['" + "', '".join(ROUTING) + "']))"
        ),
        "message": (
            "tenant Ingresses may not override the routing rule or priority by annotation;"
            " use spec.rules with granted hosts"
        ),
    },
    {
        "expression": (
            "variables.isRoute || ((!has(object.spec) || !has(object.spec.ingressClassName)"
            f" || object.spec.ingressClassName == '{TRAEFIK_CLASS}')"
            " && (!has(object.metadata.annotations) || object.metadata.annotations.all(k,"
            f" !k.startsWith('{TAILSCALE}')"
            f" && (k != '{LEGACY_CLASS}' || object.metadata.annotations[k] == '{TRAEFIK_CLASS}'))))"
        ),
        "message": (
            "tenant Ingresses may be handled only by Traefik, not another ingress operator"
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
    """The grant check and the three IngressRoute validations, on `grant`."""
    if any("*" in h for h in hosts(grant)):
        return False
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
    """The grant check and the six Ingress validations, evaluated on `grant`."""
    granted = hosts(grant)
    annotations = annotations or {}
    return (
        spec.get("ingressClassName", TRAEFIK_CLASS) == TRAEFIK_CLASS
        and annotations.get(LEGACY_CLASS, TRAEFIK_CLASS) == TRAEFIK_CLASS
        and not any(k.startswith(TAILSCALE) for k in annotations)
        and not any("*" in h for h in granted)
        and
        all(
            d.strip() in granted
            for k, v in annotations.items() if k.lower().startswith(TLS_DOMAINS)
            for d in v.split(",")
        )
        and not any(k.lower() in ROUTING for k in annotations)
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


WILDCARD_GRANTS = {
    "wildcard-only": {"hosts": "*.apps.test\n"},
    "wildcard-beside-exact": {"hosts": "demo.apps.test\n*.apps.test\n"},
    "bare-star": {"hosts": "demo.apps.test\n *\n"},
}


@pytest.mark.parametrize("grant", sorted(WILDCARD_GRANTS))
def test_a_wildcard_grant_refuses_every_route_and_ingress(grant):
    """An operator-written wildcard must not let an Ingress `*.apps.test` rule
    route every name under it, so a grant with one admits nothing."""
    granted = WILDCARD_GRANTS[grant]
    assert not route_allowed({"routes": [ROUTE]}, granted)
    for host in ("demo.apps.test", "*.apps.test", "*"):
        assert not ingress_allowed({"rules": [{"host": host}]}, granted)
        assert not ingress_allowed({"tls": [{"hosts": [host]}]}, granted)


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


@pytest.mark.parametrize(
    "key",
    [*ROUTING, "traefik.ingress.kubernetes.io/Router.Priority"],
    ids=["rule", "priority", "rulesyntax", "mixed-case-priority"],
)
def test_an_ingress_routing_override_annotation_is_refused(key):
    """Each would let a granted-host Ingress outrank or reshape a platform route."""
    assert not ingress_allowed(
        {"rules": [{"host": "demo.apps.test"}]}, annotations={key: "1000"}
    )


def test_a_mixed_case_ingress_tls_domain_annotation_is_still_held_to_the_grant():
    assert not ingress_allowed(
        {"rules": [{"host": "demo.apps.test"}]},
        annotations={"traefik.ingress.kubernetes.io/Router.TLS.Domains.0.main": "auth.apps.test"},
    )


@pytest.mark.parametrize(
    "key",
    ["router.entrypoints", "router.pathmatcher", "router.middlewares", "router.tls",
     "router.tls.certresolver", "router.tls.options", "router.observability.accesslogs"],
)
def test_the_remaining_router_annotations_are_admitted(key):
    """None of these changes the Host the rule is built from or its priority."""
    assert ingress_allowed(
        {"rules": [{"host": "demo.apps.test"}]},
        annotations={f"traefik.ingress.kubernetes.io/{key}": "x"},
    )


GRANTED_RULES = {"rules": [{"host": "demo.apps.test"}]}


@pytest.mark.parametrize(
    ("spec", "annotations"),
    [
        ({**GRANTED_RULES, "ingressClassName": "tailscale"}, None),
        ({**GRANTED_RULES, "ingressClassName": "nginx"}, None),
        (GRANTED_RULES, {LEGACY_CLASS: "tailscale"}),
        ({**GRANTED_RULES, "ingressClassName": "traefik"}, {LEGACY_CLASS: "nginx"}),
        (GRANTED_RULES, {"tailscale.com/funnel": "true"}),
        ({**GRANTED_RULES, "ingressClassName": "traefik"}, {"tailscale.com/tags": "tag:k8s"}),
    ],
    ids=["class-tailscale", "class-other", "legacy-annotation-tailscale",
         "legacy-annotation-other", "tailscale-annotation", "tailscale-annotation-on-traefik"],
)
def test_an_ingress_for_another_operator_is_refused(spec, annotations):
    """Tailscale's operator would publish it to the tailnet with no Traefik."""
    assert not ingress_allowed(spec, annotations=annotations)


@pytest.mark.parametrize(
    ("spec", "annotations"),
    [
        (GRANTED_RULES, None),
        ({**GRANTED_RULES, "ingressClassName": "traefik"}, None),
        (GRANTED_RULES, {LEGACY_CLASS: "traefik"}),
        ({**GRANTED_RULES, "ingressClassName": "traefik"}, {LEGACY_CLASS: "traefik"}),
    ],
    ids=["class-absent", "class-traefik", "legacy-annotation-traefik", "both-traefik"],
)
def test_a_traefik_or_default_class_ingress_is_admitted(spec, annotations):
    assert ingress_allowed(spec, annotations=annotations)


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
