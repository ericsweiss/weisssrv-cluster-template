"""Tests that check-tenant-traefik-isolation.py fails when a tenant lands while Traefik allows cross-namespace refs."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import load_script

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


def test_a_tenant_passes_once_cross_namespace_is_off(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "hermes-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="false"),
    )
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


POLICY = textwrap.dedent(
    """\
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicy
    metadata:
      name: tenant-traefik-route-refs
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicy
    metadata:
      name: tenant-traefik-object-refs
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicyBinding
    metadata:
      name: tenant-traefik-route-refs
    spec:
      policyName: tenant-traefik-route-refs
      validationActions: [{action}]
      matchResources:
        namespaceSelector:
          matchExpressions:
            - key: fluxcd.io/tenant
              operator: Exists
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicyBinding
    metadata:
      name: tenant-traefik-object-refs
    spec:
      policyName: tenant-traefik-object-refs
      validationActions: [Deny]
      matchResources:
        namespaceSelector:
          matchExpressions:
            - key: fluxcd.io/tenant
              operator: Exists
    """
)


def write_policy(root: Path, body: str) -> None:
    path = root / gate.TENANT_POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_a_tenant_passes_when_the_admission_policies_pin_refs(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, POLICY.format(action="Deny"))
    assert gate.check(root) == []


def test_a_warn_only_binding_does_not_open_the_gate(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    write_policy(root, POLICY.format(action="Warn"))
    assert len(gate.check(root)) == 1


def test_a_policy_file_missing_one_binding_keeps_the_gate_closed(tmp_path):
    root = build(
        tmp_path,
        ["tenant-crd-editor.yaml", "demo-tenant.yaml"],
        RELEASE_WITH_VALUES.format(allow="true"),
    )
    body = POLICY.format(action="Deny").rsplit("---\n", 1)[0]
    write_policy(root, body)
    assert len(gate.check(root)) == 1
