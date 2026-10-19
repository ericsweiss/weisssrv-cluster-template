"""audit-tenant-admission.sh reports tenant objects of every policy-covered kind
that predate the admission policies. kubectl is a PATH stub answering from a
fixture, so each case pins how the script reads `kubectl get -o json` output."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import REPO, require_tool

SCRIPT = REPO / "scripts" / "audit-tenant-admission.sh"
POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
CONTROLLER = "endpointslice-controller.k8s.io"
CERT_MANAGER_SA = "system:serviceaccount:cert-manager:cert-manager"

STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["AUDIT_FIXTURE"]) as fh:
        data = json.load(fh)
    if data.get("fail") and data["fail"] in args:
        sys.exit("stub kubectl: refused")
    ns = None
    if args[:1] == ["-n"]:
        ns, args = args[1], args[2:]
    assert args[0] == "get" and args[-2:] == ["-o", "json"], args
    kind = args[1].split(".")[0]
    if kind == "namespaces":
        assert args[2:4] == ["-l", "fluxcd.io/tenant"], args
        items = [{"metadata": {"name": n}} for n in data["tenants"]]
    else:
        items = data["objects"].get(ns, {}).get(kind, [])
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
    """
)


def service(
    name: str,
    selector: dict | None = None,
    kind: str = "ClusterIP",
    external_ips: list[str] | None = None,
) -> dict:
    spec: dict = {"type": kind}
    if selector is not None:
        spec["selector"] = selector
    if external_ips is not None:
        spec["externalIPs"] = external_ips
    return {"metadata": {"name": name}, "spec": spec}


def slice_(
    name: str,
    managed_by: str | None = CONTROLLER,
    service_name: str | None = "web",
    addresses: tuple[str, ...] = ("10.42.0.5",),
) -> dict:
    labels = {} if managed_by is None else {"endpointslice.kubernetes.io/managed-by": managed_by}
    if service_name is not None:
        labels["kubernetes.io/service-name"] = service_name
    return {
        "metadata": {"name": name, "labels": labels},
        "addressType": "IPv4",
        "endpoints": [{"addresses": [a]} for a in addresses],
    }


def endpoints(name: str, addresses: tuple[str, ...] = ()) -> dict:
    subsets = [{"addresses": [{"ip": a} for a in addresses]}] if addresses else []
    return {"metadata": {"name": name}, "subsets": subsets}


def pod(name: str, labels: dict, ip: str, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"phase": phase, "podIP": ip, "podIPs": [{"ip": ip}]},
    }


def run(tmp_path: Path, fixture: dict) -> subprocess.CompletedProcess:
    require_tool("jq", "audit-tenant-admission")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "fixture.json").write_text(json.dumps(fixture))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AUDIT_FIXTURE": str(tmp_path / "fixture.json"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def named(name: str, spec: dict | None = None, annotations: dict | None = None) -> dict:
    metadata: dict = {"name": name}
    if annotations is not None:
        metadata["annotations"] = annotations
    return {"metadata": metadata, "spec": spec or {}}


def grant(ns: str = "demo", hosts: str = "demo.apps.test\n  demo.home.test \n\n",
          max_priority: str | None = "100") -> dict:
    data = {"hosts": hosts}
    if max_priority is not None:
        data["maxPriority"] = max_priority
    return {"metadata": {"name": f"tenant-routing-{ns}"}, "data": data}


