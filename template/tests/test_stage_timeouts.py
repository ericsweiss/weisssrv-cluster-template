"""A Flux stage must outlast the releases it waits on.

A `wait: true` Kustomization fails the moment its own timeout expires, so each
stage keeps headroom over the slowest HelmRelease beneath its path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from conftest import REPO as REPO_ROOT
from conftest import assert_all_parsed, k8s_documents, to_seconds

K8S_ROOT = REPO_ROOT / "kubernetes"
CLUSTERS_DIR = K8S_ROOT / "clusters"


def _cluster_documents() -> tuple[list[tuple[Path, dict]], list[str]]:
    return k8s_documents(CLUSTERS_DIR)


# The GitRepository this repository's own stages reconcile from. A tenant stage
# names its own source, and its path lives in the tenant's repository.
CLUSTER_SOURCE = "flux-system"

# helm-controller's wait on a HelmRelease that declares no timeout of its own.
FLUX_DEFAULT_RELEASE_TIMEOUT = 300


def stage_kustomizations(
    documents: list[tuple[Path, dict]] | None = None,
) -> list[tuple[Path, dict]]:
    """Every waiting stage of this repository. Selected on `wait` alone: a stage
    with no explicit timeout is the case this gate exists for, so it must not
    drop out of the parametrisation."""
    found = []
    for path, doc in _cluster_documents()[0] if documents is None else documents:
        if doc.get("kind") != "Kustomization":
            continue
        if not str(doc.get("apiVersion", "")).startswith("kustomize.toolkit.fluxcd.io/"):
            continue
        spec = doc.get("spec") or {}
        if (spec.get("sourceRef") or {}).get("name") != CLUSTER_SOURCE:
            continue
        if spec.get("wait") and spec.get("path"):
            found.append((path, doc))
    return found


def _reported(path: Path, root: Path) -> Path:
    return path.relative_to(root) if path.is_relative_to(root) else path


def _spec_timeouts(doc: dict) -> list[str]:
    """The spec, install and upgrade timeouts one HelmRelease body declares."""
    spec = doc.get("spec") or {}
    values = [spec.get("timeout")]
    for phase in ("install", "upgrade"):
        values.append((spec.get(phase) or {}).get("timeout"))
    return [value for value in values if value]


def _component_timeouts(
    kustomization: Path, doc: dict, root: Path
) -> tuple[dict[str, int], list[str]]:
    """The HelmRelease timeouts the components a kustomization includes patch in.

    A component lives outside the stage path, so its patch is invisible to a
    walk of that path alone.
    """
    found: dict[str, int] = {}
    unreadable: list[str] = []
    for entry in doc.get("components") or []:
        if "://" in str(entry):
            continue
        base = Path(os.path.normpath(kustomization.parent / str(entry)))
        files = [base / name for name in ("kustomization.yaml", "kustomization.yml")]
        present = [candidate for candidate in files if candidate.is_file()]
        if not present:
            unreadable.append(
                f"{_reported(base, root)}: included as a component by "
                f"{_reported(kustomization, root)} but holds no kustomization file"
            )
            continue
        try:
            component = yaml.safe_load(present[0].read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{_reported(present[0], root)}: {exc}")
            continue
        for patch in component.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            target = patch.get("target") or {}
            if target.get("kind") != "HelmRelease":
                continue
            body = patch.get("patch")
            if body is None and patch.get("path"):
                try:
                    body = (base / str(patch["path"])).read_text()
                except OSError as exc:
                    unreadable.append(f"{_reported(base, root)}: {exc}")
                    continue
            try:
                patched = yaml.safe_load(body or "") or {}
            except yaml.YAMLError as exc:
                unreadable.append(f"{_reported(present[0], root)}: {exc}")
                continue
            if not isinstance(patched, dict):
                continue
            name = f"{_reported(base, root)}:{target.get('name', '*')}"
            for value in _spec_timeouts(patched):
                found[name] = max(to_seconds(value), found.get(name, 0))
    return found, unreadable


def release_timeouts(stage_path: str, root: Path = REPO_ROOT) -> dict[str, float]:
    """Every HelmRelease timeout (spec, install, upgrade) a stage path waits on,
    whether written in a release, patched in by a Kustomize component, or the
    helm-controller default for a release that sets none."""
    directory = root / stage_path.removeprefix("./")
    assert directory.is_dir(), (
        f"stage path {stage_path} does not exist — the gate examined nothing"
    )
    timeouts: dict[str, float] = {}
    documents, unreadable = k8s_documents(directory)
    for path, doc in documents:
        if doc.get("kind") == "HelmRelease":
            name = f"{_reported(path, root)}:{doc['metadata']['name']}"
            values = [to_seconds(value) for value in _spec_timeouts(doc)]
            timeouts[name] = max(values or [FLUX_DEFAULT_RELEASE_TIMEOUT])
            continue
        patched, failed = _component_timeouts(path, doc, root)
        unreadable += failed
        for name, seconds in patched.items():
            timeouts[name] = max(seconds, timeouts.get(name, 0))
    assert_all_parsed(unreadable, "HelmRelease")
    return timeouts


def _explicit_release_timeouts(stage_path: str, root: Path = REPO_ROOT) -> dict[str, float]:
    """The timeouts a stage path's releases declare outright, without the default fill."""
    directory = root / stage_path.removeprefix("./")
    return {
        f"{_reported(path, root)}:{doc['metadata']['name']}": max(map(to_seconds, declared))
        for path, doc in k8s_documents(directory)[0]
        if doc.get("kind") == "HelmRelease" and (declared := _spec_timeouts(doc))
    }


