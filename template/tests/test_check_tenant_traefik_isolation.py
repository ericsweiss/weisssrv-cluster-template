"""Tests that check-tenant-traefik-isolation.py fails when a tenant lands without the full Deny-bound policies."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script

gate = load_script("check-tenant-traefik-isolation.py")

RELEASE_WITH_VALUES = textwrap.dedent(
    """\
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: traefik
    spec:
      values:
        providers:
          kubernetesCRD:
            allowCrossNamespace: {allow}
    """
)


def routing_binding(ns: str, policy: str = "tenant-traefik-hostnames") -> dict:
    """A per-tenant binding of `policy` the platform writes into the wiring file."""
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": f"{policy}-{ns}"},
        "spec": {
            "policyName": policy,
            "validationActions": ["Deny"],
            "paramRef": {
                "name": f"tenant-routing-{ns}",
                "namespace": "flux-system",
                "parameterNotFoundAction": "Deny",
            },
            "matchResources": {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}
            },
        },
    }


def routing_configmap(ns: str, hosts: str = "demo.example.com\n") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"tenant-routing-{ns}", "namespace": "flux-system"},
        "data": {"hosts": hosts},
    }


def wiring(ns: str) -> list[dict]:
    """A tenant file's routing part: its Namespace, hostname grant and the
    hostname and certificate bindings that read it."""
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns, "labels": {"fluxcd.io/tenant": ns}},
        },
        routing_configmap(ns),
        routing_binding(ns),
        routing_binding(ns, "tenant-certificates"),
    ]


def write_tenant(root: Path, resource: str, docs: list[dict]) -> None:
    path = (root / gate.TENANTS_KUSTOMIZATION).parent / resource
    path.write_text(yaml.safe_dump_all(docs, sort_keys=False))


def build(tmp_path: Path, resources: list[str], release: str) -> Path:
    """The tenants kustomization, each listed tenant file wired for routing
    (namespace = file stem), and the Traefik release."""
    kustomization = tmp_path / gate.TENANTS_KUSTOMIZATION
    kustomization.parent.mkdir(parents=True, exist_ok=True)
    kustomization.write_text(
        "resources:\n" + "".join(f"  - {r}\n" for r in resources)
    )
    for resource in resources:
        if resource not in gate.BASELINE:
            write_tenant(tmp_path, resource, wiring(Path(resource).stem))
    path = tmp_path / gate.TRAEFIK_RELEASE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(release)
    return tmp_path


def test_baseline_only_passes(tmp_path):
    root = build(tmp_path, ["tenant-crd-editor.yaml"], RELEASE_WITH_VALUES.format(allow="true"))
    write_policy(root, policy())
    assert gate.check(root) == []


def test_baseline_only_without_the_policies_fails(tmp_path):
    """A torn-down tenant's Namespace stays live with the tenant label, so the
    label-bound policies are required with no tenant wired as well."""
    root = build(tmp_path, ["tenant-crd-editor.yaml"], RELEASE_WITH_VALUES.format(allow="true"))
    problems = gate.check(root)
    assert len(problems) == 1
    assert "wires no tenant" in problems[0]


def test_a_tenant_with_cross_namespace_still_true_fails(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "hermes-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    problems = gate.check(root)
    assert len(problems) == 1
    assert "hermes-tenant.yaml" in problems[0]


# Plain-Ingress annotations, forwardAuth addresses and endpoint resolution do
# not depend on allowCrossNamespace, so turning it off does not open the gate.
def test_a_tenant_without_the_policies_fails_with_cross_namespace_off(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "hermes-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
    problems = gate.check(root)
    assert len(problems) == 1
    assert "hermes-tenant.yaml" in problems[0]
    assert "even when providers.kubernetesCRD.allowCrossNamespace is false" in problems[0]


def test_baseline_only_passes_with_cross_namespace_off(tmp_path):
    root = build(tmp_path, ["tenant-crd-editor.yaml"], RELEASE_WITH_VALUES.format(allow="false"))
    write_policy(root, policy())
    assert gate.check(root) == []


def test_a_tenant_passes_with_the_policies_and_cross_namespace_off(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
    write_policy(root, policy())
    assert gate.check(root) == []


def test_a_null_values_block_is_reported_not_a_crash(tmp_path):
    """`values:` with nothing under it must report, not raise AttributeError."""
    release = textwrap.dedent(
        """\
        apiVersion: helm.toolkit.fluxcd.io/v2
        kind: HelmRelease
        metadata:
          name: traefik
        spec:
          values:
        """
    )
    root = build(tmp_path, ["tenant-crd-editor.yaml"], release)
    problems = gate.check(root)
    assert len(problems) == 1
    assert "no HelmRelease spec.values" in problems[0]


def test_an_empty_tenants_kustomization_is_reported(tmp_path):
    root = build(tmp_path, [], RELEASE_WITH_VALUES.format(allow="true"))
    assert "lists no resources" in gate.check(root)[0]


# An unparseable input means the gate could not inspect its subject: exit 2
# through Vacuous, never a traceback and never an isolation finding.
@pytest.mark.parametrize("target", ["TENANTS_KUSTOMIZATION", "TRAEFIK_RELEASE"])
def test_an_unparseable_input_is_vacuous_not_a_finding(tmp_path, target):
    root = build(tmp_path, ["tenant-crd-editor.yaml"], RELEASE_WITH_VALUES.format(allow="false"))
    (root / getattr(gate, target)).write_text("a: [1,\n  b: {\n")
    with pytest.raises(gate.Vacuous):
        gate.check(root)
    assert gate.main(["--repo", str(root)]) == 2


# The fixture is the shipped policy file itself, so a gate spec that drifts from
# what the file ships fails here and in the shipped-file test below.
SHIPPED_POLICY = (REPO / gate.TENANT_POLICY).read_text()


def policy(action: str = "Deny") -> str:
    """The shipped policy file, its first binding's action set to `action`."""
    return SHIPPED_POLICY.replace("validationActions: [Deny]", f"validationActions: [{action}]", 1)


