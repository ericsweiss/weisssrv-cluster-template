#!/usr/bin/env python3
"""Assert every weisssrv-lib `include:` pin matches the repo's single source.

GitLab resolves `include:` at pipeline-CREATION time, before the `variables:`
block of the same file exists, so `ref: $WEISSSRV_LIB_REF` does not work — the
pin has to be a literal on every include entry. (A project/group CI/CD variable
IS readable there, but it moves the pin out of git: a library bump would stop
appearing in a diff and could not be reviewed or reverted as an MR.)

So the copies are unavoidable, and this is what keeps them honest. It reads the
authoritative value from `variables.WEISSSRV_LIB_REF` and requires that:

  * every weisssrv-lib include entry pins exactly that ref, and
  * the ref is a release TAG (vX.Y.Z).

Both failures are silent without a gate. A stale ref on one entry runs that one
job from a different library version — the "a changed input default silently
changes this pipeline" hazard, arriving with nothing red to show for it. A
BRANCH ref is worse: the include contract forbids it because a branch deleted
after merge takes the include with it, and until then the pipeline's behaviour
can change with no commit in the consuming repo at all.

Consumers VENDOR this script (the CI job runs it from the consumer tree, like
every other library script). `--project` / `--ref-var` exist so a consumer that
pins a fork, or names its variable differently, can use the copy unmodified.

Usage:
  scripts/check-lib-pins.py            # verify (exit 1 on drift)
  scripts/check-lib-pins.py --fix      # rewrite the pins to the single source
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

LIB_PROJECT = "eric/weisssrv-lib"
REF_VAR = "WEISSSRV_LIB_REF"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


class _RefTolerantLoader(yaml.SafeLoader):
    """SafeLoader that survives GitLab's `!reference` tag.

    `safe_load` raises on `!reference`, which .gitlab-ci.yml uses freely. This
    subclasses SafeLoader — so it inherits exactly SafeLoader's constructors and
    can NOT instantiate arbitrary Python — and adds one constructor that maps
    `!reference` to None. Nothing here reads those values; the pins are plain
    strings. `yaml.load` with this Loader is therefore as safe as `safe_load`,
    unlike `yaml.load` with the default (arbitrary-object) Loader.
    """


_RefTolerantLoader.add_multi_constructor(
    "!reference", lambda loader, suffix, node: None
)


def load_ci(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_RefTolerantLoader) or {}


def lib_includes(doc: dict, project: str = LIB_PROJECT) -> list[dict]:
    """The include entries that pin the library."""
    includes = doc.get("include") or []
    if isinstance(includes, dict):
        includes = [includes]
    return [
        i for i in includes if isinstance(i, dict) and i.get("project") == project
    ]


def declared_ref(doc: dict, ref_var: str = REF_VAR) -> str | None:
    return ((doc.get("variables") or {}).get(ref_var)) or None


def files_of(entry: dict) -> list[str]:
    """`file:` is a string OR a list — a list shares one ref across templates."""
    f = entry.get("file")
    return list(f) if isinstance(f, list) else [f]


def check(
    path: Path, project: str = LIB_PROJECT, ref_var: str = REF_VAR
) -> list[str]:
    """Return a list of problems; empty means the pins are consistent."""
    doc = load_ci(path)
    entries = lib_includes(doc, project)
    problems: list[str] = []

    if not entries:
        # Nothing to check is not the same as everything being fine: if the
        # includes are ever restructured out from under this gate, say so
        # rather than passing an empty set.
        return [f"{path}: no `{project}` include entries found"]

    want = declared_ref(doc, ref_var)
    if not want:
        return [f"{path}: variables.{ref_var} is not set (the single source)"]
    if not TAG_RE.match(want):
        problems.append(
            f"{path}: {ref_var} is {want!r}, which is not a release tag "
            "(vX.Y.Z). The include contract forbids pinning a branch."
        )

    for entry in entries:
        ref = entry.get("ref")
        if ref == want:
            continue
        for name in files_of(entry):
            problems.append(
                f"{path}: {name} pins ref {ref!r}, but {ref_var} is {want!r}"
            )
    return problems


def fix(path: Path, project: str = LIB_PROJECT, ref_var: str = REF_VAR) -> int:
    """Rewrite every library include `ref:` to the declared value."""
    doc = load_ci(path)
    want = declared_ref(doc, ref_var)
    if not want:
        raise SystemExit(f"{path}: variables.{ref_var} is not set; nothing to sync")

    # Line-level rewrite so comments and formatting survive: only `ref:` lines
    # that belong to a library entry are touched, identified by the `project:`
    # line that opens the block.
    lines = path.read_text().splitlines(keepends=True)
    in_lib_entry = False
    changed = 0
    for n, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- project:") or stripped.startswith("project:"):
            # EXACT value equality, matching check(). A suffix test would treat
            # `acme/eric/weisssrv-lib` as ours and rewrite a ref that check()
            # never policed — a --fix that edits what it does not verify. Parsed
            # as YAML rather than string-stripped so a quoted value or a trailing
            # comment resolves the same way it does for the include itself.
            try:
                value = yaml.safe_load(stripped.split(":", 1)[1].strip())
            except yaml.YAMLError:
                value = None
            in_lib_entry = value == project
            continue
        if in_lib_entry and stripped.startswith("ref:"):
            current = stripped.split(":", 1)[1].strip()
            if current != want:
                lines[n] = line.replace(current, want, 1)
                changed += 1
            in_lib_entry = False
    if changed:
        path.write_text("".join(lines))
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ci-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / ".gitlab-ci.yml",
    )
    ap.add_argument(
        "--project",
        default=LIB_PROJECT,
        help=f"include project path to check (default {LIB_PROJECT})",
    )
    ap.add_argument(
        "--ref-var",
        default=REF_VAR,
        help=f"variable holding the single source (default {REF_VAR})",
    )
    ap.add_argument(
        "--fix", action="store_true", help="rewrite include refs to the ref-var"
    )
    args = ap.parse_args(argv)

    if args.fix:
        changed = fix(args.ci_file, args.project, args.ref_var)
        # Verify the result rather than trusting the rewrite. --fix cannot
        # repair a branch ref in the variable, an absent include block, or an
        # entry whose `ref:` the line rewriter did not recognise — and exiting 0
        # on any of those would report success for a file still in violation.
        problems = check(args.ci_file, args.project, args.ref_var)
        if problems:
            print("check-lib-pins: FAILED after rewrite", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(f"check-lib-pins: rewrote {changed} ref(s) in {args.ci_file}")
        return 0

    problems = check(args.ci_file, args.project, args.ref_var)
    if problems:
        print("check-lib-pins: FAILED", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nFix with: scripts/check-lib-pins.py --fix "
            f"(after setting variables.{args.ref_var})",
            file=sys.stderr,
        )
        return 1

    doc = load_ci(args.ci_file)
    n = sum(len(files_of(e)) for e in lib_includes(doc, args.project))
    print(
        f"check-lib-pins: OK — {n} template(s) pinned at "
        f"{declared_ref(doc, args.ref_var)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
