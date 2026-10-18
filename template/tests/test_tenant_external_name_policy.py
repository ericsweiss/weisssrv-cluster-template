"""A tenant Service may not be ExternalName or selectorless: Traefik would follow
its CNAME or hand-written endpoints past the forwardAuth pin. No CEL engine ships
here, so `allowed()` mirrors the validations; the first test pins their shape."""

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


def allowed(spec: dict) -> bool:
    """The validations; an omitted type defaults to ClusterIP before admission."""
    return spec.get("type", "ClusterIP") != "ExternalName" and bool(spec.get("selector"))


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
    assert spec["validations"] == [
        {"expression": EXPRESSION, "message": MESSAGE},
        {"expression": SELECTOR_EXPRESSION, "message": SELECTOR_MESSAGE},
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


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"port": 80}]},
        {"selector": SELECTOR, "ports": [{"port": 80}]},
        {"type": "NodePort", "selector": SELECTOR, "ports": [{"port": 80, "nodePort": 30080}]},
        {"type": "ClusterIP", "clusterIP": "None", "selector": SELECTOR, "ports": [{"port": 5432}]},
    ],
    ids=["cluster-ip", "default-type", "node-port", "headless"],
)
def test_other_service_types_are_admitted(spec):
    assert allowed(spec)