def write_policy(root: Path, body: str, listed: bool = True) -> None:
    """The policy file, plus the configs kustomization that applies it."""
    path = root / gate.TENANT_POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    resources = ["traefik-middlewares.yaml"] + (["tenant-traefik-policy.yaml"] if listed else [])
    (path.parent / "kustomization.yaml").write_text(
        "resources:\n" + "".join(f"  - {r}\n" for r in resources)
    )


def test_a_tenant_passes_when_the_admission_policies_pin_refs(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, policy())
    assert gate.check(root) == []


def test_a_warn_only_binding_does_not_open_the_gate(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, policy("Warn"))
    assert len(gate.check(root)) == 1


def test_a_policy_file_missing_one_binding_keeps_the_gate_closed(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    body = policy().rsplit("---\n", 1)[0]
    write_policy(root, body)
    assert len(gate.check(root)) == 1


# Each mutation leaves a Deny binding that enforces nothing on a tenant
# namespace: one selecting no tenant, or one naming a policy the file never defines.
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("key: fluxcd.io/tenant\n", "key: fluxcd.io/not-tenant\n"),
        ("operator: Exists", "operator: DoesNotExist"),
        ("kind: ValidatingAdmissionPolicy\n", "kind: ConfigMap\n"),
    ],
    ids=["selector-key", "selector-operator", "policy-undefined"],
)
def test_a_binding_that_pins_no_tenant_keeps_the_gate_closed(tmp_path, old, new):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    body = policy()
    assert old in body
    write_policy(root, body.replace(old, new, 1))
    assert len(gate.check(root)) == 1


def test_a_policy_file_without_the_ingress_policy_keeps_the_gate_closed(tmp_path):
    """A plain Ingress resolves name@provider annotations across namespaces, so
    the gate needs the Ingress policy and its binding too."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    body = policy()
    ingress_policy = body.rindex("---\n", 0, body.index("name: tenant-traefik-ingress-refs"))
    write_policy(root, body[:ingress_policy])
    assert len(gate.check(root)) == 1


# ExternalName/selectorless Services or hand-written endpoints send Traefik past
# the forwardAuth pin, a raw CertificateRequest skips tenant-certificates, an nfs
# volume mounts any NAS export and a claim on another class escapes the quota.
@pytest.mark.parametrize("kind", ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])
@pytest.mark.parametrize(
    "name",
    [
        "tenant-no-externalname-services",
        "tenant-no-manual-endpoints",
        "tenant-no-tcp-udp-routes",
        "tenant-traefik-tls-defaults",
        "tenant-certificaterequests",
        "tenant-pod-volumes",
        "tenant-pvc-storageclasses",
    ],
)
def test_a_policy_file_without_a_service_refusal_keeps_the_gate_closed(tmp_path, kind, name):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    docs = policy().split("---\n")
    kept = [
        d for d in docs
        if not (f"kind: {kind}\n" in d and f"name: {name}\n" in d)
    ]
    assert len(kept) == len(docs) - 1
    write_policy(root, "---\n".join(kept))
    assert len(gate.check(root)) == 1


@pytest.mark.parametrize("name", ["tenant-traefik-hostnames", "tenant-certificates"])
def test_a_policy_file_without_a_per_tenant_policy_keeps_the_gate_closed(tmp_path, name):
    """The per-tenant bindings name a policy; without it they enforce nothing."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    docs = policy().split("---\n")
    kept = [d for d in docs if f"name: {name}\n" not in d]
    assert len(kept) == len(docs) - 1
    write_policy(root, "---\n".join(kept))
    assert len(gate.check(root)) == 1


