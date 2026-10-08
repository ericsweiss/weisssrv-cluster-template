#!/usr/bin/env python3
"""Every alert the chart disables needs an in-tree copy at the pinned version.

The copy declares `# Taken from <chart> <version>`, matched against the
inventory's chart pins. Exit 0 clean, 1 drifted, 2 cannot inspect.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
MIRROR_DIR = "tests/prometheus-rules"
RULE_TREE = "kubernetes/infrastructure/observability"
CHART_RELEASE = "kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml"
VARS_FILE = "ansible/inventories/prod/group_vars/all.yml"

_TAKEN = re.compile(r"^#\s*Taken from\s+(?P<chart>\S+)\s+(?P<version>\S+)\s*$", re.M)
_TAKEN_LINE = re.compile(r"#\s*Taken from\s+(?P<chart>\S+)\s+(?P<version>\S+)\s*$")
_ALERT_LINE = re.compile(r"^\s*-\s*alert:\s*(?P<name>\S+)\s*$")
# The chart name as spelled in a marker -> its helm_chart_versions key.
CHART_KEYS = {"kube-prometheus-stack": "kube_prometheus_stack"}
# Both suffixes: a rule file written as `.yml` is rendered just the same.
RULE_GLOBS = ("*.yaml", "*.yml")
# Disabled upstream but replaced by a rule selecting different series, so there
# is no upstream expr to stay in step with. Each entry carries its reason.
NOT_MIRRORED: dict[str, str] = {}


class OperatorError(RuntimeError):
    """A subject the gate cannot inspect — exit 2, never the exit 1 a caller
    reads as a mirror that fell behind its chart."""


def pinned_versions(vars_text: str) -> dict[str, str]:
    """{helm_chart_versions key: version} from the inventory's version block."""
    found = {}
    for key in CHART_KEYS.values():
        match = re.search(rf'^\s*{key}:\s*"?([0-9][^"\s#]*)"?', vars_text, re.M)
        if match:
            found[key] = match.group(1)
    return found


def disabled_alerts(root: Path) -> set[str]:
    """Alert names the chart's `defaultRules.disabled` list turns off."""
    path = root / CHART_RELEASE
    try:
        release = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{CHART_RELEASE} could not be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise OperatorError(f"{CHART_RELEASE} does not parse: {exc}") from exc
    values = (release.get("spec") or {}).get("values") or {}
    disabled = (values.get("defaultRules") or {}).get("disabled") or {}
    if not disabled:
        raise OperatorError(
            f"{CHART_RELEASE} disables no default rule — the gate would pass vacuously"
        )
    return {name for name, off in disabled.items() if off}


def _marker_above(lines: list[str], index: int) -> re.Match | None:
    """The `# Taken from` match in the comment block directly above `index`."""
    for line in reversed(lines[:index]):
        if not line.strip().startswith("#"):
            return None
        match = _TAKEN_LINE.search(line)
        if match:
            return match
    return None


def _version_problem(rel, name: str, match: re.Match, pinned: dict[str, str]) -> str | None:
    chart, version = match.group("chart"), match.group("version")
    key = CHART_KEYS.get(chart)
    if key is None:
        return f"{rel}: {name} is taken from {chart!r}, which is not in CHART_KEYS — add it"
    if key not in pinned:
        return f"{rel}: {VARS_FILE} has no {key} pin to compare {name} against"
    if pinned[key] != version:
        return (
            f"{rel}: {name} taken at {chart} {version} but the pin is {pinned[key]} — "
            "re-take the expr from the live PrometheusRule and update the header"
        )
    return None


def in_tree_problems(root: Path, pinned: dict[str, str]) -> list[str]:
    """Every alert the chart disables has a local copy naming its chart version."""
    disabled = disabled_alerts(root)
    problems = []
    for name, reason in sorted(NOT_MIRRORED.items()):
        if name not in disabled:
            problems.append(
                f"{CHART_RELEASE} no longer disables {name} — drop it from "
                f"NOT_MIRRORED (exempt as: {reason})"
            )
    required = disabled - set(NOT_MIRRORED)
    if not required:
        raise OperatorError(
            f"every alert {CHART_RELEASE} disables is in NOT_MIRRORED — "
            "the in-tree check would pass vacuously"
        )
    tree = root / RULE_TREE
    scanned = sorted({p for glob in RULE_GLOBS for p in tree.rglob(glob)})
    seen: set[str] = set()
    for path in scanned:
        rel = path.relative_to(root)
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = _ALERT_LINE.match(line)
            if not match or match.group("name") not in required:
                continue
            name = match.group("name")
            seen.add(name)
            marker = _marker_above(lines, index)
            if marker is None:
                problems.append(
                    f"{rel}: {name} copies a rule {CHART_RELEASE} disables but carries "
                    "no `# Taken from <chart> <version>` line above it, so a chart "
                    "bump can move the upstream expr unnoticed"
                )
                continue
            problem = _version_problem(rel, name, marker, pinned)
            if problem:
                problems.append(problem)
    where = f"{len(scanned)} {'/'.join(RULE_GLOBS)} file(s) under {RULE_TREE}"
    for name in sorted(required - seen):
        problems.append(
            f"{CHART_RELEASE} disables {name} but none of the {where} defines it — "
            "restore the replacement, stop disabling the upstream rule, or exempt it "
            "in NOT_MIRRORED with its reason"
        )
    return problems


def mirror_problems(root: Path, pinned: dict[str, str]) -> list[str]:
    """Supplementary promtool rule files each name their chart version.

    The directory ships only *.test.yaml until a hand-copied upstream group is
    needed beside them, so an empty result is the normal state.
    """
    problems = []
    for mirror in sorted((root / MIRROR_DIR).glob("*.rules.yaml")):
        rel = mirror.relative_to(root)
        match = _TAKEN.search(mirror.read_text(encoding="utf-8"))
        if not match:
            problems.append(f"{rel}: no `# Taken from <chart> <version>` line")
            continue
        problem = _version_problem(rel, "the mirrored group", match, pinned)
        if problem:
            problems.append(problem)
    return problems


def check(root: Path = REPO) -> list[str]:
    vars_path = root / VARS_FILE
    try:
        vars_text = vars_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{VARS_FILE} could not be read: {exc}") from exc
    pinned = pinned_versions(vars_text)
    return mirror_problems(root, pinned) + in_tree_problems(root, pinned)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO)
    args = parser.parse_args(argv)
    try:
        problems = check(args.repo_root)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Mirrored upstream rules are out of step:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Mirrored upstream rules are in step with their chart pins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
