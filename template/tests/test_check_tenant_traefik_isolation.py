"""Tests that check-tenant-traefik-isolation.py fails when a tenant lands without the full Deny-bound policies."""
from __future__ import annotations

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


def build(tmp_path: Path, resources: list[str], release: str) -> Path:
    kustomization = tmp_path / gate.TENANTS_KUSTOMIZATION
    kustomization.parent.mkdir(parents=True, exist_ok=True)
    kustomization.write_text(
        "resources:\n" + "".join(f"  - {r}\n" for r in resources)
    )
    path = tmp_path / gate.TRAEFIK_RELEASE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(release)
    return tmp_path


def test_baseline_only_passes(tmp_path):
    root = build(tmp_path, ["tenant-crd-editor.yaml"], RELEASE_WITH_VALUES.format(allow="true"))
    assert gate.check(root) == []


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


# An ExternalName or selectorless Service, or hand-written endpoints, would
# send Traefik into another namespace past the forwardAuth pin, so the gate
# needs both refusals too.
@pytest.mark.parametrize("kind", ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])
@pytest.mark.parametrize("name", ["tenant-no-externalname-services", "tenant-no-manual-endpoints"])
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
    spec["matchConstraints"]["resourceRules"][0]["operations"] = ["CREATE"]


# Each mutation weakens one policy while its name and Deny binding still read
# true: errors fail open, a validation is dropped, or the match narrows.
WEAKENINGS = {
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
@pytest.mark.parametrize("name", sorted(gate.REQUIRED_POLICIES))
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