def test_the_gate_pins_twelve_policies():
    assert len(gate.REQUIRED_POLICIES) == 12
    assert "tenant-certificaterequests" in gate.REQUIRED_POLICIES
    assert "tenant-pod-volumes" in gate.REQUIRED_POLICIES
    assert "tenant-pvc-storageclasses" in gate.REQUIRED_POLICIES


# The Pod Security restricted volume set, each admitted by has() on its source.
RESTRICTED_VOLUME_SOURCES = (
    "has(v.configMap) || has(v.csi) || has(v.downwardAPI) || has(v.emptyDir) || "
    "has(v.ephemeral) || has(v.persistentVolumeClaim) || has(v.projected) || has(v.secret)"
)


def test_the_gate_pins_the_restricted_volume_set_on_pods_and_workloads():
    """Baseline Pod Security admits nfs, iscsi and the other node-mounted volume
    types, and the NAS exports trust node addresses, so the gate holds tenant
    pods and every pod template to the restricted set."""
    required = gate.REQUIRED_POLICIES["tenant-pod-volumes"]
    assert required["paramKind"] is None
    assert required["resourceRules"] == [
        {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"],
         "resources": ["pods"]},
        {"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
         "resources": ["deployments", "statefulsets", "daemonsets", "replicasets"]},
        {"apiGroups": ["batch"], "apiVersions": ["v1"], "operations": ["CREATE"],
         "resources": ["jobs"]},
        {"apiGroups": ["batch"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
         "resources": ["cronjobs"]},
    ]
    assert required["variables"] == [(
        "volumes",
        "request.resource.resource == 'pods' ? (has(object.spec) && "
        "has(object.spec.volumes) ? object.spec.volumes : []) : "
        "request.resource.resource == 'cronjobs' ? (has(object.spec) && "
        "has(object.spec.jobTemplate) && has(object.spec.jobTemplate.spec) && "
        "has(object.spec.jobTemplate.spec.template) && "
        "has(object.spec.jobTemplate.spec.template.spec) && "
        "has(object.spec.jobTemplate.spec.template.spec.volumes) ? "
        "object.spec.jobTemplate.spec.template.spec.volumes : []) : "
        "(has(object.spec) && has(object.spec.template) && "
        "has(object.spec.template.spec) && has(object.spec.template.spec.volumes) ? "
        "object.spec.template.spec.volumes : [])",
    )]
    assert required["validations"] == [
        f"variables.volumes.all(v, {RESTRICTED_VOLUME_SOURCES})"
    ]


def _admit_nfs(spec):
    spec["validations"][0]["expression"] = spec["validations"][0]["expression"].replace(
        "has(v.secret))", "has(v.secret) || has(v.nfs))"
    )


def _drop_workloads(spec):
    spec["matchConstraints"]["resourceRules"] = spec["matchConstraints"]["resourceRules"][:1]


@pytest.mark.parametrize("weakening", [_admit_nfs, _drop_workloads], ids=["nfs", "pods-only"])
def test_a_pod_volumes_policy_that_admits_nfs_or_drops_workloads_keeps_the_gate_closed(
    tmp_path, weakening
):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated("tenant-pod-volumes", weakening))
    assert len(gate.check(root)) == 1


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("tenant-no-externalname-services", "!k.startsWith('tailscale.com/')"),
        ("tenant-traefik-hostnames", "object.spec.ingressClassName == 'traefik'"),
        ("tenant-traefik-hostnames", "!k.startsWith('tailscale.com/')"),
        ("tenant-traefik-hostnames", "k != 'kubernetes.io/ingress.class'"),
        ("tenant-traefik-object-refs", "!has(object.spec.failover)"),
        ("tenant-traefik-object-refs",
         "object.spec.highestRandomWeight.services.all(s, (!has(s.namespace)"),
        ("tenant-traefik-object-refs", "object.spec.errors.service.middlewares.all(m,"),
        ("tenant-traefik-object-refs", "object.spec.mirroring.middlewares.all(m,"),
        ("tenant-traefik-object-refs",
         "object.spec.mirroring.mirrors.all(s, !has(s.middlewares)"),
        ("tenant-traefik-route-refs", "r.services.all(s, !has(s.middlewares)"),
        ("tenant-traefik-route-refs", "!has(object.spec.parentRefs)"),
    ],
    ids=["service-tailscale", "ingress-class", "ingress-tailscale", "ingress-legacy-class",
         "traefikservice-failover", "highest-random-weight", "errors-service-middlewares",
         "mirroring-middlewares", "mirror-middlewares", "route-service-middlewares",
         "route-parent-refs"],
)
def test_the_gate_pins_the_other_operator_and_failover_refusals(name, needle):
    """Another ingress operator (Tailscale's), a TraefikService failover or an
    unpinned service reference would route around the other pins, so the gate
    carries each refusal."""
    assert [v for v in gate.REQUIRED_POLICIES[name]["validations"] if needle in v]


