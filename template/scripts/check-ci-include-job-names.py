#!/usr/bin/env python3
"""Resolve every `needs: optional: true` job name to a job something creates.

GitLab ignores an optional need naming no job, so a library rename or a
non-default `job_name` input turns the dependency off. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from ci_yaml import CILoader, Reference, jobs, parse_ci  # noqa: E402
except ImportError:
    print(
        "ERROR: ci_yaml.py must sit next to this script — vendor both "
        "(see scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# `$[[ inputs.job_name ]]`, GitLab's interpolation in an included file's keys.
_INPUT_REF = re.compile(r"\$\[\[\s*inputs\.([A-Za-z0-9_-]+)\s*(?:\|[^\]]*)?\]\]")
# The same reference as the whole value, where an array input substitutes a list.
_WHOLE_INPUT_REF = re.compile(r"\A\s*" + _INPUT_REF.pattern + r"\s*\Z")

# Include keys this gate cannot read: the job set lives outside both checkouts.
UNREADABLE_INCLUDE_KEYS = ("remote", "component", "template")


class OperatorError(RuntimeError):
    """Input or invocation the gate cannot act on — exit 2, never exit 1."""


def parse_extra_jobs(values: List[str]) -> Dict[str, str]:
    """`JOB=REASON` pairs for a job created by a source this gate cannot read."""
    extra: Dict[str, str] = {}
    for raw in values or []:
        name, sep, reason = raw.partition("=")
        if not sep or not name.strip() or not reason.strip():
            raise OperatorError(f"--extra-job takes JOB=REASON, got {raw!r}")
        extra[name.strip()] = reason.strip()
    return extra


def _load(path: Path) -> dict:
    try:
        return parse_ci(path.read_text(encoding="utf-8"), loader=CILoader)
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{path} could not be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise OperatorError(f"{path} is not parseable: {exc}") from exc


def include_entries(doc: dict) -> List[dict]:
    """The `include:` block as a list of mappings, bare strings wrapped."""
    raw = doc.get("include") or []
    if isinstance(raw, dict):
        raw = [raw]
    elif isinstance(raw, str):
        raw = [{"local": raw}]
    elif not isinstance(raw, list):
        return []
    entries: List[dict] = []
    for item in raw:
        if isinstance(item, dict):
            entries.append(item)
        elif isinstance(item, str):
            entries.append({"local": item})
    return entries


def spec_inputs(path: Path) -> Dict[str, object]:
    """A file's declared input defaults, keyed by input name."""
    try:
        docs = [
            d for d in yaml.load_all(path.read_text(encoding="utf-8"), Loader=CILoader)
            if isinstance(d, dict)
        ]
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    for doc in docs:
        spec = doc.get("spec")
        if isinstance(spec, dict) and isinstance(spec.get("inputs"), dict):
            return {
                name: (value or {}).get("default") if isinstance(value, dict) else None
                for name, value in spec["inputs"].items()
            }
    return {}


def input_value(
    name: str, supplied: Dict[str, object], defaults: Dict[str, object]
) -> object | None:
    """What the include passes for `name`, else the file's own default."""
    if isinstance(supplied, dict) and supplied.get(name) is not None:
        return supplied[name]
    return defaults.get(name)


def resolve_key(key: str, supplied: dict, defaults: Dict[str, object]) -> str | None:
    """A job key with its `$[[ inputs.* ]]` references resolved, or None.

    None means an input the include neither passes nor defaults: GitLab would
    reject the pipeline, so the key names no job this gate can credit.
    """
    unresolved = False

    def repl(match: "re.Match") -> str:
        nonlocal unresolved
        value = input_value(match.group(1), supplied, defaults)
        if value is None:
            unresolved = True
            return ""
        return str(value)

    resolved = _INPUT_REF.sub(repl, key)
    return None if unresolved else resolved


