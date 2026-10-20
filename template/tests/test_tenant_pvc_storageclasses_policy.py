"""Tenant claims use the backed-up, quota-bounded tenant-local class, or "" for a
platform-written static volume pre-bound by claimRef; any other class is refused.
`allowed()` mirrors the policy; the first tests pin the shape it assumes."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-pvc-storageclasses"
VALIDATION = (
    "has(object.spec.storageClassName)"
    " && (object.spec.storageClassName == 'tenant-local'"
    " || (object.spec.storageClassName == ''"
    " && has(object.spec.volumeName)"
    " && object.spec.volumeName.startsWith(object.metadata.namespace + '-')))"
)
MESSAGE = (
    "tenant claims must use the tenant-local StorageClass, or name a platform-written"
    " static volume with storageClassName \"\" and a volumeName of the form"
    " <namespace>-<purpose>"
)
TENANT_MATCH = {
    "namespaceSelector": {
        "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
    }
}


def _docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(POLICY.read_text()) if isinstance(d, dict)]


def _policy() -> dict:
    found = [
        d for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == NAME
    ]
    assert len(found) == 1
    return found[0]["spec"]


def _cel(text: str) -> str:
    return " ".join(text.split())


def test_the_policy_is_the_shape_the_mirror_evaluates():
    spec = _policy()
    assert spec["failurePolicy"] == "Fail"
    assert "paramKind" not in spec
    assert "variables" not in spec
    assert "matchConditions" not in spec
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"],
             "resources": ["persistentvolumeclaims"]},
        ]
    }
    assert [(_cel(v["expression"]), v["message"]) for v in spec["validations"]] == [
        (VALIDATION, MESSAGE)
    ]


def test_the_policy_is_deny_bound_on_every_tenant_namespace():
    bindings = [
        d["spec"] for d in _docs()
        if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and d["spec"]["policyName"] == NAME
    ]
    assert bindings == [
        {"policyName": NAME, "validationActions": ["Deny"], "matchResources": TENANT_MATCH}
    ]


def test_the_policy_is_the_last_in_the_file():
    names = [d["metadata"]["name"] for d in _docs() if d.get("kind") == "ValidatingAdmissionPolicy"]
    assert names[-1] == NAME


def allowed(claim: dict) -> bool:
    """The validation: tenant-local, or "" naming its pre-bound volume."""
    spec = claim["spec"]
    if "storageClassName" not in spec:
        return False
    if spec["storageClassName"] == "tenant-local":
        return True
    prefix = claim["metadata"]["namespace"] + "-"
    return spec["storageClassName"] == "" and (spec.get("volumeName") or "").startswith(prefix)


def claim(storage_class: str | None, volume_name: str | None = None) -> dict:
    """A claim as admission sees it; None leaves storageClassName unset."""
    spec: dict = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    if storage_class is not None:
        spec["storageClassName"] = storage_class
    if volume_name is not None:
        spec["volumeName"] = volume_name
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "dumps", "namespace": "demo"}, "spec": spec,
    }


def test_a_claim_on_tenant_local_is_admitted():
    assert allowed(claim("tenant-local"))


def test_a_static_claim_with_an_empty_class_is_admitted():
    """The dump landing zone: a platform-written PersistentVolume pre-bound to
    the claim by claimRef, which the claim names with storageClassName ""."""
    assert allowed(claim("", volume_name="demo-dumps"))


@pytest.mark.parametrize("volume_name", ["other-dumps", "demo", "demodumps", "loki-data"])
def test_an_empty_class_claim_naming_another_tenants_or_a_platform_volume_is_refused(volume_name):
    """A platform-written tenant volume is named <namespace>-<purpose>, so a
    name without this claim's namespace prefix is not one written for it."""
    assert not allowed(claim("", volume_name=volume_name))


@pytest.mark.parametrize("storage_class", ["local-path", "nfs-client", "tenant-local-2", "TENANT-LOCAL"])
def test_a_claim_on_another_class_is_refused(storage_class):
    """Another class lands tenant data on a disk the backups and the
    class-scoped quota never see."""
    assert not allowed(claim(storage_class))


@pytest.mark.parametrize("volume_name", [None, ""], ids=["absent", "empty"])
def test_an_empty_class_claim_without_a_volume_name_is_refused(volume_name):
    """Without volumeName a "" claim binds any unbound classless volume, so the
    static exception must name the platform-written volume it was written for."""
    assert not allowed(claim("", volume_name=volume_name))


def test_a_claim_without_a_class_is_refused():
    """The DefaultStorageClass plugin fills an omitted class before validation;
    if no default exists the field stays unset and the claim is refused."""
    assert not allowed(claim(None))
