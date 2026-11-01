#!/usr/bin/env python3
"""Assert every manifest beside a kustomization is named by it, every name it
lists exists and carries an object, and every kustomization renders objects.
Exit 0 clean, 1 a finding, 2 cannot inspect its subject.
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

# kustomize accepts either spelling, so a gate that reads one walks past the other.
KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml")
# Manifest extensions kustomize renders. JSON carries resources and the Grafana
# dashboards a configMapGenerator reads.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")

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

# Files `flux bootstrap` writes into a cluster's flux-system/ directory. Until
# it has run, the kustomization there names them before either exists.
BOOTSTRAP_WRITTEN = frozenset({"gotk-components.yaml", "gotk-sync.yaml"})

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


def _entry_names(entry) -> set[str]:
    """The local path an entry names, normalised as kustomize reads it."""
    name = entry if isinstance(entry, str) else (entry or {}).get("path")
    if not isinstance(name, str) or not name or not _is_local(name):
        return set()
    # An inline patch document is text rather than a filename.
    if "\n" in name:
        return set()
    return {posixpath.normpath(name)}


def _load(kustomization: Path) -> dict:
    """The kustomization as a mapping, or exit 2 — an unreadable one proves nothing."""
    try:
        doc = yaml.safe_load(kustomization.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Vacuous(f"{kustomization} unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Vacuous(f"{kustomization} is not a mapping, so it names no resources")
    return doc


def _listed_names(doc: dict, keys: tuple[str, ...]) -> set[str]:
    """The local paths the entries under `keys` name."""
    names: set[str] = set()
    for key in keys:
        for entry in doc.get(key) or []:
            names |= _entry_names(entry)
    return names


def reference_names(doc: dict) -> set[str]:
    """Every sibling-relative path the kustomization document names."""
    names = _listed_names(doc, _PATH_LIST_KEYS)
    for key in _GENERATOR_KEYS:
        for entry in doc.get(key) or []:
            if not isinstance(entry, dict):
                continue
            for sub in _GENERATOR_FILE_KEYS:
                value = entry.get(sub)
                for item in ([value] if isinstance(value, str) else value) or []:
                    # A generator file entry may be spelled `key=path`.
                    names |= _entry_names(str(item).split("=", 1)[-1])
    for chart in doc.get("helmCharts") or []:
        if isinstance(chart, dict):
            names |= _entry_names(chart.get("valuesFile"))
    openapi = doc.get("openapi")
    if isinstance(openapi, dict):
        names |= _entry_names(openapi.get("path"))
    return names


def _walk(root: Path) -> list[Path]:
    """Every kustomization under the tree, in either spelling."""
    return sorted({path for name in KUSTOMIZATION_NAMES for path in root.rglob(name)})


def kustomizations(root: Path) -> list[Path]:
    """Every kustomization under the tree, or exit 2 if there are too few."""
    if not root.is_dir():
        raise Vacuous(f"{root} is not a directory")
    found = _walk(root)
    if len(found) < MINIMUM_KUSTOMIZATIONS:
        raise Vacuous(
            f"only {len(found)} kustomization files found under {root} — the walk "
            "is broken and this gate is examining almost nothing"
        )
    return found


def unlisted_siblings(root: Path) -> list[str]:
    """Manifests beside a kustomization that it does not name."""
    orphans = []
    for kustomization in _walk(root):
        doc = _load(kustomization)
        base = kustomization.parent
        referenced = {(base / name).resolve() for name in reference_names(doc)}
        for sibling in sorted(kustomization.parent.iterdir()):
            if not sibling.is_file() or sibling.suffix not in MANIFEST_SUFFIXES:
                continue
            if sibling.name in KUSTOMIZATION_NAMES:
                continue
            if sibling.resolve() not in referenced:
                orphans.append(sibling.relative_to(root.parent).as_posix())
    return orphans


def missing_references(root: Path) -> list[str]:
    """`kustomization -> name` pairs naming a local path that is not on disk,
    which fails `kustomize build` for the whole stage. The bootstrap files are
    not missing while neither exists, before `flux bootstrap` has run."""
    missing = []
    for kustomization in _walk(root):
        doc = _load(kustomization)
        base = kustomization.parent
        pre_bootstrap = base.name == "flux-system" and not any(
            (base / name).exists() for name in BOOTSTRAP_WRITTEN
        )
        for name in sorted(reference_names(doc)):
            if pre_bootstrap and name in BOOTSTRAP_WRITTEN:
                continue
            if not (base / name).exists():
                missing.append(f"{kustomization.relative_to(root.parent).as_posix()} -> {name}")
    return missing


def carries_an_object(manifest: Path) -> bool:
    """A manifest renders nothing unless some document carries a `kind`."""
    try:
        with manifest.open(encoding="utf-8") as handle:
            return any(
                isinstance(doc, dict) and doc.get("kind") for doc in yaml.safe_load_all(handle)
            )
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Vacuous(f"{manifest} unreadable: {exc}") from exc


def empty_listed_manifests(root: Path) -> list[str]:
    """`kustomization -> name` pairs listing a resource file with no object, so
    kustomize renders nothing from it and Flux prunes what it applied."""
    empty = []
    for kustomization in _walk(root):
        doc = _load(kustomization)
        base = kustomization.parent
        for name in sorted(_listed_names(doc, ("resources", "bases"))):
            manifest = base / name
            if manifest.is_file() and not carries_an_object(manifest):
                empty.append(f"{kustomization.relative_to(root.parent).as_posix()} -> {name}")
    return empty


def contentless(root: Path) -> list[str]:
    """Kustomizations that contribute no objects to the render.

    CRITICAL: the Flux Kustomizations prune, so an emptied list does not ship
    less — it deletes every object that file applied.
    """
    findings = []
    for kustomization in _walk(root):
        doc = _load(kustomization)
        component = doc.get("kind") == "Component"
        keys = _CONTENT_KEYS if component else _RESOURCE_KEYS
        if any(doc.get(key) for key in keys):
            continue
        subject = "Component" if component else kustomization.name
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
        f"{orphan}: on disk, named by no kustomization, so Flux never applies it"
        for orphan in unlisted_siblings(root)
        if orphan not in EXEMPT
    ]
    problems += [
        f"{pair}: names a path that is not on disk, so `kustomize build` fails the stage"
        for pair in missing_references(root)
    ]
    problems += [
        f"{pair}: lists a manifest that carries no object, so it renders nothing "
        "and Flux prunes what it applied"
        for pair in empty_listed_manifests(root)
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
        f"({count} kustomizations walked under {root})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
