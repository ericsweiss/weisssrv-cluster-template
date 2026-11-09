#!/usr/bin/env python3
"""Assert every repo path an agent skill cites as a backticked path still exists.

Exit 0 clean, 1 a missing path, 2 a vacuous or unreadable scan.
See docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A backticked path with at least one "/" and a known file extension. Bare
# basenames, directory mentions, `<placeholder>` and glob paths never match.
PATH_RE = re.compile(
    r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*"
    r"\.(?:md|py|sh|yml|yaml|tf|json|hujson|toml))`"
)

# The scan is vacuous below this many checked citations: a regex that stopped
# matching, or a skill that lost its pages, would otherwise pass on nothing.
MIN_TOKENS = 30


def _skill_pages(skill_dir: Path) -> list[Path]:
    return sorted(p for p in skill_dir.rglob("*") if p.is_file() and ".md" in p.suffixes)


class OperatorError(Exception):
    """A skill page the scan cannot read, so the scan cannot inspect it."""


Token = tuple[Path, str]


def classify(
    skill_dir: Path,
    repo_root: Path,
    foreign: frozenset[str] = frozenset(),
    shorthand: tuple[Path, ...] = (),
) -> tuple[list[Token], list[Token]]:
    """-> (checked tokens, the ones resolving against no base).

    The bases are the citing page's directory, the skill directory, the repo
    root and each shorthand root.
    """
    checked: list[Token] = []
    broken: list[Token] = []
    for md in _skill_pages(skill_dir):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError(f"{md}: {exc}") from exc
        for match in PATH_RE.finditer(text):
            raw = match.group(1)
            if raw in foreign:
                continue
            checked.append((md, raw))
            if not any((base / raw).exists()
                       for base in (md.parent, skill_dir, repo_root, *shorthand)):
                broken.append((md, raw))
    return checked, broken


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument(
        "--skill-dir",
        type=Path,
        required=True,
        help="the skill directory, relative to --repo-root or absolute",
    )
    parser.add_argument(
        "--foreign",
        action="append",
        default=[],
        metavar="PATH",
        help="a cited path that lives in another repository; repeatable",
    )
    parser.add_argument(
        "--shorthand-root",
        action="append",
        default=[],
        metavar="DIR",
        help="a directory, relative to --repo-root, that citations may name paths under; repeatable",
    )
    parser.add_argument("--min-tokens", type=int, default=MIN_TOKENS)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    skill_dir = (repo_root / args.skill_dir).resolve()

    if not repo_root.is_dir() or not skill_dir.is_dir() or not _skill_pages(skill_dir):
        print(f"ERROR: no skill Markdown under {skill_dir}", file=sys.stderr)
        return 2

    shorthand = tuple((repo_root / r).resolve() for r in args.shorthand_root)
    absent = [str(r) for r, path in zip(args.shorthand_root, shorthand) if not path.is_dir()]
    if absent:
        print(f"ERROR: --shorthand-root not a directory: {', '.join(absent)}", file=sys.stderr)
        return 2

    try:
        tokens, broken = classify(skill_dir, repo_root, frozenset(args.foreign), shorthand)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if len(tokens) < args.min_tokens:
        print(
            f"ERROR: only {len(tokens)} backticked repo paths parsed out of the "
            f"skill (expected at least {args.min_tokens}): the scan is not seeing "
            "its subject",
            file=sys.stderr,
        )
        return 2

    gone = sorted(
        f"{md.relative_to(repo_root) if md.is_relative_to(repo_root) else md}: {raw}"
        for md, raw in set(broken)
    )
    if gone:
        print("Skill cites paths that do not exist:", file=sys.stderr)
        for entry in gone:
            print(f"  {entry}", file=sys.stderr)
        return 1

    print(f"check-skill-refs: {len(tokens)} cited paths resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
