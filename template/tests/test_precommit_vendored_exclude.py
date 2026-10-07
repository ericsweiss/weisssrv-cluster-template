"""The whitespace hooks must skip exactly the byte-identical library copies.

A pattern that misses one lets pre-commit rewrite a vendored file and red the
byte-identity gate; one that is wider exempts repo-owned files from the fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "scripts" / "vendored-manifest.yml"
PRECOMMIT = REPO / ".pre-commit-config.yaml"
MUTATING_HOOKS = ("end-of-file-fixer", "trailing-whitespace")


def vendored_consumers() -> list[str]:
    """Consumer paths the manifest declares byte-identical."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    out = []
    for entry in doc.get("vendored") or []:
        out.append(entry if isinstance(entry, str) else entry["consumer"])
    return out


def excludes() -> dict[str, str]:
    """The `exclude` pattern of each mutating whitespace hook."""
    doc = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8")) or {}
    found = {}
    for repo in doc.get("repos") or []:
        for hook in repo.get("hooks") or []:
            if hook.get("id") in MUTATING_HOOKS:
                found[hook["id"]] = hook.get("exclude", "")
    return found


def uncovered(pattern: str, paths: list[str]) -> list[str]:
    """Manifest paths the pattern does not exempt."""
    if not pattern:
        return list(paths)
    rx = re.compile(pattern)
    return [p for p in paths if not rx.match(p)]


def test_every_vendored_copy_is_exempt_from_the_whitespace_hooks():
    paths = vendored_consumers()
    assert paths, "scripts/vendored-manifest.yml declares no vendored copy"
    found = excludes()
    for hook in MUTATING_HOOKS:
        assert hook in found, f".pre-commit-config.yaml declares no `{hook}` hook"
        missing = uncovered(found[hook], paths)
        assert not missing, (
            f"the `{hook}` hook would rewrite vendored copies: {missing}. "
            "Extend its exclude alternation to match the manifest."
        )


def test_the_exclude_does_not_exempt_repo_owned_files():
    """A `^scripts/` style prefix would skip the repo's own scripts too."""
    paths = set(vendored_consumers())
    owned = [
        p.relative_to(REPO).as_posix()
        for p in sorted((REPO / "scripts").iterdir())
        if p.is_file() and p.relative_to(REPO).as_posix() not in paths
    ]
    assert owned, "scripts/ holds nothing but vendored copies"
    for hook, pattern in excludes().items():
        rx = re.compile(pattern)
        wide = [p for p in owned if rx.match(p)]
        assert not wide, f"the `{hook}` exclude also exempts repo-owned files: {wide}"


def test_a_too_narrow_pattern_is_reported():
    """Mutation case: the collector, not just the shipped state."""
    assert uncovered(r"^scripts/shell-lib\.py$", ["scripts/shell-lib.sh"])
    assert not uncovered(r"^scripts/shell-lib\.sh$", ["scripts/shell-lib.sh"])
    assert uncovered("", ["scripts/shell-lib.sh"])
