#!/usr/bin/env python3
"""Assert every weisssrv-lib pin matches the repo's single source.

Covers two literals GitLab and Ansible-Galaxy both refuse to interpolate from a
variable: the `include: ref:` entries in .gitlab-ci.yml, and the weisssrv-lib
collection `version:` in the sibling ansible/requirements.yml (absent, and so a
no-op, in a repo that does not install the collection). Both are synced from
variables.WEISSSRV_LIB_REF; the include logic is described next, the collection
logic near main().

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
    return parse_ci(path.read_text(encoding="utf-8"))


def parse_ci(text: str) -> dict:
    """Parse already-read text, so a caller that also needs the raw source
    reads the file ONCE. Two reads could disagree if the file changed between
    them, and --fix rewrites lines located by one read into text from another.
    """
    doc = yaml.load(text, Loader=_RefTolerantLoader)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        # Valid YAML, wrong shape. Raised as a YAMLError so it lands on the
        # operator-error path (exit 2) rather than reaching `doc.get(...)` and
        # surfacing as an AttributeError traceback.
        raise yaml.YAMLError("the top-level CI document must be a mapping")
    variables = doc.get("variables")
    if variables is not None and not isinstance(variables, dict):
        # A non-mapping `variables:` would reach .get(ref_var) on a scalar.
        raise yaml.YAMLError("`variables:` must be a mapping")
    return doc


def lib_includes(doc: dict, project: str = LIB_PROJECT) -> list[dict]:
    """The include entries that pin the library."""
    includes = doc.get("include") or []
    if isinstance(includes, dict):
        includes = [includes]        # a single entry, written unwrapped
    elif not isinstance(includes, list):
        # `include:` may legitimately be one string (a local file), or any
        # scalar when malformed. None carries a project pin, and a scalar is
        # not iterable.
        includes = []
    return [
        i for i in includes if isinstance(i, dict) and i.get("project") == project
    ]


def declared_ref(doc: dict, ref_var: str = REF_VAR) -> str | None:
    return ((doc.get("variables") or {}).get(ref_var)) or None


def files_of(entry: dict) -> list[str]:
    """`file:` is a string OR a list — a list shares one ref across templates.

    An entry may carry no `file:` at all (a malformed include, or one using a
    different selector). Naming it `<entry with no file:>` beats reporting the
    drift against a bare `None`, which reads like a bug in this script.
    """
    f = entry.get("file")
    if isinstance(f, list):
        # `or [...]`: an EMPTY list must not collapse the caller's reporting
        # loop to zero iterations, silently passing a drifted pin.
        return [str(x) for x in f] or ["<entry with an empty file: list>"]
    return [str(f)] if f else ["<entry with no file:>"]


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
    # fullmatch, not match: Python's `$` also matches before a trailing
    # newline, so a multiline scalar like "v0.5.1\n" would pass the tag gate.
    if not isinstance(want, str) or TAG_RE.fullmatch(want) is None:
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

    include_pairs = [
        (key, value)
        for key, value in root.value
        if isinstance(key, yaml.ScalarNode) and key.value == "include"
    ]
    if not include_pairs:
        return set()
    if len(include_pairs) > 1:
        # PyYAML keeps the LAST duplicate key, so check() reads that one while
        # taking the first here would rewrite a block GitLab and the gate both
        # ignore — and the post-write verification would then pass against the
        # other block. The two halves must agree on which block they mean.
        raise SystemExit(
            "multiple top-level `include:` keys make the rewrite ambiguous "
            "(YAML keeps the last, so the others are silently ignored); "
            "merge them into one block and retry"
        )
    include_key, include_node = include_pairs[0]

    # Invariant: rewrite targets are bounded to the include node's own textual
    # span. An alias resolves to its anchor, whose marks may sit anywhere in the
    # file, so an unbounded rewrite could edit shared configuration.
    include_start = include_key.start_mark.index
    # The node's own end mark is the exact span; deriving it from the next
    # top-level key can overshoot when that key is reached through an alias.
    include_end = include_node.end_mark.index
    if include_end <= include_start:
        # The include value is itself an alias, so its node carries the
        # anchor's marks and the span reads as empty. Fall back to the
        # conservative bound, which keeps the alias inside it and refused.
        include_end = min(
            (
                key.start_mark.index
                for key, _ in root.value
                if key.start_mark.index > include_start
            ),
            default=len(text),
        )

    # Invariant: an alias inside the span refuses --fix. Composing resolves
    # aliases away, so they are detected on the event stream instead. fix()
    # then reports the drift unrepaired rather than editing a shared line.
    for event in yaml.parse(text, Loader=_RefTolerantLoader):
        if not include_start <= event.start_mark.index < include_end:
            continue
        is_alias = isinstance(event, yaml.AliasEvent)
        # An anchor DEFINED here can be referenced from outside the block, so
        # rewriting this pin would change what that reference resolves to.
        defines_anchor = not is_alias and getattr(event, "anchor", None)
        if is_alias or defines_anchor:
            raise SystemExit(
                "the `include:` block contains a YAML anchor or alias, which "
                "may share configuration with the rest of the file; refusing "
                "to rewrite it. Update the pins by hand."
            )

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
            if include_start <= ref[0].start_mark.index < include_end:
                found.add(ref[0].start_mark.line)
    return found


def fix(path: Path, project: str = LIB_PROJECT, ref_var: str = REF_VAR) -> int:
    """Rewrite every library include `ref:` to the declared value."""
    text = path.read_text(encoding="utf-8")
    doc = parse_ci(text)
    want = declared_ref(doc, ref_var)
    if not want:
        raise SystemExit(f"{path}: variables.{ref_var} is not set; nothing to sync")
    # Validated BEFORE anything is written: a branch ref would otherwise be
    # propagated to every include and only then reported.
    if not isinstance(want, str) or TAG_RE.fullmatch(want) is None:
        raise SystemExit(
            f"{path}: variables.{ref_var} must be a release tag (vX.Y.Z), got "
            f"{want!r}; file left unchanged"
        )

    # Textual rewrite so comments and formatting survive, but the lines to
    # touch come from the parsed tree (see _ref_key_lines) rather than a scan.
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
    remaining = check(path, project, ref_var) if not changed else []
    if remaining:
        # fix() reporting 0 is a claim that nothing needed doing. It cannot add
        # a `ref:` that is absent, rewrite a flow-style entry, or reach a pin
        # that lives outside `include:` — and returning 0 for any of those hands
        # the caller a clean result over an unrepaired file.
        raise SystemExit(
            f"{path}: --fix could not repair this file; fix it by hand:\n  "
            + "\n  ".join(remaining)
        )
    if changed:
        updated = "".join(lines)
        # Invariant: re-parse and require every library pin to have landed as
        # the exact string intended. Nothing is written until that passes, so a
        # refusal is never a half-edited file.
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
        path.write_text(updated, encoding="utf-8")
    return changed


# ---------------------------------------------------------------------------
# Ansible collection pin. A consumer's sibling ansible/requirements.yml installs
# the SAME library at the SAME tag, but as a Galaxy collection rather than a CI
# include. Galaxy cannot reference variables.WEISSSRV_LIB_REF any more than
# `include: ref:` can, so its `version:` literal is synced from the same single
# source. A repo that does not install the collection (a tenant app scaffold)
# has no requirements.yml, and all of this is a no-op there.

_REQUIREMENTS_REL = Path("ansible") / "requirements.yml"


def requirements_path(ci_file: Path) -> Path:
    """The requirements.yml sibling of the CI file (it may not exist)."""
    return ci_file.parent / _REQUIREMENTS_REL


def _collection_version(text: str, project: str) -> tuple[int, str] | None:
    """(0-based line of the `version:` KEY, its value) for the collection whose
    `name` names `project`, read from the parsed node tree so a `version:` under
    a DIFFERENT collection is never matched. None if the entry or its version is
    absent. requirements.yml carries no GitLab `!reference`, so plain SafeLoader.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return None
    for top_key, top_val in root.value:
        if not (isinstance(top_key, yaml.ScalarNode) and top_key.value == "collections"):
            continue
        if not isinstance(top_val, yaml.SequenceNode):
            return None
        for entry in top_val.value:
            if not isinstance(entry, yaml.MappingNode):
                continue
            name = version = None
            for key, value in entry.value:
                if not (isinstance(key, yaml.ScalarNode) and isinstance(value, yaml.ScalarNode)):
                    continue
                if key.value == "name":
                    name = value.value
                elif key.value == "version":
                    version = (key.start_mark.line, value.value)
            if name and project in name and version is not None:
                return version
    return None


