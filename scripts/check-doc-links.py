#!/usr/bin/env python3
"""Offline checker for relative Markdown cross-links.

A renamed or deleted doc silently rots every `](docs/NN-*.md)` link pointing at
it. This gate resolves every relative `.md` link against the filesystem and
fails on a missing target.

Scope: every *tracked* `*.md` in the repo (role/app/agent READMEs cross-link
into docs/ too), falling back to docs/ + $CHECK_DOC_LINKS_EXTRA when the root
is not a git checkout. Only inline `[text](target)` links to relative `.md`
paths are checked — URLs, in-page anchors and non-`.md` targets are ignored,
and anchors themselves are not validated.

  scripts/check-doc-links.py            # scan every tracked *.md in the repo
  scripts/check-doc-links.py <root>...  # scan explicit roots
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# [text](target) — capture the target up to the first unescaped ')'. Good enough
# for a lint gate; the target is further split on whitespace to drop any
# `](path "title")` title and on '#' to drop the anchor.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

_SKIP_PREFIXES = ("http://", "https://", "mailto:", "//", "#", "tel:")


def _tracked_markdown(root: Path) -> list[Path]:
    """Every git-tracked *.md under `root`; empty when git cannot answer.

    Tracked-only is deliberate: untracked scratch Markdown is not ours to gate,
    and including it would make the check fail differently on every machine.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    files = [root / p for p in out.split("\0") if p]
    return sorted(f for f in files if f.is_file())


def doc_files(root: Path) -> list[Path]:
    """The Markdown files to scan: every tracked *.md, or — outside a git
    checkout — everything under docs/ plus $CHECK_DOC_LINKS_EXTRA."""
    tracked = _tracked_markdown(root)
    if tracked:
        return tracked

    files: list[Path] = []
    docs = root / "docs"
    if docs.is_dir():
        files.extend(sorted(docs.rglob("*.md")))
    for name in os.environ.get("CHECK_DOC_LINKS_EXTRA", "README.md CLAUDE.md").split():
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
