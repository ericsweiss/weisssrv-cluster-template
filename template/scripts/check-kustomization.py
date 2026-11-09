#!/usr/bin/env python3
"""Assert every kustomization lists every manifest beside it, names no missing
path but the two gotk files `flux bootstrap` writes, lists no empty manifest and
renders something. Exit 0 clean, 1 violation, 2 operator error.
"""

from __future__ import annotations

import argparse
import pathlib
import posixpath
import sys

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml")
# JSON is a manifest too: kustomize renders JSON resources, and a
# configMapGenerator reads Grafana dashboards from JSON siblings.
MANIFEST_GLOBS = ("*.yaml", "*.yml", "*.json")
# Keys that put objects into the render. A kustomization carrying none of them
# builds empty however many transformers it lists.
RESOURCE_KEYS = (
    "resources", "bases", "components", "generators",
    "configMapGenerator", "secretGenerator", "helmCharts",
)
# A Component only reshapes its parent's render, so in a Component any of these
# keys, a lone transformer included, counts as content.
CONTENT_KEYS = RESOURCE_KEYS + (
    "patches", "patchesStrategicMerge", "patchesJson6902", "replacements",
    "transformers", "images", "labels", "commonLabels", "commonAnnotations",
    "replicas", "openapi", "crds", "configurations",
)
# `flux bootstrap` writes these two files, so a kustomization whose resources
# name both is a bootstrap directory and may name them before they exist.
FLUX_BOOTSTRAP_FILES = frozenset({"gotk-components.yaml", "gotk-sync.yaml"})


class GateError(Exception):
    """An operator error: the gate could not run, so it proves nothing."""


def is_local(name: str) -> bool:
    """A remote base is fetched by kustomize, so it is not a path on disk."""
    if name.startswith(("git::", "git@", "ssh://", "github.com/")) or "://" in name:
        return False
    if "?" in name:  # ?ref=, ?timeout=, ?submodules=
        return False
    head, separator, _ = name.partition("//")
    if separator and head and not head.startswith("."):  # host/org/repo//path
        return False
    return not any(part.endswith(".git") for part in name.split("/"))


def normalize(name: str) -> str:
    """kustomize treats `./foo.yaml`, `foo.yaml` and `foo/` as one path."""
    return posixpath.normpath(name)


def _local(name: object) -> set[str]:
    if not isinstance(name, str) or not name or "\n" in name or not is_local(name):
        return set()
    return {normalize(name)}


def referenced_paths(doc: dict) -> set[str]:
    """Every local path kustomization.yaml names, however it names it."""
    referenced: set[str] = set()
    for key in ("resources", "bases", "components", "crds", "transformers",
                "generators", "configurations"):
        for entry in doc.get(key) or []:
            referenced |= _local(entry)
    # patchesStrategicMerge also takes an inline patch document, which _local skips.
    for entry in doc.get("patchesStrategicMerge") or []:
        referenced |= _local(entry)
    for key in ("patches", "patchesJson6902", "replacements"):
        for entry in doc.get(key) or []:
            referenced |= _local(entry.get("path") if isinstance(entry, dict) else entry)
    for key in ("configMapGenerator", "secretGenerator"):
        for generator in doc.get(key) or []:
            if not isinstance(generator, dict):
                continue
            # `env` is kustomize's deprecated singular spelling of `envs`.
            for field in ("files", "envs", "env"):
                value = generator.get(field)
                for entry in [value] if isinstance(value, str) else value or []:
                    if isinstance(entry, str):
                        # A generator file entry may be spelled `key=path`.
                        referenced |= _local(entry.split("=", 1)[-1])
    for chart in doc.get("helmCharts") or []:
        if isinstance(chart, dict):
            referenced |= _local(chart.get("valuesFile"))
    openapi = doc.get("openapi")
    if isinstance(openapi, dict):
        referenced |= _local(openapi.get("path"))
    return referenced


