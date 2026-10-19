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
@pytest.mark.parametrize(
    "name",
    [
        "tenant-no-externalname-services",
        "tenant-no-manual-endpoints",
        "tenant-no-tcp-udp-routes",
        "tenant-traefik-tls-defaults",
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
    spec["matchConstraints"]["resourceRules"][0]["operations"] = ["CREATE"]


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
    "maxPriority-not-a-number": lambda: _configmap(
        lambda d: d["data"].__setitem__("maxPriority", "high")
    ),
    "namespace-missing": lambda: [d for d in wiring("demo") if d["kind"] != "Namespace"],
}


@pytest.mark.parametrize("gap", sorted(ROUTING_GAPS))
def test_a_tenant_without_its_hostname_grant_fails(tmp_path, gap):
    problems = _routed(tmp_path, ROUTING_GAPS[gap]())
    assert len(problems) == 1
    assert "demo" in problems[0]


def test_a_max_priority_number_is_accepted(tmp_path):
    docs = _configmap(lambda d: d["data"].__setitem__("maxPriority", "100"))
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
