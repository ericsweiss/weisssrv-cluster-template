"""Baseline Pod Security admits nfs and other node-mounted volumes, which the NAS
exports trust by node address, so tenant pods keep to the restricted volume set.
`allowed()` mirrors the policy; the first tests pin the shape it assumes."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

POLICY = REPO / "kubernetes" / "infrastructure" / "configs" / "tenant-traefik-policy.yaml"
NAME = "tenant-pod-volumes"
# The Pod Security restricted volume set. A Volume carries exactly one source,
# so the policy admits a volume only when one of these is set; a source type
# added upstream is refused until it is listed here.
ALLOWED_SOURCES = [
    "configMap", "csi", "downwardAPI", "emptyDir", "ephemeral",
    "persistentVolumeClaim", "projected", "secret",
]
# Every path is has()-guarded down to the list, so no matched kind can make the
# variable throw: a Fail policy that errors refuses every tenant write.
VARIABLE = (
    "request.resource.resource == 'pods'"
    " ? (has(object.spec) && has(object.spec.volumes) ? object.spec.volumes : [])"
    " : request.resource.resource == 'cronjobs'"
    " ? (has(object.spec) && has(object.spec.jobTemplate)"
    " && has(object.spec.jobTemplate.spec)"
    " && has(object.spec.jobTemplate.spec.template)"
    " && has(object.spec.jobTemplate.spec.template.spec)"
    " && has(object.spec.jobTemplate.spec.template.spec.volumes)"
    " ? object.spec.jobTemplate.spec.template.spec.volumes : [])"
    " : (has(object.spec) && has(object.spec.template)"
    " && has(object.spec.template.spec)"
    " && has(object.spec.template.spec.volumes)"
    " ? object.spec.template.spec.volumes : [])"
)
# has() on each source, not all() over the Volume's keys: the all() macro takes
# lists and maps, and a typed Volume is neither.
VALIDATION = (
    "variables.volumes.all(v, has(v.configMap) || has(v.csi) || has(v.downwardAPI)"
    " || has(v.emptyDir) || has(v.ephemeral) || has(v.persistentVolumeClaim)"
    " || has(v.projected) || has(v.secret))"
)
MESSAGE = (
    "tenant pods may use only configMap, csi, downwardAPI, emptyDir, ephemeral,"
    " persistentVolumeClaim, projected and secret volumes; nfs and other node-level"
    " volume types are platform-owned"
)
TENANT_MATCH = {
    "namespaceSelector": {
        "matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]
    }
}
WRITES = ["CREATE", "UPDATE"]


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
    assert spec["matchConstraints"] == {
        "resourceRules": [
            {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"],
             "resources": ["pods"]},
            {"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": WRITES,
             "resources": ["deployments", "statefulsets", "daemonsets", "replicasets"]},
            {"apiGroups": ["batch"], "apiVersions": ["v1"], "operations": ["CREATE"],
             "resources": ["jobs"]},
            {"apiGroups": ["batch"], "apiVersions": ["v1"], "operations": WRITES,
             "resources": ["cronjobs"]},
        ]
    }
    assert [(v["name"], _cel(v["expression"])) for v in spec["variables"]] == [
        ("volumes", VARIABLE)
    ]
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


def _at(obj: dict, *path: str) -> list[dict]:
    """has()-guarded descent: any absent step reads as no volumes."""
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return []
        obj = obj[key]
    return obj


def volumes_of(resource: str, obj: dict) -> list[dict]:
    """The `volumes` variable: the Pod's own list, a CronJob's job template's,
    or any other workload's pod template's; any absent step reads as empty."""
    if resource == "pods":
        return _at(obj, "spec", "volumes")
    if resource == "cronjobs":
        return _at(obj, "spec", "jobTemplate", "spec", "template", "spec", "volumes")
    return _at(obj, "spec", "template", "spec", "volumes")


def allowed(resource: str, obj: dict) -> bool:
    return all(any(s in v for s in ALLOWED_SOURCES) for v in volumes_of(resource, obj))


def shaped(resource: str, volumes: list[dict] | None) -> dict:
    """A `resource` object whose pod spec carries `volumes` (None: no list)."""
    pod_spec: dict = {"containers": [{"name": "app", "image": "app"}]}
    if volumes is not None:
        pod_spec["volumes"] = volumes
    if resource == "pods":
        return {"spec": pod_spec}
    template = {"spec": pod_spec}
    if resource == "cronjobs":
        return {"spec": {"schedule": "@daily", "jobTemplate": {"spec": {"template": template}}}}
    return {"spec": {"template": template}}


RESOURCES = ["pods", "deployments", "statefulsets", "daemonsets", "replicasets", "jobs",
             "cronjobs"]
NFS = {"name": "dump", "nfs": {"server": "nas.lan", "path": "/mnt/ssd/tenant-dumps"}}


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize(
    "volume",
    [
        NFS,
        {"name": "lun", "iscsi": {"targetPortal": "10.0.0.2:3260", "iqn": "iqn.x", "lun": 0}},
        {"name": "host", "hostPath": {"path": "/"}},
        {"name": "fs", "cephfs": {"monitors": ["10.0.0.3"]}},
        {"name": "repo", "gitRepo": {"repository": "https://example.com/r.git"}},
        {"name": "flex", "flexVolume": {"driver": "x/y"}},
    ],
    ids=["nfs", "iscsi", "hostPath", "cephfs", "gitRepo", "flexVolume"],
)
def test_a_node_level_volume_is_refused_on_every_matched_kind(resource, volume):
    """kubelet mounts these from the node, so any export or device that trusts
    the node's address is open to the tenant."""
    assert not allowed(resource, shaped(resource, [volume]))


