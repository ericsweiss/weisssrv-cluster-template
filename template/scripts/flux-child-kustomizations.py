#!/usr/bin/env python3
"""Print a cluster's child Flux Kustomizations in dependsOn order.

Topologically sorted by `spec.dependsOn`, alphabetical ties; `flux-system` is
not printed. Flags and output format: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

CLUSTERS_DIR = Path("kubernetes") / "clusters"
# The bootstrap Kustomization reconciles the Flux controllers themselves, so
# feeding its path to a render corpus judges them against consumer policy.
DEFAULT_EXCLUDE = frozenset({"flux-system"})


def default_dir() -> Path:
    """The single directory under kubernetes/clusters/, when there is exactly
    one. A multi-cluster repo has to say which."""
    if not CLUSTERS_DIR.is_dir():
        print(
            "no %s directory — pass --dir with the cluster directory" % CLUSTERS_DIR,
            file=sys.stderr,
        )
        raise SystemExit(2)
    candidates = sorted(p for p in CLUSTERS_DIR.iterdir() if p.is_dir())
    if len(candidates) != 1:
        print(
            "%s holds %d cluster directories — pass --dir to choose one"
            % (CLUSTERS_DIR, len(candidates)),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return candidates[0]


def _kustomizations(
    directory: Path, unreadable: list | None = None
) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    deps: Dict[str, Set[str]] = {}
    paths: Dict[str, str] = {}
    # Both suffixes: Flux reconciles either, so globbing one would drop a stage
    # from the order and leave its whole path ungated downstream.
    for path in sorted({*directory.glob("*.yaml"), *directory.glob("*.yml")}):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            if unreadable is not None:
                unreadable.append((path, exc))
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            if not str(doc.get("apiVersion", "")).startswith("kustomize.toolkit"):
                continue
            name = ((doc.get("metadata") or {}).get("name"))
            if not name or name in DEFAULT_EXCLUDE:
                continue
            spec = doc.get("spec") or {}
            deps[name] = {
                d["name"]
                for d in (spec.get("dependsOn") or [])
                if isinstance(d, dict) and d.get("name")
            }
            if spec.get("path"):
                paths[name] = str(spec["path"])
    return deps, paths


def _order(deps: Dict[str, Set[str]]) -> Tuple[List[str], List[str]]:
    """(dependency-ordered names, names caught in a dependsOn cycle)."""
    ordered: List[str] = []
    cycled: List[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(n for n, d in remaining.items() if not (d & set(remaining)) - {n})
        if not ready:
            # A cycle would loop forever; emit what is left deterministically so
            # the names still print for diagnosis.
            cycled = sorted(remaining)
            ready = cycled
        for name in ready:
            ordered.append(name)
            del remaining[name]
    return ordered, cycled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dir", type=Path, default=None,
        help="cluster directory (default: the single one under kubernetes/clusters)",
    )
    parser.add_argument(
        "--paths", action="store_true",
        help="print `name<TAB>spec.path` instead of the name alone",
    )
    args = parser.parse_args(argv)

    directory = args.dir or default_dir()
    if not directory.is_dir():
        print("no such directory: %s" % directory, file=sys.stderr)
        return 2

    unreadable: List = []
    deps, paths = _kustomizations(directory, unreadable=unreadable)
    if unreadable:
        for path, exc in unreadable:
            print("%s: %s" % (path, exc), file=sys.stderr)
        print("    Fix: Flux would reject the file — make it parse, or move it "
              "out of the cluster directory.", file=sys.stderr)
        return 2
    names, cycled = _order(deps)
    if not names:
        print("no Flux Kustomizations found under %s" % directory, file=sys.stderr)
        return 1
    if args.paths:
        skipped = [n for n in names if n not in paths]
        # `name<TAB>path`: a caller reading only the path takes `cut -f2`, and a
        # job log that names the stage is what makes a failing one identifiable.
        names = ["%s\t%s" % (n, paths[n]) for n in names if n in paths]
        if skipped:
            print("ERROR: %d Kustomization(s) declare no spec.path: %s"
                  % (len(skipped), ", ".join(skipped)), file=sys.stderr)
            return 1
    print("\n".join(names))
    if cycled:
        print("ERROR: dependsOn cycle among %s - the printed order is NOT "
              "dependency-satisfying" % ", ".join(cycled), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
