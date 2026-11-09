#!/usr/bin/env python3
"""Tenancy names and the target resolution the two tenant gates share.

Vendor this file and gate_common.py beside check-tenant-wiring.py and
check-tenant-traefik-isolation.py.
"""
from __future__ import annotations

import argparse
import posixpath
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this module's own directory)
        OperatorError,
        load_docs,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside tenant_common.py "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

import yaml  # noqa: E402  (gate_common exits 2 when PyYAML is absent)

AGGREGATOR = "kustomization.yaml"
# Applied unconditionally and bound by every tenant, so it is not a tenant file.
SHARED = frozenset({"tenant-crd-editor.yaml"})
FLUX_NAMESPACE = "flux-system"
TENANT_LABEL = "fluxcd.io/tenant"
GRANT_PREFIX = "tenant-routing-"
HOSTNAME_POLICY = "tenant-traefik-hostnames"
CERTIFICATE_POLICY = "tenant-certificates"
GRANT_POLICIES = (HOSTNAME_POLICY, CERTIFICATE_POLICY)
CLUSTERS_DIR = "kubernetes/clusters"
TENANTS_SUBDIR = "tenants"
# The one StorageClass a tenant claim may name.
TENANT_STORAGE_CLASS = "tenant-local"


def label(path: Path, root: Path) -> str:
    """`path` relative to `root` when it sits under it, else as given."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class Targets:
    """What one run inspects: a tenants directory with its aggregator, or files."""

    def __init__(self, root: Path, tenants_dir: Path | None = None,
                 files: list[Path] | None = None):
        self.root = root
        self.tenants_dir = tenants_dir
        self.files = files or []

    def label(self, path: Path) -> str:
        return label(path, self.root)


def as_mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def metadata(doc: dict) -> dict:
    """A document's metadata, or an empty mapping when it has none."""
    return as_mapping(doc.get("metadata"))


def of_kind(docs: list, kind: str) -> list[dict]:
    """The documents of one kind, in order."""
    return [d for d in docs if d.get("kind") == kind]


def discover_tenants_dir(root: Path) -> Path:
    """The one `kubernetes/clusters/<name>/tenants` directory under `root`."""
    found = sorted(
        p for p in (root / CLUSTERS_DIR).glob(f"*/{TENANTS_SUBDIR}") if p.is_dir()
    )
    if not found:
        raise OperatorError(
            f"no {CLUSTERS_DIR}/<cluster>/{TENANTS_SUBDIR} directory under {root}; "
            "pass --tenants-dir or --cluster-dir"
        )
    if len(found) > 1:
        names = ", ".join(p.relative_to(root).as_posix() for p in found)
        raise OperatorError(f"several tenant directories ({names}); pass --cluster-dir")
    return found[0]


def add_target_arguments(parser: argparse.ArgumentParser, default_root: Path) -> None:
    parser.add_argument(
        "--repo-root", "--repo", dest="repo_root", type=Path, default=default_root,
        help="repository root the relative paths resolve against (default: %(default)s)",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--tenants-dir", type=Path,
        help="directory holding the tenants kustomization.yaml and the wiring files",
    )
    where.add_argument(
        "--cluster-dir", type=Path,
        help=f"cluster directory whose {TENANTS_SUBDIR}/ holds the wiring",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="wiring files checked on their own, or a tenants directory",
    )


def resolve_targets(args: argparse.Namespace) -> Targets:
    """Positional paths win; otherwise the flag or the one tree found.

    Every relative path, positional or flag, resolves against --repo-root.
    """
    root = args.repo_root

    def under_root(path: Path) -> Path:
        return path if path.is_absolute() else root / path

    if args.paths:
        if args.tenants_dir is not None or args.cluster_dir is not None:
            raise OperatorError("pass wiring paths or --tenants-dir/--cluster-dir, not both")
        paths = [under_root(p) for p in args.paths]
        directories = [p for p in paths if p.is_dir()]
        files = [p for p in paths if not p.is_dir()]
        if directories and (files or len(directories) > 1):
            raise OperatorError("pass one tenants directory, or wiring files, not a mix")
        if directories:
            return Targets(root, tenants_dir=directories[0])
        missing = [p.as_posix() for p in files if not p.is_file()]
        if missing:
            raise OperatorError(f"no such wiring file: {', '.join(missing)}")
        return Targets(root, files=files)
    if args.tenants_dir is not None:
        tenants = under_root(args.tenants_dir)
    elif args.cluster_dir is not None:
        tenants = under_root(args.cluster_dir) / TENANTS_SUBDIR
    else:
        tenants = discover_tenants_dir(root)
    if not tenants.is_dir():
        raise OperatorError(f"{tenants} is not a directory")
    return Targets(root, tenants_dir=tenants)


def normalized(name: str) -> str:
    """kustomize reads `./acme.yaml` and `acme.yaml` as the same resource."""
    return posixpath.normpath(name)


def kustomization_resources(path: Path, where: str) -> set[str]:
    """A kustomization's `resources`, normalized; OperatorError, labelled `where`,
    when it cannot be read or names something other than a file."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorError(f"{where} unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise OperatorError(f"{where} is not a mapping, so it lists no resources")
    resources = doc.get("resources")
    if resources is None:
        return set()
    if not isinstance(resources, list):
        raise OperatorError(f"{where} `resources` is not a list, so it names no file")
    bad = [r for r in resources if not isinstance(r, str) or not r.strip()]
    if bad:
        raise OperatorError(f"{where} `resources` holds entries that are not file names: {bad!r}")
    return {normalized(r) for r in resources}


def listed_resources(tenants_dir: Path) -> set[str]:
    """The aggregator's `resources`, or OperatorError: it is what the subject scan is."""
    listed = kustomization_resources(tenants_dir / AGGREGATOR, AGGREGATOR)
    outside = sorted(
        r for r in listed
        if posixpath.isabs(r) or r in {".", "..", AGGREGATOR} or r.startswith("../")
    )
    if outside:
        raise OperatorError(
            f"{AGGREGATOR} `resources` lists paths outside the tenant directory or "
            f"referring to itself: {outside!r}"
        )
    return listed


def load_mappings(path: Path, where: str) -> list[dict]:
    """The mapping documents of one input, through gate_common.load_docs;
    OperatorError, labelled `where`, when it cannot be read or parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{where}: unreadable: {exc}") from exc
    try:
        return load_docs(text)
    except OperatorError as exc:
        raise OperatorError(
            f"{where}: unparseable YAML: {exc.__cause__ or exc}\n    Fix: Flux would "
            "reject the file — make it parse, or move it out of the tree."
        ) from exc

