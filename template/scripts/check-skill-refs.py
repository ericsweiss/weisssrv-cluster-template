#!/usr/bin/env python3
"""Assert every repo path the cluster-development skill names still exists.

The skill cites files as backticked paths, not Markdown links, so
check-doc-links.py sees nothing in it and a rename rots its pointers silently.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = Path(".claude/skills/cluster-development")

# A backticked path with at least one "/" and a known file extension. Bare
# basenames (`hosts.yml`, `kustomization.yaml`) are prose shorthand, not
# pointers, and directory mentions carry no extension to match.
PATH_RE = re.compile(
    r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*"
    r"\.(?:md|py|sh|yml|yaml|tf|json|hujson|toml))`"
)

# Paths that belong to weisssrv-lib, which this repository does not contain.
FOREIGN = {
    "docs/INCLUDE-CONTRACT.md",
    "docs/VERSIONING.md",
    "docs/EXTENSIBILITY.md",
}

# Minimum tokens a healthy scan finds; a regex that stopped matching would make
# the assertion vacuous. The skill ships SKILL.md plus the references/ pages,
# and both answer fixtures resolve well over 40 paths.
MIN_TOKENS = 30


def cited_paths(skill_dir: Path, repo_root: Path) -> list[tuple[Path, str]]:
    """(skill file, cited path) for every resolvable-looking path token."""
    top_level = {p.name for p in repo_root.iterdir()}
    found: list[tuple[Path, str]] = []
    for md in sorted(skill_dir.rglob("*.md")):
        for match in PATH_RE.finditer(md.read_text(encoding="utf-8")):
            raw = match.group(1)
            if "<" in raw or "*" in raw or raw in FOREIGN:
                continue
            if (md.parent / raw).exists():
                found.append((md, raw))
                continue
            if raw.split("/", 1)[0] not in top_level:
                continue
            found.append((md, raw))
    return found


def missing(tokens: list[tuple[Path, str]], repo_root: Path) -> list[str]:
    return sorted(
        {
            f"{md.relative_to(repo_root)}: {raw}"
            for md, raw in tokens
            if not (md.parent / raw).exists() and not (repo_root / raw).exists()
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument("--skill-dir", type=Path, default=None)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    skill_dir = (args.skill_dir or (repo_root / SKILL_DIR)).resolve()

    if not skill_dir.is_dir() or not list(skill_dir.rglob("*.md")):
        print(f"ERROR: no skill Markdown under {skill_dir}", file=sys.stderr)
        return 2

    tokens = cited_paths(skill_dir, repo_root)
    if len(tokens) < MIN_TOKENS:
        print(
            f"ERROR: only {len(tokens)} backticked repo paths parsed out of the "
            f"skill (expected at least {MIN_TOKENS}) - the scan is not seeing "
            "its subject",
            file=sys.stderr,
        )
        return 2

    gone = missing(tokens, repo_root)
    if gone:
        print("Skill cites paths that do not exist:", file=sys.stderr)
        for entry in gone:
            print(f"  {entry}", file=sys.stderr)
        return 1

    print(f"check-skill-refs: {len(tokens)} cited paths resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
