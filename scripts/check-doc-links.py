#!/usr/bin/env python3
"""Offline checker for relative Markdown cross-links and section citations.

Resolves each relative `.md` target against the filesystem and each
`<doc> § <Heading>` citation against that document's headings. See docs/SCRIPTS.md.
"""
from __future__ import annotations

import difflib
import fnmatch
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

# A single-backtick span, and the characters that mark a token as prose, a
# placeholder or an absolute/foreign path rather than a repo-relative path.
_CODE_RE = re.compile(r"`([^`\n]+)`")
_NOT_A_PATH = set("<>{}*$|:=,()[]\"'!?")


def _in_git_tree(root: Path) -> bool:
    """Whether `root` sits inside a git work tree.

    Asked of git, not of a `.git` entry, since the gate is often pointed at a
    subdirectory. A missing git binary raises rather than shrinking coverage.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        if (root / ".git").exists():
            raise RuntimeError(f"cannot run git under {root}: {exc}") from exc
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _tracked_markdown(root: Path) -> list[Path]:
    """Every git-tracked *.md under `root`.

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
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            f"cannot enumerate tracked Markdown under {root}: {exc}"
        ) from exc
    files = [root / p for p in out.split("\0") if p]
    return sorted(f for f in files if f.is_file())


def doc_files(root: Path) -> list[Path]:
    """The Markdown files to scan.

    Inside a git work tree the tracked set is authoritative, empty included, so
    an unstaged tree reports no Markdown rather than falling back.
    """
    if _in_git_tree(root):
        return _tracked_markdown(root)

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


def _repo_path_token(raw: str, root: Path, source_dir: Path | None = None) -> str | None:
    """Return the path a backticked token names, or None.

    A token qualifies when its first segment is a directory of `root` or of the
    citing document's own directory; prose and foreign paths are left alone.
    """
    token = raw.strip()
    if "/" not in token or any(c.isspace() for c in token):
        return None
    if token.startswith(("/", ".", "~")) or token.startswith(_SKIP_PREFIXES):
        return None
    if _NOT_A_PATH & set(token):
        return None
    head = token.split("/", 1)[0]
    bases = [root] if source_dir is None else [root, source_dir]
    if not head or not any((base / head).is_dir() for base in bases):
        return None
    return token.rstrip("/")


def rotted_paths(files: list[Path], root: Path) -> list[tuple[Path, str]]:
    """Return (source_file, token) for every backticked repo path that is gone.

    A cited path resolves either repo-relative or against the citing document's
    own directory, so only a token that resolves under neither has rotted.
    """
    ignore = os.environ.get("CHECK_DOC_LINKS_IGNORE", "").split()
    rotted: list[tuple[Path, str]] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        source_dir = f.parent
        for m in _CODE_RE.finditer(text):
            rel = _repo_path_token(m.group(1), root, source_dir)
            if rel is None or any(fnmatch.fnmatch(rel, pat) for pat in ignore):
                continue
            if not ((root / rel).exists() or (source_dir / rel).exists()):
                rotted.append((f, rel))
    return rotted