PROD = {"name": "letsencrypt-prod", "kind": "ClusterIssuer", "group": "cert-manager.io"}
ROUTE = {
    "match": "Host(`demo.apps.test`) && PathPrefix(`/`)",
    "priority": 100,
    "services": [{"name": "web", "port": 80}],
    "middlewares": [
        {"name": "own-auth"},
        {"name": "own-chain", "namespace": "demo"},
        {"name": "lan-tailscale-only", "namespace": "traefik"},
    ],
}
CLEAN_APP = {
    "services": [service("web", {"app": "web"})],
    "endpointslices": [slice_("web-abc12")],
    "endpoints": [endpoints("web", ("10.42.0.5",))],
    "pods": [pod("web-0", {"app": "web", "pod-template-hash": "x"}, "10.42.0.5")],
    "ingressroutes": [
        named("web", {
            "routes": [ROUTE],
            "tls": {
                "domains": [{"main": "demo.apps.test", "sans": ["demo.home.test"]}],
                "options": {"name": "modern"},
                "store": {"name": "own", "namespace": "demo"},
            },
        })
    ],
    "ingresses": [
        named(
            "web",
            {"ingressClassName": "traefik", "rules": [{"host": "demo.home.test"}],
             "tls": [{"hosts": ["demo.home.test"]}]},
            {
                "kubernetes.io/ingress.class": "traefik",
                "traefik.ingress.kubernetes.io/router.middlewares":
                    "traefik-hsts-header@kubernetescrd, authentik-authentik-auth@kubernetescrd",
                "traefik.ingress.kubernetes.io/Router.TLS.Domains.0.main": "demo.apps.test",
                "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
            },
        )
    ],
    "middlewares": [
        named("own-auth", {"forwardAuth": {
            "address": "http://auth.demo.svc.cluster.local:9000/verify"}}),
        named("platform-auth", {"forwardAuth": {"address": (
            "http://authentik-server.authentik.svc.cluster.local"
            "/outpost.goauthentik.io/auth/traefik")}}),
        named("own-chain", {"chain": {"middlewares": [{"name": "own-auth"}]}}),
        named("pages", {"errors": {"service": {"name": "web", "port": 80}}}),
    ],
    "traefikservices": [
        named("split", {"weighted": {"services": [{"name": "web", "port": 80}]}}),
        named("shadow", {"mirroring": {"name": "web", "mirrors": [{"name": "web"}]}}),
    ],
    "ingressroutetcps": [],
    "ingressrouteudps": [],
    "certificates": [
        named("web", {"dnsNames": ["demo.apps.test", "demo.home.test"],
                      "commonName": "demo.apps.test", "issuerRef": PROD}),
        named("own", {"dnsNames": ["demo.apps.test"], "issuerRef": {"name": "own-ca"}}),
    ],
    "certificaterequests": [
        named("web-1", {"username": CERT_MANAGER_SA, "issuerRef": PROD}),
        named("own-1", {"username": "alice", "issuerRef": {"name": "own-ca", "kind": "Issuer"}}),
    ],
    "tlsoptions": [named("modern")],
    "tlsstores": [named("own")],
}


def fixture(objects: dict, grants: list[dict] | None = None, tenants=("demo",)) -> dict:
    """`objects` per tenant namespace, with the grants in flux-system."""
    return {
        "tenants": list(tenants),
        "objects": {**objects, "flux-system": {"configmaps": [grant()] if grants is None
                                               else grants}},
    }