def manifests_on_disk(base: pathlib.Path, referenced: set[str], deep: bool) -> set[str]:
    """Manifests beside the kustomization, or under it when `deep`, minus what a
    listed directory covers."""
    listed_dirs = [n for n in referenced if (base / n).is_dir()]
    on_disk = set()
    for pattern in MANIFEST_GLOBS:
        for path in base.rglob(pattern) if deep else base.glob(pattern):
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            if relative in KUSTOMIZATION_NAMES:
                continue
            if any(relative.startswith(directory + "/") for directory in listed_dirs):
                continue
            on_disk.add(relative)
    return on_disk


def carries_an_object(path: pathlib.Path) -> bool:
    """A manifest whose documents hold no `kind` renders nothing."""
    return any(
        isinstance(doc, dict) and doc.get("kind")
        for doc in _read_yaml(path, every=True)
    )


def _read_yaml(path: pathlib.Path, *, every: bool = False):
    """The file's document, or with `every` all of them; a failure is a GateError.

    The handle, not the text: PyYAML names the stream in its mark, so a parse
    error points at the file, not at "<unicode string>"."""
    try:
        with path.open(encoding="utf-8") as handle:
            return list(yaml.safe_load_all(handle)) if every else yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError) as error:
        raise GateError(f"{path}: unreadable: {error}") from error
    except yaml.YAMLError as error:
        raise GateError(f"{path}: unparseable YAML: {error}") from error


def _load_mapping(path: pathlib.Path, what: str) -> dict:
    """The file's single YAML mapping, empty when the file is; else a GateError."""
    loaded = _read_yaml(path) or {}
    if not isinstance(loaded, dict):
        raise GateError(f"{path}: {what}")
    return loaded


def load(base: pathlib.Path) -> dict | None:
    """The directory's kustomization, or None when it has none."""
    for name in KUSTOMIZATION_NAMES:
        if (base / name).is_file():
            return _load_mapping(base / name, "kustomization.yaml is not a mapping")
    return None


