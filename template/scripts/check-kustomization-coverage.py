#!/usr/bin/env python3
"""Assert every manifest beside a kustomization.yaml is named by it, and that
every kustomization.yaml renders objects: an unlisted file ships nothing, an
emptied list prunes. Exit 0 clean, 1 a finding, 2 cannot inspect its subject.
"""

from __future__ import annotations

import argparse
import posixpath
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

MANIFEST_SUFFIXES = (".yaml", ".yml")
KUSTOMIZATION = "kustomization.yaml"

# Keys whose entries name a path kustomize reads.
_PATH_LIST_KEYS = (
    "resources",
    "bases",
    "components",
    "crds",
    "transformers",
    "generators",
    "configurations",
    "patches",
    "patchesStrategicMerge",
    "patchesJson6902",
    "replacements",
)
_GENERATOR_KEYS = ("configMapGenerator", "secretGenerator")
_GENERATOR_FILE_KEYS = ("files", "envs", "env")

# Fields that put objects INTO the render. A kustomization carrying none of them
# builds empty however many transformers it lists.
_RESOURCE_KEYS = (
    "resources",
    "bases",
    "components",
    "generators",
    *_GENERATOR_KEYS,
    "helmCharts",
)

# Any key through which a Component contributes to the render. A Component only
# reshapes what its parent lists, so a transformer alone is content there.
_CONTENT_KEYS = (
    *_PATH_LIST_KEYS,
    *_GENERATOR_KEYS,
    "helmCharts",
    "images",
    "labels",
    "commonLabels",
    "commonAnnotations",
    "replicas",
)

# "kubernetes/<path>": reason — a manifest deliberately not reconciled. Each one
# is applied by hand, and the file's own header says when.
EXEMPT: dict[str, str] = {
    "kubernetes/apps/vm-ingress/example-route.yaml": "copy-me example, not a route",
    "kubernetes/apps/vm-ingress/certificate.yaml": "listed alongside your own route",
    "kubernetes/infrastructure/observability/loki/nodeport.yaml": (
        "on-demand only: always on it is an unauthenticated Loki path"
    ),
}

# Floor on the walk itself: a broken glob or a moved tree would otherwise report
# clean having inspected nothing.
MINIMUM_KUSTOMIZATIONS = 20


class Vacuous(Exception):
    """The gate could not inspect its subject — exit 2, never a silent pass."""


def _is_local(name: str) -> bool:
    """A remote base is fetched by kustomize, so it is not a path on disk."""
    if name.startswith(("git::", "git@", "ssh://")) or "://" in name or "?" in name:
        return False
    head, separator, _ = name.partition("//")
    if separator and head and not head.startswith("."):
        return False
    return not any(part.endswith(".git") for part in name.split("/"))


def _entry_paths(base: Path, entry) -> set[Path]:
    name = entry if isinstance(entry, str) else (entry or {}).get("path")
    if not isinstance(name, str) or not _is_local(name):
        return set()
    # kustomize reads `./x.yaml` and `x.yaml` as one path, and an inline patch
    # document is text rather than a filename.
    if "\n" in name:
        return set()
    return {(base / posixpath.normpath(name)).resolve()}


def _load(kustomization: Path) -> dict:
    """The kustomization as a mapping, or exit 2 — an unreadable one proves nothing."""
    try:
        doc = yaml.safe_load(kustomization.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Vacuous(f"{kustomization} unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Vacuous(f"{kustomization} is not a mapping, so it names no resources")
    return doc


def referenced_paths(kustomization: Path) -> set[Path]:
    """Every sibling-relative path the kustomization names."""
    doc = _load(kustomization)
    base = kustomization.parent
    paths: set[Path] = set()
    for key in _PATH_LIST_KEYS:
        for entry in doc.get(key) or []:
            paths |= _entry_paths(base, entry)
    for key in _GENERATOR_KEYS:
        for entry in doc.get(key) or []:
            if not isinstance(entry, dict):
                continue
            for sub in _GENERATOR_FILE_KEYS:
                value = entry.get(sub)
                for item in ([value] if isinstance(value, str) else value) or []:
                    # A generator file entry may be spelled `key=path`.
                    paths |= _entry_paths(base, str(item).split("=", 1)[-1])
    for chart in doc.get("helmCharts") or []:
        if isinstance(chart, dict):
            paths |= _entry_paths(base, chart.get("valuesFile"))
    openapi = doc.get("openapi")
    if isinstance(openapi, dict):
        paths |= _entry_paths(base, openapi.get("path"))
    return paths


def kustomizations(root: Path) -> list[Path]:
    """Every kustomization.yaml under the tree, or exit 2 if there are too few."""
    if not root.is_dir():
        raise Vacuous(f"{root} is not a directory")
    found = sorted(root.rglob(KUSTOMIZATION))
    if len(found) < MINIMUM_KUSTOMIZATIONS:
        raise Vacuous(
            f"only {len(found)} {KUSTOMIZATION} found under {root} — the walk is "
            "broken and this gate is examining almost nothing"
        )
    return found


def unlisted_siblings(root: Path) -> list[str]:
    """Manifests beside a kustomization.yaml that it does not name."""
    orphans = []
    for kustomization in sorted(root.rglob(KUSTOMIZATION)):
        referenced = referenced_paths(kustomization)
        for sibling in sorted(kustomization.parent.iterdir()):
            if not sibling.is_file() or sibling.suffix not in MANIFEST_SUFFIXES:
                continue
            if sibling.name == KUSTOMIZATION:
                continue
            if sibling.resolve() not in referenced:
                orphans.append(sibling.relative_to(root.parent).as_posix())
    return orphans


def contentless(root: Path) -> list[str]:
    """Kustomizations that contribute no objects to the render.

    CRITICAL: the Flux Kustomizations prune, so an emptied list does not ship
    less — it deletes every object that file applied.
    """
    findings = []
    for kustomization in sorted(root.rglob(KUSTOMIZATION)):
        doc = _load(kustomization)
        component = doc.get("kind") == "Component"
        keys = _CONTENT_KEYS if component else _RESOURCE_KEYS
        if any(doc.get(key) for key in keys):
            continue
        subject = "Component" if component else "kustomization.yaml"
        detail = (
            "lists only transformers"
            if any(doc.get(key) for key in _CONTENT_KEYS)
            else "names no resources"
        )
        findings.append(
            f"{kustomization.relative_to(root.parent).as_posix()}: the {subject} "
            f"{detail}, so it renders nothing and Flux prunes what it applied"
        )
    return findings


def check(root: Path) -> list[str]:
    problems = [
        f"{orphan}: on disk, named by no kustomization.yaml, so Flux never applies it"
        for orphan in unlisted_siblings(root)
        if orphan not in EXEMPT
    ]
    stale = sorted(p for p in EXEMPT if not (root.parent / p).is_file())
    problems += [f"EXEMPT names {p}, which is gone" for p in stale]
    return problems + contentless(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default="kubernetes",
        help="manifest tree to walk (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    root = Path(args.directory)

    try:
        count = len(kustomizations(root))
        problems = check(root)
    except (Vacuous, OSError) as exc:
        print(f"check-kustomization-coverage inspected nothing: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Kustomization coverage findings:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        f"Every manifest is listed and every kustomization renders objects "
        f"({count} kustomization.yaml walked under {root})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
