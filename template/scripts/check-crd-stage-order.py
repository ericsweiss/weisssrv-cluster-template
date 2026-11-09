#!/usr/bin/env python3
"""Assert every custom resource's CRD is installed by a stage its own stage waits on.

Reads the Flux stage Kustomizations in a cluster directory and the manifests each
stage applies; exits 0 clean, 1 on a finding, 2 on an error. Contract: docs/SCRIPTS.md.
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
    from gate_common import (
        OperatorError,
        api_group,
        doc_key,
        load_docs,
        load_exempt,
        report_exemptions,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

FLUX_KUSTOMIZE_API = "kustomize.toolkit.fluxcd.io/"
KUSTOMIZE_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
SUFFIXES = (".yaml", ".yml")

# Groups the API server serves itself, plus kustomize's own config group; their
# kinds never wait on a CRD.
BUILTIN_GROUPS = frozenset({
    "", "admissionregistration.k8s.io", "apiextensions.k8s.io",
    "apiregistration.k8s.io", "apps", "authentication.k8s.io",
    "authorization.k8s.io", "autoscaling", "batch", "certificates.k8s.io",
    "coordination.k8s.io", "discovery.k8s.io", "events.k8s.io",
    "flowcontrol.apiserver.k8s.io", "internal.apiserver.k8s.io",
    "networking.k8s.io", "node.k8s.io", "policy", "rbac.authorization.k8s.io",
    "resource.k8s.io", "scheduling.k8s.io", "storage.k8s.io",
    "storagemigration.k8s.io", "kustomize.config.k8s.io",
})

# Flux serves these before any stage can reconcile at all.
FLUX_GROUP_SUFFIX = ".toolkit.fluxcd.io"

# Chart name -> the API groups its CRDs serve with the chart's default values,
# or with an OPT_IN_CHARTS toggle set; --crd-source overrides or adds a chart.
KNOWN_CHART_GROUPS: dict[str, tuple[str, ...]] = {
    "cert-manager": ("cert-manager.io", "acme.cert-manager.io"),
    "cloudnative-pg": ("postgresql.cnpg.io",),
    "external-secrets": ("external-secrets.io", "generators.external-secrets.io"),
    "kube-prometheus-stack": ("monitoring.coreos.com",),
    "metallb": ("metallb.io",),
    "plugin-barman-cloud": ("barmancloud.cnpg.io",),
    "prometheus-operator-crds": ("monitoring.coreos.com",),
    "tailscale-operator": ("tailscale.com",),
    "traefik": ("traefik.io", "hub.traefik.io"),
    "traefik-crds": ("traefik.io",),
    "vertical-pod-autoscaler": ("autoscaling.k8s.io",),
    "vpa": ("autoscaling.k8s.io",),
}

# Values toggles charts read to turn their CRDs off. Any one set false wins.
_VALUES_OFF = (("installCRDs",), ("crds", "enabled"), ("crds", "create"), ("crds", "install"))

# Built-in charts whose CRDs install only when one of these values is true.
OPT_IN_CHARTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "cert-manager": (("crds", "enabled"), ("installCRDs",)),
}


def _dig(mapping: object, path: tuple[str, ...]) -> object:
    for key in path:
        if not isinstance(mapping, dict):
            return None
        mapping = mapping.get(key)
    return mapping


class CrdSources:
    """Which API groups each HelmRelease installs: map first, heuristic second."""

    def __init__(self, charts=None, releases=None, external=None) -> None:
        self.charts: dict[str, list[str]] = {**{k: list(v) for k, v in KNOWN_CHART_GROUPS.items()}, **(charts or {})}
        # A mapped chart replaces the built-in entry, its opt-in rule included.
        self.opt_in = {k: v for k, v in OPT_IN_CHARTS.items() if k not in (charts or {})}
        # namespace/HelmRelease/name -> groups, authoritative over every toggle.
        self.releases: dict[str, list[str]] = dict(releases or {})
        # group -> why it is installed outside this tree.
        self.external: dict[str, str] = dict(external or {})

    def groups(self, release: dict) -> list[str]:
        """The groups `release` installs, or [] when its CRDs are turned off."""
        key = doc_key(release)
        if key in self.releases:
            return self.releases[key]
        spec = release.get("spec") or {}
        if _dig(spec, ("install", "crds")) == "Skip":
            return []
        values = spec.get("values") or {}
        # spec.chart.spec.chart, or the source name a chartRef points at.
        chart = str(_dig(spec, ("chart", "spec", "chart")) or _dig(spec, ("chartRef", "name")) or "")
        if chart in self.opt_in:
            on = any(_dig(values, path) is True for path in self.opt_in[chart])
            return self.charts.get(chart, []) if on else []
        if any(_dig(values, path) is False for path in _VALUES_OFF):
            return []
        return self.charts.get(chart, [])


def load_crd_sources(path: str | None) -> CrdSources:
    """The `--crd-source` YAML chart/release -> API group map."""
    charts: dict[str, list[str]] = {}
    releases: dict[str, list[str]] = {}
    external: dict[str, str] = {}
    if path:
        try:
            docs = load_docs(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError(f"--crd-source {path}: {exc}") from exc
        if len(docs) != 1:
            raise OperatorError(f"--crd-source {path}: expected one mapping document")
        doc = docs[0]
        unknown = set(doc) - {"charts", "releases", "external_groups"}
        if unknown:
            raise OperatorError(f"--crd-source {path}: unknown keys {sorted(unknown)}")
        for section, target in (("charts", charts), ("releases", releases)):
            value = doc.get(section) or {}
            if not isinstance(value, dict):
                raise OperatorError(f"--crd-source {path}: {section} must be a mapping")
            for key, groups in value.items():
                if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                    raise OperatorError(f"--crd-source {path}: {section}[{key}] must be a list of groups")
                if section == "releases" and str(key).count("/") != 2:
                    raise OperatorError(f"--crd-source {path}: releases key {key!r} is not namespace/HelmRelease/name")
                target[str(key)] = list(groups)
        value = doc.get("external_groups") or {}
        if not isinstance(value, dict):
            raise OperatorError(f"--crd-source {path}: external_groups must be a mapping")
        for group, reason in value.items():
            if not str(reason or "").strip():
                raise OperatorError(f"--crd-source {path}: external_groups[{group}] has no reason")
            external[str(group)] = str(reason).strip()
    return CrdSources(charts, releases, external)


class Stage:
    """One Flux Kustomization: its name, path, dependencies and documents."""

    def __init__(self, name: str, path: str, depends_on: list[str], spec: dict) -> None:
        self.name = name
        self.path = path
        self.depends_on = depends_on
        self.wait = spec.get("wait") is True
        self.health_checks = [h for h in spec.get("healthChecks") or [] if isinstance(h, dict)]
        self.docs: list[dict] = []

    def waits_for(self, release: dict) -> bool:
        """Whether this stage turns Ready only after `release` is Ready."""
        if self.wait:
            return True
        meta = release.get("metadata") or {}
        return any(
            h.get("kind") == "HelmRelease" and h.get("name") == meta.get("name")
            and (h.get("namespace") or meta.get("namespace")) == meta.get("namespace")
            for h in self.health_checks
        )


def _read(path: Path) -> list[dict]:
    try:
        return load_docs(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{path}: {exc}") from exc
    except OperatorError as exc:
        raise OperatorError(f"{path}: {exc}") from exc


def load_stages(cluster_dir: Path) -> dict[str, Stage]:
    """Every Flux Kustomization in the top-level files of `cluster_dir`."""
    stages: dict[str, Stage] = {}
    for path in sorted({*cluster_dir.glob("*.yaml"), *cluster_dir.glob("*.yml")}):
        for doc in _read(path):
            if doc.get("kind") != "Kustomization":
                continue
            if not str(doc.get("apiVersion", "")).startswith(FLUX_KUSTOMIZE_API):
                continue
            name = str((doc.get("metadata") or {}).get("name") or "")
            spec = doc.get("spec") or {}
            deps = [
                str(d.get("name")) for d in spec.get("dependsOn") or []
                if isinstance(d, dict) and d.get("name")
            ]
            if not name or name in stages:
                raise OperatorError(f"{path}: a Kustomization with no name or a duplicate name {name!r}")
            stages[name] = Stage(name, str(spec.get("path") or "."), deps, spec)
    return stages


def walk(target: Path, seen: set[Path], others: frozenset[Path] = frozenset()) -> list[dict]:
    """The documents kustomize would read from `target`, following resources.

    A directory with no kustomization file is read whole, less other stages' paths.
    Remote resources and generators are skipped: they render no CRD or CR here.
    """
    target = target.resolve()
    if target in seen:
        return []
    seen.add(target)
    if target.is_file():
        return _read(target)
    if not target.is_dir():
        raise OperatorError(f"{target} does not exist")
    kfile = next((target / n for n in KUSTOMIZE_FILES if (target / n).is_file()), None)
    if kfile is None:
        docs: list[dict] = []
        for path in sorted(target.rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES and not (
                set(path.resolve().parents) & others
            ):
                docs.extend(_read(path))
        return docs
    spec = (_read(kfile) or [{}])[0]
    docs = []
    for section in ("resources", "components"):
        for entry in spec.get(section) or []:
            entry = str(entry)
            if "://" in entry or entry.startswith(("github.com/", "git@")):
                continue
            docs.extend(walk(target / entry, seen, others))
    return docs


def ancestors(stages: dict[str, Stage]) -> dict[str, set[str]]:
    """Each stage -> every stage it transitively depends on."""
    out: dict[str, set[str]] = {}

    def visit(name: str, trail: tuple[str, ...]) -> set[str]:
        if name in out:
            return out[name]
        if name in trail:
            raise OperatorError("dependsOn cycle: " + " -> ".join((*trail, name)))
        found: set[str] = set()
        for dep in stages[name].depends_on:
            if dep not in stages:
                continue
            found.add(dep)
            found |= visit(dep, (*trail, name))
        out[name] = found
        return found

    for name in stages:
        visit(name, ())
    return out


MANIFEST = "manifest"
Installers = dict[str, dict[str, list[tuple[str, bool]]]]


def _crd_group(doc: dict) -> str:
    """A CustomResourceDefinition's spec.group, or "" when it names none."""
    spec = doc.get("spec")
    return str((spec.get("group") if isinstance(spec, dict) else None) or "")


