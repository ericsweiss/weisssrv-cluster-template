"""scripts/comment-length.yaml must exclude every copy vendored from weisssrv-lib.

A vendored file's comments are the library's: a trim here is reverted by the next
re-vendor, so the gate skips those copies and the library gates its own tree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
GATE = "scripts/check-comment-length.py"
CONFIG = REPO / "scripts" / "comment-length.yaml"
MANIFEST = REPO / "scripts" / "vendored-manifest.yml"


def _config() -> dict:
    doc = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{CONFIG} must be a mapping"
    return doc


def _excludes() -> set[str]:
    excluded = set(_config().get("exclude") or [])
    assert excluded, f"{CONFIG} excludes nothing — the vendored copies would be scanned"
    return excluded


def _vendored_paths() -> set[str]:
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    paths = {
        entry if isinstance(entry, str) else (entry.get("consumer") or entry["lib"])
        for section in ("vendored", "forked")
        for entry in doc.get(section) or []
    }
    assert paths, f"{MANIFEST} lists no vendored copies"
    return paths


def _missing(excluded: set[str], vendored: set[str]) -> list[str]:
    return sorted(vendored - excluded)


def test_every_vendored_copy_is_excluded() -> None:
    missing = _missing(_excludes(), _vendored_paths())
    assert not missing, (
        f"{CONFIG.name} must exclude every path {MANIFEST.name} declares, or the gate "
        f"fails on comments only a re-vendor can change: {missing}"
    )


def test_an_unexcluded_vendored_copy_is_caught() -> None:
    """Mutation case: the gate's claim is empty if a gap reads as clean."""
    assert _missing({"scripts/shell-lib.sh"}, {"scripts/shell-lib.sh"}) == []
    assert _missing(set(), {"scripts/shell-lib.sh"}) == ["scripts/shell-lib.sh"]


def test_the_gate_excludes_itself() -> None:
    """It is a library copy too, and its own docstring is over the limit."""
    assert GATE in _excludes(), f"{CONFIG.name} must exclude {GATE}"


def test_every_exclude_names_a_real_file() -> None:
    absent = sorted(path for path in _excludes() if not (REPO / path).exists())
    assert not absent, (
        f"{CONFIG.name} excludes paths that do not exist — a stale exclude drops a file "
        f"from the scan silently: {absent}"
    )


def test_the_whole_tree_is_in_scope() -> None:
    assert _config().get("paths") == ["."], (
        f"{CONFIG.name} must scan the repository root: the comment rule applies to "
        "every file, and a narrowed path list lets an over-long block land outside it"
    )
