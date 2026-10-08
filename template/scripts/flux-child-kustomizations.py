#!/usr/bin/env python3
"""Print a cluster's child Flux Kustomizations in dependsOn order.

Topologically sorted by `spec.dependsOn`, alphabetical ties; `flux-system` is
not printed. Usage: flux-child-kustomizations.py [--dir DIR] [--paths].
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


def default_dir() -> Path:
    """The single directory under kubernetes/clusters/, when there is exactly
    one. A multi-cluster repo has to say which."""
    if not CLUSTERS_DIR.is_dir():
        raise SystemExit(
            "no %s directory — pass --dir with the cluster directory" % CLUSTERS_DIR
        )
    candidates = sorted(p for p in CLUSTERS_DIR.iterdir() if p.is_dir())
    if len(candidates) != 1:
        raise SystemExit(
            "%s holds %d cluster directories — pass --dir to choose one"
            % (CLUSTERS_DIR, len(candidates))
        )
    return candidates[0]


def _kustomizations(directory: Path) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    deps: Dict[str, Set[str]] = {}
    paths: Dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            if not str(doc.get("apiVersion", "")).startswith("kustomize.toolkit"):
                continue
            name = ((doc.get("metadata") or {}).get("name"))
            if not name:
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


def child_kustomizations(directory: Path) -> Tuple[List[str], List[str]]:
    """(dependency-ordered names, names caught in a dependsOn cycle)."""
    deps, _ = _kustomizations(directory)
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
        help="print each Kustomization's spec.path instead of its name",
    )
    args = parser.parse_args(argv)

    directory = args.dir or default_dir()
    if not directory.is_dir():
        print("no such directory: %s" % directory, file=sys.stderr)
        return 2

    names, cycled = child_kustomizations(directory)
    if not names:
        print("no Flux Kustomizations found under %s" % directory, file=sys.stderr)
        return 1
    if args.paths:
        _, paths = _kustomizations(directory)
        names = [paths[n] for n in names if n in paths]
        if not names:
            print("no Flux Kustomization under %s declares a spec.path" % directory,
                  file=sys.stderr)
            return 1
    print("\n".join(names))
    if cycled:
        print("ERROR: dependsOn cycle among %s - the printed order is NOT "
              "dependency-satisfying" % ", ".join(cycled), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
