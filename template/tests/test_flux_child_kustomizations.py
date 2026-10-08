"""scripts/flux-child-kustomizations.py: dependsOn order and the empty cases.

The flux-render child pipeline renders exactly what this prints, so a silently
empty list would pass every render job on nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO, load_script

tool = load_script("flux-child-kustomizations.py")

CLUSTERS = REPO / "kubernetes" / "clusters"

needs_cluster = pytest.mark.skipif(
    not CLUSTERS.is_dir(), reason="this repository ships no kubernetes/clusters tree"
)

KUSTOMIZATION = """---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: %s
spec:
  path: ./kubernetes/%s
%s
"""

PATHLESS_KUSTOMIZATION = """---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: %s
spec:
  sourceRef:
    kind: GitRepository
    name: flux-system
"""


def _cluster(tmp_path: Path, stages) -> Path:
    """A scratch cluster directory holding one Kustomization per stage."""
    directory = tmp_path / "kubernetes" / "clusters" / "demo"
    directory.mkdir(parents=True)
    for name, deps in stages:
        block = ""
        if deps:
            block = "  dependsOn:\n" + "".join("    - name: %s\n" % d for d in deps)
        (directory / f"{name}.yaml").write_text(
            KUSTOMIZATION % (name, name, block), encoding="utf-8"
        )
    return directory


def _add_pathless(directory: Path, name: str) -> None:
    (directory / f"{name}.yaml").write_text(
        PATHLESS_KUSTOMIZATION % name, encoding="utf-8"
    )


def _ordered(directory: Path):
    """(ordered names, names caught in a cycle), as main() composes them."""
    return tool.child_kustomizations(directory)


def test_dependencies_come_before_their_dependants(tmp_path) -> None:
    directory = _cluster(tmp_path, [
        ("apps", ["configs"]),
        ("configs", ["controllers"]),
        ("controllers", []),
    ])
    assert _ordered(directory)[0] == ["controllers", "configs", "apps"]


def test_independent_stages_are_sorted_so_the_output_is_stable(tmp_path) -> None:
    directory = _cluster(tmp_path, [("zebra", []), ("alpha", []), ("middle", [])])
    assert _ordered(directory)[0] == ["alpha", "middle", "zebra"]


def test_a_subdirectory_is_not_walked(tmp_path) -> None:
    """The glob is flat, which is what keeps flux-system/ and tenants/ out."""
    directory = _cluster(tmp_path, [("apps", [])])
    nested = directory / "flux-system"
    nested.mkdir()
    (nested / "gotk-sync.yaml").write_text(
        KUSTOMIZATION % ("flux-system", "flux-system", ""), encoding="utf-8"
    )
    assert _ordered(directory)[0] == ["apps"]


def test_a_dependson_cycle_is_reported_and_still_deterministic(tmp_path) -> None:
    directory = _cluster(tmp_path, [("one", ["two"]), ("two", ["one"])])
    ordered, cycled = _ordered(directory)
    assert ordered == ["one", "two"]
    assert cycled == ["one", "two"]


def test_a_dependson_cycle_is_an_operator_error(tmp_path, capsys) -> None:
    """The printed order cannot satisfy the declared dependencies."""
    directory = _cluster(tmp_path, [("one", ["two"]), ("two", ["one"])])
    assert tool.main(["--dir", str(directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out.split() == ["one", "two"]
    assert "cycle among one, two" in captured.err


def test_a_non_flux_kustomization_is_ignored(tmp_path) -> None:
    directory = _cluster(tmp_path, [("apps", [])])
    (directory / "kustomize.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources: []\n", encoding="utf-8",
    )
    assert _ordered(directory)[0] == ["apps"]


def test_paths_mode_prints_the_spec_paths(tmp_path, capsys) -> None:
    directory = _cluster(tmp_path, [("apps", ["configs"]), ("configs", [])])
    assert tool.main(["--dir", str(directory), "--paths"]) == 0
    assert capsys.readouterr().out.split() == [
        "./kubernetes/configs", "./kubernetes/apps",
    ]


def test_a_directory_with_no_kustomization_is_a_finding(tmp_path, capsys) -> None:
    """Vacuity arm one: an empty list must not read as a clean run."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tool.main(["--dir", str(empty)]) == 1
    assert capsys.readouterr().out.strip() == ""


def test_a_pathless_kustomization_is_never_printed_as_a_path(tmp_path, capsys) -> None:
    """Vacuity arm two: a stage declaring no spec.path cannot enter the corpus."""
    directory = _cluster(tmp_path, [("apps", [])])
    _add_pathless(directory, "observability")
    tool.main(["--dir", str(directory), "--paths"])
    assert "observability" not in capsys.readouterr().out


def test_a_tree_where_no_kustomization_declares_a_path_is_a_finding(
    tmp_path, capsys
) -> None:
    """Vacuity arm three: the child pipeline would render nothing at all."""
    directory = _cluster(tmp_path, [])
    _add_pathless(directory, "apps")
    _add_pathless(directory, "observability")
    assert tool.main(["--dir", str(directory), "--paths"]) == 1
    captured = capsys.readouterr()
    assert "declares a spec.path" in captured.err
    assert captured.out.strip() == ""


def test_a_missing_directory_is_an_operator_error(tmp_path) -> None:
    """exit 1 would read as a policy finding, not a wrong --dir."""
    assert tool.main(["--dir", str(tmp_path / "nope")]) == 2


def test_the_default_directory_is_the_single_cluster(tmp_path, monkeypatch) -> None:
    _cluster(tmp_path, [("apps", [])])
    monkeypatch.chdir(tmp_path)
    assert tool.main([]) == 0


def test_two_cluster_directories_require_an_explicit_choice(tmp_path, monkeypatch) -> None:
    _cluster(tmp_path, [("apps", [])])
    (tmp_path / "kubernetes" / "clusters" / "other").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        tool.main([])


@needs_cluster
def test_this_cluster_yields_a_path_for_every_stage(capsys) -> None:
    """The shipped tree, not a fixture: the corpus the render jobs actually get."""
    directories = sorted(p for p in CLUSTERS.iterdir() if p.is_dir())
    assert len(directories) == 1, f"expected one cluster directory, got {directories}"
    assert tool.main(["--dir", str(directories[0]), "--paths"]) == 0
    paths = capsys.readouterr().out.split()
    assert paths, "the flux-render child pipeline would render nothing"
    stray = [p for p in paths if not p.startswith("./kubernetes/")]
    assert not stray, f"spec.path outside the manifest tree: {stray}"
