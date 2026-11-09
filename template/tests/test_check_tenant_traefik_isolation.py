"""Site wiring for the vendored check-tenant-traefik-isolation.py.

weisssrv-lib's suite covers the gate's arms. This cluster owns the shipped
tenant policies, its live tenants tree and the onboarding example.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from conftest import REPO, load_script, tenant_readme_example, yaml_mappings

gate = load_script("check-tenant-traefik-isolation.py")


def test_the_live_tree_is_clean() -> None:
    """The shipped policies satisfy the gate, with or without a tenant wired."""
    assert gate.main(["--repo-root", str(REPO)]) == 0


def test_the_shipped_policies_have_no_gap() -> None:
    assert gate.policy_gaps(REPO / gate.TENANT_POLICY, REPO) == []


def test_the_gate_pins_every_shipped_policy() -> None:
    """A policy shipped here but unknown to the gate would go unpinned."""
    shipped = {
        d["metadata"]["name"]
        for d in yaml_mappings((REPO / gate.TENANT_POLICY).read_text(encoding="utf-8"))
        if d.get("kind") == "ValidatingAdmissionPolicy"
    }
    assert set(gate.REQUIRED_POLICIES) == shipped


def test_a_policy_binding_dropped_from_the_shipped_file_fails(tmp_path: Path) -> None:
    """Mutation case: removing one tenant-label Deny binding opens a gap."""
    for rel in (gate.TENANT_POLICY, gate.CONFIGS_KUSTOMIZATION):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, tmp_path / rel)
    path = tmp_path / gate.TENANT_POLICY
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    binding = next(
        i for i, d in enumerate(docs)
        if isinstance(d, dict) and d.get("kind") == "ValidatingAdmissionPolicyBinding"
    )
    del docs[binding]
    path.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    assert gate.policy_gaps(path, tmp_path) != []


def readme_example() -> list[dict]:
    return yaml_mappings(tenant_readme_example())


def test_the_readme_example_wires_its_hostnames() -> None:
    """The onboarding example is what an operator copies, so it must carry the
    hostname grant and both bindings the gate requires."""
    assert gate.routing_problems_for({"example.yaml": readme_example()}) == []


def test_the_readme_example_without_its_hostname_grant_fails() -> None:
    """Mutation case: the example routes only while its grant is present."""
    docs = readme_example()
    kept = [
        d for d in docs
        if not (d.get("kind") == "ConfigMap"
                and d["metadata"]["name"].startswith(gate.GRANT_PREFIX))
    ]
    assert len(kept) < len(docs)
    assert gate.routing_problems_for({"example.yaml": kept}) != []
