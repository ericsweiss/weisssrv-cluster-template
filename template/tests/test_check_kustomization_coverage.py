"""Coverage for check-kustomization-coverage.py.

The live tree is in step, so it proves nothing about failure. Each arm runs
against a fixture tree, and every mutation case must FAIL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO, load_script

gate = load_script("check-kustomization-coverage.py")


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch) -> Path:
    """A one-app kubernetes/ tree, with the walk floor lowered to match."""
    monkeypatch.setattr(gate, "MINIMUM_KUSTOMIZATIONS", 1)
    monkeypatch.setattr(gate, "EXEMPT", {})
    write(tmp_path, "kubernetes/apps/demo/release.yaml", "kind: HelmRelease\n")
    write(tmp_path, "kubernetes/apps/demo/kustomization.yaml", "resources:\n  - release.yaml\n")
    return tmp_path / "kubernetes"


def test_a_fully_listed_tree_passes(tree: Path) -> None:
    assert gate.check(tree) == []
    assert gate.main([str(tree)]) == 0


def test_an_unlisted_sibling_is_reported(tree: Path) -> None:
    """Mutation case: the orphan this gate exists to catch."""
    write(tree.parent, "kubernetes/apps/demo/networkpolicy.yaml", "kind: NetworkPolicy\n")
    problems = gate.check(tree)
    assert any("networkpolicy.yaml" in p for p in problems), problems
    assert gate.main([str(tree)]) == 1


def test_a_dot_prefixed_resource_is_the_same_file(tree: Path) -> None:
    """kustomize accepts `./release.yaml`, so the gate must not call it unlisted."""
    write(tree.parent, "kubernetes/apps/demo/kustomization.yaml", "resources:\n  - ./release.yaml\n")
    assert gate.check(tree) == []


def test_generator_and_patch_forms_count_as_references(tree: Path) -> None:
    write(tree.parent, "kubernetes/apps/demo/values.yaml", "a: 1\n")
    write(tree.parent, "kubernetes/apps/demo/patch.yaml", "kind: Deployment\n")
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n"
        "  - release.yaml\n"
        "patches:\n"
        "  - path: patch.yaml\n"
        "configMapGenerator:\n"
        "  - name: demo\n"
        "    files:\n"
        "      - renamed.yaml=values.yaml\n",
    )
    assert gate.check(tree) == []


def test_a_component_in_another_directory_is_not_a_sibling(tree: Path) -> None:
    """A component is listed by its including kustomization, not by its own."""
    write(tree.parent, "kubernetes/components/netpol/deny.yaml", "kind: NetworkPolicy\n")
    write(
        tree.parent,
        "kubernetes/components/netpol/kustomization.yaml",
        "kind: Component\nresources:\n  - deny.yaml\n",
    )
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n  - release.yaml\ncomponents:\n  - ../../components/netpol\n",
    )
    assert gate.check(tree) == []


def test_an_exempt_path_is_not_reported(tree: Path, monkeypatch) -> None:
    write(tree.parent, "kubernetes/apps/demo/nodeport.yaml", "kind: Service\n")
    monkeypatch.setattr(
        gate, "EXEMPT", {"kubernetes/apps/demo/nodeport.yaml": "applied by hand"}
    )
    assert gate.check(tree) == []


def test_an_exemption_naming_a_vanished_file_is_reported(tree: Path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "EXEMPT", {"kubernetes/apps/demo/gone.yaml": "stale"})
    assert any("gone.yaml" in p for p in gate.check(tree))


def test_an_emptied_kustomization_is_reported(tree: Path) -> None:
    """Mutation case: an emptied list prunes every object the file applied."""
    write(tree.parent, "kubernetes/apps/demo/kustomization.yaml", "resources: []\n")
    assert any("renders nothing" in p for p in gate.check(tree))
    assert gate.main([str(tree)]) == 1


def test_a_transformer_only_kustomization_is_reported(tree: Path) -> None:
    """A kustomization that only transforms builds empty, so Flux prunes every
    object it applied even though the file carries keys."""
    write(tree.parent, "kubernetes/apps/demo/patch.yaml", "kind: Deployment\n")
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources: []\n"
        "patches:\n"
        "  - path: patch.yaml\n"
        "images:\n"
        "  - name: nginx\n"
        "    newTag: '1.0'\n"
        "commonLabels:\n"
        "  app: demo\n",
    )
    problems = gate.contentless(tree)
    assert any("only transformers" in p for p in problems), problems
    assert gate.main([str(tree)]) == 1


def test_a_component_carrying_only_patches_is_not_contentless(tree: Path) -> None:
    """A Component legally names no resources, so `patches` alone is content."""
    write(tree.parent, "kubernetes/apps/demo/patch.yaml", "kind: Deployment\n")
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "kind: Component\npatches:\n  - path: patch.yaml\n",
    )
    assert gate.contentless(tree) == []


def test_a_broken_walk_exits_2(tmp_path: Path, capsys) -> None:
    """A moved or emptied tree must not report clean having inspected nothing."""
    (tmp_path / "kubernetes").mkdir()
    assert gate.main([str(tmp_path / "kubernetes")]) == 2
    assert "inspected nothing" in capsys.readouterr().err


def test_a_missing_directory_exits_2(tmp_path: Path) -> None:
    assert gate.main([str(tmp_path / "absent")]) == 2


def test_an_unparseable_kustomization_exits_2(tree: Path) -> None:
    write(tree.parent, "kubernetes/apps/demo/kustomization.yaml", "resources: [\n")
    assert gate.main([str(tree)]) == 2


def test_the_live_tree_is_fully_listed() -> None:
    assert gate.check(REPO / "kubernetes") == []
    assert len(gate.kustomizations(REPO / "kubernetes")) >= gate.MINIMUM_KUSTOMIZATIONS


def test_both_remote_base_forms_are_classified_remote() -> None:
    """A pinned `?ref=` base and a scheme-only `ssh://` base must both pass, so
    neither half of the classification is dead code."""
    assert not gate._is_local("git::https://example.com/x.git//overlay?ref=v1.2.3")
    assert not gate._is_local("ssh://git@example.com/org/repo//overlay")
    assert gate._is_local("./release.yaml")
    assert gate._is_local("../../components/netpol")


def test_a_remote_base_is_not_a_missing_file(tree: Path) -> None:
    """kustomize fetches it, so the gate must not look for it on disk."""
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n"
        "  - release.yaml\n"
        "  - ssh://git@example.com/org/repo//overlay\n"
        "bases:\n"
        "  - git::https://example.com/x.git//overlay?ref=v1.2.3\n",
    )
    assert gate.check(tree) == []
    assert gate.main([str(tree)]) == 0


def test_a_name_with_no_file_behind_it_is_reported(tree: Path) -> None:
    """Mutation case: a typo'd resource fails `kustomize build` for the stage."""
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n  - release.yaml\n  - relase.yaml\n",
    )
    problems = gate.check(tree)
    assert any("kustomization.yaml -> relase.yaml" in p for p in problems), problems
    assert gate.main([str(tree)]) == 1


