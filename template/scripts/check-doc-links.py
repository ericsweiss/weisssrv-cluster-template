#!/usr/bin/env python3
"""Offline checker for relative Markdown cross-links in the repo's docs.

docs/ and the top-level READMEs are the declared source of truth (CLAUDE.md),
and the tree carries dozens of internal `](docs/NN-*.md)` / `](../NN-*.md)`
links plus the README docs-index table. Docs are renumbered/reorganized over
time, so a renamed or deleted doc silently rots these links. This gate resolves
every relative `.md` link against the filesystem and fails on a missing target.

Scope (intentionally narrow to stay false-positive-free):
- Only Markdown inline links `[text](target)` whose target is a *relative*
  `.md` path (optionally with a `#anchor`) are checked. URLs (http/https/
  mailto), in-page `#anchor` links, and links to non-`.md` targets (images,
  directories, code) are ignored — the finding's scope is doc cross-links.
- Targets are resolved relative to the containing file's directory.
- Anchors are not validated (only that the target file exists).

Run via `pytest scripts/` (test_check_doc_links.py) or directly:
  scripts/check-doc-links.py            # scan the repo (docs/, README.md, CLAUDE.md)
  scripts/check-doc-links.py <root>...  # scan an explicit repo root
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# [text](target) — capture the target up to the first unescaped ')'. Good enough
# for a lint gate; the target is further split on whitespace to drop any
# `](path "title")` title and on '#' to drop the anchor.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

_SKIP_PREFIXES = ("http://", "https://", "mailto:", "//", "#", "tel:")


def doc_files(root: Path) -> list[Path]:
    """The Markdown files to scan: everything under docs/, plus the top-level
    README.md / CLAUDE.md and ansible/TESTING.md when present."""
    files: list[Path] = []
    docs = root / "docs"
    if docs.is_dir():
        files.extend(sorted(docs.rglob("*.md")))
    for name in ("README.md", "CLAUDE.md", "ansible/TESTING.md"):
        p = root / name
        if p.is_file():
            files.append(p)
    return files


def _relative_md_target(raw: str) -> str | None:
    """Return the relative `.md` path from a link target, or None if the link
    is a URL, a pure anchor, or does not point at a `.md` file."""
    target = raw.strip()
    if not target or target.startswith(_SKIP_PREFIXES):
        return None
    # Drop a `](path "title")` title, then a trailing #anchor.
    path = target.split()[0].split("#", 1)[0]
    if not path or not path.endswith(".md"):
        return None
    return path


def broken_links(files: list[Path]) -> list[tuple[Path, str]]:
    """Return (source_file, link_target) for every relative `.md` link whose
    resolved target file does not exist."""
    broken: list[tuple[Path, str]] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _LINK_RE.finditer(text):
            rel = _relative_md_target(m.group(1))
            if rel is None:
                continue
            resolved = (f.parent / rel).resolve()
            if not resolved.is_file():
                broken.append((f, m.group(1).strip()))
    return broken


def main(argv: list[str]) -> int:
    roots = [Path(a).resolve() for a in argv[1:]] or [REPO]
    files: list[Path] = []
    for root in roots:
        files.extend(doc_files(root))
    if not files:
        print(f"ERROR: no Markdown files found under: {', '.join(str(r) for r in roots)}")
        return 1

    broken = broken_links(files)
    if broken:
        print(f"ERROR: {len(broken)} broken relative Markdown link(s):")
        for src, target in broken:
            try:
                shown = src.relative_to(REPO)
            except ValueError:
                shown = src
            print(f"  {shown}: [...]({target})")
        return 1

    print(f"OK: {len(files)} Markdown file(s) scanned, all relative .md links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
