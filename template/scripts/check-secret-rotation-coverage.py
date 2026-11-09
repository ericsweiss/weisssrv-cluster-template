#!/usr/bin/env python3
"""Assert every ESO-managed credential has a rotation path written down.

Each key and ExternalSecret is named in a `--doc` file or its `--section`s, or declared
manual; a `dataFrom.find` read must be declared manual. Inputs: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# left out takes its ExternalSecrets with it, silently covered.
MANIFEST_GLOBS = ("*.yaml", "*.yml")

SECRET_KINDS = ("ExternalSecret", "ClusterExternalSecret")


class OperatorError(RuntimeError):
    """Input or invocation the gate cannot act on — exit 2, never exit 1."""


def parse_declared_manual(values: List[str]) -> Dict[str, str]:
    """`NAME=REASON` pairs. A reason is mandatory: an unexplained exemption is
    a credential nobody rotates."""
    declared: Dict[str, str] = {}
    for raw in values or []:
        name, sep, reason = raw.partition("=")
        if not sep or not name.strip() or not reason.strip():
            raise OperatorError(
                f"--declared-manual takes NAME=REASON, got {raw!r}"
            )
        declared[name.strip()] = reason.strip()
    return declared


def _remote_keys(node: object) -> List[str]:
    """Every vault item title the document reads, at any nesting depth."""
    found: List[str] = []
    if isinstance(node, dict):
        for field in ("remoteRef", "extract"):
            ref = node.get(field)
            if isinstance(ref, dict) and ref.get("key"):
                found.append(str(ref["key"]))
        for value in node.values():
            found += _remote_keys(value)
    elif isinstance(node, list):
        for item in node:
            found += _remote_keys(item)
    return found


def _finds(node: object) -> bool:
    """Whether the document reads vault items by `dataFrom.find`, a pattern no
    rotation entry can name item by item."""
    if isinstance(node, dict):
        if isinstance(node.get("find"), dict):
            return True
        return any(_finds(value) for value in node.values())
    if isinstance(node, list):
        return any(_finds(item) for item in node)
    return False


def external_secrets(
    root: Path, manifest_dirs: List[str], reports: List[str] | None = None
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """({ns/name: file}, {remoteRef key: file}, {ns/name: file} reading by `find`).

    A namespace-less ExternalSecret goes to `reports`; an unreadable file raises."""
    names: Dict[str, str] = {}
    keys: Dict[str, str] = {}
    finds: Dict[str, str] = {}
    for manifest_dir in manifest_dirs:
        tree = root / manifest_dir
        if not tree.is_dir():
            raise OperatorError(
                f"manifest directory {manifest_dir!r} does not exist — "
                "the gate has no corpus to inspect"
            )
        for path in sorted({p for glob in MANIFEST_GLOBS for p in tree.rglob(glob)}):
            rel = path.relative_to(root).as_posix()
            try:
                # The handle, not the text: PyYAML names the file in its error mark.
                with path.open(encoding="utf-8") as handle:
                    docs = list(yaml.safe_load_all(handle))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                raise OperatorError(f"{rel} unreadable, so its objects cannot be checked: {exc}") from exc
            for doc in docs:
                if not isinstance(doc, dict) or doc.get("kind") not in SECRET_KINDS:
                    continue
                meta = doc.get("metadata") or {}
                name = None
                if doc.get("kind") == "ClusterExternalSecret":
                    # Cluster-scoped: no namespace, and the name operators
                    # rotate is the per-namespace externalSecretName it fans out.
                    spec = doc.get("spec") or {}
                    name = str(spec.get("externalSecretName") or meta.get("name"))
                elif meta.get("namespace"):
                    name = f"{meta['namespace']}/{meta.get('name')}"
                if name is not None:
                    names.setdefault(name, rel)
                    if _finds(doc):
                        finds.setdefault(name, rel)
                elif reports is not None:
                    # A namespace-less copy, as a Kustomize component ships,
                    # would key once instead of once per including namespace, so
                    # coverage would stand for one and silently cover the rest.
                    reports.append(
                        f"{rel}: ExternalSecret {meta.get('name')} declares no "
                        "metadata.namespace, so rotation coverage cannot be keyed "
                        "to a namespace — name it here, or key the entry per "
                        "including kustomization in this gate"
                    )
                for key in _remote_keys(doc):
                    keys.setdefault(key, rel)
    return names, keys, finds


def section_text(text: str, headings: List[str], where: str) -> str:
    """The bodies of the named `#` or `##` sections, each running to the next
    `#` or `##` heading outside a code fence; OperatorError when one is absent."""
    bodies: Dict[str, List[str]] = {}
    current = None
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        heading = None if fenced else re.match(r"#{1,2}\s+(.*?)\s*#*\s*$", line)
        if heading:
            current = heading.group(1) if heading.group(1) in headings else None
            if current is not None:
                bodies.setdefault(current, [])
            continue
        if current is not None:
            bodies[current].append(line)
    missing = [h for h in headings if h not in bodies]
    if missing:
        raise OperatorError(
            f"{where} has no `{'` / `'.join(missing)}` section (a # or ## heading), "
            "so the gate cannot tell where a credential is documented"
        )
    return "\n".join(line for h in headings for line in bodies[h])


def documents(name: str, text: str) -> bool:
    """Whole-name match: a longer name never covers a shorter one.

    The boundary class carries `-` and `/` besides word characters, so a
    space-separated vault title can still match inside a longer phrase.
    """
    pattern = r"(?<![A-Za-z0-9_/-])" + re.escape(name) + r"(?![A-Za-z0-9_/-])"
    return re.search(pattern, text) is not None


def check(
    root: Path,
    manifest_dirs: List[str],
    docs: List[str],
    declared_manual: Dict[str, str],
    sections: List[str] | None = None,
) -> Tuple[List[str], int, int]:
    """(problems, vault keys seen, ExternalSecrets seen)."""
    reports: List[str] = []
    names, keys, finds = external_secrets(root, manifest_dirs, reports)
    if not names and not keys:
        raise OperatorError(
            "no ExternalSecret or ClusterExternalSecret found under "
            + ", ".join(f"{d}/" for d in manifest_dirs)
            + " — a gate that checks nothing is not a gate"
        )
    text = ""
    for doc in docs:
        doc_path = root / doc
        try:
            body = doc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError(f"{doc} could not be read: {exc}") from exc
        text += (section_text(body, sections, doc) if sections else body) + "\n"
    doc_list = " / ".join(docs)
    if sections:
        doc_list += " under " + " / ".join(f"`{h}`" for h in sections)

    problems = list(reports)
    for key, rel in sorted(keys.items()):
        if not documents(key, text):
            problems.append(
                f"{rel}: remoteRef.key {key!r} is named nowhere in {doc_list} — "
                "the vault item has no documented rotation"
            )
    for name, rel in sorted(finds.items()):
        if name not in declared_manual:
            problems.append(
                f"{rel}: ExternalSecret {name} reads vault items by dataFrom.find, "
                "a pattern no rotation entry can name — read them by key, or pass "
                "--declared-manual NAME=REASON for it"
            )
    for name, rel in sorted(names.items()):
        if documents(name, text) or name in declared_manual:
            continue
        problems.append(
            f"{rel}: ExternalSecret {name} is reached by no rotation path — "
            f"name it in {doc_list}, or pass "
            "--declared-manual NAME=REASON for it"
        )
    for name in sorted(declared_manual):
        if name not in names:
            problems.append(
                f"--declared-manual names {name}, which no ExternalSecret "
                "declares — drop the stale entry"
            )
    return problems, len(keys), len(names)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotation coverage for ESO-managed secrets"
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--manifest-dir", action="append", default=None,
        help="manifest tree to walk, repeatable (default: kubernetes)",
    )
    parser.add_argument(
        "--doc", action="append", required=True,
        help="rotation document the names must appear in, repeatable",
    )
    parser.add_argument(
        "--section", action="append", default=[], metavar="HEADING",
        help="match only inside this `#` or `##` section of every --doc, repeatable; "
             "a doc without it exits 2",
    )
    parser.add_argument(
        "--declared-manual", action="append", default=[], metavar="NAME=REASON",
        help="ExternalSecret rotated outside the document, with its reason",
    )
    args = parser.parse_args(argv)

    try:
        declared_manual = parse_declared_manual(args.declared_manual)
        problems, n_keys, n_names = check(
            args.repo_root,
            args.manifest_dir or ["kubernetes"],
            args.doc,
            declared_manual,
            args.section,
        )
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if problems:
        print(
            "ERROR: credentials outside the documented rotation lifecycle:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(
        "Rotation coverage OK — %d vault item(s) and %d ExternalSecret(s) "
        "documented, %d declared manual." % (n_keys, n_names, len(declared_manual))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
