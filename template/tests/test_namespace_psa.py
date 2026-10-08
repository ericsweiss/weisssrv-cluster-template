"""Every Namespace this repo declares carries Pod Security Admission labels.

An unlabelled namespace admits at `privileged` silently, built-ins included.
"""

from __future__ import annotations

import fnmatch
import functools
from pathlib import Path

import pytest
from conftest import REPO as REPO_ROOT
from conftest import assert_all_parsed, k8s_documents

K8S_ROOT = REPO_ROOT / "kubernetes"
LEVELS = {"privileged", "baseline", "restricted"}
ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"

# Files `flux bootstrap` writes and replaces wholesale on every Flux bump.
EXEMPT = {
    "kubernetes/clusters/*/flux-system/gotk-components.yaml": (
        "written by `flux bootstrap` with pod-security warn labels only; the "
        "enforce label comes from the hand-authored flux-system kustomization patch"
    ),
}


def _exempt(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in EXEMPT)


def _enforces(doc: dict) -> bool:
    labels = (doc.get("metadata") or {}).get("labels") or {}
    return labels.get(ENFORCE_LABEL) in LEVELS


@functools.cache
def _scan() -> tuple[tuple[tuple[str, Path, dict], ...], tuple[str, ...]]:
    """(declared namespaces, unreadable manifests) from one walk of the tree."""
    documents, unreadable = k8s_documents(K8S_ROOT)
    found = [
        (doc["metadata"]["name"], path, doc)
        for path, doc in documents
        if doc.get("kind") == "Namespace"
    ]
    return tuple(found), tuple(unreadable)


def namespaces() -> tuple[tuple[str, Path, dict], ...]:
    """The namespaces this gate judges: files matching EXEMPT fall out."""
    return tuple(
        item for item in _scan()[0] if not _exempt(str(item[1].relative_to(REPO_ROOT)))
    )


def test_the_repo_declares_namespaces():
    assert namespaces(), "no Namespace object found under kubernetes/"


def test_every_manifest_parsed():
    """A file that will not parse drops its Namespaces from the parametrisation
    above, which reads as a pass."""
    assert_all_parsed(list(_scan()[1]), "Namespace")


@pytest.mark.parametrize(
    "name,path,doc", namespaces(), ids=lambda item: item if isinstance(item, str) else ""
)
def test_a_namespace_sets_its_admission_level(name, path, doc):
    assert _enforces(doc), (
        f"{path.relative_to(REPO_ROOT)}: namespace {name} has no "
        f"{ENFORCE_LABEL} label, so it admits at privileged"
    )


def test_every_exemption_is_still_needed():
    """An exempt file that gained an enforce label leaves EXEMPT. That a pattern
    matches nothing is not an error: before bootstrap the tree ships no gotk file."""
    for pattern, reason in EXEMPT.items():
        assert len(reason.split()) >= 8, f"{pattern} needs a stated reason"
    for name, path, doc in _scan()[0]:
        rel = str(path.relative_to(REPO_ROOT))
        if _exempt(rel):
            assert not _enforces(doc), (
                f"{rel}: namespace {name} now enforces an admission level — "
                "drop its pattern from EXEMPT"
            )


def test_the_exemption_is_scoped_to_the_bootstrap_file():
    """Mutation case: the path filter and the label check, not the shipped corpus."""
    assert _exempt("kubernetes/clusters/anything/flux-system/gotk-components.yaml")
    assert not _exempt("kubernetes/clusters/anything/flux-system/gotk-sync.yaml")
    assert not _exempt("kubernetes/apps/downloads/namespace.yaml")
    assert not _enforces({"metadata": {"labels": {"pod-security.kubernetes.io/warn": "restricted"}}})
    assert _enforces({"metadata": {"labels": {ENFORCE_LABEL: "restricted"}}})


def test_the_shared_walk_reports_a_manifest_that_will_not_parse(tmp_path):
    """Mutation case for conftest.k8s_documents: an unparseable file must come
    back as a reported path, not be skipped out of the corpus."""
    (tmp_path / "ns.yaml").write_text("kind: Namespace\nmetadata:\n  name: demo\n")
    (tmp_path / "broken.yaml").write_text("kind: Namespace\n  name: [unclosed\n")
    documents, unreadable = k8s_documents(tmp_path)
    assert [doc.get("kind") for _path, doc in documents] == ["Namespace"]
    assert len(unreadable) == 1 and "broken.yaml" in unreadable[0]
    with pytest.raises(AssertionError, match="any Namespace they declare"):
        assert_all_parsed(unreadable, "Namespace")
    assert assert_all_parsed([], "Namespace") is None


def test_the_shared_walk_sees_both_yaml_spellings(tmp_path):
    """A `.yml` manifest is as live as a `.yaml` one, so it must be in the
    corpus; `suffix` is still there for a caller that means one spelling."""
    (tmp_path / "ns.yaml").write_text("kind: Namespace\nmetadata:\n  name: long\n")
    (tmp_path / "ns.yml").write_text("kind: Namespace\nmetadata:\n  name: short\n")
    documents, unreadable = k8s_documents(tmp_path)
    assert not unreadable
    assert {doc["metadata"]["name"] for _path, doc in documents} == {"long", "short"}
    narrowed, _ = k8s_documents(tmp_path, suffix="*.yaml")
    assert {doc["metadata"]["name"] for _path, doc in narrowed} == {"long"}
