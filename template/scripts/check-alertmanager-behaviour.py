#!/usr/bin/env python3
"""Assert what the Alertmanager config DOES, not just that it parses.

Resolves each --config route case with amtool (required on PATH) and checks
every inhibit rule; exits 0 clean, 1 finding, 2 error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

MATCHER_RE = re.compile(r'^\s*(\w+)\s*(=~|!~|!=|=)\s*"?(.*?)"?\s*$')


DEFAULT_ESCALATION_SUFFIXES = ("Prolonged", "Critical")


class Config:
    """The consumer data from --config. A value, not module state."""

    def __init__(self, route_cases, synthetic, upstream, escalation_suffixes,
                 escalation_pairs, escalation_exceptions, escalation_equal,
                 matcher_parity_labels=()):
        self.route_cases = route_cases
        self.synthetic_route_alerts = synthetic
        self.upstream_alerts = upstream
        self.escalation_suffixes = escalation_suffixes
        self.escalation_pairs = escalation_pairs
        self.escalation_exceptions = escalation_exceptions
        self.escalation_equal = escalation_equal
        # Matcher labels whose regex must appear verbatim in the rule's own expr.
        # Opt-in: a consumer whose matchers do not mirror an expr declares none.
        self.matcher_parity_labels = tuple(matcher_parity_labels or ())


def _escalation_pairs(raw, path, key) -> list[tuple[str, str, tuple[str, ...]]]:
    """Parse an escalation-pair list into (critical, warning, equal) triples."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: `{key}` must be a list")
    pairs = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("critical") or not entry.get("warning"):
            raise ValueError(f"{path}: `{key}` entry needs critical + warning: {entry!r}")
        extra = entry.get("equal") or []
        if not isinstance(extra, list):
            raise ValueError(f"{path}: `{key}` entry `equal` must be a list: {entry!r}")
        pairs.append(
            (str(entry["critical"]), str(entry["warning"]), tuple(str(x) for x in extra))
        )
    return pairs


def load_config(path) -> Config:
    """Read and validate a --config file. Raises ValueError on a bad file."""
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    raw_cases = doc.get("route_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{path}: `route_cases` must be a non-empty list")
    cases = []
    for case in raw_cases:
        if not isinstance(case, dict) or not case.get("receiver") or not case.get("labels"):
            raise ValueError(f"{path}: route case needs receiver + labels: {case!r}")
        cases.append((str(case["receiver"]), [str(label) for label in case["labels"]]))
    suffixes = doc.get("escalation_suffixes")
    if suffixes is None:
        suffixes = list(DEFAULT_ESCALATION_SUFFIXES)
    elif not isinstance(suffixes, list):
        raise ValueError(f"{path}: `escalation_suffixes` must be a list")
    equal = doc.get("escalation_equal") or []
    if not isinstance(equal, list):
        raise ValueError(f"{path}: `escalation_equal` must be a list")
    parity_labels = doc.get("matcher_parity_labels") or []
    if not isinstance(parity_labels, list):
        raise ValueError(f"{path}: `matcher_parity_labels` must be a list")
    return Config(
        cases,
        {str(a) for a in doc.get("synthetic_route_alerts") or []},
        {str(a) for a in doc.get("upstream_alerts") or []},
        [str(x) for x in suffixes],
        _escalation_pairs(doc.get("escalation_pairs"), path, "escalation_pairs"),
        _escalation_pairs(doc.get("escalation_exceptions"), path, "escalation_exceptions"),
        [str(x) for x in equal],
        [str(x) for x in parity_labels],
    )


class ExtractionError(RuntimeError):
    """The consumer's extractor failed — an operator error (exit 2)."""


