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
VOLUME_TYPE_VALIDATION = (
    "!has(object.metadata.annotations) || !('volumeType' in object.metadata.annotations)"
)
VOLUME_TYPE_MESSAGE = (
    "tenant claims may not carry a volumeType annotation; tenant-local volumes are always local"
)
# Skipped: an UPDATE to a terminating claim that leaves spec and annotations
# unchanged, so a claim admitted before the policy can shed its finalizer.
FINALIZING_CONDITIONS = [{
    "name": "enforce-unless-finalizing-unchanged",
    "expression": (
        "request.operation != 'UPDATE' || !has(object.metadata.deletionTimestamp) || "
        "object.spec != oldObject.spec || (has(object.metadata.annotations) ? "
        "object.metadata.annotations : {}) != (has(oldObject.metadata.annotations) ? "
        "oldObject.metadata.annotations : {})"
    ),
}]
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
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
             "resources": ["persistentvolumeclaims"]},
        ]
    }
    assert [
        {"name": c["name"], "expression": _cel(c["expression"])} for c in spec["matchConditions"]
    ] == FINALIZING_CONDITIONS
    assert [(_cel(v["expression"]), v["message"]) for v in spec["validations"]] == [
        (VALIDATION, MESSAGE),
        (VOLUME_TYPE_VALIDATION, VOLUME_TYPE_MESSAGE),
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


def volume_type_allowed(claim: dict) -> bool:
    """The second validation: no volumeType annotation at all."""
    return "volumeType" not in (claim["metadata"].get("annotations") or {})


@pytest.mark.parametrize("volume_type", ["hostPath", "local"])
def test_a_claim_with_a_volume_type_annotation_is_refused(volume_type):
    """The provisioner honours the claim's annotation over the class default, and
    a hostPath volume has no node affinity; even "local" is refused so the class
    stays the only place the type is set."""
    c = claim("tenant-local")
    c["metadata"]["annotations"] = {"volumeType": volume_type}
    assert not volume_type_allowed(c)


def test_a_claim_without_annotations_passes_the_volume_type_rule():
    assert volume_type_allowed(claim("tenant-local"))
    c = claim("tenant-local")
    c["metadata"]["annotations"] = {"app": "demo"}
    assert volume_type_allowed(c)


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


def enforced(operation: str, old: dict | None, new: dict) -> bool:
    """The match condition; a missing annotations map reads as an empty one."""
    if operation != "UPDATE" or "deletionTimestamp" not in new["metadata"]:
        return True
    return new["spec"] != old["spec"] or (
        new["metadata"].get("annotations") or {}
    ) != (old["metadata"].get("annotations") or {})


def admitted(operation: str, old: dict | None, new: dict) -> bool:
    """The whole policy: skipped by the match condition, or both validations pass."""
    return not enforced(operation, old, new) or (allowed(new) and volume_type_allowed(new))


def _annotated(c: dict, **annotations: str) -> dict:
    merged = {**(c["metadata"].get("annotations") or {}), **annotations}
    return {**c, "metadata": {**c["metadata"], "annotations": merged}}


def _bound(c: dict) -> dict:
    """kube-controller-manager's bind: its two annotations, plus spec.volumeName
    unless the claim already names its pre-bound volume."""
    bound = _annotated(c, **{
        "pv.kubernetes.io/bind-completed": "yes",
        "pv.kubernetes.io/bound-by-controller": "yes",
    })
    return {**bound, "spec": {"volumeName": "pvc-0f3a", **c["spec"]}}


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("volume_type", ["hostPath", "local"])
def test_an_update_adding_a_volume_type_annotation_is_refused(bound, volume_type):
    """Annotations are mutable and a WaitForFirstConsumer claim is provisioned
    after creation, so a clean CREATE then an annotating UPDATE is refused."""
    old = _bound(claim("tenant-local")) if bound else claim("tenant-local")
    assert not admitted("UPDATE", old, _annotated(old, volumeType=volume_type))


def _scheduled(c: dict) -> dict:
    return _annotated(c, **{"volume.kubernetes.io/selected-node": "node-a"})


def _provisioning(c: dict) -> dict:
    return _annotated(c, **{
        "volume.kubernetes.io/storage-provisioner": "rancher.io/local-path",
        "volume.beta.kubernetes.io/storage-provisioner": "rancher.io/local-path",
    })


def _resized(c: dict) -> dict:
    return {**c, "spec": {**c["spec"], "resources": {"requests": {"storage": "2Gi"}}}}


@pytest.mark.parametrize(
    "update", [_bound, _scheduled, _provisioning, _resized],
    ids=["bind", "selected-node", "storage-provisioner", "resize"],
)
@pytest.mark.parametrize(
    "old", [claim("tenant-local"), claim("", volume_name="demo-dumps")],
    ids=["tenant-local", "static"],
)
def test_legitimate_updates_to_a_compliant_claim_are_admitted(old, update):
    """The policy is actor-neutral: the controllers' bind, scheduling and provisioning
    updates and a tenant's resize keep the class and add no volumeType key."""
    assert admitted("UPDATE", old, update(old))


def _terminating(c: dict) -> dict:
    meta = {**c["metadata"], "finalizers": ["kubernetes.io/pvc-protection"],
            "deletionTimestamp": "2026-10-20T00:00:00Z"}
    return {**c, "metadata": meta}


def test_a_terminating_legacy_claim_may_shed_its_finalizer():
    """A claim admitted before the policy, on another class and annotated, is
    refused by both validations, so only the match condition lets it go."""
    legacy = _terminating(_annotated(claim("local-path"), volumeType="hostPath"))
    shed = {**legacy, "metadata": {**legacy["metadata"], "finalizers": []}}
    assert not allowed(legacy) and not volume_type_allowed(legacy)
    assert not enforced("UPDATE", legacy, shed)
    assert admitted("UPDATE", legacy, shed)


def test_a_terminating_claim_gaining_a_volume_type_annotation_is_refused():
    """A tenant can hold its own claim in Terminating with pvc-protection, so
    an annotation change there is still validated."""
    old = _terminating(claim("tenant-local"))
    new = _annotated({**old, "metadata": {**old["metadata"], "finalizers": []}},
                     volumeType="hostPath")
    assert enforced("UPDATE", old, new)
    assert not admitted("UPDATE", old, new)
