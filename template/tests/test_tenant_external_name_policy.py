"""A tenant Service must be an internal ClusterIP with a selector and no
externalIPs, so Traefik reaches only the tenant's pods and nothing bypasses it.
No CEL engine ships here, so `allowed()` mirrors the validations."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-no-externalname-services"
EXPRESSION = "!has(object.spec.type) || object.spec.type != 'ExternalName'"
MESSAGE = (
    "tenant Services may not be ExternalName (it would CNAME Traefik to another namespace)"
)
SELECTOR_EXPRESSION = "has(object.spec.selector) && size(object.spec.selector) > 0"
SELECTOR_MESSAGE = (
    "tenant Services must select pods (selectorless Services allow hand-written endpoints)"
)
SHAPE_EXPRESSION = (
    "(!has(object.spec.type) || object.spec.type == 'ClusterIP')"
    " && (!has(object.spec.externalIPs) || size(object.spec.externalIPs) == 0)"
)
SHAPE_MESSAGE = "tenant Services must be internal ClusterIP Services without externalIPs"
# The Tailscale operator publishes any Service carrying one of its annotations,
# with no Traefik in the path, whether or not this cluster installs it today.
TAILSCALE = "tailscale.com/"
ANNOTATION_EXPRESSION = (
    "!has(object.metadata.annotations)"
    f" || object.metadata.annotations.all(k, !k.startsWith('{TAILSCALE}'))"
)
ANNOTATION_MESSAGE = "tenant Services may not bypass Traefik through the Tailscale operator"
TENANT_MATCH = {
    "namespaceSelector": {
        "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
    }
}


def _docs(kind: str) -> list[dict]:
    return [
        d for d in yaml.safe_load_all(POLICY.read_text())
        if isinstance(d, dict) and d.get("kind") == kind
        and d["metadata"]["name"] == NAME
    ]


def allowed(spec: dict, annotations: dict | None = None) -> bool:
    """The validations; an omitted type defaults to ClusterIP before admission."""
    kind = spec.get("type", "ClusterIP")
    return (
        kind != "ExternalName"
        and bool(spec.get("selector"))
        and kind == "ClusterIP"
        and not spec.get("externalIPs")
        and not any(k.startswith(TAILSCALE) for k in annotations or {})
    )


def test_the_policy_is_the_shape_the_mirror_evaluates():
    found = _docs("ValidatingAdmissionPolicy")
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["failurePolicy"] == "Fail"
    assert spec["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["services"],
        }
    ]
    assert [
        {"expression": " ".join(v["expression"].split()), "message": v["message"]}
        for v in spec["validations"]
    ] == [
        {"expression": EXPRESSION, "message": MESSAGE},
        {"expression": SELECTOR_EXPRESSION, "message": SELECTOR_MESSAGE},
        {"expression": SHAPE_EXPRESSION, "message": SHAPE_MESSAGE},
        {"expression": ANNOTATION_EXPRESSION, "message": ANNOTATION_MESSAGE},
    ]


def test_the_binding_denies_on_every_tenant_namespace():
    found = _docs("ValidatingAdmissionPolicyBinding")
    assert len(found) == 1
    spec = found[0]["spec"]
    assert spec["policyName"] == NAME
    assert spec["validationActions"] == ["Deny"]
    assert spec["matchResources"] == TENANT_MATCH


def test_an_external_name_service_is_refused():
    spec = {"type": "ExternalName", "externalName": "grafana.observability.svc.cluster.local"}
    assert not allowed(spec)


# A selectorless Service takes whatever Endpoints or EndpointSlices name it,
# including another namespace's pod IPs.
@pytest.mark.parametrize(
    "spec",
    [
        {"type": "ClusterIP", "ports": [{"port": 80}]},
        {"type": "ClusterIP", "selector": {}, "ports": [{"port": 80}]},
        {"clusterIP": "None", "ports": [{"port": 5432}]},
    ],
    ids=["no-selector", "empty-selector", "headless-no-selector"],
)
def test_a_selectorless_service_is_refused(spec):
    assert not allowed(spec)


SELECTOR = {"app.kubernetes.io/name": "demo"}


# Each reaches the tenant's pods without Traefik: a port on every node, a MetalLB
# VIP, or an address kube-proxy captures cluster-wide (the CVE-2020-8554 class).
@pytest.mark.parametrize(
    "spec",
    [
        {"type": "NodePort", "selector": SELECTOR, "ports": [{"port": 80, "nodePort": 30080}]},
        {"type": "LoadBalancer", "selector": SELECTOR, "ports": [{"port": 80}]},
        {"type": "ClusterIP", "selector": SELECTOR, "externalIPs": ["1.1.1.1"],
         "ports": [{"port": 53}]},
        {"selector": SELECTOR, "externalIPs": ["10.0.0.1"], "ports": [{"port": 80}]},
    ],
    ids=["node-port", "load-balancer", "external-ips", "default-type-external-ips"],
)
def test_a_service_reachable_without_traefik_is_refused(spec):
    assert not allowed(spec)


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"port": 80}]},
        {"selector": SELECTOR, "ports": [{"port": 80}]},
        {"type": "ClusterIP", "selector": SELECTOR, "externalIPs": [], "ports": [{"port": 80}]},
        {"type": "ClusterIP", "clusterIP": "None", "selector": SELECTOR, "ports": [{"port": 5432}]},
        {"clusterIP": "None", "selector": SELECTOR, "ports": [{"port": 5432}]},
    ],
    ids=["cluster-ip", "default-type", "empty-external-ips", "headless", "headless-default-type"],
)
def test_an_internal_cluster_ip_service_is_admitted(spec):
    assert allowed(spec)


INTERNAL = {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"port": 80}]}


@pytest.mark.parametrize(
    "annotations",
    [
        {"tailscale.com/expose": "true"},
        {"tailscale.com/hostname": "demo"},
        {"tailscale.com/tailnet-fqdn": "db.tailnet.ts.net"},
        {"tailscale.com/proxy-class": "default"},
        {"app.kubernetes.io/part-of": "demo", "tailscale.com/tags": "tag:k8s"},
    ],
    ids=["expose", "hostname", "tailnet-fqdn", "proxy-class", "beside-another"],
)
def test_a_tailscale_operator_annotation_is_refused(annotations):
    """The operator would publish the Service to the tailnet with no Traefik."""
    assert not allowed(INTERNAL, annotations)


@pytest.mark.parametrize(
    "annotations",
    [
        None,
        {},
        {"traefik.ingress.kubernetes.io/service.sticky.cookie": "true"},
        {"example.com/tailscale.com": "x"},
    ],
    ids=["none", "empty", "traefik", "tailscale-not-a-prefix"],
)
def test_an_internal_service_without_tailscale_annotations_is_admitted(annotations):
    assert allowed(INTERNAL, annotations)