def _drop_acme_writes(spec):
    spec["matchConstraints"]["resourceRules"] = spec["matchConstraints"]["resourceRules"][:1]
    spec["validations"] = spec["validations"][:1]


def test_a_certificaterequests_policy_without_acme_writes_keeps_the_gate_closed(tmp_path):
    """Orders and Challenges are controller-only too: a copy that covers
    CertificateRequests alone drops that defence in depth."""
    rules = gate.REQUIRED_POLICIES["tenant-certificaterequests"]["resourceRules"]
    assert {"orders", "challenges"} <= {r for rule in rules for r in rule["resources"]}
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated("tenant-certificaterequests", _drop_acme_writes))
    assert len(gate.check(root)) == 1


def test_the_gate_matches_update_only_where_the_webhook_leaves_spec_mutable():
    """cert-manager's webhook freezes CertificateRequest and Challenge spec but
    Order spec only in spec.request, so UPDATE is matched on Orders alone and a
    garbage-collector finalizer removal elsewhere is not refused."""
    rules = gate.REQUIRED_POLICIES["tenant-certificaterequests"]["resourceRules"]
    ops = {r: tuple(rule["operations"]) for rule in rules for r in rule["resources"]}
    assert ops == {
        "certificaterequests": ("CREATE",),
        "challenges": ("CREATE",),
        "orders": ("CREATE", "UPDATE"),
    }


def test_the_gate_matches_immutable_templates_on_create_and_mutable_ones_on_both():
    """A Pod's volumes and a Job's pod template are immutable after creation, so
    UPDATE on them adds nothing and would refuse finalizer-only updates on objects
    admitted before the policy; the other kinds' templates stay mutable."""
    rules = gate.REQUIRED_POLICIES["tenant-pod-volumes"]["resourceRules"]
    ops = {r: tuple(rule["operations"]) for rule in rules for r in rule["resources"]}
    both = ("CREATE", "UPDATE")
    assert ops == {
        "pods": ("CREATE",), "jobs": ("CREATE",),
        "deployments": both, "statefulsets": both, "daemonsets": both,
        "replicasets": both, "cronjobs": both,
    }


def test_the_gate_matches_claims_on_create_and_update():
    """A claim's class is immutable but its volumeType annotation is not, and the
    provisioner reads it at binding, so UPDATE is matched as well."""
    rules = gate.REQUIRED_POLICIES["tenant-pvc-storageclasses"]["resourceRules"]
    ops = {r: tuple(rule["operations"]) for rule in rules for r in rule["resources"]}
    assert ops == {"persistentvolumeclaims": ("CREATE", "UPDATE")}


# Every policy that matches UPDATE on a kind with a spec, bar tenant-certificaterequests
# (its Order rule admits the garbage collector); endpoints carry their own form below,
# and the CREATE-only policies never see a finalizer update.
FINALIZING_POLICIES = (
    "tenant-traefik-route-refs", "tenant-traefik-object-refs", "tenant-traefik-ingress-refs",
    "tenant-no-externalname-services", "tenant-no-tcp-udp-routes", "tenant-traefik-tls-defaults",
    "tenant-traefik-hostnames", "tenant-certificates", "tenant-pod-volumes",
    "tenant-pvc-storageclasses",
)
# The endpoints policy carries its own form of the exemption (routing data in
# place of a spec); the weakening tests below cover it through its own list.
ENDPOINTS_FINALIZING = ("tenant-no-manual-endpoints",)
FINALIZING_EXPRESSION = (
    "request.operation != 'UPDATE' || !has(object.metadata.deletionTimestamp) || "
    "object.spec != oldObject.spec || (has(object.metadata.annotations) ? "
    "object.metadata.annotations : {}) != (has(oldObject.metadata.annotations) ? "
    "oldObject.metadata.annotations : {})"
)