def installers(stages: dict[str, Stage], sources: CrdSources) -> Installers:
    """group -> stage -> [(how it installs the CRDs, whether the stage waits for it)]."""
    out: Installers = {}
    for stage in stages.values():
        for doc in stage.docs:
            kind = doc.get("kind")
            if kind == "CustomResourceDefinition":
                group = _crd_group(doc)
                if group:
                    out.setdefault(group, {}).setdefault(stage.name, []).append((MANIFEST, True))
            elif kind == "HelmRelease":
                waited = stage.waits_for(doc)
                for group in sources.groups(doc):
                    out.setdefault(group, {}).setdefault(stage.name, []).append((doc_key(doc), waited))
    return out


def describe(installed: dict[str, list[tuple[str, bool]]]) -> str:
    """`stage (how[, unwaited])` for each stage installing a group."""
    parts = []
    for stage, entries in sorted(installed.items()):
        hows = sorted({how if waited else f"{how}, not waited for" for how, waited in entries})
        parts.append(f"{stage} ({'; '.join(hows)})")
    return "; ".join(parts)


def findings(
    stages: dict[str, Stage], sources: CrdSources, exempt: dict[str, str], bootstrap: set[str]
) -> tuple[list[str], int, set[str], dict[str, set[str]]]:
    """-> (violations, CRs checked, exemptions exercised, unresolved group -> stages)."""
    order = ancestors(stages)
    where = installers(stages, sources)
    out: list[str] = []
    checked = 0
    used: set[str] = set()
    unresolved: dict[str, set[str]] = {}
    for stage in sorted(stages.values(), key=lambda s: s.name):
        for doc in stage.docs:
            group = api_group(doc)
            if (
                group in BUILTIN_GROUPS or group in bootstrap
                or group.endswith(FLUX_GROUP_SUFFIX)
                or doc.get("kind") == "CustomResourceDefinition"
            ):
                continue
            if group not in where:
                if group not in sources.external:
                    unresolved.setdefault(group, set()).add(stage.name)
                continue
            checked += 1
            installed = where[group]
            # An earlier stage counts only once Ready implies the CRDs exist.
            if any(
                waited for s, entries in installed.items() if s in order[stage.name]
                for _how, waited in entries
            ):
                continue
            # kustomize-controller applies a stage's CRD manifests before its CRs.
            if any(how == MANIFEST for how, _ in installed.get(stage.name, [])):
                continue
            key = doc_key(doc)
            if key in exempt:
                used.add(key)
                continue
            out.append(
                f"  {stage.name}: {key} ({group}) — its CRD is installed only by "
                f"{describe(installed)}; {stage.name} depends on no stage that waits for "
                f"it. On an empty cluster the dry-run rejects the whole stage."
            )
    return out, checked, used, unresolved


