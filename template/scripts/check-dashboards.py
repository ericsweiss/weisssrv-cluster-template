#!/usr/bin/env python3
"""Gate Grafana dashboard JSON before it reaches Grafana.

Each directory must parse, carry no import placeholders, use allowlisted
datasource uids and match its configMapGenerator. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, List, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# Grafana's built-in datasource uids. They are not provisioned by any chart, so
# a consumer allowlist never has to name them.
BUILTIN_UIDS = {"-- Grafana --", "-- Mixed --", "-- Dashboard --", "grafana"}
DEFAULT_ALLOWED = ("prometheus", "loki")
IMPORT_PLACEHOLDER = re.compile(r"\$\{?DS_[A-Z0-9_]+\}?")
TEMPLATE_VAR = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")


class OperatorError(Exception):
    """Input the gate cannot read or parse: a wrong path, not a finding."""


def _datasource_refs(node: Any) -> Iterable[Tuple[str, str]]:
    """Every `datasource` reference in the document, at any depth.

    Kinds: `uid` a dict uid, `name` the legacy string form, `nouid` a dict with
    no uid. A `null` datasource is skipped; a row panel carries one.
    """
    if isinstance(node, dict):
        source = node.get("datasource")
        if isinstance(source, dict):
            if isinstance(source.get("uid"), str):
                yield ("uid", source["uid"])
            else:
                yield ("nouid", "")
        elif isinstance(source, str):
            yield ("name", source)
        for value in node.values():
            yield from _datasource_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _datasource_refs(item)


def _declared_vars(doc: Any) -> set:
    """Variable names the dashboard declares in `templating.list`."""
    templating = doc.get("templating") if isinstance(doc, dict) else None
    items = templating.get("list") if isinstance(templating, dict) else None
    return {
        str(item["name"]) for item in items or []
        if isinstance(item, dict) and item.get("name")
    }


def _registered(kustomization: Path) -> tuple:
    """({filename: generator entry}, file-level generatorOptions)."""
    try:
        doc = yaml.safe_load(kustomization.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorError("%s: %s" % (kustomization.as_posix(), exc)) from exc
    out = {}
    for entry in doc.get("configMapGenerator") or []:
        if not isinstance(entry, dict):
            continue
        for name in entry.get("files") or []:
            # kustomize allows `key=path`: the key names the ConfigMap entry and
            # the path is the source file, which may sit in a subdirectory.
            source = os.path.normpath(str(name).split("=")[-1])
            out.setdefault(source, entry)
    options = doc.get("generatorOptions")
    return out, options if isinstance(options, dict) else {}


def _effective(entry: dict, generator_options: dict, slot: str) -> dict:
    """kustomize merges generatorOptions into every entry; the entry wins."""
    shared = generator_options.get(slot)
    own = (entry.get("options") or {}).get(slot)
    merged = dict(shared if isinstance(shared, dict) else {})
    merged.update(own if isinstance(own, dict) else {})
    return merged


def check_dir(directory: Path, allowed: set, label: str, label_value: str,
              annotation: str,
              check_registration: bool = True) -> Tuple[List[str], int]:
    findings: List[str] = []
    on_disk = {path.relative_to(directory).as_posix()
               for path in directory.glob("*.json")}
    kustomization = directory / "kustomization.yaml"
    registered: dict = {}
    generator_options: dict = {}
    unshipped = check_registration and not kustomization.is_file()
    if check_registration and not unshipped:
        registered, generator_options = _registered(kustomization)

    # A dashboard registered from a subdirectory is content-checked too, or it
    # ships to Grafana unvalidated.
    dashboards = sorted(
        directory / name for name in
        on_disk | {n for n in registered if (directory / n).is_file()}
    )
    for path in dashboards:
        where = path.as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError("%s: %s" % (where, exc)) from exc
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            findings.append("%s: not valid JSON (%s)" % (where, exc))
            continue
        if isinstance(doc, dict) and "__inputs" in doc:
            findings.append(
                "%s: carries `__inputs` — this is a grafana.com export-for-sharing "
                "file, not a provisioned dashboard; re-export it or strip the block"
                % where
            )
        placeholder = IMPORT_PLACEHOLDER.search(raw)
        if placeholder:
            findings.append(
                "%s: import placeholder %s survived — every panel will show "
                "'Datasource not found'" % (where, placeholder.group(0))
            )
        declared = _declared_vars(doc)
        for kind, value in sorted(set(_datasource_refs(doc))):
            if kind == "name":
                if value in BUILTIN_UIDS or TEMPLATE_VAR.match(value):
                    continue
                findings.append(
                    "%s: legacy string datasource %r — provisioned Grafana matches "
                    "by uid, so this panel resolves to the default datasource or "
                    "none" % (where, value)
                )
            elif kind == "nouid":
                findings.append(
                    "%s: a datasource with no `uid` — it resolves to whichever "
                    "datasource is default" % where
                )
            elif TEMPLATE_VAR.match(value):
                # A surviving `${DS_*}` placeholder is already reported above.
                if (not IMPORT_PLACEHOLDER.fullmatch(value)
                        and value.strip("${}") not in declared):
                    findings.append(
                        "%s: datasource variable %s is declared in no "
                        "`templating.list` entry — declared: %s"
                        % (where, value, ", ".join(sorted(declared)) or "none")
                    )
            elif value not in allowed and value not in BUILTIN_UIDS:
                findings.append(
                    "%s: datasource uid %r is not provisioned — allowed: %s"
                    % (where, value, ", ".join(sorted(allowed)))
                )

    if not check_registration:
        return findings, len(dashboards)

    if unshipped:
        findings.append("%s: no kustomization.yaml, so nothing ships these files"
                        % directory.as_posix())
        return findings, len(dashboards)

    for name in sorted(on_disk - set(registered)):
        findings.append(
            "%s/%s: on disk but in no configMapGenerator `files:` entry — it never "
            "reaches the cluster" % (directory.as_posix(), name)
        )
    for name in sorted(registered):
        if not (directory / name).is_file():
            findings.append(
                "%s: kustomization.yaml registers %s, which does not exist"
                % (directory.as_posix(), name)
            )
            continue
        entry = registered[name]
        labels = _effective(entry, generator_options, "labels")
        annotations = _effective(entry, generator_options, "annotations")
        if str(labels.get(label, "")) != label_value:
            findings.append(
                "%s/%s: neither the generator entry nor `generatorOptions` sets "
                "`%s: \"%s\"` — the Grafana sidecar never picks the ConfigMap up"
                % (directory.as_posix(), name, label, label_value)
            )
        if not annotations.get(annotation):
            findings.append(
                "%s/%s: neither the generator entry nor `generatorOptions` sets "
                "the `%s` annotation — the dashboard lands in the sidecar's "
                "default folder" % (directory.as_posix(), name, annotation)
            )
    return findings, len(dashboards)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "dirs", nargs="*", type=Path,
        help="dashboard directories (default: every kubernetes/**/dashboards)",
    )
    parser.add_argument(
        "--allowed-uid", action="append", default=None,
        help="datasource uid the cluster provisions; repeatable "
             "(default: prometheus loki)",
    )
    parser.add_argument(
        "--skip-registration", action="store_true",
        help="do not require a kustomization.yaml, for a directory that holds "
             "dashboards as source rather than shipping them",
    )
    parser.add_argument("--dashboard-label", default="grafana_dashboard")
    parser.add_argument(
        "--dashboard-label-value", default="1",
        help="label value the sidecar matches (default: 1)",
    )
    parser.add_argument("--folder-annotation", default="grafana_folder")
    args = parser.parse_args(argv)

    if args.dirs:
        missing = [d for d in args.dirs if not d.is_dir()]
        if missing:
            print("no such dashboard directory: %s"
                  % ", ".join(d.as_posix() for d in missing), file=sys.stderr)
            return 2
        directories = list(args.dirs)
    else:
        # glob can also match a FILE (or a dangling symlink) named dashboards:
        # a layout quirk, not the operator error the --dirs branch reports.
        directories = sorted(
            p for p in Path("kubernetes").glob("**/dashboards") if p.is_dir()
        )
    if not directories:
        print("no dashboard directory found — nothing was checked", file=sys.stderr)
        return 2

    allowed = set(args.allowed_uid or DEFAULT_ALLOWED)
    findings: List[str] = []
    scanned = 0
    for directory in directories:
        try:
            found, count = check_dir(
                directory, allowed, args.dashboard_label,
                args.dashboard_label_value, args.folder_annotation,
                check_registration=not args.skip_registration,
            )
            findings.extend(found)
            scanned += count
        except OperatorError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2

    if not scanned:
        print("scanned 0 dashboards in %s — the gate inspected nothing"
              % ", ".join(d.as_posix() for d in directories), file=sys.stderr)
        return 2

    if findings:
        for finding in findings:
            print("FAIL %s" % finding, file=sys.stderr)
        return 1
    print("%d dashboard(s) in %d director(ies) OK" % (scanned, len(directories)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