def test_exactly_the_finalizing_policies_skip_a_finalizer_only_update():
    """A terminating legacy object sheds its finalizer by an UPDATE that changes
    neither spec nor annotations; exactly FINALIZING_POLICIES and the endpoints
    policy skip it, in the gate's pins and in the shipped file alike."""
    pinned = {
        name: required.get("matchConditions", [])
        for name, required in gate.REQUIRED_POLICIES.items()
    }
    shipped = {
        d["metadata"]["name"]: [
            (c["name"], gate._cel(c["expression"]))
            for c in d["spec"].get("matchConditions") or []
        ]
        for d in yaml.safe_load_all(SHIPPED_POLICY)
        if d and d["kind"] == "ValidatingAdmissionPolicy"
    }
    expected = {
        name: [("enforce-unless-finalizing-unchanged", FINALIZING_EXPRESSION)]
        if name in FINALIZING_POLICIES else []
        for name in gate.REQUIRED_POLICIES
    }
    # No spec to compare: the endpoints policy keys the same exemption on the
    # routing data, labels and annotations instead.
    expected["tenant-no-manual-endpoints"] = [
        ("enforce-unless-finalizing-unchanged", gate._cel(" ".join(gate.FINALIZING_UNCHANGED_ENDPOINTS[0][1].split())))
    ]
    assert pinned == expected
    assert shipped == expected


def _drop_conditions(spec):
    spec.pop("matchConditions")


def _condition_false(spec):
    spec["matchConditions"][0]["expression"] = "false"


def _ignore_annotations(spec):
    condition = spec["matchConditions"][0]
    condition["expression"] = condition["expression"].split(
        "|| (has(object.metadata.annotations)"
    )[0]
    assert "annotations" not in condition["expression"]


@pytest.mark.parametrize(
    "weakening", [_drop_conditions, _condition_false, _ignore_annotations],
    ids=["dropped", "false", "annotations-ignored"],
)
@pytest.mark.parametrize("name", FINALIZING_POLICIES + ENDPOINTS_FINALIZING)
def test_a_widened_finalizer_exemption_keeps_the_gate_closed(tmp_path, name, weakening):
    """Without the annotations clause a tenant could hold its own Service in
    Terminating and rewrite the annotations these policies validate."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated(name, weakening))
    assert len(gate.check(root)) == 1


def test_the_grant_policies_are_bound_per_tenant_not_by_label():
    """A tenant-label binding would need one paramRef for every tenant; the
    hostname grant is per tenant, so the bindings that read it live in the
    wiring files."""
    bound = {
        d["spec"]["policyName"]
        for d in yaml.safe_load_all(SHIPPED_POLICY)
        if d and d["kind"] == "ValidatingAdmissionPolicyBinding"
    }
    assert gate.PER_TENANT_POLICIES == ("tenant-traefik-hostnames", "tenant-certificates")
    assert bound == set(gate.REQUIRED_POLICIES) - set(gate.PER_TENANT_POLICIES)


def test_a_policy_file_the_configs_kustomization_does_not_list_keeps_the_gate_closed(tmp_path):
    """A policy file nothing applies pins nothing."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, policy(), listed=False)
    assert len(gate.check(root)) == 1


def test_the_shipped_policy_file_pins_tenants():
    """The rendered policy file and configs kustomization must open the gate on
    their own, before any tenant exists."""
    assert gate.policies_pin_tenants(REPO / gate.TENANT_POLICY, REPO)


# Each mutation narrows the route binding so it no longer covers every traefik.io
# object in every tenant namespace, while its tenant-label Exists still reads true.
@pytest.mark.parametrize(
    "narrowing",
    [
        "    objectSelector:\n      matchLabels: {protected: \"true\"}\n",
        "    resourceRules:\n      - apiGroups: [\"traefik.io\"]\n"
        "        apiVersions: [\"*\"]\n        operations: [\"CREATE\"]\n"
        "        resources: [\"ingressroutetcps\"]\n",
        "    excludeResourceRules:\n      - apiGroups: [\"traefik.io\"]\n"
        "        apiVersions: [\"*\"]\n        operations: [\"*\"]\n"
        "        resources: [\"ingressroutes\"]\n",
        "    namespaceSelector:\n      matchExpressions:\n"
        "        - key: fluxcd.io/tenant\n          operator: Exists\n"
        "        - key: kubernetes.io/metadata.name\n          operator: In\n"
        "          values: [nobody]\n",
        "    namespaceSelector:\n      matchLabels: {protected: \"true\"}\n"
        "      matchExpressions:\n"
        "        - key: fluxcd.io/tenant\n          operator: Exists\n",
        "    namespaceSelector:\n      matchExpressions:\n"
        "        - key: fluxcd.io/tenant\n          operator: Exists\n"
        "          values: [nobody]\n",
    ],
    ids=[
        "objectSelector",
        "resourceRules",
        "excludeResourceRules",
        "second-expression",
        "matchLabels",
        "expression-values",
    ],
)
def test_a_binding_that_narrows_its_match_keeps_the_gate_closed(tmp_path, narrowing):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    body = policy()
    selector = (
        "  matchResources:\n    namespaceSelector:\n      matchExpressions:\n"
        "        - key: fluxcd.io/tenant\n          operator: Exists\n"
    )
    assert selector in body
    narrowed = "  matchResources:\n" + (
        narrowing if "namespaceSelector" in narrowing else selector.split("\n", 1)[1] + narrowing
    )
    write_policy(root, body.replace(selector, narrowed, 1))
    assert len(gate.check(root)) == 1


