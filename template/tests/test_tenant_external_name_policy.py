"""A tenant Service may not be ExternalName: Traefik would follow its CNAME into
another namespace past the forwardAuth pin. No CEL engine ships here, so
`allowed()` mirrors the validation, and the first test pins its shape."""

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
    """The validation; an omitted type defaults to ClusterIP before admission."""
    return spec.get("type", "ClusterIP") != "ExternalName"


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
    assert spec["validations"] == [{"expression": EXPRESSION, "message": MESSAGE}]


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


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "ClusterIP", "ports": [{"port": 80}]},
        {"ports": [{"port": 80}]},
        {"type": "NodePort", "ports": [{"port": 80, "nodePort": 30080}]},
        {"type": "ClusterIP", "clusterIP": "None", "ports": [{"port": 5432}]},
    ],
    ids=["cluster-ip", "default-type", "node-port", "headless"],
)
def test_other_service_types_are_admitted(spec):
    assert allowed(spec)