@pytest.mark.parametrize("resource", RESOURCES)
def test_one_node_level_volume_among_allowed_ones_is_refused(resource):
    volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}, NFS]
    assert not allowed(resource, shaped(resource, volumes))


@pytest.mark.parametrize("resource", RESOURCES)
def test_the_restricted_volume_set_is_admitted(resource):
    volumes = [
        {"name": "cm", "configMap": {"name": "cm"}},
        {"name": "csi", "csi": {"driver": "x"}},
        {"name": "down", "downwardAPI": {"items": []}},
        {"name": "tmp", "emptyDir": {}},
        {"name": "eph", "ephemeral": {"volumeClaimTemplate": {"spec": {}}}},
        {"name": "data", "persistentVolumeClaim": {"claimName": "data"}},
        {"name": "token", "projected": {"sources": []}},
        {"name": "creds", "secret": {"secretName": "creds"}},
    ]
    assert allowed(resource, shaped(resource, volumes))


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize("volumes", [None, []], ids=["absent", "empty"])
def test_a_pod_spec_without_volumes_is_admitted(resource, volumes):
    assert allowed(resource, shaped(resource, volumes))


@pytest.mark.parametrize(
    ("resource", "obj"),
    [
        ("pods", {}),
        ("deployments", {}),
        ("deployments", {"spec": {}}),
        ("deployments", {"spec": {"template": {}}}),
        ("cronjobs", {"spec": {}}),
        ("cronjobs", {"spec": {"jobTemplate": {}}}),
        ("cronjobs", {"spec": {"jobTemplate": {"spec": {}}}}),
        ("cronjobs", {"spec": {"jobTemplate": {"spec": {"template": {}}}}}),
    ],
)
def test_an_absent_step_on_the_path_reads_as_no_volumes(resource, obj):
    """Each step is guarded, so a partial object evaluates instead of erroring
    a Fail policy into refusing the write; API validation refuses it later."""
    assert volumes_of(resource, obj) == []


def test_every_path_the_variable_reads_is_guarded():
    """Every object.<path> the variable dereferences is has()-checked on each
    of its prefixes, so no branch can throw on any matched kind."""
    import re

    paths = set(re.findall(r"object((?:\.[A-Za-z]+)+)", VARIABLE))
    guarded = set(re.findall(r"has\(object((?:\.[A-Za-z]+)+)\)", VARIABLE))
    for path in paths:
        steps = path.lstrip(".").split(".")
        for i in range(1, len(steps) + 1):
            assert "." + ".".join(steps[:i]) in guarded, path


def test_a_volume_with_no_listed_source_is_refused():
    """A source type added upstream carries a key the list does not name."""
    assert not allowed("pods", shaped("pods", [{"name": "new", "futureSource": {}}]))
    assert not allowed("pods", shaped("pods", [{"name": "bare"}]))