# A `<doc> § <Heading>` citation: the document either backticked or bare, then
# the section name up to a bracket, a backtick or the end of the line.
_SECTION_RE = re.compile(
    r"(?:`(?P<quoted>[^`\s]+)`|(?P<bare>[A-Za-z0-9_./-]+))"
    r"[^\S\n]*(?:§|&sect;)[^\S\n]*"
    r"(?P<heading>[^\n`)\]]+)"
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_MARKUP = str.maketrans("", "", "`*_")
# Sentence punctuation and quotes a citation picks up from the prose around it.
_TRAILING = ".,;:!?\"'()[]-\u2014\u2013"
# Where a cited section name ends and the sentence around it resumes. Bounding
# only shortens the citation, and a shortened one still matches by prefix.
_BOUND = re.compile(r"[.,;:)\]`]|\s[-\u2014\u2013]\s|[\u2014\u2013]")


def _words(text: str) -> list[str]:
    """Heading or citation text reduced to comparable words."""
    stripped = _LINK_RE.sub(r"\1", text).translate(_MARKUP)
    return [w for w in (p.strip(_TRAILING).casefold() for p in stripped.split()) if w]


# Words of overlap that identify a section when neither side is a clean prefix
# of the other: the citation keeps going without punctuation and the heading
# carries a qualifier the citation left out.
_MIN_SHARED_WORDS = 3


def _same_section(cited: list[str], heading: list[str]) -> bool:
    """One word list is a prefix of the other, or they agree on enough words.

    A citation runs on into the prose and a line wrap cuts the section name
    short, so neither side's full text is comparable.
    """
    if not cited:
        return False
    if cited[: len(heading)] == heading or heading[: len(cited)] == cited:
        return True
    shared = 0
    for mine, theirs in zip(cited, heading):
        if mine != theirs:
            break
        shared += 1
    return shared >= _MIN_SHARED_WORDS


def headings(doc: Path) -> list[list[str]]:
    """Every heading a Markdown file offers, as word lists, fences skipped.

    An odd fence count means the map is unreliable, so fences stop being
    skipped: dropping a real heading would fail a sound citation.
    """
    found: list[list[str]] = []
    lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
    track_fences = sum(1 for ln in lines if ln.lstrip().startswith("```")) % 2 == 0
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = track_fences and not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            words = _words(match.group("text"))
            if words:
                found.append(words)
    return found


# Copier renders a template by dropping this suffix, so a citation inside a
# template tree resolves against `docs/X.md.jinja` as well as `docs/X.md`.
TEMPLATE_SUFFIX = ".jinja"


def _ancestors(start: Path, root: Path) -> list[Path]:
    """`start` and every directory above it up to `root`, nearest first.

    A citation resolves against the nearest matching tree, so a template repo
    carrying both its own docs and a generated repo's does not cross over.
    """
    chain = [start]
    current = start
    while current != root and root in current.parents:
        current = current.parent
        chain.append(current)
    if root not in chain:
        chain.append(root)
    return chain


def _cited_doc(token: str, quoted: bool, source: Path, root: Path) -> Path | None:
    """The document a citation names, or None when it cannot be pinned down.

    A bare `docs/07` resolves through the numbered-prefix convention. Prose, an
    ambiguous prefix and an absent file are left alone.
    """
    if token.startswith(_SKIP_PREFIXES) or _NOT_A_PATH & set(token):
        return None
    # A suffixless token is a path prefix, never a word: `docs/07` resolves,
    # "README" and "that role's README" do not.
    if not token.endswith(".md") and "/" not in token:
        return None
    if token.endswith(".md") and not quoted and "/" not in token:
        return None
    for base in _ancestors(source.parent, root):
        if token.endswith(".md"):
            for candidate in (base / token, base / (token + TEMPLATE_SUFFIX)):
                if candidate.is_file():
                    return candidate
            continue
        matches = sorted(
            set(base.glob(token + "*.md")) | set(base.glob(token + "*.md" + TEMPLATE_SUFFIX))
        )
        if len(matches) == 1:
            return matches[0]
    return None


def dangling_sections(files: list[Path], root: Path) -> list[tuple[Path, str, str, list[str]]]:
    """(source, cited document, cited heading, nearest real headings) per citation
    whose document resolves but whose heading no longer exists.

    The citation is bounded at the punctuation that resumes the sentence.
    """
    cache: dict[Path, list[list[str]]] = {}
    dangling: list[tuple[Path, str, str, list[str]]] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _SECTION_RE.finditer(text):
            token = m.group("quoted") or m.group("bare")
            doc = _cited_doc(token, bool(m.group("quoted")), f, root)
            if doc is None:
                continue
            if doc not in cache:
                cache[doc] = headings(doc)
            cited = _words(_BOUND.split(m.group("heading"), 1)[0])
            if not cited or any(_same_section(cited, h) for h in cache[doc]):
                continue
            titles = [" ".join(h) for h in cache[doc]]
            dangling.append(
                (f, token, m.group("heading").strip(),
                 difflib.get_close_matches(" ".join(cited), titles, n=3, cutoff=0.4))
            )
    return dangling


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"0", "false", "no"}


def _shown(path: Path) -> Path:
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def main(argv: list[str]) -> int:
    roots = [Path(a).resolve() for a in argv[1:]] or [REPO]
    scanned: list[tuple[Path, list[Path]]] = []
    try:
        for root in roots:
            scanned.append((root, doc_files(root)))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    files = [f for _, group in scanned for f in group]
    if not files:
        print(
            f"ERROR: no Markdown files found under: {', '.join(str(r) for r in roots)}",
            file=sys.stderr,
        )
        return 2

    broken = broken_links(files)
    if broken:
        print(f"ERROR: {len(broken)} broken relative Markdown link(s):")
        for src, target in broken:
            print(f"  {_shown(src)}: [...]({target})")
        return 1

    sections = (
        [] if _disabled("CHECK_DOC_LINKS_SECTIONS")
        else [c for root, group in scanned for c in dangling_sections(group, root)]
    )
    if sections:
        print(f"ERROR: {len(sections)} section citation(s) naming a heading that does not exist:")
        for src, token, heading, near in sections:
            suffix = f" — nearest: {', '.join(near)}" if near else ""
            print(f"  {_shown(src)}: {token} § {heading}{suffix}")
        return 1

    if _enabled("CHECK_DOC_LINKS_PATHS"):
        rotted = [p for root, group in scanned for p in rotted_paths(group, root)]
        if rotted:
            print(f"ERROR: {len(rotted)} backticked repo path(s) that no longer exist:")
            for src, token in rotted:
                print(f"  {_shown(src)}: `{token}`")
            return 1

    print(
        f"OK: {len(files)} Markdown file(s) scanned, all relative .md links and "
        "section citations resolve."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