def _mutated(name: str, mutate) -> str:
    """The shipped policy file with policy `name`'s spec passed through `mutate`."""
    docs = [d for d in yaml.safe_load_all(SHIPPED_POLICY) if d]
    target = [
        d for d in docs
        if d["kind"] == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == name
    ]
    assert len(target) == 1
    mutate(target[0]["spec"])
    return yaml.safe_dump_all(docs, sort_keys=False)


def _narrow_resources(spec):
    rules = spec["matchConstraints"]["resourceRules"]
    rules[0]["resources"] = rules[0]["resources"][:-1]


def _narrow_operations(spec):
    rules = [r for r in spec["matchConstraints"]["resourceRules"] if "UPDATE" in r["operations"]]
    if not rules:
        pytest.skip("no rule matches UPDATE")
    rules[0]["operations"] = ["CREATE"]


def _expression_true(index: int):
    def mutate(spec):
        spec["validations"][index]["expression"] = "true"
    return mutate


# Each mutation weakens one policy while its name and Deny binding still read
# true: errors fail open, a validation is dropped or made vacuous, or the match
# narrows.
WEAKENINGS = {
    "first-expression-true": _expression_true(0),
    "last-expression-true": _expression_true(-1),
    "failurePolicy-Ignore": lambda spec: spec.__setitem__("failurePolicy", "Ignore"),
    "failurePolicy-absent": lambda spec: spec.pop("failurePolicy"),
    "validation-dropped": lambda spec: spec["validations"].pop(),
    "validations-empty": lambda spec: spec.__setitem__("validations", []),
    "resources-narrowed": _narrow_resources,
    "operations-narrowed": _narrow_operations,
    "namespaceSelector-added": lambda spec: spec["matchConstraints"].__setitem__(
        "namespaceSelector", {"matchLabels": {"protected": "true"}}
    ),
    "matchConditions-added": lambda spec: spec.__setitem__(
        "matchConditions", [{"name": "never", "expression": "false"}]
    ),
    "paramKind-secret": lambda spec: spec.__setitem__(
        "paramKind", {"apiVersion": "v1", "kind": "Secret"}
    ),
}


@pytest.mark.parametrize("weakening", sorted(WEAKENINGS))
@pytest.mark.parametrize("name", sorted(gate.REQUIRED_POLICIES))
def test_a_weakened_policy_keeps_the_gate_closed(tmp_path, name, weakening):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated(name, WEAKENINGS[weakening]))
    assert len(gate.check(root)) == 1


SHIPPED_VARIABLES = sorted(
    (d["metadata"]["name"], v["name"])
    for d in yaml.safe_load_all(SHIPPED_POLICY)
    if d and d["kind"] == "ValidatingAdmissionPolicy"
    for v in d["spec"].get("variables") or []
)


# A widened variable vacates every validation that reads it while each
# expression string stays the same: an allowlist that admits anything, or a
# pattern that matches any address.
@pytest.mark.parametrize(("name", "variable"), SHIPPED_VARIABLES)
def test_a_widened_variable_keeps_the_gate_closed(tmp_path, name, variable):
    def widen(spec):
        for v in spec["variables"]:
            if v["name"] == variable:
                v["expression"] = "'.*'"

    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated(name, widen))
    assert len(gate.check(root)) == 1


def test_whitespace_in_an_expression_does_not_close_the_gate(tmp_path):
    """The pin compares whitespace-normalized CEL, so reflowing a folded
    expression is not a weakening."""
    def reflow(spec):
        for v in spec["validations"]:
            v["expression"] = v["expression"].replace(" ", "\n  ", 1)

    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated("tenant-traefik-object-refs", reflow))
    assert gate.check(root) == []


def test_the_unmutated_round_trip_still_opens_the_gate(tmp_path):
    """The YAML round trip the weakening cases use changes nothing by itself."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated("tenant-traefik-route-refs", lambda spec: None))
    assert gate.check(root) == []


def _mutated_binding(name: str, mutate) -> str:
    """The shipped policy file with binding `name`'s spec passed through `mutate`."""
    docs = [d for d in yaml.safe_load_all(SHIPPED_POLICY) if d]
    target = [
        d for d in docs
        if d["kind"] == "ValidatingAdmissionPolicyBinding" and d["metadata"]["name"] == name
    ]
    assert len(target) == 1
    mutate(target[0]["spec"])
    return yaml.safe_dump_all(docs, sort_keys=False)


