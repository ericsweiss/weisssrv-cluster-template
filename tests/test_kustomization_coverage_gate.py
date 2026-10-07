"""Prove the shipped kustomization-coverage gate can FAIL.

The rendered manifests pass it by construction, so both silent shapes get a
case: a manifest no kustomization names, and a kustomization that names nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
from conftest import load_script

import render_cluster

# The copy every generated cluster runs, so this is the gate a cluster's own
# manifest edits are checked by.
GATE = render_cluster.REPO_ROOT / "template" / "scripts" / "check-kustomization-coverage.py"
# Read off the gate itself, so the fixture cannot drift from the exemption list
# whose staleness arm it has to satisfy.
EXEMPT = tuple(load_script(GATE).EXEMPT)

_CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "example"},
    "data": {"key": "value"},
}
_KUSTOMIZATION = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}


def _write(directory: Path, doc: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "kustomization.yaml").write_text(yaml.safe_dump(doc))


def _tree(tmp_path: Path) -> Path:
    """A fully listed manifest tree over the gate's MINIMUM_KUSTOMIZATIONS floor.

    One directory per kustomization, so each case below mutates app-00 alone.
    """
    root = tmp_path / "kubernetes"
    for index in range(25):
        directory = root / f"app-{index:02d}"
        directory.mkdir(parents=True)
        (directory / "configmap.yaml").write_text(yaml.safe_dump(_CONFIGMAP))
        _write(directory, {**_KUSTOMIZATION, "resources": ["configmap.yaml"]})
    # The hand-applied manifests EXEMPT names, in bare directories: the gate
    # reports a vanished exemption, and no kustomization is their sibling.
    for exempt in EXEMPT:
        path = tmp_path / exempt
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(_CONFIGMAP))
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), str(root)], capture_output=True, text=True, check=False
    )


def test_a_fully_listed_tree_passes(tmp_path):
    result = _run(_tree(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_manifest_no_kustomization_names_fails(tmp_path):
    """The file is valid YAML and lints clean; only this gate sees that Flux
    never builds it."""
    root = _tree(tmp_path)
    (root / "app-00" / "orphan.yaml").write_text(yaml.safe_dump(_CONFIGMAP))
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "orphan.yaml" in result.stderr


def test_an_emptied_resource_list_fails(tmp_path):
    """CRITICAL: the Flux Kustomizations prune, so an emptied list deletes every
    object that file applied rather than shipping less."""
    root = _tree(tmp_path)
    (root / "app-00" / "configmap.yaml").unlink()
    _write(root / "app-00", {**_KUSTOMIZATION, "resources": []})
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "renders nothing" in result.stderr


def test_a_kustomization_with_no_content_keys_fails(tmp_path):
    """An omitted `resources:` is the same prune as an emptied one."""
    root = _tree(tmp_path)
    (root / "app-00" / "configmap.yaml").unlink()
    _write(root / "app-00", dict(_KUSTOMIZATION))
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "renders nothing" in result.stderr


def test_a_patches_only_component_passes(tmp_path):
    """A Component legally carries patches and no resources, so the prune arm
    must not fire on one."""
    root = _tree(tmp_path)
    component = root / "app-00"
    (component / "configmap.yaml").unlink()
    (component / "patch.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "example"}})
    )
    _write(
        component,
        {
            "apiVersion": "kustomize.config.k8s.io/v1alpha1",
            "kind": "Component",
            "patches": [{"path": "patch.yaml"}],
        },
    )
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_too_few_kustomizations_is_an_operator_error(tmp_path):
    """A moved or renamed manifest tree must red the gate, not pass it having
    inspected almost nothing."""
    root = tmp_path / "kubernetes"
    _write(root / "only", {**_KUSTOMIZATION, "resources": []})
    result = _run(root)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "examining almost nothing" in result.stderr
