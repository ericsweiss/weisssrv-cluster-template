#!/usr/bin/env python3
"""Extract alert rules and the Alertmanager config into promtool/amtool inputs.

Reads the kube-prometheus-stack HelmRelease, --rules-dir and the Alertmanager
ExternalSecret template, and writes one lintable file. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_OBS = Path("kubernetes/infrastructure/observability/kube-prometheus-stack")
DEFAULT_RELEASE = DEFAULT_OBS / "release.yaml"
DEFAULT_AM_CONFIG = DEFAULT_OBS / "alertmanager-config.yaml"
DEFAULT_RULES_DIR = Path("kubernetes/infrastructure/observability/rules")

DUMMY_URL = "https://dummy.example/00000000-0000-0000-0000-000000000000"
DUMMY_SCALAR = "dummy"
_PLACEHOLDER_RE = re.compile(r"\{\{-?\s*\.(\w+)\s*(?:\|\s*quote\s*)?-?\}\}")


def _load(path: Path) -> dict:
    """Parse a single-document YAML file, always returning a mapping — an empty
    or explicitly-null document yields {} so callers can chain `.get()`."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    return doc if isinstance(doc, dict) else {}


def dummy_for(name: str, overrides: dict[str, str] | None = None) -> str:
    if overrides and name in overrides:
        return overrides[name]
    return DUMMY_URL if name.lower().endswith("url") else DUMMY_SCALAR


def _release_groups(release: Path) -> list:
    """Rule groups declared inline in the HelmRelease values."""
    values = ((_load(release).get("spec") or {}).get("values") or {})
    rules_map = values.get("additionalPrometheusRulesMap") or {}
    groups: list = []
    for entry in rules_map.values():
        groups.extend((entry or {}).get("groups") or [])
    return groups


def _prometheusrule_groups(rules_dir: Path) -> list:
    """Rule groups declared as standalone PrometheusRule manifests."""
    groups: list = []
    if not rules_dir.is_dir():
        return groups
    for path in sorted({*rules_dir.rglob("*.yaml"), *rules_dir.rglob("*.yml")}):
        with path.open() as f:
            for doc in yaml.safe_load_all(f):
                if isinstance(doc, dict) and doc.get("kind") == "PrometheusRule":
                    groups.extend((doc.get("spec") or {}).get("groups") or [])
    return groups


def _rules_dirs(value: Path | str | Iterable[Path | str]) -> list[Path]:
    """Normalise one directory or several into a list, so a caller passing a
    single Path keeps working."""
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value]


def extract_rules(
    out: Path,
    release: Path = DEFAULT_RELEASE,
    rules_dir: Path | str | Iterable[Path | str] = DEFAULT_RULES_DIR,
    require_release_rules: bool = False,
) -> int:
    dirs = _rules_dirs(rules_dir)
    listed = ", ".join(f"{d}/" for d in dirs)
    release_groups = _release_groups(release)
    if require_release_rules and not release_groups:
        print(
            f"ERROR: no rule groups in {release} (additionalPrometheusRulesMap); "
            "drop --require-release-rules if this consumer keeps every rule "
            f"under {listed}",
            file=sys.stderr,
        )
        return 1
    groups = list(release_groups)
    for rules_dir_path in dirs:
        groups.extend(_prometheusrule_groups(rules_dir_path))
    if not groups:
        print(
            f"ERROR: no rule groups found in {release} "
            f"(additionalPrometheusRulesMap) or "
            + ", ".join(f"{d}/**/*.y*ml" for d in dirs),
            file=sys.stderr,
        )
        return 1
    out.write_text(yaml.safe_dump({"groups": groups}, default_flow_style=False, sort_keys=False))
    print(f"Wrote {len(groups)} rule group(s) to {out}")
    return 0


def render_placeholders(template: str, overrides: dict[str, str] | None = None) -> str:
    return _PLACEHOLDER_RE.sub(
        lambda m: '"' + dummy_for(m.group(1), overrides) + '"', template
    )


def extract_alertmanager(
    out: Path,
    am_config: Path = DEFAULT_AM_CONFIG,
    overrides: dict[str, str] | None = None,
) -> int:
    doc = _load(am_config)
    # `(x or {})` at every hop: a key present with an explicit null value makes
    # .get(k, {}) return None, not the default.
    template = (
        (((doc.get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {}
    ).get("alertmanager.yaml")
    if not template:
        print(f"ERROR: alertmanager.yaml template not found in {am_config}", file=sys.stderr)
        return 1
    rendered = render_placeholders(template, overrides)
    if "{{" in rendered:
        print("ERROR: unrendered template expression remains after substitution", file=sys.stderr)
        print(rendered, file=sys.stderr)
        return 1
    out.write_text(rendered)
    print(f"Wrote rendered Alertmanager config to {out}")
    return 0


def _parse_dummy(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--dummy expects NAME=VALUE, got {item!r}")
        out[key] = value
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=Path(argv[0]).name,
        description=__doc__,
        epilog=(
            "rules is the union of additionalPrometheusRulesMap and every "
            "--rules-dir (repeatable); "
            "a group defined twice is caught by promtool's duplicate-name check.\n"
            "--dummy (repeatable) overrides the value substituted for an ESO "
            "`{{ .name | quote }}` placeholder. An unset name renders as a dummy "
            "https URL when it ends in `url`, and as the literal `dummy` otherwise."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("subcommand", choices=("rules", "alertmanager"))
    parser.add_argument("out", type=Path)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument(
        "--rules-dir", type=Path, action="append", default=None, metavar="DIR",
        help="PrometheusRule manifest tree; repeat for every tree a consumer "
             "ships, e.g. the shared rules dir plus the per-app one",
    )
    parser.add_argument("--am-config", type=Path, default=DEFAULT_AM_CONFIG)
    parser.add_argument("--dummy", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument(
        "--require-release-rules", action="store_true",
        help="fail when the HelmRelease declares no inline rule groups, for a "
             "consumer whose rules live in additionalPrometheusRulesMap",
    )
    parser.add_argument(
        "--require-rules-dir", action="store_true",
        help="fail when the standalone-PrometheusRule tree is absent, for a "
             "consumer that keeps its rules there",
    )
    args = parser.parse_args(argv[1:])
    if args.subcommand == "rules":
        rules_dirs = args.rules_dir or [DEFAULT_RULES_DIR]
        # An absent tree the operator named, or declared it has, drops every
        # standalone PrometheusRule from the lint while promtool still passes.
        named = args.rules_dir is not None or args.require_rules_dir
        missing = [d for d in rules_dirs if not d.is_dir()]
        if named and missing:
            print(
                "ERROR: rules directory "
                + ", ".join(str(d) for d in missing)
                + " does not exist; pass --rules-dir to point at the "
                "PrometheusRule manifests, or drop --require-rules-dir if "
                f"every rule is inline in {args.release}",
                file=sys.stderr,
            )
            return 2
        return extract_rules(args.out, args.release, rules_dirs,
                             args.require_release_rules)
    return extract_alertmanager(args.out, args.am_config, _parse_dummy(args.dummy))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
