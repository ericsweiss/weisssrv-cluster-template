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


def _ref_key_lines(text: str, project: str) -> set[int]:
    """0-based line numbers of the `ref:` KEY in each library include entry.

    Derived from the parsed node tree rather than by scanning for `project:`
    lines. Every indentation heuristic tried here leaked: a nested `inputs:`
    block may legitimately carry `project` and `ref` keys of its own, and a
    line scanner cannot tell those from an entry's own pin without effectively
    reimplementing the parser. Composing the document gives the source line of
    exactly the nodes `check()` reads, so --fix edits precisely what the gate
    verifies and nothing else.
    """
    root = yaml.compose(text, Loader=_RefTolerantLoader)
    if not isinstance(root, yaml.MappingNode):
        return set()

    include_node = next(
        (
            value
            for key, value in root.value
            if isinstance(key, yaml.ScalarNode) and key.value == "include"
        ),
        None,
    )
    if include_node is None:
        return set()

    entries = (
        include_node.value
        if isinstance(include_node, yaml.SequenceNode)
        else [include_node]
    )

    found: set[int] = set()
    for entry in entries:
        if not isinstance(entry, yaml.MappingNode):
            continue
        fields = {
            key.value: (key, value)
            for key, value in entry.value
            if isinstance(key, yaml.ScalarNode)
        }
        proj = fields.get("project")
        ref = fields.get("ref")
        if ref is None or proj is None:
            continue
        if isinstance(proj[1], yaml.ScalarNode) and proj[1].value == project:
            # The KEY's line, not the value's: an empty `ref:` parses to null,
            # whose node can be marked on the FOLLOWING line.
            found.add(ref[0].start_mark.line)
    return found


def fix(path: Path, project: str = LIB_PROJECT, ref_var: str = REF_VAR) -> int:
    """Rewrite every library include `ref:` to the declared value."""
    doc = load_ci(path)
    want = declared_ref(doc, ref_var)
    if not want:
        raise SystemExit(f"{path}: variables.{ref_var} is not set; nothing to sync")

    # Textual rewrite so comments and formatting survive, but the lines to
    # touch come from the parsed tree (see _ref_key_lines) rather than a scan.
    text = path.read_text()
    targets = _ref_key_lines(text, project)
    lines = text.splitlines(keepends=True)
    changed = 0
    for n in sorted(targets):
        line = lines[n]
        # A `#` only opens a YAML comment when whitespace precedes it, and a
        # quoted scalar may contain one outright. Splitting on a bare `#` would
        # cut a ref like `v1.0#rc1` in half and paste the remainder back as a
        # comment.
        m = re.match(
            r"""^(\s*)ref:[^\S\n]*"""
            r"""("(?:\\.|[^"\\])*"|'(?:''|[^'])*'|.*?)"""
            r"""([^\S\n]+#.*|[^\S\n]*)$""",
            line.rstrip("\n"),
        )
        if m and m.group(2) != want:
            newline = "\n" if line.endswith("\n") else ""
            # `ref: ` rebuilt rather than reused: an EMPTY `ref:` has no
            # trailing space to preserve, so reusing the matched prefix would
            # emit `ref:v0.5.0`.
            lines[n] = f"{m.group(1)}ref: {want}{m.group(3)}{newline}"
            changed += 1
    if changed:
        updated = "".join(lines)
        # Verify by OUTCOME rather than enumerating the ways a textual rewrite
        # can go wrong. A `ref: >-` block scalar leaves its body behind; a value
        # YAML would retype (`on`, `null`, a date) lands as the wrong type; an
        # aliased include can carry marks from elsewhere. Each has its own
        # special case, and the list is open-ended — so instead: re-parse, and
        # require every library pin to have landed as the exact string intended.
        # Anything else keeps the original file. Nothing is written until this
        # passes, so a refusal is never a half-edited file.
        try:
            reparsed = yaml.load(updated, Loader=_RefTolerantLoader) or {}
        except yaml.YAMLError as exc:
            raise SystemExit(
                f"{path}: rewrite would produce invalid YAML ({exc}); "
                "file left unchanged"
            ) from exc
        landed = [entry.get("ref") for entry in lib_includes(reparsed, project)]
        if any(ref != want for ref in landed):
            raise SystemExit(
                f"{path}: rewrite did not land cleanly (pins parsed back as "
                f"{landed!r}, wanted {want!r}); file left unchanged"
            )
        path.write_text(updated)
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
