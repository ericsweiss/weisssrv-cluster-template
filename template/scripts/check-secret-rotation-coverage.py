#!/usr/bin/env python3
"""Assert every ESO-managed credential has a rotation path written down.

Each `remoteRef.key` and each ExternalSecret must be in the runbook or in DECLARED_MANUAL
(a ClusterExternalSecret by its externalSecretName). Exit 0 clean, 1 uncovered, 2 vacuous.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

MANIFEST_TREE = "kubernetes"
# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# left out takes its ExternalSecrets with it, silently covered.
MANIFEST_GLOBS = ("*.yaml", "*.yml")
DOC = "docs/RUNBOOKS.md"

# ExternalSecrets exempted from the runbook requirement, each with a reason.
# Ships empty: an exemption is an edit to this gate, and a stale one fails it.
DECLARED_MANUAL: dict[str, str] = {}


class Vacuous(Exception):
    """The gate could not inspect its subject — exit 2, never a silent pass."""


def external_secrets(
    root: Path, reports: list[str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({ns/name: file}, {remoteRef key: file}) for the manifest tree.

    Anything that would silently drop an ExternalSecret from coverage — an
    unparseable file, a namespace-less ExternalSecret — is appended to `reports`.
    """
    names: dict[str, str] = {}
    keys: dict[str, str] = {}
    tree = root / MANIFEST_TREE
    for path in sorted({p for glob in MANIFEST_GLOBS for p in tree.rglob(glob)}):
        rel = path.relative_to(root).as_posix()
        try:
            # The handle, not the text: PyYAML names the file in its error mark.
            with path.open(encoding="utf-8") as handle:
                docs = list(yaml.safe_load_all(handle))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            if reports is not None:
                reports.append(f"{rel} unparseable, its objects were not checked: {exc}")
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in (
                "ExternalSecret",
                "ClusterExternalSecret",
            ):
                continue
            meta = doc.get("metadata") or {}
            if doc.get("kind") == "ClusterExternalSecret":
                # Cluster-scoped: no namespace, and the name operators rotate is
                # the per-namespace externalSecretName it fans out.
                spec = doc.get("spec") or {}
                names.setdefault(
                    str(spec.get("externalSecretName") or meta.get("name")), rel
                )
            elif meta.get("namespace"):
                names.setdefault(f"{meta['namespace']}/{meta.get('name')}", rel)
            elif reports is not None:
                # A namespace-less copy, as a Kustomize component ships, would
                # key once instead of once per including namespace, so coverage
                # would stand for one namespace and silently cover the rest.
                reports.append(
                    f"{rel}: ExternalSecret {meta.get('name')} declares no "
                    "metadata.namespace, so rotation coverage cannot be keyed to a "
                    "namespace — name it here, or key the entry per including "
                    "kustomization in this gate"
                )
            for key in _remote_keys(doc):
                keys.setdefault(key, rel)
    return names, keys


def _remote_keys(node) -> list[str]:
    """Every vault item title the document reads, at any nesting depth."""
    found: list[str] = []
    if isinstance(node, dict):
        for field in ("remoteRef", "extract", "find"):
            ref = node.get(field)
            if isinstance(ref, dict) and ref.get("key"):
                found.append(str(ref["key"]))
        for value in node.values():
            found += _remote_keys(value)
    elif isinstance(node, list):
        for item in node:
            found += _remote_keys(item)
    return found


def documents(name: str, text: str) -> bool:
    """Whole-name match: a longer name never covers a shorter one. The boundary
    class carries `-` and `/` besides word characters, so a space-separated
    vault title can still match inside a longer phrase.
    """
    pattern = r"(?<![A-Za-z0-9_/-])" + re.escape(name) + r"(?![A-Za-z0-9_/-])"
    return re.search(pattern, text) is not None


def check(root: Path) -> list[str]:
    return check_detailed(root)[0]


def check_detailed(root: Path) -> tuple[list[str], int, int]:
    """(problems, vault keys seen, ExternalSecrets seen)."""
    reports: list[str] = []
    names, keys = external_secrets(root, reports)
    if not names:
        raise Vacuous(
            f"no ExternalSecret found under {MANIFEST_TREE}/ — "
            "a gate that checks nothing is not a gate"
        )
    doc = (root / DOC).read_text(encoding="utf-8")

    problems = list(reports)
    for key, rel in sorted(keys.items()):
        if not documents(key, doc):
            problems.append(
                f"{rel}: remoteRef.key {key!r} is named nowhere in {DOC} — "
                "the vault item has no documented rotation"
            )
    for name, rel in sorted(names.items()):
        if documents(name, doc) or name in DECLARED_MANUAL:
            continue
        problems.append(
            f"{rel}: ExternalSecret {name} is reached by no rotation path — "
            f"add a row to {DOC} § Rotating a secret, or an entry to "
            "DECLARED_MANUAL with a reason"
        )
    for name in sorted(DECLARED_MANUAL):
        if name not in names:
            problems.append(
                f"DECLARED_MANUAL names {name}, which no ExternalSecret declares — "
                "drop the stale entry"
            )
    return problems, len(keys), len(names)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rotation coverage for ESO secrets")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    root = Path(args.repo_root)

    try:
        problems, n_keys, n_names = check_detailed(root)
    except Vacuous as exc:
        print(f"check-secret-rotation-coverage inspected nothing: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        print(f"check-secret-rotation-coverage could not read a file: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Credentials outside the documented rotation lifecycle:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        f"Rotation coverage OK ({n_keys} vault items in {DOC}, "
        f"{n_names} ExternalSecrets, {len(DECLARED_MANUAL)} declared manual)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