class Run:
    """One gate run: the repo root, the exemptions and what has been inspected."""

    def __init__(self, repo_root: pathlib.Path, exempt: dict[str, str] | None = None):
        self.repo_root = repo_root
        self.exempt = exempt or {}
        self.visited: set[pathlib.Path] = set()
        self.inspected = 0

    def relative(self, path: pathlib.Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def check(self, base: pathlib.Path, entry: bool, deep: bool) -> tuple[list[str], int]:
        """Problems with one directory and every directory it lists, and its resource count.

        `entry` marks the directory a build starts from: one with no kustomization
        is an operator error there, and a Component there is held to the regular rule."""
        resolved = base.resolve()
        if resolved in self.visited:
            return [], 0
        self.visited.add(resolved)

        doc = load(base)
        if doc is None:
            absent = f"{base / 'kustomization.yaml'} does not exist: kustomize builds nothing here"
            if entry:
                raise GateError(absent)
            return [absent], 0
        self.inspected += 1

        listed = [
            name for key in ("resources", "bases")
            for name in doc.get(key) or [] if isinstance(name, str)
        ]
        if doc.get("kind") == "Component" and not entry:
            if not any(doc.get(key) for key in CONTENT_KEYS):
                return [f"{base}: the Component contributes nothing, so it is inert"], 0
        elif not any(doc.get(key) for key in RESOURCE_KEYS):
            what = (
                "lists only transformers" if any(doc.get(key) for key in CONTENT_KEYS)
                else "lists no resources"
            )
            return [f"{base}: kustomization.yaml {what}, so the render is empty"], 0

        problems = []
        referenced = referenced_paths(doc)
        resources = {normalize(n) for n in doc.get("resources") or [] if isinstance(n, str)}
        absent_ok = FLUX_BOOTSTRAP_FILES if FLUX_BOOTSTRAP_FILES <= resources else set()
        missing = sorted(
            f"{base / name}" for name in referenced - absent_ok if not (base / name).exists()
        )
        if missing:
            problems.append(f"kustomization.yaml names paths that do not exist: {missing}")
        empty = sorted(
            f"{base / name}"
            for name in {normalize(n) for n in listed if is_local(n)}
            if (base / name).is_file() and not carries_an_object(base / name)
        )
        if empty:
            problems.append(
                "manifests that carry no object, so kustomize renders nothing from them "
                f"and the cluster prunes what they used to apply: {empty}"
            )
        unlisted = sorted(
            f"{base / name}"
            for name in manifests_on_disk(base, referenced, deep) - referenced
            if self.relative(base / name) not in self.exempt
        )
        if unlisted:
            problems.append(
                f"manifests present but not listed, so Flux never builds them: {unlisted}"
            )
        for name in sorted(referenced):
            if (base / name).is_dir():
                problems += self.check(base / name, entry=False, deep=deep)[0]
        return problems, len(listed)

    def stale_exemptions(self) -> list[str]:
        return [
            f"the exemption for {path} names a file that is gone; drop it"
            for path in sorted(self.exempt)
            if not (self.repo_root / path).is_file()
        ]


def load_exemptions(path: pathlib.Path) -> dict[str, str]:
    """A mapping of repo-relative manifest path to the reason it is not listed."""
    doc = _load_mapping(path, "an exemption file is a mapping of path to reason")
    bad = sorted(
        str(k) for k, v in doc.items()
        if not isinstance(k, str) or not isinstance(v, str) or not v.strip()
    )
    if bad:
        raise GateError(f"{path}: every exemption needs a path and a reason: {bad}")
    return {normalize(k): v for k, v in doc.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default="kubernetes/flux",
        help="kustomization directory, or with --walk the tree to walk "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--walk", action="store_true",
        help="check every kustomization under the directory, each against its "
             "own siblings, instead of descending from the one at its root; a "
             "manifest in a subdirectory with no kustomization is not seen",
    )
    parser.add_argument(
        "--min-kustomizations", type=int, default=1, metavar="N",
        help="exit 2 when fewer kustomizations are inspected: a walk that finds "
             "almost nothing is broken, not clean (default: %(default)s)",
    )
    parser.add_argument(
        "--exempt", type=pathlib.Path, metavar="FILE",
        help="YAML mapping of repo-relative manifest path to the reason it is "
             "deliberately unlisted; an entry naming a gone file is a violation",
    )
    parser.add_argument(
        "--repo-root", type=pathlib.Path, default=pathlib.Path("."),
        help="root the directory, the exemption file and its paths resolve "
             "against (default: the working directory)",
    )
    arguments = parser.parse_args(argv)

    root = arguments.repo_root
    base = root / arguments.directory
    if not base.is_dir():
        print(
            f"ERROR: {base} is not a directory: the gate was pointed at a path that "
            "does not exist",
            file=sys.stderr,
        )
        return 2
    try:
        exempt = {}
        if arguments.exempt is not None:
            exempt = load_exemptions(root / arguments.exempt)
        run = Run(root, exempt)
        if arguments.walk:
            problems, count = [], 0
            # Every directory under the tree holding a kustomization, in either spelling.
            walked = {p.parent for name in KUSTOMIZATION_NAMES for p in base.rglob(name)}
            for directory in sorted(walked):
                problems += run.check(directory, entry=False, deep=False)[0]
        else:
            problems, count = run.check(base, entry=True, deep=True)
        if run.inspected < arguments.min_kustomizations:
            raise GateError(
                f"only {run.inspected} kustomization(s) under {base}, fewer than "
                f"--min-kustomizations {arguments.min_kustomizations}: a broken walk "
                "rather than a clean tree"
            )
        problems += run.stale_exemptions()
    except GateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    if arguments.walk:
        print(f"{run.inspected} kustomizations under {base}; no manifest is unlisted")
    else:
        print(f"kustomization.yaml lists {count} resources; no manifest is unlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