def whole_input(
    text: str, supplied: Dict[str, object], defaults: Dict[str, object]
) -> object:
    """The value an `$[[ inputs.x ]]` standing alone substitutes, else the text.

    `needs: $[[ inputs.needs ]]` is how a template takes its dependencies from
    the include, and GitLab substitutes the whole array there.
    """
    match = _WHOLE_INPUT_REF.match(text)
    return input_value(match.group(1), supplied, defaults) if match else text


def optional_need_targets(
    body: dict, doc: dict, supplied: Dict[str, object], defaults: Dict[str, object]
) -> List[str]:
    """The job names one job declares as optional needs, as written."""
    needs = body.get("needs")
    if isinstance(needs, Reference):
        needs = needs.resolve(doc, [])
    if isinstance(needs, str):
        needs = whole_input(needs, supplied, defaults)
    if isinstance(needs, dict):
        needs = [needs]
    targets: List[str] = []
    for need in needs if isinstance(needs, list) else []:
        if isinstance(need, Reference):
            need = need.resolve(doc, None)
        if not isinstance(need, dict) or not need.get("optional"):
            continue
        target = need.get("job")
        if isinstance(target, str) and target:
            targets.append(target)
    return targets


def child_inputs(
    entry: dict, supplied: Dict[str, object], defaults: Dict[str, object]
) -> Dict[str, object]:
    """An include's `inputs:`, its own `$[[ inputs.* ]]` references resolved.

    An unresolvable value lands as None, so the included file falls back to its
    own default and reports the gap if it has none.
    """
    passed = entry.get("inputs")
    if not isinstance(passed, dict):
        return {}
    return {
        name: resolve_key(value, supplied, defaults) if isinstance(value, str) else value
        for name, value in passed.items()
    }


def include_targets(
    entry: dict,
    label: str,
    base: Path,
    lib_root: Path | None,
    supplied: Dict[str, object],
    defaults: Dict[str, object],
    unreadable: List[str],
) -> Iterator[Tuple[Path, str, Dict[str, object]]]:
    """Each file an include names, as `(checkout, path, inputs)`.

    A `local:` resolves in the including file's own checkout, a `project:` in
    the library one.
    """
    if any(key in entry for key in UNREADABLE_INCLUDE_KEYS):
        unreadable.append(f"{label}: include {entry!r} is not a file in either checkout")
        return
    passed = child_inputs(entry, supplied, defaults)
    if "local" in entry:
        child_base, files = base, entry["local"]
    elif "project" in entry and "file" in entry:
        if lib_root is None:
            unreadable.append(
                f"{label}: include of {entry.get('project')} needs a "
                "library checkout (--lib-path or $WEISSSRV_LIB_PATH)"
            )
            return
        child_base, files = lib_root, entry["file"]
    else:
        unreadable.append(f"{label}: include {entry!r} has no readable file")
        return
    for relpath in files if isinstance(files, list) else [files]:
        resolved = resolve_key(str(relpath), supplied, defaults)
        if resolved is None:
            unreadable.append(
                f"{label}: include path {relpath!r} needs an input the include "
                "neither passes nor defaults"
            )
            continue
        yield child_base, resolved.lstrip("/"), passed


def collect(
    doc: dict,
    path: Path,
    label: str,
    base: Path,
    supplied: Dict[str, object],
    lib_root: Path | None,
    ancestry: Tuple[Path, ...],
    created: Set[str],
    needs: Dict[str, Set[str]],
    unreadable: List[str],
) -> None:
    """Record this file's job names and optional needs, then its includes.

    `ancestry` holds the files already open on this branch, so an include cycle
    stops instead of recursing forever.
    """
    defaults = spec_inputs(path)
    for key, body in jobs(doc).items():
        name = resolve_key(key, supplied, defaults)
        if name is None:
            unreadable.append(
                f"{label}: job key {key!r} needs an input the include "
                "neither passes nor defaults"
            )
            continue
        created.add(name)
        for target in optional_need_targets(body, doc, supplied, defaults):
            resolved = resolve_key(target, supplied, defaults)
            if resolved is None:
                unreadable.append(
                    f"{label}: optional need {target!r} of job {name!r} needs an "
                    "input the include neither passes nor defaults"
                )
                continue
            needs.setdefault(resolved, set()).add(f"{label}:{name}")

    for entry in include_entries(doc):
        for child_base, relpath, passed in include_targets(
            entry, label, base, lib_root, supplied, defaults, unreadable
        ):
            included = child_base / relpath
            if included.resolve() in ancestry:
                continue
            if not included.is_file():
                unreadable.append(
                    f"{label}: included file {relpath} is not in {child_base}"
                )
                continue
            try:
                child_doc = parse_ci(
                    included.read_text(encoding="utf-8"), loader=CILoader
                )
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                unreadable.append(f"{label}: {relpath} is not parseable: {exc}")
                continue
            collect(
                child_doc,
                included,
                relpath,
                child_base,
                passed,
                lib_root,
                ancestry + (included.resolve(),),
                created,
                needs,
                unreadable,
            )