def _extract(work: Path, extract_script: Path, repo_root: Path,
             extract_args: list[str] | None = None) -> tuple[Path, Path]:
    """Run the consumer's extractor from the consumer's repo root.

    The extractor resolves its manifest defaults against the process cwd, and
    `extract_args` carries the flags a consumer's rule layout needs.
    """
    rules, am = work / "rules.yaml", work / "alertmanager.yaml"
    for args in (["rules", str(rules)], ["alertmanager", str(am)]):
        run = subprocess.run(
            [sys.executable, str(extract_script), *args, *(extract_args or [])],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if run.returncode:
            raise ExtractionError(f"extraction failed:\n{run.stdout}{run.stderr}")
    return am, rules


def check_routes(am_config: Path, route_cases) -> list[str]:
    problems = []
    for want, labels in route_cases:
        run = subprocess.run(
            ["amtool", "config", "routes", "test", f"--config.file={am_config}", *labels],
            capture_output=True,
            text=True,
        )
        # amtool failing is an operator error: without this the error text's
        # first token reads as the resolved receiver and every case is a misroute.
        if run.returncode:
            raise ExtractionError(
                f"amtool config routes test failed for [{' '.join(labels)}]:\n"
                f"{run.stdout}{run.stderr}"
            )
        got = " ".join((run.stdout + run.stderr).split())
        # amtool can print several matching receivers in tree order, so only
        # the first token is the resolution, compared exactly. A prefix test
        # would pass `critical-page` for an expected `critical`.
        tokens = got.split()
        first = tokens[0] if tokens else ""
        if first != want:
            problems.append(f"[{' '.join(labels)}] expected receiver {want!r}, resolved {got!r}")
    return problems


def _parse_matchers(
    matchers, index: int, side: str, problems: list[str]
) -> list[tuple[str, str, str]]:
    """Every matcher as (label, op, value), in the order written.

    A label can carry several matchers and Alertmanager ANDs them, so a
    positive regex narrowed by a negative one must keep both.
    """
    out = []
    for raw in matchers or []:
        m = MATCHER_RE.match(raw)
        if not m:
            problems.append(f"rule {index}: unparseable {side} matcher {raw!r}")
            continue
        out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _matchers_for(parsed, label: str) -> list[tuple[str, str]]:
    """Every (op, value) the matcher set carries for `label`, in order."""
    return [(op, value) for name, op, value in parsed if name == label]


def _exact_alertnames(parsed) -> tuple[list[str], str | None]:
    """Return (alertnames the matcher set pins, why it could not be validated).

    Exactly one of the two is meaningful. A regex that is not a plain
    alternation returns a reason, never an empty list. See docs/SCRIPTS.md.
    """
    names: list[str] | None = None
    for op, val in _matchers_for(parsed, "alertname"):
        if op == "=":
            members = [val]
        elif op == "=~":
            if not re.fullmatch(r"[A-Za-z0-9_|]+", val or ""):
                return [], _not_an_alternation("=~", val)
            members = (val or "").split("|")
        else:
            continue
        # Matchers are ANDed, so each positive one narrows the pinned set.
        names = members if names is None else [n for n in names if n in members]
    return names or [], None


def _negated_alertnames(parsed) -> tuple[list[str], str | None]:
    """Return (alertnames the matcher set exempts, why they could not be checked).

    The negated mirror of _exact_alertnames: `alertname!=` and `alertname!~`
    name alerts to exclude, and a name that matches nothing exempts nothing.
    """
    names: list[str] = []
    for op, val in _matchers_for(parsed, "alertname"):
        if op == "!=":
            members = [val]
        elif op == "!~":
            if not re.fullmatch(r"[A-Za-z0-9_|]+", val or ""):
                return [], _not_an_alternation("!~", val)
            members = (val or "").split("|")
        else:
            continue
        # Each negated matcher excludes more, so the exemptions union.
        names += [n for n in members if n not in names]
    return names, None


def _covered_alertnames(parsed) -> tuple[list[str], str | None]:
    """Return (alertnames the matcher set can match, why it could not be read).

    Matchers are ANDed, so a negated member subtracts from the positive set, and
    an unreadable regex on either side leaves the covered set unknowable.
    """
    positive, unreadable = _exact_alertnames(parsed)
    if unreadable:
        return [], unreadable
    negated, unreadable = _negated_alertnames(parsed)
    if unreadable:
        return [], unreadable
    return [n for n in positive if n not in negated], None


def _not_an_alternation(op: str, val) -> str:
    return (
        f'alertname regex "{val}" ({op}) is not a plain alternation of names, '
        f"so its members cannot be checked against the rules corpus. Write it "
        f'as "AlertOne|AlertTwo" (Alertmanager anchors matchers itself, so '
        f"^…$ is redundant), or split the rule."
    )


def _load_extracted(path: Path, what: str) -> dict:
    """Parse one extracted file, or raise ExtractionError (exit 2).

    The extractor copies the alertmanager.yaml block scalar out unparsed, so a
    typo inside it first surfaces here. See docs/SCRIPTS.md.
    """
    try:
        # The handle, not the text, so PyYAML's mark names the file.
        with path.open() as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ExtractionError(f"extracted {what} ({path}) is not parseable YAML: {exc}") from exc
    if doc is None:
        raise ExtractionError(f"extracted {what} ({path}) is empty")
    if not isinstance(doc, dict):
        raise ExtractionError(
            f"extracted {what} ({path}) is a {type(doc).__name__}, not a YAML mapping"
        )
    return doc


def _known_alertnames(rules_doc: dict) -> set[str]:
    return {
        r["alert"]
        for g in rules_doc.get("groups") or []
        for r in g.get("rules") or []
        if "alert" in r
    }


def _alert_severities(rules_doc: dict) -> dict[str, set[str]]:
    """alertname -> every severity label the corpus declares for it."""
    out: dict[str, set[str]] = {}
    for group in rules_doc.get("groups") or []:
        for rule in group.get("rules") or []:
            name = rule.get("alert")
            if not name:
                continue
            severity = ((rule.get("labels") or {}).get("severity"))
            out.setdefault(str(name), set()).add(str(severity) if severity else "")
    return out


def derive_escalation_pairs(rules_doc: dict, suffixes) -> list[tuple[str, str]]:
    """(critical, warning) pairs the rules corpus itself declares.

    A warning alert with a same-stem critical sibling escalates into it, so both
    fire while the failure persists.
    """
    severities = _alert_severities(rules_doc)
    pairs = []
    for name in sorted(severities):
        if "warning" not in severities[name]:
            continue
        for suffix in suffixes:
            escalation = name + suffix
            if "critical" in severities.get(escalation, set()):
                pairs.append((escalation, name))
    return pairs


def _inhibit_index(am_doc: dict) -> list[tuple[int, set[str], set[str], set[str]]]:
    """Each inhibit rule as (position, source names, target names, equal labels).

    The names are what each side can match, so a rule that negates an alertname
    never certifies a pair naming it.
    """
    index = []
    for i, rule in enumerate(am_doc.get("inhibit_rules") or []):
        discard: list[str] = []
        src = _parse_matchers(rule.get("source_matchers"), i, "source", discard)
        tgt = _parse_matchers(rule.get("target_matchers"), i, "target", discard)
        index.append((
            i,
            set(_covered_alertnames(src)[0]),
            set(_covered_alertnames(tgt)[0]),
            {str(label) for label in rule.get("equal") or []},
        ))
    return index


def check_escalation_inhibits(am_doc: dict, rules_doc: dict, config: Config) -> list[str]:
    """Every warning/critical escalation pair must be inhibited.

    Without the inhibit rule both alerts deliver for as long as the failure
    lasts, so a sustained failure pages twice.
    """
    exceptions = {(c, w) for c, w, _ in config.escalation_exceptions}
    pairs: dict[tuple[str, str], tuple[str, ...]] = {}
    for critical, warning in derive_escalation_pairs(rules_doc, config.escalation_suffixes):
        pairs[(critical, warning)] = tuple(config.escalation_equal)
    for critical, warning, extra in config.escalation_pairs:
        pairs[(critical, warning)] = tuple(extra) or tuple(config.escalation_equal)

    index = _inhibit_index(am_doc)
    problems = []
    for (critical, warning), want_equal in sorted(pairs.items()):
        if (critical, warning) in exceptions:
            continue
        matching = [
            entry for entry in index if critical in entry[1] and warning in entry[2]
        ]
        if not matching:
            problems.append(
                f"escalation pair {critical} -> {warning} has no inhibit rule, so a "
                f"sustained failure pages twice. Add an inhibit_rule whose "
                f"source_matchers name {critical} and target_matchers name {warning}, "
                f"or declare the pair under escalation_exceptions."
            )
            continue
        # One rule must carry EVERY required label. Two rules each missing a
        # different one both silence unrelated instances, so their equal sets
        # never combine.
        want = set(want_equal)
        if any(want <= entry[3] for entry in matching):
            continue
        best = min(matching, key=lambda entry: (len(want - entry[3]), entry[0]))
        missing = sorted(want - best[3])
        problems.append(
            f"escalation pair {critical} -> {warning} is inhibited without "
            f"equal:{missing}, so one instance's critical silences every "
            f"other instance's warning. Rule {best[0]} comes closest with "
            f"equal:{sorted(best[3])}; every label must sit on that one rule."
        )
    return problems


def _alert_exprs(rules_doc: dict) -> dict[str, list[str]]:
    """alertname -> every expr the corpus declares for it, whitespace collapsed."""
    out: dict[str, list[str]] = {}
    for group in rules_doc.get("groups") or []:
        for rule in group.get("rules") or []:
            name = rule.get("alert")
            if not name:
                continue
            out.setdefault(str(name), []).append(" ".join(str(rule.get("expr") or "").split()))
    return out


def _expr_selector_values(expr: str, label: str) -> set[str]:
    """Every quoted value `expr` selects `label` on, whatever the operator."""
    pattern = r'\b%s\s*(?:=~|!~|!=|=)\s*"([^"]*)"' % re.escape(label)
    return set(re.findall(pattern, expr))


def check_matcher_value_parity(am_doc: dict, rules_doc: dict, labels) -> list[str]:
    """A matcher regex that mirrors a rule's own selector must mirror it exactly.

    An inhibit rule reproducing an alert's exclusion set drifts the moment the
    rule's expr gains or loses a member, and nothing else compares the two.
    """
    if not labels:
        return []
    exprs = _alert_exprs(rules_doc)
    problems: list[str] = []
    for i, rule in enumerate(am_doc.get("inhibit_rules") or []):
        discard: list[str] = []
        for side, key in (("source", "source_matchers"), ("target", "target_matchers")):
            parsed = _parse_matchers(rule.get(key), i, side, discard)
            names = [n for n in _covered_alertnames(parsed)[0] if n in exprs]
            if not names:
                continue
            for label in labels:
                for op, value in _matchers_for(parsed, label):
                    if op not in ("=~", "!~") or not value:
                        continue
                    collapsed = " ".join(str(value).split())
                    for name in names:
                        declared = set()
                        for expr in exprs[name]:
                            declared |= _expr_selector_values(expr, label)
                        if collapsed not in declared:
                            problems.append(
                                f"rule {i}: {side} {label}{op}\"{value}\" is not a selector "
                                f"the expr of {name} declares ({sorted(declared) or 'none'}), "
                                f"and the matcher is written to mirror it — one of the two has "
                                f"drifted. Rule expr: {exprs[name][0]!r}"
                            )
    return problems


def check_route_case_alertnames(known: set[str], config: Config) -> list[str]:
    """Every route-case alertname must still name a real rule.

    amtool resolves a route from labels alone, so a renamed rule stays green.
    """
    problems = []
    for _want, labels in config.route_cases:
        for label in labels:
            key, _, value = label.partition("=")
            if key != "alertname" or value in config.synthetic_route_alerts:
                continue
            if value not in known and value not in config.upstream_alerts:
                problems.append(
                    f"route case alertname {value!r} matches no extracted rule and is "
                    f"not in upstream_alerts. The route case still passes (amtool "
                    f"resolves labels, not rules), so it is asserting nothing — "
                    f"renamed, deleted, or a typo?"
                )
    return problems


def check_inhibits(am_doc: dict, known: set[str], upstream: set[str]) -> list[str]:
    inhibits = am_doc.get("inhibit_rules") or []
    if not inhibits:
        return ["no inhibit_rules found in the Alertmanager config"]

    problems: list[str] = []
    for i, rule in enumerate(inhibits):
        src = _parse_matchers(rule.get("source_matchers"), i, "source", problems)
        tgt = _parse_matchers(rule.get("target_matchers"), i, "target", problems)
        if not src or not tgt:
            problems.append(f"rule {i}: both source_matchers and target_matchers are required")
            continue
        for label in rule.get("equal") or []:
            shared = sorted(
                {v for op, v in _matchers_for(src, label) if op == "="}
                & {v for op, v in _matchers_for(tgt, label) if op == "="}
            )
            if shared:
                problems.append(
                    f"rule {i}: equal:[{label}] is redundant — both matcher sets already "
                    f'pin it to "{shared[0]}", so the pair dedups nothing'
                )
        # Every alertname a matcher pins must resolve, to one of ours or to a
        # declared upstream alert. Checked per alternation member so a stale
        # name cannot hide behind a surviving sibling.
        defined = known | upstream
        for side, (names, unvalidatable) in (
            ("source", _exact_alertnames(src)),
            ("target", _exact_alertnames(tgt)),
        ):
            if unvalidatable:
                problems.append(f"rule {i}: {side} {unvalidatable}")
                continue
            dead = [n for n in names if n not in defined]
            if dead:
                problems.append(
                    f"rule {i}: {side} alertname(s) {dead} match no extracted rule and "
                    f"are not in upstream_alerts — renamed, deleted, or a typo? "
                    f"An unmatched name in an alternation is an inert matcher, "
                    f"not an error at runtime."
                )
        # A negated matcher is an exemption list, so a name that resolves to
        # nothing exempts nothing and the alert it meant to spare stays silenced.
        for side, (names, unvalidatable) in (
            ("source", _negated_alertnames(src)),
            ("target", _negated_alertnames(tgt)),
        ):
            if unvalidatable:
                problems.append(f"rule {i}: {side} {unvalidatable}")
                continue
            dead = [n for n in names if n not in defined]
            if dead:
                problems.append(
                    f"rule {i}: {side} exempts alertname(s) {dead}, which match no "
                    f"extracted rule and are not in upstream_alerts — renamed, "
                    f"deleted, or a typo? The exemption spares nothing."
                )
    return problems

_BY_RE = re.compile(r"\bby\s*\(([^)]*)\)")
_NEGATIVE_SELECTOR_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:!~|!=)")


