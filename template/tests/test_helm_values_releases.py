"""Coverage for scripts/helm-values-releases.yaml.

The list decides which HelmRelease gets `helm template` run over its values, and
chart identity comes from the entry, so a missing or drifted entry validates nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import REPO, assert_all_parsed, k8s_documents

RELEASES = REPO / "scripts" / "helm-values-releases.yaml"
SOURCES = REPO / "kubernetes/infrastructure/sources"
FIELDS = ("name", "manifest", "chart", "repo_name", "repo_url")


def _config() -> dict:
    return yaml.safe_load(RELEASES.read_text())


def _entries() -> list[dict]:
    return _config()["releases"]


def _excluded() -> dict[str, str]:
    return _config().get("excluded") or {}


def helmrelease_manifests(root: Path) -> tuple[set[str], list[str]]:
    """Paths under kubernetes/ holding a `kind: HelmRelease` document, with the
    manifests that would not parse."""
    documents, unreadable = k8s_documents(root / "kubernetes")
    found = {
        path.relative_to(root).as_posix()
        for path, doc in documents
        if doc.get("kind") == "HelmRelease"
    }
    return found, unreadable


def uncovered(root: Path, listed: set[str], excluded: set[str]) -> list[str]:
    return sorted(helmrelease_manifests(root)[0] - listed - excluded)


def _helm_release(relpath: str) -> dict:
    """The HelmRelease document in a manifest that may hold siblings."""
    path = REPO / relpath
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict) and doc.get("kind") == "HelmRelease":
            return doc
    raise AssertionError(f"{relpath} holds no HelmRelease")


def _helmrepositories() -> dict[str, str]:
    """name -> url for every HelmRepository under infrastructure/sources/."""
    repos: dict[str, str] = {}
    for path in sorted(SOURCES.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "HelmRepository":
                repos[doc["metadata"]["name"]] = (doc.get("spec") or {}).get("url")
    return repos


def test_every_entry_is_complete_and_names_a_real_manifest() -> None:
    for rel in _entries():
        for field in FIELDS:
            assert rel.get(field), f"{rel} is missing {field}"
        assert (REPO / rel["manifest"]).is_file(), f"{rel['name']}: {rel['manifest']} missing"


def test_entries_agree_with_the_helmrelease_they_name() -> None:
    problems = []
    for rel in _entries():
        doc = _helm_release(rel["manifest"])
        chart_spec = (((doc.get("spec") or {}).get("chart") or {}).get("spec")) or {}
        # `name` is a logging label, not what the gate renders from, so it is not
        # compared: a release name can differ from the chart name.
        if rel["chart"] != chart_spec.get("chart"):
            problems.append(
                f"{rel['name']}: entry chart {rel['chart']!r} != manifest chart "
                f"{chart_spec.get('chart')!r}"
            )
        source_ref = (chart_spec.get("sourceRef") or {}).get("name")
        if rel["repo_name"] != source_ref:
            problems.append(
                f"{rel['name']}: entry repo_name {rel['repo_name']!r} != manifest "
                f"sourceRef {source_ref!r}"
            )
    assert not problems, problems


def test_every_helmrelease_is_listed_or_excluded() -> None:
    listed = {rel["manifest"] for rel in _entries()}
    missing = uncovered(REPO, listed, set(_excluded()))
    assert not missing, (
        "HelmRelease manifests with no `helm template` coverage: "
        f"{missing}. Add each to scripts/helm-values-releases.yaml, or to its "
        "`excluded:` block with the reason it cannot be rendered."
    )


def test_every_manifest_parsed() -> None:
    """A file that will not parse drops its HelmReleases from the coverage set,
    which reads as a pass while its values are never helm-template validated."""
    assert_all_parsed(helmrelease_manifests(REPO)[1], "HelmRelease")


def test_no_exclusion_is_stale_and_each_carries_a_reason() -> None:
    found = helmrelease_manifests(REPO)[0]
    excluded = _excluded()
    stale = sorted(set(excluded) - found)
    assert not stale, f"`excluded:` names manifests that hold no HelmRelease: {stale}"
    for manifest, reason in excluded.items():
        assert str(reason).strip(), f"{manifest} is excluded with no reason"


def test_repo_urls_match_the_helmrepository_flux_uses() -> None:
    repos = _helmrepositories()
    problems = []
    for rel in _entries():
        url = repos.get(rel["repo_name"])
        if url is None:
            problems.append(
                f"{rel['name']}: no HelmRepository named {rel['repo_name']!r} under "
                "infrastructure/sources/"
            )
        elif url.rstrip("/") != rel["repo_url"].rstrip("/"):
            problems.append(
                f"{rel['name']}: entry repo_url {rel['repo_url']!r} != HelmRepository "
                f"url {url!r}"
            )
    assert not problems, problems


def test_an_unlisted_helmrelease_is_reported(tmp_path: Path) -> None:
    """The live tree is covered, so the walk is proved against a fixture tree."""
    manifest = tmp_path / "kubernetes/apps/demo/release.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n"
        "metadata:\n  name: demo\n"
    )
    assert uncovered(tmp_path, set(), set()) == ["kubernetes/apps/demo/release.yaml"]
    assert uncovered(tmp_path, {"kubernetes/apps/demo/release.yaml"}, set()) == []
    assert uncovered(tmp_path, set(), {"kubernetes/apps/demo/release.yaml"}) == []


def test_the_release_reader_handles_a_multi_document_manifest(tmp_path: Path) -> None:
    """Mutation case: a manifest that gained a sibling document must still yield
    its HelmRelease, and one holding none must fail with this gate's message."""
    pair = tmp_path / "release.yaml"
    pair.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo-values\n---\n"
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n"
        "metadata:\n  name: demo\nspec:\n  chart:\n    spec:\n      chart: demo\n"
    )
    doc = _helm_release(str(pair))
    chart = (((doc.get("spec") or {}).get("chart") or {}).get("spec")) or {}
    assert chart.get("chart") == "demo"

    alone = tmp_path / "configmap.yaml"
    alone.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n")
    with pytest.raises(AssertionError, match="holds no HelmRelease"):
        _helm_release(str(alone))
