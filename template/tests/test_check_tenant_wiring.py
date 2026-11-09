"""Site wiring for the vendored check-tenant-wiring.py.

weisssrv-lib's suite covers the gate's arms. This cluster owns the storage flag
it passes, its live tenants tree and the onboarding example a tenant copies.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO, load_script, tenant_readme_example, yaml_mappings

gate = load_script("check-tenant-wiring.py")
tenant_common = load_script("tenant_common.py")

TENANTS_DIR = tenant_common.discover_tenants_dir(REPO).relative_to(REPO)
# The gate demands the claim caps exactly when the tenant-local StorageClass ships.
TENANT_LOCAL = (
    REPO / "kubernetes" / "infrastructure" / "controllers" / "local-path-provisioner"
    / "storageclass.yaml"
)
STORAGE_FLAG = (
    "--tenant-storage-class" if TENANT_LOCAL.is_file() else "--no-tenant-storage-class"
)


def run(root: Path) -> int:
    return gate.main([STORAGE_FLAG, "--repo-root", str(root)])


def test_the_live_tree_is_clean() -> None:
    assert run(REPO) == 0


def stage_example(root: Path, example: str) -> None:
    docs = yaml_mappings(example)
    namespace = next(d for d in docs if d.get("kind") == "Namespace")["metadata"]["name"]
    tenants = root / TENANTS_DIR
    tenants.mkdir(parents=True)
    (tenants / "tenant-crd-editor.yaml").write_text("kind: ClusterRole\n")
    (tenants / f"{namespace}.yaml").write_text(example)
    (tenants / "kustomization.yaml").write_text(
        f"resources:\n  - tenant-crd-editor.yaml\n  - {namespace}.yaml\n"
    )


def test_the_readme_example_satisfies_the_gate(tmp_path: Path) -> None:
    """The onboarding example is what a tenant copies, so it must pass the gate."""
    stage_example(tmp_path, tenant_readme_example())
    assert run(tmp_path) == 0


def without(needle: str) -> str:
    """The README example with every line holding `needle` dropped."""
    lines = tenant_readme_example().splitlines()
    return "\n".join(line for line in lines if needle not in line) + "\n"


@pytest.mark.parametrize(
    "needle", ["serviceAccountName", "infrastructure-configs", "prune: true"]
)
def test_a_wiring_piece_dropped_from_the_readme_example_fails(
    tmp_path: Path, needle: str
) -> None:
    """Mutation case: the example is checked, not merely parsed."""
    stage_example(tmp_path, without(needle))
    assert run(tmp_path) == 1


CLAIM_SIZE_CAP = "tenant-local.storageclass.storage.k8s.io/requests.storage"


def test_the_claim_size_cap_is_required_exactly_when_tenant_local_ships(
    tmp_path: Path,
) -> None:
    """Mutation case for STORAGE_FLAG: the cap is the only size limit on a
    tenant-local claim, so a derivation that misses the shipped StorageClass
    would stop requiring it on every tenant."""
    assert (CLAIM_SIZE_CAP in tenant_readme_example()) == TENANT_LOCAL.is_file()
    stage_example(tmp_path, without(CLAIM_SIZE_CAP))
    assert run(tmp_path) == (1 if TENANT_LOCAL.is_file() else 0)