def test_a_listed_manifest_with_no_object_is_reported(tree: Path) -> None:
    """Mutation case: a listed file emptied to comments renders nothing."""
    write(tree.parent, "kubernetes/apps/demo/release.yaml", "# moved elsewhere\n")
    problems = gate.check(tree)
    assert any("carries no object" in p for p in problems), problems
    assert gate.main([str(tree)]) == 1


def test_a_kustomization_yml_is_walked(tree: Path) -> None:
    """kustomize reads either spelling, so a `.yml` one still has its siblings
    checked."""
    (tree / "apps/demo/kustomization.yaml").rename(tree / "apps/demo/kustomization.yml")
    assert gate.check(tree) == []
    write(tree.parent, "kubernetes/apps/demo/networkpolicy.yaml", "kind: NetworkPolicy\n")
    assert gate.check(tree) == [
        "kubernetes/apps/demo/networkpolicy.yaml: on disk, named by no kustomization, "
        "so Flux never applies it"
    ]


def test_an_unlisted_json_sibling_is_reported(tree: Path) -> None:
    """Mutation case: a dashboard JSON no configMapGenerator names never ships."""
    write(tree.parent, "kubernetes/apps/demo/dashboard.json", "{}\n")
    assert any("dashboard.json" in p for p in gate.check(tree))
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n  - release.yaml\nconfigMapGenerator:\n  - name: dash\n"
        "    files:\n      - dashboard.json\n",
    )
    assert gate.check(tree) == []


def test_files_flux_bootstrap_writes_are_not_missing_yet(tree: Path) -> None:
    """Before `flux bootstrap` runs, flux-system/ names files it will write."""
    write(
        tree.parent,
        "kubernetes/clusters/demo/flux-system/kustomization.yaml",
        "resources:\n  - gotk-components.yaml\n  - gotk-sync.yaml\n",
    )
    assert gate.missing_references(tree) == []
    write(
        tree.parent,
        "kubernetes/clusters/demo/flux-system/kustomization.yaml",
        "resources:\n  - gotk-components.yaml\n  - extra.yaml\n",
    )
    assert gate.missing_references(tree) == [
        "kubernetes/clusters/demo/flux-system/kustomization.yaml -> extra.yaml"
    ]


def test_a_bootstrapped_flux_system_missing_one_file_is_reported(tree: Path) -> None:
    """Mutation case: once bootstrap has written one file, the other is missing."""
    write(
        tree.parent,
        "kubernetes/clusters/demo/flux-system/kustomization.yaml",
        "resources:\n  - gotk-components.yaml\n  - gotk-sync.yaml\n",
    )
    write(tree.parent, "kubernetes/clusters/demo/flux-system/gotk-sync.yaml", "kind: Kustomization\n")
    assert gate.missing_references(tree) == [
        "kubernetes/clusters/demo/flux-system/kustomization.yaml -> gotk-components.yaml"
    ]


def test_a_bootstrap_name_outside_flux_system_is_missing(tree: Path) -> None:
    """Mutation case: the bootstrap exemption holds in flux-system/ only."""
    write(
        tree.parent,
        "kubernetes/apps/demo/kustomization.yaml",
        "resources:\n  - release.yaml\n  - gotk-sync.yaml\n",
    )
    assert gate.missing_references(tree) == [
        "kubernetes/apps/demo/kustomization.yaml -> gotk-sync.yaml"
    ]


def test_a_listed_manifest_that_does_not_parse_exits_2(tree: Path) -> None:
    """A listed file the gate cannot read is an inspection failure, not a finding."""
    write(tree.parent, "kubernetes/apps/demo/release.yaml", "kind: [unclosed\n")
    assert gate.main([str(tree)]) == 2