# Each mutation keeps the Deny action and the tenant match, so only a check of
# the complete binding spec refuses it: a paramRef that admits on a missing
# parameter fails open, and any other added field is unreviewed.
BINDING_WEAKENINGS = {
    "paramRef-allow": lambda spec: spec.__setitem__(
        "paramRef", {"name": "absent", "parameterNotFoundAction": "Allow"}
    ),
    "paramRef-selector": lambda spec: spec.__setitem__(
        "paramRef", {"selector": {}, "parameterNotFoundAction": "Allow"}
    ),
    "extra-action": lambda spec: spec.__setitem__("validationActions", ["Deny", "Audit"]),
}


@pytest.mark.parametrize("weakening", sorted(BINDING_WEAKENINGS))
@pytest.mark.parametrize(
    "name", sorted(set(gate.REQUIRED_POLICIES) - set(gate.PER_TENANT_POLICIES))
)
def test_a_binding_with_more_than_the_required_spec_keeps_the_gate_closed(
    tmp_path, name, weakening
):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated_binding(name, BINDING_WEAKENINGS[weakening]))
    assert len(gate.check(root)) == 1


def test_the_unmutated_binding_round_trip_still_opens_the_gate(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated_binding("tenant-no-manual-endpoints", lambda spec: None))
    assert gate.check(root) == []


@pytest.mark.parametrize("name", ["tenant-traefik-hostnames", "tenant-certificates"])
def test_a_grant_policy_without_its_param_kind_keeps_the_gate_closed(tmp_path, name):
    """Without paramKind the per-tenant paramRef is never read."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, _mutated(name, lambda spec: spec.pop("paramKind")))
    assert len(gate.check(root)) == 1


def _routed(tmp_path: Path, docs: list[dict]) -> list[str]:
    """The gate's findings for one tenant `demo` whose file holds `docs`."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
    write_policy(root, policy())
    write_tenant(root, "demo.yaml", docs)
    return gate.check(root)


def test_a_wired_tenant_passes(tmp_path):
    assert _routed(tmp_path, wiring("demo")) == []


def _binding(mutate, index: int = 2) -> list[dict]:
    docs = wiring("demo")
    mutate(docs[index]["spec"])
    return docs


def _certificate_binding(mutate) -> list[dict]:
    return _binding(mutate, 3)


def _configmap(mutate) -> list[dict]:
    docs = wiring("demo")
    mutate(docs[1])
    return docs


