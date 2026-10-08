#!/usr/bin/env python3
"""Assert every weisssrv-lib pin matches variables.WEISSSRV_LIB_REF.

Checks each weisssrv-lib `include:` ref and the sibling ansible/requirements.yml
collection `version:`; `--fix` rewrites them. Contract: docs/INCLUDE-CONTRACT.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import ci_yaml  # noqa: E402  (resolved from this script's own directory)
except ImportError:
    print(
        "ERROR: ci_yaml.py must sit next to this script — vendor both "
        "(see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

LIB_PROJECT = "eric/weisssrv-lib"
REF_VAR = "WEISSSRV_LIB_REF"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Shared with the other CI-reading gates: every `!`-tagged node becomes None, so
# a pipeline file carrying any custom tag parses instead of crashing this gate.
_LOADER = ci_yaml.NullTagCILoader


def load_ci(path: Path) -> dict:
    return parse_ci(path.read_text(encoding="utf-8"))


def parse_ci(text: str) -> dict:
    """Parse already-read text, so a caller that also needs the raw source
    reads the file ONCE. Two reads could disagree if the file changed between
    them, and --fix rewrites lines located by one read into text from another.
    """
    documents = [d for d in yaml.load_all(text, Loader=_LOADER) if d is not None]
    if not documents:
        return {}
    # GitLab's inputs syntax makes a pipeline file two documents, `spec:` then
    # the jobs, so the last mapping is the one to read (as ci_yaml.parse_ci does).
    mappings = [d for d in documents if isinstance(d, dict)]
    if not mappings:
        # Valid YAML, wrong shape. Raised as a YAMLError so it lands on the
        # operator-error path (exit 2) rather than reaching `doc.get(...)` and
        # surfacing as an AttributeError traceback.
        raise yaml.YAMLError("the top-level CI document must be a mapping")
    doc = mappings[-1]
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
    """`file:` as a list of paths; a list shares one ref across templates.

    An entry may carry no `file:` at all, so a placeholder string stands in
    rather than a bare `None` in the drift report.
    """
    f = entry.get("file")
    if isinstance(f, list):
        # `or [...]`: an EMPTY list must not collapse the caller's reporting
        # loop to zero iterations, silently passing a drifted pin.
        return [str(x) for x in f] or ["<entry with an empty file: list>"]
    return [str(f)] if f else ["<entry with no file:>"]


def _rewrite_scalar_line(line: str, key: str, want: str) -> str | None:
    """`<indent>key: want<trailing comment>`, or None when nothing to rewrite.

    A `#` opens a YAML comment only after whitespace, and a quoted scalar may
    hold one, so the regex matches the scalar rather than splitting on `#`.
    """
    m = re.match(
        rf"""^(\s*){re.escape(key)}:[^\S\n]*"""
        r"""("(?:\\.|[^"\\])*"|'(?:''|[^'])*'|.*?)"""
        r"""([^\S\n]+#.*|[^\S\n]*)$""",
        line.rstrip("\n"),
    )
    if not m or m.group(2) == want:
        return None
    newline = "\n" if line.endswith("\n") else ""
    return f"{m.group(1)}{key}: {want}{m.group(3)}{newline}"


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


def _document_node(text: str, key: str, loader: type) -> yaml.MappingNode | None:
    """The last mapping document carrying `key`, of a multi-document stream.

    Matches what parse_ci reads, and compose_all marks are absolute in the
    stream, so a caller's line offsets hold.
    """
    found = None
    for root in yaml.compose_all(text, Loader=loader):
        if isinstance(root, yaml.MappingNode) and any(
            isinstance(k, yaml.ScalarNode) and k.value == key for k, _ in root.value
        ):
            found = root
    return found


def _ref_key_lines(text: str, project: str) -> set[int]:
    """0-based line numbers of each library include entry's own `ref:` key.

    Taken from the parsed node tree, so a nested `inputs:` `ref` is never
    matched: --fix edits exactly the nodes check() reads.
    """
    root = _document_node(text, "include", _LOADER)
    if root is None:
        return set()

    include_pairs = [
        (key, value)
        for key, value in root.value
        if isinstance(key, yaml.ScalarNode) and key.value == "include"
    ]
    if not include_pairs:
        return set()
    if len(include_pairs) > 1:
        # YAML keeps the LAST duplicate key, which is the one check() reads.
        # Rewriting a different block would leave the verification passing
        # against the wrong one, so refuse instead.
        raise SystemExit(
            "multiple top-level `include:` keys make the rewrite ambiguous "
            "(YAML keeps the last, so the others are silently ignored); "
            "merge them into one block and retry"
        )
    include_key, include_node = include_pairs[0]

    # Rewrites are bounded to the include node's own span; an alias resolves to
    # an anchor whose marks may sit anywhere in the file.
    include_start = include_key.start_mark.index
    # The node end mark is the exact span; the next top-level key can overshoot.
    include_end = include_node.end_mark.index
    if include_end <= include_start:
        # An aliased include value carries the anchor's marks, so the span
        # reads as empty. The conservative bound keeps the alias inside it.
        include_end = min(
            (
                key.start_mark.index
                for key, _ in root.value
                if key.start_mark.index > include_start
            ),
            default=len(text),
        )

    # An anchor or alias inside the span refuses --fix: the pin may be shared
    # with the rest of the file. Composing resolves aliases, so parse events
    # detect them.
    for event in yaml.parse(text, Loader=_LOADER):
        if not include_start <= event.start_mark.index < include_end:
            continue
        is_alias = isinstance(event, yaml.AliasEvent)
        # An anchor defined here can be referenced from outside the block.
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
        rewritten = _rewrite_scalar_line(lines[n], "ref", want)
        if rewritten is not None:
            lines[n] = rewritten
            changed += 1
    remaining = check(path, project, ref_var) if not changed else []
    if remaining:
        # --fix cannot add an absent ref, rewrite flow style, or reach a pin
        # outside `include:`; reporting 0 for any of those would hand the caller
        # a clean result over an unrepaired file.
        raise SystemExit(
            f"{path}: --fix could not repair this file; fix it by hand:\n  "
            + "\n  ".join(remaining)
        )
    if changed:
        updated = "".join(lines)
        # Re-parsed and required to have landed as the exact string intended.
        # Nothing is written until that passes, so a refusal is never a
        # half-edited file.
        try:
            reparsed = parse_ci(updated)
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


