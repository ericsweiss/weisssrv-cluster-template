#!/usr/bin/env python3
"""Assert the image-GC alert fires at the kubelet's image-gc high watermark.

Expr, prose and inhibitors must match it plus any --alert-offset, up to --alert-cap.
Exit 0 clean, 1 drifted, 2 uninspectable subject. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        OperatorError,
        parse_alertmanager_matchers,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside check-image-gc-threshold.py "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

import yaml  # noqa: E402  (gate_common exits 2 when PyYAML is absent)

REPO = Path(__file__).resolve().parent.parent

K3S_VARS = Path("ansible/inventories/prod/group_vars/k3s.yml")
KUBELET_ARGS_VAR = "k3s_kubelet_args"
RULES = Path("kubernetes/infrastructure/observability/rules")
ALERTMANAGER = Path(
    "kubernetes/infrastructure/observability/kube-prometheus-stack/alertmanager-config.yaml"
)
ALERT = "KubeletImageGCIneffective"
MIN_PROSE_MENTIONS = 2
YAML_SUFFIXES = (".yml", ".yaml")

# `image-gc-high-threshold=70` in a kubelet args entry.
_KUBELET_ARG = re.compile(r"^image-gc-high-threshold=(\d+)$")
# The trailing comparison of an alert expr, e.g. `... > 70`.
_EXPR_THRESHOLD = re.compile(r">=?\s*(\d+(?:\.\d+)?)\s*$")
# A percentage attached to an `image-gc-high-threshold` mention, in the three
# spellings rule prose uses: `70% image-gc-high-threshold`,
# `image-gc-high-threshold (70%)` and `image-gc-high-threshold of 70%`.
_PROSE_THRESHOLD = re.compile(
    r"(?:(\d+)\s*%\s*image-gc-high-threshold"
    r"|image-gc-high-threshold\s*\(\s*(\d+)\s*%\s*\)"
    r"|image-gc-high-threshold\s+of\s+(\d+)\s*%)"
)
# An equality label selector in an expr, e.g. `mountpoint="/"`.
_SELECTOR_PIN = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=(?![=~])\s*"([^"]*)"')
_SELECTOR = re.compile(r"\{([^{}]*)\}")
# The ExternalSecret body is a Go template; a value stands in so it parses.
_GO_TEMPLATE = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def _load_all(path: Path) -> list:
    try:
        with path.open(encoding="utf-8") as fh:
            return [doc for doc in yaml.safe_load_all(fh) if doc is not None]
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{path}: unreadable: {exc}") from exc
    except yaml.YAMLError as exc:
        raise OperatorError(f"{path}: unparseable YAML: {exc}") from exc


def kubelet_threshold(k3s_vars: Path) -> int:
    """The image-gc-high-threshold percentage the kubelet is started with."""
    docs = _load_all(k3s_vars)
    doc = docs[0] if docs and isinstance(docs[0], dict) else {}
    args = doc.get(KUBELET_ARGS_VAR)
    if not args or not isinstance(args, list):
        raise OperatorError(f"{k3s_vars}: no {KUBELET_ARGS_VAR} entries")
    for arg in args:
        match = _KUBELET_ARG.match(str(arg).strip())
        if match:
            return int(match.group(1))
    raise OperatorError(f"{k3s_vars}: {KUBELET_ARGS_VAR} sets no image-gc-high-threshold")


def rule_files(paths: list[Path]) -> list[Path]:
    """Every YAML file named, with directories expanded recursively."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in YAML_SUFFIXES)
        elif path.is_file():
            files.append(path)
        else:
            raise OperatorError(f"{path}: no such rules file or directory")
    if not files:
        raise OperatorError(f"no rule files under {', '.join(map(str, paths))}")
    return files


class Rule(NamedTuple):
    """One alerting rule definition and the labels its alerts are known to carry."""

    name: str
    path: Path
    expr: str
    labels: dict[str, str]


def _alert_nodes(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("alert"), str) and "expr" in node:
            out.append(node)
        for value in node.values():
            _alert_nodes(value, out)
    elif isinstance(node, list):
        for value in node:
            _alert_nodes(value, out)


def _known_labels(name: str, expr: str, static) -> dict[str, str]:
    """alertname, every label the expr pins to one value, then the rule's static labels."""
    pins: dict[str, set[str]] = {}
    for selector in _SELECTOR.findall(expr):
        for label, value in _SELECTOR_PIN.findall(selector):
            pins.setdefault(label, set()).add(value)
    labels = {label: values.pop() for label, values in pins.items() if len(values) == 1}
    if isinstance(static, dict):
        labels.update({str(k): str(v) for k, v in static.items()})
    labels["alertname"] = name
    return labels