def _too_tight(stage: float, releases: dict[str, float]) -> list[str]:
    """Releases the stage does not outlast."""
    return [f"{name}'s {release}s" for name, release in releases.items() if stage <= release]


def test_there_are_waiting_stages_to_check():
    assert stage_kustomizations(), "no wait:true cluster Kustomization found"


def test_every_cluster_manifest_parsed():
    """A file that will not parse drops its stage out of the parametrisation
    below, which reads as a pass."""
    assert_all_parsed(_cluster_documents()[1], "Kustomization")


def test_to_seconds_parses_the_flux_spellings():
    assert to_seconds("90s") == 90
    assert to_seconds("15m") == 900
    assert to_seconds("1h30m") == 5400
    assert to_seconds("500ms") == 0.5
    with pytest.raises(AssertionError):
        to_seconds("soon")


def test_a_release_without_a_timeout_takes_the_flux_default(tmp_path):
    """Mutation case: helm-controller waits its 5m default on a release that
    sets no timeout, so a stage at or under 5m has no headroom over it."""
    stage = tmp_path / "kubernetes" / "apps"
    stage.mkdir(parents=True)
    (stage / "release.yaml").write_text(
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n"
        "metadata:\n  name: plain\nspec:\n  interval: 30m\n"
    )
    found = release_timeouts("./kubernetes/apps", root=tmp_path)
    assert found == {"kubernetes/apps/release.yaml:plain": FLUX_DEFAULT_RELEASE_TIMEOUT}
    assert _too_tight(300, found)
    assert _explicit_release_timeouts("./kubernetes/apps", root=tmp_path) == {}


def test_a_stage_that_does_not_outlast_a_release_is_reported():
    """Mutation case: the comparison, not just the shipped corpus. Equal
    timeouts are a failure — the stage has no headroom at all."""
    releases = {"release.yaml:app": 900}
    assert _too_tight(900, releases) == ["release.yaml:app's 900s"]
    assert _too_tight(600, releases)
    assert _too_tight(901, releases) == []


def test_at_least_one_stage_resolved_release_timeouts():
    """A refactor that moved every explicit HelmRelease timeout out of the stage
    paths would leave each parametrised case below comparing nothing."""
    assert any(
        _explicit_release_timeouts(doc["spec"]["path"]) for _path, doc in stage_kustomizations()
    ), (
        "no stage path holds a HelmRelease with an explicit timeout — the "
        "comparison in this gate examined nothing"
    )


def test_a_component_patched_timeout_is_counted(tmp_path):
    """Mutation case: a release can take its timeout from a component outside
    the stage path, so a walk of that path alone misses it."""
    app = tmp_path / "kubernetes" / "apps" / "runner"
    component = tmp_path / "kubernetes" / "components" / "common"
    app.mkdir(parents=True)
    component.mkdir(parents=True)
    (app / "release.yaml").write_text(
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n"
        "metadata:\n  name: runner\nspec:\n  interval: 30m\n"
    )
    (app / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources:\n  - release.yaml\ncomponents:\n  - ../../components/common\n"
    )
    (component / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\n"
        "patches:\n  - target:\n      kind: HelmRelease\n      name: runner.*\n"
        "    patch: |\n      apiVersion: helm.toolkit.fluxcd.io/v2\n"
        "      kind: HelmRelease\n      metadata:\n        name: common\n"
        "      spec:\n        timeout: 15m\n        install:\n"
        "          timeout: 15m\n"
    )
    found = release_timeouts("./kubernetes/apps", root=tmp_path)
    assert found == {
        "kubernetes/apps/runner/release.yaml:runner": FLUX_DEFAULT_RELEASE_TIMEOUT,
        "kubernetes/components/common:runner.*": 900,
    }
    assert _too_tight(900, found) == ["kubernetes/components/common:runner.*'s 900s"]


def test_a_tenant_sourced_stage_is_not_a_local_stage():
    """Mutation case: a tenant Kustomization waits on a path in its own
    repository, so measuring it against this tree fails on a missing path."""
    def kustomization(name: str, source: str) -> tuple[Path, dict]:
        return Path(f"{name}.yaml"), {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name},
            "spec": {
                "wait": True,
                "path": "./kubernetes/flux",
                "sourceRef": {"kind": "GitRepository", "name": source},
            },
        }

    documents = [kustomization("apps", CLUSTER_SOURCE), kustomization("example-app", "example-app")]
    assert [doc["metadata"]["name"] for _path, doc in stage_kustomizations(documents)] == ["apps"]


@pytest.mark.parametrize(
    "path,doc",
    stage_kustomizations(),
    ids=lambda item: item.name if isinstance(item, Path) else "",
)
def test_a_stage_outlasts_the_releases_it_waits_on(path, doc):
    timeout = (doc.get("spec") or {}).get("timeout")
    assert timeout, (
        f"{path.name}: wait:true with no explicit spec.timeout — it inherits the "
        "interval-derived default and is outside this gate"
    )
    tight = _too_tight(to_seconds(timeout), release_timeouts(doc["spec"]["path"]))
    assert not tight, (
        f"{path.name} timeout {timeout} does not exceed "
        + ", ".join(tight)
        + " — a slow but healthy install fails the stage"
    )