# Each mutation leaves the tenant `demo` without a Deny binding that reads its
# own platform-owned hostname grant, or without a usable grant.
ROUTING_GAPS = {
    "binding-missing": lambda: [
        d for d in wiring("demo")
        if d.get("spec", {}).get("policyName") != "tenant-traefik-hostnames"
    ],
    "certificates-binding-missing": lambda: [
        d for d in wiring("demo")
        if d.get("spec", {}).get("policyName") != "tenant-certificates"
    ],
    "certificates-binding-warn": lambda: _certificate_binding(
        lambda s: s.__setitem__("validationActions", ["Warn"])
    ),
    "certificates-binding-other-namespace": lambda: _certificate_binding(
        lambda s: s["matchResources"]["namespaceSelector"]["matchLabels"].__setitem__(
            "kubernetes.io/metadata.name", "other"
        )
    ),
    "certificates-paramRef-other-grant": lambda: _certificate_binding(
        lambda s: s["paramRef"].__setitem__("name", "tenant-routing-other")
    ),
    "certificates-paramRef-allow": lambda: _certificate_binding(
        lambda s: s["paramRef"].__setitem__("parameterNotFoundAction", "Allow")
    ),
    "certificates-binding-label-selector": lambda: _certificate_binding(
        lambda s: s["matchResources"].__setitem__(
            "namespaceSelector",
            {"matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]},
        )
    ),
    "binding-warn": lambda: _binding(lambda s: s.__setitem__("validationActions", ["Warn"])),
    "binding-extra-action": lambda: _binding(
        lambda s: s.__setitem__("validationActions", ["Deny", "Audit"])
    ),
    "binding-other-policy": lambda: _binding(
        lambda s: s.__setitem__("policyName", "tenant-traefik-route-refs")
    ),
    "binding-other-namespace": lambda: _binding(
        lambda s: s["matchResources"]["namespaceSelector"]["matchLabels"].__setitem__(
            "kubernetes.io/metadata.name", "other"
        )
    ),
    "binding-label-selector": lambda: _binding(
        lambda s: s["matchResources"].__setitem__(
            "namespaceSelector",
            {"matchExpressions": [{"key": "fluxcd.io/tenant", "operator": "Exists"}]},
        )
    ),
    "binding-object-selector": lambda: _binding(
        lambda s: s["matchResources"].__setitem__(
            "objectSelector", {"matchLabels": {"protected": "true"}}
        )
    ),
    "paramRef-other-name": lambda: _binding(
        lambda s: s["paramRef"].__setitem__("name", "tenant-routing-other")
    ),
    "paramRef-tenant-namespace": lambda: _binding(
        lambda s: s["paramRef"].__setitem__("namespace", "demo")
    ),
    "paramRef-no-namespace": lambda: _binding(lambda s: s["paramRef"].pop("namespace")),
    "paramRef-allow": lambda: _binding(
        lambda s: s["paramRef"].__setitem__("parameterNotFoundAction", "Allow")
    ),
    "paramRef-selector": lambda: _binding(
        lambda s: s.__setitem__(
            "paramRef",
            {"selector": {}, "namespace": "flux-system", "parameterNotFoundAction": "Deny"},
        )
    ),
    "paramRef-missing": lambda: _binding(lambda s: s.pop("paramRef")),
    "configmap-missing": lambda: [d for d in wiring("demo") if d["kind"] != "ConfigMap"],
    "configmap-tenant-namespace": lambda: _configmap(
        lambda d: d["metadata"].__setitem__("namespace", "demo")
    ),
    "configmap-other-name": lambda: _configmap(
        lambda d: d["metadata"].__setitem__("name", "tenant-routing-other")
    ),
    "hosts-empty": lambda: _configmap(lambda d: d["data"].__setitem__("hosts", "")),
    "hosts-blank-lines": lambda: _configmap(lambda d: d["data"].__setitem__("hosts", "\n  \n")),
    "data-missing": lambda: _configmap(lambda d: d.pop("data")),
    "hosts-wildcard": lambda: _configmap(
        lambda d: d["data"].__setitem__("hosts", "*.example.com\n")
    ),
    "hosts-wildcard-beside-exact": lambda: _configmap(
        lambda d: d["data"].__setitem__("hosts", d["data"]["hosts"] + "*.demo.example.com\n")
    ),
    "maxPriority-not-a-number": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "high")
    ),
    # str.isdigit() admits these; CEL int() does not, and under failurePolicy Fail
    # the policy would then refuse every route that sets a priority.
    "maxPriority-non-ascii-digits": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "\u0661\u0660\u0660")
    ),
    "maxPriority-superscript": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "\u00b2")
    ),
    "maxPriority-overflow": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "2147483648")
    ),
    "maxPriority-int64-overflow": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "99999999999999999999")
    ),
    "maxPriority-negative": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "-1")
    ),
    "maxPriority-padded": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", " 100\n")
    ),
    "namespace-missing": lambda: [d for d in wiring("demo") if d["kind"] != "Namespace"],
}


@pytest.mark.parametrize("gap", sorted(ROUTING_GAPS))
def test_a_tenant_without_its_hostname_grant_fails(tmp_path, gap):
    problems = _routed(tmp_path, ROUTING_GAPS[gap]())
    assert len(problems) == 1
    assert "demo" in problems[0]


@pytest.mark.parametrize("ceiling", ["0", "100", "2147483647"])
def test_a_max_priority_number_is_accepted(tmp_path, ceiling):
    docs = _configmap(lambda d: d["data"].__setitem__("maxPriority", ceiling))
    assert _routed(tmp_path, docs) == []


def test_another_tenants_binding_does_not_cover_a_tenant(tmp_path):
    """Each tenant namespace needs its own binding and grant."""
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "alpha.yaml", "beta.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
    write_policy(root, policy())
    beta = wiring("beta")
    beta[2] = routing_binding("alpha")
    beta[2]["metadata"]["name"] = "tenant-traefik-hostnames-beta"
    beta[3] = routing_binding("alpha", "tenant-certificates")
    beta[3]["metadata"]["name"] = "tenant-certificates-beta"
    write_tenant(root, "beta.yaml", beta)
    problems = gate.check(root)
    assert len(problems) == 2
    assert all("beta" in p for p in problems)


def test_a_listed_tenant_file_that_is_missing_fails(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
    write_policy(root, policy())
    ((root / gate.TENANTS_KUSTOMIZATION).parent / "demo.yaml").unlink()
    problems = gate.check(root)
    assert len(problems) == 1
    assert "demo.yaml" in problems[0]


def test_the_readme_example_wires_its_hostnames(tmp_path):
    """The onboarding example is what an operator copies, so it must carry the
    hostname grant and both bindings the gate requires."""
    readme = (REPO / gate.TENANTS_KUSTOMIZATION).with_name("README.md").read_text()
    example = next(
        b for b in re.findall(r"```yaml\n(.*?)```", readme, re.S)
        if "kind: Kustomization" in b and "serviceAccountName" in b
    )
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "example-app.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, policy())
    ((root / gate.TENANTS_KUSTOMIZATION).parent / "example-app.yaml").write_text(example)
    assert gate.check(root) == []