def alert_index(files: list[Path]) -> dict[str, list[Rule]]:
    """alert name -> every definition of it, in any shape of manifest."""
    index: dict[str, list[Rule]] = {}
    for path in files:
        nodes: list[dict] = []
        for doc in _load_all(path):
            _alert_nodes(doc, nodes)
        for node in nodes:
            name, expr = node["alert"], str(node["expr"])
            labels = _known_labels(name, expr, node.get("labels"))
            index.setdefault(name, []).append(Rule(name, path, expr, labels))
    return index


def expr_threshold(path: Path, alert: str, expr: str) -> float:
    match = _EXPR_THRESHOLD.search(expr.strip())
    if not match:
        raise OperatorError(f"{path}: {alert} expr has no trailing `> <number>` comparison")
    return float(match.group(1))


def _inhibit_rules(node, out: list) -> None:
    """Every inhibit_rules list: top level, nested in Helm values, or in an embedded config."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "inhibit_rules" and isinstance(value, list):
                out += value
            elif isinstance(value, str) and str(key).endswith("alertmanager.yaml"):
                try:
                    _inhibit_rules(yaml.safe_load(_GO_TEMPLATE.sub('"x"', value)), out)
                except yaml.YAMLError as exc:
                    raise OperatorError(f"embedded {key} is unparseable: {exc}") from exc
            else:
                _inhibit_rules(value, out)
    elif isinstance(node, list):
        for value in node:
            _inhibit_rules(value, out)


def _matchers(rule: dict, side: str, where: str) -> list[tuple[str, str, str]]:
    """(label, operator, value) for every matcher on one side of an inhibit rule."""
    found: list[tuple[str, str, str]] = []
    for entry in rule.get(f"{side}_matchers") or []:
        try:
            found += parse_alertmanager_matchers(entry)
        except ValueError as exc:
            raise OperatorError(f"{where}: {side} {exc}") from exc
    for key, op in ((f"{side}_match", "="), (f"{side}_match_re", "=~")):
        legacy = rule.get(key) or {}
        if not isinstance(legacy, dict):
            raise OperatorError(f"{where}: {key} is not a label map")
        found += [(str(label), op, str(value)) for label, value in legacy.items()]
    return found


def _satisfies(labels: dict[str, str], matchers, where: str) -> bool:
    """Alertmanager semantics: every matcher holds, a missing label reads as empty."""
    for label, op, value in matchers:
        have = labels.get(label, "")
        if op in ("=", "!="):
            hit = have == value
        else:
            try:
                hit = re.fullmatch(value, have) is not None
            except re.error as exc:
                raise OperatorError(f"{where}: {label} regex {value!r}: {exc}") from exc
        if hit != op.startswith("="):
            return False
    return True


def _pairable(source: Rule, target: Rule, equal) -> bool:
    """False only when a static label in `equal` differs, so the two can never pair."""
    for label in equal or []:
        mine, theirs = source.labels.get(str(label)), target.labels.get(str(label))
        if mine is not None and theirs is not None and mine != theirs:
            return False
    return True


def inhibitors(
    config: Path, alert: str, index: dict[str, list[Rule]]
) -> list[tuple[Rule, list[Rule]]]:
    """(source rule, target definitions of `alert` it mutes) for every inhibit rule."""
    rules: list = []
    for doc in _load_all(config):
        try:
            _inhibit_rules(doc, rules)
        except OperatorError as exc:
            raise OperatorError(f"{config}: {exc}") from exc
    if not rules:
        raise OperatorError(f"{config}: declares no inhibit_rules")
    everything = [rule for defs in index.values() for rule in defs]
    found: list[tuple[Rule, list[Rule]]] = []
    for number, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        where = f"{config}: inhibit_rules[{number}]"
        targets = [
            t for t in index[alert] if _satisfies(t.labels, _matchers(rule, "target", where), where)
        ]
        if not targets:
            continue
        sources = _matchers(rule, "source", where)
        for label, op, value in sources:
            if label != "alertname" or op not in ("=", "=~"):
                continue
            if op == "=" and value not in index:
                raise OperatorError(
                    f"{value} inhibits {alert} in {config} but no rule file searched "
                    "defines it: pass the file that does with --rules"
                )
            if op == "=~" and not any(_satisfies({"alertname": n}, [(label, op, value)], where)
                                      for n in index):
                raise OperatorError(
                    f"{config}: an inhibitor of {alert} matches alertname=~{value!r}, "
                    "which names no alert in the rules searched"
                )
        for source in everything:
            if not _satisfies(source.labels, sources, where):
                continue
            muted = [
                t for t in targets
                if t is not source and _pairable(source, t, rule.get("equal"))
            ]
            if muted:
                found.append((source, muted))
    return found


def prose_thresholds(files: list[Path], minimum: int) -> list[int]:
    """Every percentage spelled next to an `image-gc-high-threshold` mention."""
    found: list[int] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError(f"{path}: unreadable: {exc}") from exc
        found += [int(a or b or c) for a, b, c in _PROSE_THRESHOLD.findall(text)]
    if len(found) < minimum:
        raise OperatorError(
            f"{', '.join(map(str, files))}: found {len(found)} percentage(s) next to an "
            f"image-gc-high-threshold mention, expected at least {minimum}"
        )
    return found


def check(
    root: Path,
    rules: tuple[Path, ...] = (RULES,),
    offset: int = 0,
    cap: int | None = None,
    min_prose: int = MIN_PROSE_MENTIONS,
    unthresholded: frozenset[str] = frozenset(),
) -> list[str]:
    watermark = kubelet_threshold(root / K3S_VARS)
    expected = watermark + offset if cap is None else min(watermark + offset, cap)
    index = alert_index(rule_files([root / p for p in rules]))
    if ALERT not in index:
        raise OperatorError(f"no {ALERT} alert in {', '.join(map(str, rules))}")
    problems = []
    homes = sorted({r.path for r in index[ALERT]})
    thresholds = [expr_threshold(r.path, ALERT, r.expr) for r in index[ALERT]]
    for value in sorted(set(thresholds)):
        if value != expected:
            problems.append(
                f"{ALERT} fires above {value:g}% but the kubelet prunes from "
                f"{watermark}% ({K3S_VARS}), so the alert should fire above {expected}%: "
                "it would fire while image GC still works, or stay silent while GC "
                f"cannot reach its low watermark. Update the expr in {homes[0]}."
            )
    for value in sorted(set(prose_thresholds(homes, min_prose))):
        if value != watermark:
            problems.append(
                f"{', '.join(map(str, homes))} names {value}% as the "
                f"image-gc-high-threshold, but {K3S_VARS} sets {watermark}%."
            )
    for source, targets in inhibitors(root / ALERTMANAGER, ALERT, index):
        if source.name in unthresholded:
            continue
        try:
            muting = expr_threshold(source.path, source.name, source.expr)
        except OperatorError as exc:
            raise OperatorError(
                f"{source.path}: {source.name} inhibits {ALERT} in {ALERTMANAGER} but its "
                f"expr has no trailing `> <number>` comparison, so whether it mutes {ALERT} "
                f"for good is unknown: rewrite it, or pass --unthresholded-inhibitor {source.name}"
            ) from exc
        lowest = min(expr_threshold(t.path, ALERT, t.expr) for t in targets)
        if muting <= lowest:
            problems.append(
                f"{source.name} fires above {muting:g}% and inhibits {ALERT} at "
                f"{lowest:g}%, so {ALERT} can never notify: the inhibitor is "
                f"already firing whenever it is. Raise {source.name}'s threshold, "
                f"lower image-gc-high-threshold in {K3S_VARS}, or drop the "
                f"pair in {ALERTMANAGER}."
            )
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="The image-GC alert must track the kubelet image-gc high watermark.",
    )
    parser.add_argument("--repo-root", default=REPO, type=Path)
    parser.add_argument(
        "--rules",
        action="append",
        type=Path,
        default=None,
        help=f"rule file or directory to search; repeatable (default {RULES})",
    )
    parser.add_argument(
        "--alert-offset", type=int, default=0,
        help="points the alert threshold sits above the watermark",
    )
    parser.add_argument("--alert-cap", type=int, default=None, help="ceiling on the alert threshold")
    parser.add_argument(
        "--min-prose-mentions", type=int, default=MIN_PROSE_MENTIONS,
        help="watermark mentions the alert's file must carry",
    )
    parser.add_argument(
        "--unthresholded-inhibitor", action="append", default=[], metavar="ALERT",
        help="an inhibitor whose expr has no percentage threshold to compare; repeatable",
    )
    args = parser.parse_args(argv)
    try:
        problems = check(
            args.repo_root,
            rules=tuple(args.rules or [RULES]),
            offset=args.alert_offset,
            cap=args.alert_cap,
            min_prose=args.min_prose_mentions,
            unthresholded=frozenset(args.unthresholded_inhibitor),
        )
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(
            f"ERROR: {ALERT} has drifted from the kubelet image-gc high watermark:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"{ALERT} tracks the kubelet image-gc-high-threshold in {K3S_VARS}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
