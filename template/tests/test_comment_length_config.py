"""scripts/comment-length.yaml excludes exactly the vendored weisssrv-lib copies.

A vendored file's comments are the library's; a fork's are its own and scanned.
"""
from __future__ import annotations

import yaml
from conftest import REPO, SCRIPTS, vendored_consumer_paths
from conftest import VENDORED_MANIFEST as MANIFEST

CONFIG = SCRIPTS / "comment-length.yaml"


def _config() -> dict:
    doc = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{CONFIG} must be a mapping"
    return doc


def _excludes() -> set[str]:
    excluded = set(_config().get("exclude") or [])
    assert excluded, f"{CONFIG} excludes nothing — the vendored copies would be scanned"
    return excluded


def _drift(excluded: set[str], vendored: set[str]) -> tuple[list[str], list[str]]:
    """(vendored copies left in scope, excludes that are no vendored copy)."""
    return sorted(vendored - excluded), sorted(excluded - vendored)


def test_the_excludes_are_exactly_the_vendored_copies() -> None:
    vendored = set(vendored_consumer_paths(("vendored",)))
    assert vendored, f"{MANIFEST} lists no vendored copies"
    missing, extra = _drift(_excludes(), vendored)
    assert not missing, (
        f"{CONFIG.name} must exclude every path {MANIFEST.name} vendors, or the gate "
        f"fails on comments only a re-vendor can change: {missing}"
    )
    assert not extra, (
        f"{CONFIG.name} exempts paths {MANIFEST.name} does not vendor — a local file "
        f"or fork cannot be exempted: {extra}"
    )


def test_a_drifted_exclude_list_is_caught() -> None:
    """Mutation case: a gap or a stray exclude must not read as clean."""
    assert _drift({"scripts/a.py"}, {"scripts/a.py"}) == ([], [])
    assert _drift({"scripts/a.py", "scripts/local.py"}, {"scripts/a.py", "scripts/b.py"}) == (
        ["scripts/b.py"],
        ["scripts/local.py"],
    )


def test_every_exclude_names_a_real_file() -> None:
    absent = sorted(path for path in _excludes() if not (REPO / path).exists())
    assert not absent, (
        f"{CONFIG.name} excludes paths that do not exist — a stale exclude drops a file "
        f"from the scan silently: {absent}"
    )


def test_the_whole_tree_is_in_scope() -> None:
    assert _config().get("paths") == ["."], (
        f"{CONFIG.name} must scan the repository root: the comment rule applies to every "
        f"file, and a narrowed path list lets an over-long block land outside it"
    )
