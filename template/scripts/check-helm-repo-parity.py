#!/usr/bin/env python3
"""Hold every chart-repo URL equal to the HelmRepository Flux pulls from.

The HelmRepository CRs are the source of truth for the helm-values release list
and the version registry. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Dict, List

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# The registry loader lives in check-versions.py so the two gates that read the
# same registry cannot disagree about where it is. The hyphenated filename is
# not importable normally, hence the spec loader.
_CV_SRC = Path(__file__).resolve().parent / "check-versions.py"
if not _CV_SRC.is_file():
    print(
        "ERROR: check-versions.py must sit next to this script — "
        "vendor both (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2)
_cv_spec = importlib.util.spec_from_file_location("check_versions", _CV_SRC)
check_versions = importlib.util.module_from_spec(_cv_spec)
_cv_spec.loader.exec_module(check_versions)


def helm_repositories(
    sources_dir: Path,
    unreadable: list | None = None,
    missing_url: list | None = None,
) -> Dict[str, str]:
    """{metadata.name: spec.url} for every HelmRepository under the directory.

    A file that will not parse goes to `unreadable` and a CR with no `spec.url`
    to `missing_url`; dropping either misattributes the failure to a release entry.
    """
    found: Dict[str, str] = {}
    for path in sorted({*sources_dir.rglob("*.yaml"), *sources_dir.rglob("*.yml")}):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            if unreadable is not None:
                unreadable.append((path, exc))
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRepository":
                continue
            name = (doc.get("metadata") or {}).get("name")
            url = (doc.get("spec") or {}).get("url")
            if name and url:
                found[str(name)] = str(url).rstrip("/")
            elif name and missing_url is not None:
                missing_url.append(str(name))
    return found


def _releases(path: Path) -> List[dict]:
    """The release list, or a ValueError naming the file that would not read.

    Named: the caller reports `could not read a repo list`, which without the
    path reads as a drift finding against an unidentified file.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("%s: %s" % (path, exc)) from exc
    if isinstance(doc, dict):
        doc = doc.get("releases") or []
    return [entry for entry in doc or [] if isinstance(entry, dict)]


def check_releases(path: Path, repos: Dict[str, str]) -> "tuple[int, List[str]]":
    """(entries that declared a repo, findings)."""
    findings = []
    compared = 0
    for entry in _releases(path):
        name = entry.get("repo_name")
        url = entry.get("repo_url")
        if not name and not url:
            continue  # the entry resolves its repo from the manifest
        compared += 1
        if name not in repos:
            findings.append(
                "%s: repo_name %r names no HelmRepository — the validator would "
                "render from an index Flux does not use"
                % (entry.get("name", "?"), name)
            )
            continue
        if url and str(url).rstrip("/") != repos[name]:
            findings.append(
                "%s: repo_url %s but HelmRepository/%s pulls from %s"
                % (entry.get("name", "?"), url, name, repos[name])
            )
    return compared, findings


def check_registry(path: Path, repos: Dict[str, str]) -> "tuple[int, List[str]]":
    """(services that declared a helm_repo, findings)."""
    known = set(repos.values())
    findings = []
    compared = 0
    for service in check_versions.read_registry(path).get("services") or []:
        if not isinstance(service, dict):
            continue
        url = service.get("helm_repo")
        if not url:
            continue
        compared += 1
        if str(url).rstrip("/") not in known:
            findings.append(
                "%s: helm_repo %s matches no HelmRepository url — the version "
                "checker queries an index the cluster does not pull from"
                % (service.get("name", "?"), url)
            )
    return compared, findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sources-dir", type=Path,
        default=Path("kubernetes/infrastructure/sources"),
    )
    parser.add_argument(
        "--releases", type=Path, default=Path("scripts/helm-values-releases.yaml")
    )
    parser.add_argument(
        "--registry", type=Path, default=Path("scripts/version-registry.py")
    )
    parser.add_argument(
        "--allow-empty", action="store_true",
        help="a consumer that resolves every helm repo from its manifests",
    )
    args = parser.parse_args(argv)

    if not args.sources_dir.is_dir():
        print("no such sources directory: %s" % args.sources_dir, file=sys.stderr)
        return 2
    unreadable: List = []
    missing_url: List[str] = []
    repos = helm_repositories(
        args.sources_dir, unreadable=unreadable, missing_url=missing_url
    )
    if unreadable:
        for path, exc in unreadable:
            print("%s: %s" % (path, exc), file=sys.stderr)
        print("    Fix: Flux would reject the file — make it parse, or move it "
              "out of the sources directory.", file=sys.stderr)
        return 2
    if missing_url:
        for name in missing_url:
            print("FAIL HelmRepository/%s declares no spec.url" % name, file=sys.stderr)
        return 1
    if not repos:
        print("no HelmRepository under %s — the gate inspected nothing"
              % args.sources_dir, file=sys.stderr)
        return 2

    findings: List[str] = []
    inspected = 0
    compared = 0
    try:
        if args.releases.is_file():
            inspected += 1
            count, found = check_releases(args.releases, repos)
            compared += count
            findings.extend(found)
        if args.registry.is_file():
            inspected += 1
            count, found = check_registry(args.registry, repos)
            compared += count
            findings.extend(found)
    except (ValueError, yaml.YAMLError) as exc:
        print("could not read a repo list: %s" % exc, file=sys.stderr)
        return 2

    if not inspected:
        print("neither %s nor %s exists — nothing was compared against the %d "
              "HelmRepository CR(s)" % (args.releases, args.registry, len(repos)),
              file=sys.stderr)
        return 2

    if not compared and not args.allow_empty:
        print("neither %s nor %s declares a helm repo, so nothing was compared "
              "against the %d HelmRepository CR(s); pass --allow-empty if this "
              "consumer resolves every repo from its manifests"
              % (args.releases, args.registry, len(repos)), file=sys.stderr)
        return 2

    if findings:
        for finding in findings:
            print("FAIL %s" % finding, file=sys.stderr)
        return 1
    print("helm repo URLs agree (%d comparison(s) against %d HelmRepository "
          "CR(s))" % (compared, len(repos)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