# Ansible collection pin: the sibling ansible/requirements.yml installs the same
# library at the same tag as a Galaxy collection. A repo without that file (a
# tenant app scaffold) makes this a no-op.

_REQUIREMENTS_REL = Path("ansible") / "requirements.yml"


def requirements_path(ci_file: Path) -> Path:
    """The requirements.yml sibling of the CI file (it may not exist)."""
    return ci_file.parent / _REQUIREMENTS_REL


def _repo_name(url: str) -> str:
    """The repository name in a Galaxy source URL or an include project path."""
    return url.split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _names_project(value: str, project: str) -> bool:
    """Whether a collection `name:`/`source:` installs `project`.

    The include path resolves instance-locally, so a mirror or a fork on
    another host matches on repository name rather than as a substring.
    """
    return project in value or _repo_name(value) == _repo_name(project)


def _collection_entries(text: str) -> list[dict[str, tuple[int, str]]]:
    """Each `collections:` entry as {key: (0-based key line, value)}.

    Read from the node tree so a `version:` under a different collection never
    matches. requirements.yml carries no `!reference`, so plain SafeLoader.
    """
    root = _document_node(text, "collections", yaml.SafeLoader)
    if root is None:
        return []
    entries: list[dict[str, tuple[int, str]]] = []
    for top_key, top_val in root.value:
        if not (isinstance(top_key, yaml.ScalarNode) and top_key.value == "collections"):
            continue
        if not isinstance(top_val, yaml.SequenceNode):
            return []
        for entry in top_val.value:
            if not isinstance(entry, yaml.MappingNode):
                continue
            entries.append(
                {
                    key.value: (key.start_mark.line, value.value)
                    for key, value in entry.value
                    if isinstance(key, yaml.ScalarNode)
                    and isinstance(value, yaml.ScalarNode)
                }
            )
    return entries


def _is_git_collection(entry: dict[str, tuple[int, str]]) -> bool:
    source = entry.get("source", entry.get("name", (0, "")))[1]
    return entry.get("type", (0, ""))[1] == "git" or source.startswith("git+")


def _collection_version(text: str, project: str) -> tuple[int, str] | None:
    """(0-based line of the `version:` KEY, its value) for the collection that
    installs `project`. None if the entry or its version is absent."""
    for entry in _collection_entries(text):
        named = [entry[k][1] for k in ("name", "source") if k in entry]
        if any(_names_project(v, project) for v in named) and "version" in entry:
            return entry["version"]
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
    text = req.read_text(encoding="utf-8")
    found = _collection_version(text, project)
    if found is None:
        entries = _collection_entries(text)
        named = [
            v for e in entries for k in ("name", "source") if k in e for v in [e[k][1]]
        ]
        # A requirements.yml that DOES install the library but carries no
        # version: is a floating pin, the drift this exists to prevent.
        if any(_names_project(v, project) for v in named):
            return [f"{req}: the {project} collection is installed without a version: pin"]
        # Finding no subject is not the same as finding no drift: a git
        # collection that matches nothing means the pin is going unchecked.
        if any(_is_git_collection(e) for e in entries):
            return [
                f"{req}: no git collection matching {project!r} - the library "
                "pin is not being checked (pass --project)"
            ]
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
    rewritten = _rewrite_scalar_line(lines[line_no], "version", want)
    if rewritten is None:
        raise SystemExit(
            f"{req}: could not rewrite the {project} collection version: line; "
            "fix it by hand"
        )
    lines[line_no] = rewritten
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

    # Unreadable or malformed input is operator error, not a pin finding: exit 2
    # so CI can tell them apart. Wrapping the real calls, not a preflight read,
    # keeps the guard around every read.
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
        # Verified rather than trusted: --fix cannot repair a branch ref, an
        # absent include block, or a `ref:` the line rewriter did not match.
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