def test_a_clean_tenant_passes(tmp_path):
    result = run(tmp_path, fixture({"demo": CLEAN_APP}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 tenant namespace" in result.stdout


def test_no_tenant_namespace_passes_and_says_so(tmp_path):
    result = run(tmp_path, {"tenants": [], "objects": {}})
    assert result.returncode == 0, result.stderr
    assert "no fluxcd.io/tenant namespaces" in result.stdout


@pytest.mark.parametrize(
    ("objects", "finding"),
    [
        (
            {"services": [service("ext", {"app": "x"}, kind="ExternalName")]},
            "demo Service/ext: type ExternalName",
        ),
        (
            {"services": [service("np", {"app": "x"}, kind="NodePort")]},
            "demo Service/np: type NodePort",
        ),
        (
            {"services": [service("lb", {"app": "x"}, kind="LoadBalancer")]},
            "demo Service/lb: type LoadBalancer",
        ),
        (
            {"services": [service("eip", {"app": "x"}, external_ips=["1.1.1.1", "8.8.8.8"])]},
            "demo Service/eip: externalIPs 1.1.1.1,8.8.8.8",
        ),
        ({"services": [service("bare")]}, "demo Service/bare: no selector"),
        ({"services": [service("empty", {})]}, "demo Service/empty: no selector"),
        (
            {"endpointslices": [slice_("hand", "someone")]},
            "demo EndpointSlice/hand: managed-by someone",
        ),
        (
            {"endpointslices": [slice_("hand", None)]},
            "demo EndpointSlice/hand: managed-by unset",
        ),
        (
            {"endpointslices": [slice_("bare-x", "endpointslicemirroring-controller.k8s.io")]},
            "demo EndpointSlice/bare-x: managed-by endpointslicemirroring-controller.k8s.io",
        ),
        (
            {"endpointslices": [slice_("evil", addresses=("192.168.1.10",))]},
            "demo EndpointSlice/evil: address 192.168.1.10 is not a Pod selected by Service/web",
        ),
        (
            {"endpointslices": [slice_("nameless", service_name=None)]},
            "demo EndpointSlice/nameless: no kubernetes.io/service-name label",
        ),
        (
            {"endpointslices": [slice_("stray", service_name="ghost")]},
            "demo EndpointSlice/stray: no Service ghost with a selector",
        ),
        (
            {"endpoints": [endpoints("web", ("192.168.1.10",))]},
            "demo Endpoints/web: address 192.168.1.10 is not a Pod selected by Service/web",
        ),
        (
            {
                "pods": [pod("other", {"app": "other"}, "10.42.0.9")],
                "endpointslices": [slice_("sideways", addresses=("10.42.0.9",))],
            },
            "demo EndpointSlice/sideways: address 10.42.0.9 is not a Pod selected by Service/web",
        ),
        (
            {
                "pods": [pod("done", {"app": "web"}, "10.42.0.7", phase="Succeeded")],
                "endpointslices": [slice_("stale", addresses=("10.42.0.7",))],
            },
            "demo EndpointSlice/stale: address 10.42.0.7 is not a Pod selected by Service/web",
        ),
        (
            {"endpoints": [endpoints("orphan")]},
            "demo Endpoints/orphan: no Service with a selector",
        ),
        (
            {"services": [service("bare")], "endpoints": [endpoints("bare")]},
            "demo Endpoints/bare: no Service with a selector",
        ),
    ],
    ids=[
        "externalname",
        "node-port",
        "load-balancer",
        "external-ips",
        "selectorless",
        "empty-selector",
        "foreign-slice",
        "unlabelled-slice",
        "mirrored-slice",
        "labelled-but-foreign-slice",
        "slice-without-service-name",
        "slice-for-missing-service",
        "foreign-endpoints-address",
        "slice-to-unselected-pod",
        "slice-to-finished-pod",
        "orphan-endpoints",
        "selectorless-endpoints",
    ],
)
def test_each_pre_policy_object_is_reported(tmp_path, objects, finding):
    merged = {k: CLEAN_APP[k] + objects.get(k, []) for k in CLEAN_APP}
    result = run(tmp_path, fixture({"demo": merged}))
    assert result.returncode == 1, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert finding in lines
    assert not [line for line in lines if line != finding and "web" in line.replace("web-", "")]


def test_a_controller_slice_whose_addresses_are_selected_pods_passes(tmp_path):
    objects = {
        "services": [service("web", {"app": "web"})],
        "endpointslices": [slice_("web-abc12", addresses=("10.42.0.5", "10.42.1.6"))],
        "endpoints": [endpoints("web", ("10.42.0.5", "10.42.1.6"))],
        "pods": [
            pod("web-0", {"app": "web"}, "10.42.0.5"),
            pod("web-1", {"app": "web", "extra": "y"}, "10.42.1.6"),
        ],
    }
    result = run(tmp_path, fixture({"demo": objects}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_headless_or_default_type_service_passes(tmp_path):
    headless = service("web-headless", {"app": "web"}, external_ips=[])
    headless["spec"]["clusterIP"] = "None"
    untyped = service("web-untyped", {"app": "web"})
    del untyped["spec"]["type"]
    objects = {**CLEAN_APP, "services": CLEAN_APP["services"] + [headless, untyped]}
    result = run(tmp_path, fixture({"demo": objects}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_tenant_namespace_is_audited(tmp_path):
    result = run(
        tmp_path,
        fixture(
            {"demo": CLEAN_APP, "other": {"services": [service("bare")]}},
            tenants=("demo", "other"),
        ),
    )
    assert result.returncode == 1
    assert "other Service/bare: no selector" in result.stdout.splitlines()


@pytest.mark.parametrize(
    "resource",
    [
        "endpointslices.discovery.k8s.io",
        "ingressroutes.traefik.io",
        "certificates.cert-manager.io",
        "configmaps",
    ],
)
def test_a_kubectl_failure_is_an_error_not_a_pass(tmp_path, resource):
    result = run(tmp_path, {**fixture({"demo": CLEAN_APP}), "fail": resource})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "OK" not in result.stdout


def _with(kind: str, *docs: dict) -> dict:
    return {**CLEAN_APP, kind: CLEAN_APP[kind] + list(docs)}


def _route(**changes) -> dict:
    return named("bad", {"routes": [{**ROUTE, **changes}]})


def _ingress(spec: dict | None = None, annotations: dict | None = None) -> dict:
    return named("bad", spec or {"rules": [{"host": "demo.apps.test"}]}, annotations)


def _cert(**changes) -> dict:
    return named("bad", {"dnsNames": ["demo.apps.test"], "issuerRef": PROD, **changes})


# One object each that a tenant policy now refuses, and the one line it reports.
POLICY_CASES = {
    "route-ungranted-host": (
        _with("ingressroutes", _route(match="Host(`grafana.apps.test`)")),
        "demo IngressRoute/bad: match Host(`grafana.apps.test`) is not Host() on granted"
        " hostnames only",
    ),
    "route-or": (
        _with("ingressroutes", _route(match="Host(`demo.apps.test`) || PathPrefix(`/`)")),
        "demo IngressRoute/bad: match Host(`demo.apps.test`) || PathPrefix(`/`) is not Host()"
        " on granted hostnames only",
    ),
    "route-hostregexp": (
        _with("ingressroutes", _route(match="Host(`demo.apps.test`) && HostRegexp(`.+`)")),
        "demo IngressRoute/bad: match Host(`demo.apps.test`) && HostRegexp(`.+`) is not"
        " Host() on granted hostnames only",
    ),
    "route-second-host": (
        _with("ingressroutes", _route(match="Host(`demo.apps.test`) && Host(`x.apps.test`)")),
        "demo IngressRoute/bad: match Host(`demo.apps.test`) && Host(`x.apps.test`) is not"
        " Host() on granted hostnames only",
    ),
    "route-priority-above-ceiling": (
        _with("ingressroutes", _route(priority=101)),
        "demo IngressRoute/bad: priority 101 above the grant's maxPriority 100",
    ),
    "route-tls-domain": (
        _with("ingressroutes", named("bad", {
            "routes": [ROUTE], "tls": {"domains": [{"main": "demo.apps.test",
                                                    "sans": ["auth.apps.test"]}]}})),
        "demo IngressRoute/bad: TLS domain auth.apps.test is not granted",
    ),
    "route-cross-namespace-service": (
        _with("ingressroutes", _route(services=[{"name": "x", "namespace": "other"}])),
        "demo IngressRoute/bad: service other/x in another namespace",
    ),
    "route-provider-service": (
        _with("ingressroutes", _route(services=[{"name": "api@internal", "kind":
                                                 "TraefikService"}])),
        "demo IngressRoute/bad: service api@internal uses a name@provider reference",
    ),
    "route-provider-transport": (
        _with("ingressroutes", _route(services=[{"name": "web", "serversTransport": "t@file"}])),
        "demo IngressRoute/bad: service web uses a name@provider reference",
    ),
    "route-cross-namespace-middleware": (
        _with("ingressroutes", _route(middlewares=[{"name": "mw", "namespace": "other"}])),
        "demo IngressRoute/bad: middleware other/mw outside its namespace and the platform"
        " allowlist",
    ),
    "route-provider-middleware": (
        _with("ingressroutes", _route(middlewares=[{"name": "mw@kubernetescrd"}])),
        "demo IngressRoute/bad: middleware mw@kubernetescrd uses a name@provider reference",
    ),
    "route-cross-namespace-tls-options": (
        _with("ingressroutes", named("bad", {
            "routes": [ROUTE], "tls": {"options": {"name": "default", "namespace": "traefik"}}})),
        "demo IngressRoute/bad: TLS options traefik/default outside its namespace",
    ),
    "route-provider-tls-store": (
        _with("ingressroutes", named("bad", {
            "routes": [ROUTE], "tls": {"store": {"name": "default@file"}}})),
        "demo IngressRoute/bad: TLS store demo/default@file outside its namespace",
    ),
    "ingress-default-backend": (
        _with("ingresses", _ingress({"defaultBackend": {"service": {"name": "web"}}})),
        "demo Ingress/bad: default backend answers every hostname",
    ),
    "ingress-hostless-rule": (
        _with("ingresses", _ingress({"rules": [{"http": {}}]})),
        "demo Ingress/bad: rule without a host",
    ),
    "ingress-ungranted-host": (
        _with("ingresses", _ingress({"rules": [{"host": "*.apps.test"}]})),
        "demo Ingress/bad: host *.apps.test is not granted",
    ),
    "ingress-tls-host": (
        _with("ingresses", _ingress({"rules": [{"host": "demo.apps.test"}],
                                     "tls": [{"hosts": ["auth.apps.test"]}]})),
        "demo Ingress/bad: TLS host auth.apps.test is not granted",
    ),
    "ingress-tls-domains-annotation": (
        _with("ingresses", _ingress(annotations={
            "traefik.ingress.kubernetes.io/router.tls.domains.0.sans":
                "demo.home.test, auth.apps.test"})),
        "demo Ingress/bad: router.tls.domains name auth.apps.test is not granted",
    ),
    "ingress-router-rule-annotation": (
        _with("ingresses", _ingress(annotations={
            "traefik.ingress.kubernetes.io/Router.Priority": "1000"})),
        "demo Ingress/bad: annotation traefik.ingress.kubernetes.io/Router.Priority is refused",
    ),
    "ingress-provider-annotation": (
        _with("ingresses", _ingress(annotations={
            "traefik.ingress.kubernetes.io/router.middlewares":
                "traefik-hsts-header@kubernetescrd,other-mw@kubernetescrd"})),
        "demo Ingress/bad: annotation traefik.ingress.kubernetes.io/router.middlewares"
        " names other-mw@kubernetescrd, outside the platform allowlist",
    ),
    "service-provider-annotation": (
        _with("services", {**service("svc", {"app": "web"}), "metadata": {
            "name": "svc", "annotations": {
                "traefik.ingress.kubernetes.io/service.serverstransport": "t@file"}}}),
        "demo Service/svc: annotation traefik.ingress.kubernetes.io/service.serverstransport"
        " names t@file, outside the platform allowlist",
    ),
    "service-tailscale-annotation": (
        _with("services", {**service("svc", {"app": "web"}), "metadata": {
            "name": "svc", "annotations": {"tailscale.com/expose": "true"}}}),
        "demo Service/svc: annotation tailscale.com/expose publishes through the Tailscale"
        " operator",
    ),
    "ingress-tailscale-class": (
        _with("ingresses", _ingress({"ingressClassName": "tailscale",
                                     "rules": [{"host": "demo.apps.test"}]})),
        "demo Ingress/bad: ingressClassName tailscale is not traefik",
    ),
    "ingress-legacy-class-annotation": (
        _with("ingresses", _ingress(annotations={"kubernetes.io/ingress.class": "tailscale"})),
        "demo Ingress/bad: annotation kubernetes.io/ingress.class tailscale is not traefik",
    ),
    "ingress-tailscale-annotation": (
        _with("ingresses", _ingress(annotations={"tailscale.com/funnel": "true"})),
        "demo Ingress/bad: annotation tailscale.com/funnel publishes through the Tailscale"
        " operator",
    ),
    "middleware-forward-auth-external": (
        _with("middlewares", named("bad", {"forwardAuth": {"address": "http://evil.test/"}})),
        "demo Middleware/bad: forwardAuth address http://evil.test/ is neither a Service in"
        " demo nor the platform outpost",
    ),
    "middleware-forward-auth-other-namespace": (
        _with("middlewares", named("bad", {"forwardAuth": {
            "address": "http://auth.other.svc.cluster.local/"}})),
        "demo Middleware/bad: forwardAuth address http://auth.other.svc.cluster.local/ is"
        " neither a Service in demo nor the platform outpost",
    ),
    "middleware-cross-namespace-chain": (
        _with("middlewares", named("bad", {"chain": {"middlewares": [
            {"name": "mw", "namespace": "other"}]}})),
        "demo Middleware/bad: chain middleware other/mw in another namespace",
    ),
    "middleware-provider-chain": (
        _with("middlewares", named("bad", {"chain": {"middlewares": [{"name": "mw@file"}]}})),
        "demo Middleware/bad: chain middleware mw@file uses a name@provider reference",
    ),
    "middleware-cross-namespace-errors": (
        _with("middlewares", named("bad", {"errors": {"service": {
            "name": "pages", "namespace": "other"}}})),
        "demo Middleware/bad: errors service other/pages in another namespace",
    ),
    "traefikservice-cross-namespace-weighted": (
        _with("traefikservices", named("bad", {"weighted": {"services": [
            {"name": "x", "namespace": "other"}]}})),
        "demo TraefikService/bad: weighted service other/x in another namespace",
    ),
    "traefikservice-provider-mirroring": (
        _with("traefikservices", named("bad", {"mirroring": {"name": "api@internal"}})),
        "demo TraefikService/bad: mirroring service api@internal uses a name@provider"
        " reference",
    ),
    "traefikservice-cross-namespace-mirror": (
        _with("traefikservices", named("bad", {"mirroring": {"name": "web", "mirrors": [
            {"name": "x", "namespace": "other"}]}})),
        "demo TraefikService/bad: mirror service other/x in another namespace",
    ),
    "ingressroutetcp": (
        _with("ingressroutetcps", named("bad")),
        "demo IngressRouteTCP/bad: tenants may not route TCP or UDP",
    ),
    "ingressrouteudp": (
        _with("ingressrouteudps", named("bad")),
        "demo IngressRouteUDP/bad: tenants may not route TCP or UDP",
    ),
    "certificate-ungranted": (
        _with("certificates", _cert(dnsNames=["demo.apps.test", "auth.apps.test"])),
        "demo Certificate/bad: name auth.apps.test is not granted",
    ),
    "certificate-wildcard-cn": (
        _with("certificates", _cert(commonName="*.apps.test")),
        "demo Certificate/bad: name *.apps.test is not granted",
    ),
    "certificate-ip": (
        _with("certificates", _cert(ipAddresses=["10.0.0.1"])),
        "demo Certificate/bad: sets ipAddresses",
    ),
    "certificate-other-cluster-issuer": (
        _with("certificates", _cert(issuerRef={**PROD, "name": "other-ca"})),
        "demo Certificate/bad: issuer ClusterIssuer/other-ca is not a platform ClusterIssuer"
        " or an Issuer in its namespace",
    ),
    "certificate-external-issuer": (
        _with("certificates", _cert(issuerRef={
            "name": "pca", "kind": "AWSPCAIssuer", "group": "awspca.cert-manager.io"})),
        "demo Certificate/bad: issuer AWSPCAIssuer/pca is not a platform ClusterIssuer"
        " or an Issuer in its namespace",
    ),
    "certificaterequest-tenant-to-cluster-issuer": (
        _with("certificaterequests", named("bad", {"username": "alice", "issuerRef": PROD})),
        "demo CertificateRequest/bad: alice asked ClusterIssuer/letsencrypt-prod, not a"
        " core Issuer in its namespace",
    ),
    "certificaterequest-tenant-to-external-kind": (
        _with("certificaterequests", named("bad", {"issuerRef": {
            "name": "pca", "kind": "AWSPCAClusterIssuer", "group": "awspca.cert-manager.io"}})),
        "demo CertificateRequest/bad: (no username) asked AWSPCAClusterIssuer/pca, not a"
        " core Issuer in its namespace",
    ),
    "tlsoption-default": (
        _with("tlsoptions", named("default")),
        "demo TLSOption/default: the default TLSOption is platform-owned",
    ),
    "tlsstore-default": (
        _with("tlsstores", named("default")),
        "demo TLSStore/default: the default TLSStore is platform-owned",
    ),
}


@pytest.mark.parametrize("case", sorted(POLICY_CASES))
def test_each_policy_covered_kind_is_audited(tmp_path, case):
    objects, finding = POLICY_CASES[case]
    result = run(tmp_path, fixture({"demo": objects}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [finding]


def test_a_priority_without_a_granted_ceiling_is_reported(tmp_path):
    result = run(tmp_path, fixture({"demo": CLEAN_APP}, [grant(max_priority=None)]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "demo IngressRoute/web: priority 100 above the grant's maxPriority (none)"
    ]


GRANT_OBJECTS = ["IngressRoute/web", "Ingress/web", "Certificate/web", "Certificate/own"]


@pytest.mark.parametrize(
    ("grants", "why"),
    [
        ([], "no hostname grant flux-system/tenant-routing-demo"),
        ([grant("other")], "no hostname grant flux-system/tenant-routing-demo"),
        ([grant(hosts="demo.apps.test\n*.apps.test\n")],
         "grant flux-system/tenant-routing-demo lists a wildcard host"),
    ],
    ids=["no-grant", "other-tenants-grant", "wildcard-grant"],
)
def test_every_host_bearing_object_is_reported_without_a_usable_grant(tmp_path, grants, why):
    """The grant policies refuse every write while the grant is missing or lists
    a wildcard, so each object that reads it is listed once, not per host."""
    result = run(tmp_path, fixture({"demo": CLEAN_APP}, grants))
    assert result.returncode == 1, result.stdout + result.stderr
    assert sorted(result.stdout.splitlines()) == sorted(
        f"demo {o}: {why}" for o in GRANT_OBJECTS
    )


@pytest.mark.parametrize(
    "ceiling",
    ["100\n", "99999999999999999999", "2147483648", " 100", "-1", "high", "\u0661\u0660\u0660"],
    ids=["trailing-newline", "int64-overflow", "int32-overflow", "padded", "negative",
         "word", "non-ascii-digits"],
)
def test_an_unparseable_max_priority_is_a_grant_problem(tmp_path, ceiling):
    """CEL's int() and the isolation gate take only ASCII digits up to 2^31-1, so
    any other maxPriority is reported on every object that reads the grant."""
    result = run(tmp_path, fixture({"demo": CLEAN_APP}, [grant(max_priority=ceiling)]))
    assert result.returncode == 1, result.stdout + result.stderr
    why = (f"grant flux-system/tenant-routing-demo has an unparseable maxPriority "
           f"{json.dumps(ceiling, ensure_ascii=False)}")
    assert sorted(result.stdout.splitlines()) == sorted(
        f"demo {o}: {why}" for o in GRANT_OBJECTS
    )


@pytest.mark.parametrize("ceiling", ["100", "2147483647"])
def test_a_plain_max_priority_up_to_the_int32_bound_is_fine(tmp_path, ceiling):
    result = run(tmp_path, fixture({"demo": CLEAN_APP}, [grant(max_priority=ceiling)]))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_tenant_without_host_bearing_objects_needs_no_grant(tmp_path):
    bare = {k: v for k, v in CLEAN_APP.items()
            if k not in ("ingressroutes", "ingresses", "certificates")}
    result = run(tmp_path, fixture({"demo": bare}, []))
    assert result.returncode == 0, result.stdout + result.stderr


def _script_list(name: str) -> list[str]:
    found = re.search(rf"^{name}='(\[.*?\])'$", SCRIPT.read_text(), re.M | re.S)
    assert found, name
    return json.loads(found.group(1))


def _policy_variable(policy: str, variable: str) -> list[str]:
    spec = next(
        d["spec"] for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == "ValidatingAdmissionPolicy"
        and d["metadata"]["name"] == policy
    )
    expression = next(v["expression"] for v in spec["variables"] if v["name"] == variable)
    return ast.literal_eval(" ".join(expression.split()))


@pytest.mark.parametrize(
    ("constant", "policy", "variable"),
    [
        ("PLATFORM_MIDDLEWARES", "tenant-traefik-route-refs", "platformMiddlewares"),
        ("PLATFORM_REFS", "tenant-traefik-ingress-refs", "platformRefs"),
        ("PLATFORM_FORWARD_AUTH", "tenant-traefik-object-refs", "platformForwardAuth"),
        ("PLATFORM_ISSUERS", "tenant-certificates", "platformIssuers"),
    ],
)
def test_the_audit_allowlists_are_the_policies(constant, policy, variable):
    """The audit repeats each policy allowlist, so the two cannot drift apart."""
    assert _script_list(constant) == _policy_variable(policy, variable)


def test_the_audit_forward_auth_pattern_is_the_policies():
    spec = next(
        d["spec"] for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == "ValidatingAdmissionPolicy"
        and d["metadata"]["name"] == "tenant-traefik-object-refs"
    )
    expression = " ".join(next(
        v["expression"] for v in spec["variables"] if v["name"] == "ownServiceAddress"
    ).split())
    head, tail = re.fullmatch(r"'(.*)' \+ variables\.ns \+ '(.*)'", expression).groups()
    text = SCRIPT.read_text()
    assert f"OWN_SERVICE_HEAD='{head}'" in text
    assert f"OWN_SERVICE_TAIL='{tail}'" in text