def _members(group: str) -> set[str]:
    return {part.strip() for part in group.split(",") if part.strip()}


def aggregates_away(expr: str, label: str) -> bool:
    """True when every `by (...)` clause in `expr` drops `label`.

    Only a visible `by (...)` counts, so an expression this cannot read is
    never reported.
    """
    groups = _BY_RE.findall(str(expr))
    return bool(groups) and not any(label in _members(g) for g in groups)


def _matches_labels(rule: dict, parsed) -> bool:
    """Whether a rule's own labels satisfy every non-alertname matcher."""
    labels = rule.get("labels") or {}
    for key, op, want in parsed:
        if key == "alertname":
            continue
        got = str(labels.get(key, ""))
        if op == "=" and got != want:
            return False
        if op == "!=" and got == want:
            return False
        if op in ("=~", "!~"):
            try:
                hit = re.fullmatch(want or "", got) is not None
            except re.error:
                return False
            if hit != (op == "=~"):
                return False
    return True


def check_equal_label_scope(am_doc: dict, rules_doc: dict) -> list[str]:
    """An alert that drops an inhibit's `equal:` label is suppressed always.

    Alertmanager matches an absent `equal:` label to an absent one, so such an
    alert pairs with any source instance and never notifies.
    """
    problems = []
    for index, rule in enumerate(am_doc.get("inhibit_rules") or []):
        equal = [str(label) for label in rule.get("equal") or []]
        # alertname is always present, so an equal set naming it always scopes.
        if not equal or "alertname" in equal:
            continue
        discard: list[str] = []
        tgt = _parse_matchers(rule.get("target_matchers"), index, "target", discard)
        if not tgt or _exact_alertnames(tgt)[0]:
            # A target pinned to exact alertnames is a deliberate pair, not a scope.
            continue
        exempt, unreadable = _negated_alertnames(tgt)
        # An unreadable exemption regex leaves the spared set unknown.
        if unreadable:
            continue
        for group in rules_doc.get("groups") or []:
            for alert in group.get("rules") or []:
                name = alert.get("alert") if isinstance(alert, dict) else None
                if not name or name in exempt or not _matches_labels(alert, tgt):
                    continue
                labels = alert.get("labels") or {}
                dropped = [
                    label for label in equal
                    if label not in labels and aggregates_away(alert.get("expr", ""), label)
                ]
                # Suppression is unconditional only when EVERY equal label is
                # absent; one surviving label still scopes the pair, and
                # alertname is always present.
                if dropped and len(dropped) == len(equal):
                    problems.append(
                        f"rule {index}: {name} carries none of equal:{equal} (its "
                        f"expression aggregates them away), and Alertmanager matches "
                        f"an absent label to an absent one — so {name} is suppressed "
                        f"whenever the source fires anywhere. Name it in the target's "
                        f"alertname!~ matcher, or label it explicitly."
                    )
    return problems