def check_requirements(
    ci_file: Path, want: str, project: str = LIB_PROJECT, ref_var: str = REF_VAR
) -> list[str]:
    """The collection pin in the sibling requirements.yml must equal the single
    source. Empty when there is no requirements.yml or it does not install the
    library."""
    req = requirements_path(ci_file)
    if not req.is_file():
        return []
    found = _collection_version(req.read_text(encoding="utf-8"), project)
    if found is None:
        # A requirements.yml that DOES install the library but carries no
        # version: is a floating pin — the drift this exists to prevent.
        if project in req.read_text(encoding="utf-8"):
            return [f"{req}: the {project} collection is installed without a version: pin"]
        return []
    _, current = found
    if current == want:
        return []
    return [
        f"{req}: the {project} collection pins version {current!r}, "
        f"but {ref_var} is {want!r}"
    ]


def fix_requirements(ci_file: Path, want: str, project: str = LIB_PROJECT) -> int:
    """Rewrite the collection version in the sibling requirements.yml to `want`.
    Returns 0 when absent, unpinned, or already correct. Mirrors fix(): a textual
    rewrite of the exact node line, re-parsed and verified before writing."""
    req = requirements_path(ci_file)
    if not req.is_file():
        return 0
    text = req.read_text(encoding="utf-8")
    found = _collection_version(text, project)
    if found is None or found[1] == want:
        return 0
    line_no, _ = found
    lines = text.splitlines(keepends=True)
    line = lines[line_no]
    m = re.match(
        r"""^(\s*)version:[^\S\n]*"""
        r"""("(?:\\.|[^"\\])*"|'(?:''|[^'])*'|.*?)"""
        r"""([^\S\n]+#.*|[^\S\n]*)$""",
        line.rstrip("\n"),
    )
    if not m:
        raise SystemExit(
            f"{req}: could not rewrite the {project} collection version: line; "
            "fix it by hand"
        )
    newline = "\n" if line.endswith("\n") else ""
    lines[line_no] = f"{m.group(1)}version: {want}{m.group(3)}{newline}"
    updated = "".join(lines)
    landed = _collection_version(updated, project)
    if landed is None or landed[1] != want:
        raise SystemExit(
            f"{req}: rewrite did not land cleanly (version parsed back as "
            f"{(landed[1] if landed else None)!r}, wanted {want!r}); file left unchanged"
        )
    req.write_text(updated, encoding="utf-8")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        "--fix",
        action="store_true",
        help="rewrite the include refs and the requirements.yml collection pin to the ref-var",
    )
    args = ap.parse_args(argv)

    # Unreadable or malformed input is an operator error, not a pin finding:
    # exit 2 so CI can tell the two apart. Wrapping the real calls (not a
    # preflight read) is what keeps the guard around every read. SystemExit
    # from fix() passes through — those refusals are already worded.
    try:
        return _run(args)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: could not read {args.ci_file}: {exc}", file=sys.stderr)
        return 2
    except yaml.YAMLError as exc:
        print(f"ERROR: {args.ci_file} is not valid YAML: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    # `want` is None when variables.<ref_var> is unset; check()/fix() report that
    # for the includes, so the requirements checks stay silent (guarded on want)
    # rather than adding a confusing "pins vX but ref is None" line.
    want = declared_ref(load_ci(args.ci_file), args.ref_var)
    req_problems = (
        check_requirements(args.ci_file, want, args.project, args.ref_var)
        if want
        else []
    )
    if args.fix:
        changed = fix(args.ci_file, args.project, args.ref_var)
        # fix() has validated `want` is a release tag (or raised); sync the
        # sibling collection pin from the same single source.
        changed += fix_requirements(args.ci_file, want, args.project)
        # Verify the result rather than trusting the rewrite. --fix cannot
        # repair a branch ref in the variable, an absent include block, or an
        # entry whose `ref:` the line rewriter did not recognise — and exiting 0
        # on any of those would report success for a file still in violation.
        problems = check(args.ci_file, args.project, args.ref_var) + check_requirements(
            args.ci_file, want, args.project, args.ref_var
        )
        if problems:
            print("check-lib-pins: FAILED after rewrite", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(f"check-lib-pins: rewrote {changed} pin(s) in {args.ci_file} + requirements.yml")
        return 0

    problems = check(args.ci_file, args.project, args.ref_var) + req_problems
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