def bootstrap_groups(cluster_dir: Path) -> set[str]:
    """Groups of the CRD manifests the cluster root applies before any stage."""
    groups: set[str] = set()
    for path in sorted(cluster_dir.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        for doc in _read(path):
            if doc.get("kind") == "CustomResourceDefinition":
                group = _crd_group(doc)
                if group:
                    groups.add(group)
    return groups


def stage_dir(root: Path, path: str) -> Path:
    """A stage's spec.path resolved against `root`; Flux reads it repo-relative."""
    return (root / posixpath.normpath(path).lstrip("/")).resolve()


def run(args) -> int:
    root = Path(args.repo_root)
    cluster_dir = root / args.cluster_dir
    if not cluster_dir.is_dir():
        raise OperatorError(f"--cluster-dir {cluster_dir} does not exist")
    sources = load_crd_sources(args.crd_source)
    exempt = load_exempt(args.exempt, args.exempt_file)
    stages = load_stages(cluster_dir)
    if not stages:
        raise OperatorError(f"no Flux Kustomization in the top-level files of {cluster_dir}")
    paths = {name: stage_dir(root, s.path) for name, s in stages.items()}
    for stage in stages.values():
        others = frozenset(p for n, p in paths.items() if n != stage.name and p != paths[stage.name])
        stage.docs = walk(paths[stage.name], set(), others)
    found, checked, used, unresolved = findings(stages, sources, exempt, bootstrap_groups(cluster_dir))

    if unresolved:
        label = "WARNING" if args.allow_unresolved else "ERROR"
        print(f"{label}: {len(unresolved)} custom resource group(s) unchecked, with no CRD "
              "source in this tree:", file=sys.stderr)
        for group, names in sorted(unresolved.items()):
            print(f"  {group}: used by {', '.join(sorted(names))}", file=sys.stderr)
        print("Map each under charts or releases, or declare it in external_groups with "
              "a reason.", file=sys.stderr)
    if found:
        print("Custom resources applied before their CRD on a fresh bootstrap:", file=sys.stderr)
        print("\n".join(found), file=sys.stderr)
        return 1
    if unresolved and not args.allow_unresolved:
        return 1
    if not checked:
        raise OperatorError(
            f"0 custom resources across {len(stages)} stage(s) resolved to a CRD source "
            f"— the gate checked nothing; check --cluster-dir and --crd-source"
        )
    report_exemptions(exempt, used)
    print(f"CRD stage order OK — {checked} custom resource(s) across {len(stages)} stage(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every custom resource's CRD is installed by an earlier Flux stage."
    )
    parser.add_argument("--repo-root", default=".", help="root that spec.path resolves against")
    parser.add_argument(
        "--cluster-dir", required=True,
        help="directory holding the stage Kustomizations, relative to --repo-root",
    )
    parser.add_argument("--crd-source", metavar="PATH", help="YAML chart/release -> API group map")
    parser.add_argument(
        "--exempt", action="append", default=[], metavar="NAMESPACE/KIND/NAME=REASON",
        help="a custom resource exempt from the rule, with its reason; repeatable",
    )
    parser.add_argument(
        "--exempt-file", action="append", default=[], metavar="PATH",
        help="YAML mapping of namespace/Kind/name to reason; repeatable",
    )
    parser.add_argument(
        "--allow-unresolved", action="store_true",
        help="warn instead of failing when a custom resource's group has no CRD source",
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