def check_inhibit_target_scope(am_doc: dict, rules_doc: dict) -> list[str]:
    """A target scope must not be wider than the source alert's own selector.

    A source that excludes series with a negative selector can never cover them,
    so a target admitting them silences objects nothing is watching.
    """
    exprs = {
        alert["alert"]: str(alert.get("expr", ""))
        for group in rules_doc.get("groups") or []
        for alert in group.get("rules") or []
        if isinstance(alert, dict) and alert.get("alert")
    }
    problems = []
    for index, rule in enumerate(am_doc.get("inhibit_rules") or []):
        discard: list[str] = []
        src = _parse_matchers(rule.get("source_matchers"), index, "source", discard)
        tgt = _parse_matchers(rule.get("target_matchers"), index, "target", discard)
        names = _covered_alertnames(src)[0]
        if len(names) != 1 or names[0] not in exprs:
            continue
        excluded = set(_NEGATIVE_SELECTOR_RE.findall(exprs[names[0]]))
        for label in sorted(excluded):
            ops = {op for op, _v in _matchers_for(tgt, label)}
            if ops & {"=", "=~"} and not ops & {"!=", "!~"}:
                problems.append(
                    f"rule {index}: source {names[0]} excludes some {label} values "
                    f"with a negative selector, but the target scopes {label} "
                    f"positively only — the excluded objects are silenced by a "
                    f"source that can never cover them. Alertmanager regexps have "
                    f"no lookahead, so add a second {label}!~ matcher rather than "
                    f"an inline exclusion."
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert what the Alertmanager config does, not just that it parses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="route cases + alert sets (see docstring)")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--extract-script",
        type=Path,
        help="extract-prometheus-config.py (default: <repo-root>/scripts/)",
    )
    parser.add_argument(
        "--extract-arg", action="append", default=[], metavar="FLAG",
        help="flag passed through to the extractor; repeatable",
    )
    args = parser.parse_args(argv)

    # Resolved ONCE, against the caller's cwd, before anything is validated: the
    # extractor runs with `cwd=repo_root`, so a relative path handed straight
    # through would be re-resolved against the CHILD's cwd and double its prefix.
    repo_root = args.repo_root.resolve()
    extract = (
        args.extract_script or repo_root / "scripts" / "extract-prometheus-config.py"
    ).resolve()
    if not repo_root.is_dir():
        print(f"ERROR: --repo-root {repo_root} is not a directory", file=sys.stderr)
        return 2
    if not extract.is_file():
        print(f"ERROR: {extract} not found (pass --extract-script)", file=sys.stderr)
        return 2
    if shutil.which("amtool") is None:
        print("ERROR: amtool not found on PATH", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        try:
            am_config, rules_file = _extract(
                Path(tmp), extract, repo_root, args.extract_arg
            )
            # Parsed ONCE here so a malformed body is an operator error (exit 2)
            # rather than a traceback from mid-check on exit 1.
            am_doc = _load_extracted(am_config, "Alertmanager config")
            rules_doc = _load_extracted(rules_file, "rules")
            known = _known_alertnames(rules_doc)
            route_problems = check_routes(
                am_config, config.route_cases
            ) + check_route_case_alertnames(known, config)
        except ExtractionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        inhibit_problems = check_inhibits(am_doc, known, config.upstream_alerts)
        inhibit_problems += check_escalation_inhibits(am_doc, rules_doc, config)
        inhibit_problems += check_equal_label_scope(am_doc, rules_doc)
        inhibit_problems += check_inhibit_target_scope(am_doc, rules_doc)
        inhibit_problems += check_matcher_value_parity(
            am_doc, rules_doc, config.matcher_parity_labels
        )
        inhibit_count = len(am_doc.get("inhibit_rules") or [])

    if route_problems:
        print("ERROR: Alertmanager routing does not match the expected receivers:", file=sys.stderr)
        for p in route_problems:
            print(f"  - {p}", file=sys.stderr)
    if inhibit_problems:
        print("ERROR: inhibit rule problems:", file=sys.stderr)
        for p in inhibit_problems:
            print(f"  - {p}", file=sys.stderr)
    if route_problems or inhibit_problems:
        return 1

    print(
        f"Alertmanager behaviour OK: {len(config.route_cases)} route case(s) resolve as "
        f"expected, {inhibit_count} inhibit rule(s) well-formed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