def check(
    repo_root: Path,
    ci_files: List[str],
    lib_root: Path | None,
    extra_jobs: Dict[str, str],
) -> Tuple[List[str], int, int]:
    """(problems, optional needs seen, job names created)."""
    created: Set[str] = set(extra_jobs)
    unreadable: List[str] = []
    needs: Dict[str, Set[str]] = {}

    for relpath in ci_files:
        path = repo_root / relpath
        if not path.is_file():
            raise OperatorError(
                f"--ci-file names {relpath}, which does not exist in {repo_root}"
            )
        collect(
            _load(path),
            path,
            relpath,
            repo_root,
            {},
            lib_root,
            (path.resolve(),),
            created,
            needs,
            unreadable,
        )

    if not created:
        raise OperatorError(
            "no job names were resolved from "
            + ", ".join(ci_files)
            + " or their includes — the gate has nothing to resolve needs against"
        )

    problems: List[str] = []
    for target in sorted(needs):
        if target in created:
            continue
        declarers = ", ".join(sorted(needs[target]))
        problems.append(
            f"{target!r} is an optional need of {declarers} but no include "
            "creates it — GitLab ignores the need silently, so the gate it "
            "waits for does not block the pipeline"
        )
    if problems and unreadable:
        problems.append(
            "the job set is incomplete because these includes could not be "
            "read: " + "; ".join(sorted(set(unreadable)))
            + " — pass --extra-job JOB=REASON for a job they create"
        )
    for name in sorted(extra_jobs):
        if name not in needs:
            problems.append(
                f"--extra-job names {name}, which no optional need references — "
                "drop the stale entry"
            )
    return problems, len(needs), len(created)


def resolve_lib_root(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("WEISSSRV_LIB_PATH")
    return Path(env) if env else None


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve optional `needs:` names against the included job set"
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--ci-file", action="append", default=None,
        help="pipeline file to check, repeatable (default: .gitlab-ci.yml)",
    )
    parser.add_argument(
        "--lib-path",
        help="library checkout the `project:` includes resolve in "
             "(default: $WEISSSRV_LIB_PATH)",
    )
    parser.add_argument(
        "--extra-job", action="append", default=[], metavar="JOB=REASON",
        help="job created by a source this gate cannot read, with its reason",
    )
    parser.add_argument(
        "--require-optional-needs", action="store_true",
        help="fail when the pipeline declares no optional need at all",
    )
    args = parser.parse_args(argv)

    try:
        problems, n_needs, n_created = check(
            args.repo_root,
            args.ci_file or [".gitlab-ci.yml"],
            resolve_lib_root(args.lib_path),
            parse_extra_jobs(args.extra_job),
        )
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if problems:
        print("ERROR: optional `needs:` that resolve to no job:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    if not n_needs and args.require_optional_needs:
        print(
            "ERROR: no `needs: optional: true` entry found — this pipeline "
            "declares none, so the gate inspected nothing; drop "
            "--require-optional-needs if that is intended",
            file=sys.stderr,
        )
        return 2

    print(
        "Optional needs OK — %d optional need(s) resolve against %d job name(s)."
        % (n_needs, n_created)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
