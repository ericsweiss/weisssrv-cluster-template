"""The Loki ruler's LogQL reaches no other gate, so it is checked here.

These are bare `groups:` files the loki-sc-rules sidecar loads, so promtool
never sees them and kubeconform sees only the generated ConfigMap.
"""

from __future__ import annotations

import codecs
import re

import pytest
import yaml
from conftest import OBSERVABILITY, assert_all_parsed, loki_alert_rules, reported

LOKI_DIR = OBSERVABILITY / "loki"

# A LogQL string literal: double-quoted with Go escapes, or raw in backticks.
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')
_SELECTOR_RE = re.compile(r"\{[^{}]*\}")
_MATCHER_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\s*(?:=~|!~|!=|=)\s*["`]')
_LINE_FILTER_RE = re.compile(r'\|~\s*"((?:[^"\\]|\\.)*)"')
# Everything that may precede the leaf stream selector: aggregations, range
# functions and their arguments.
_HEAD_RE = re.compile(r"^[\w\s(,.]*$")
_CLOSERS = {")": "(", "]": "[", "}": "{"}

REQUIRED_KEYS = ("expr", "for")

needs_corpus = pytest.mark.skipif(
    not LOKI_DIR.is_dir(), reason="no Loki ruler directory in this repository"
)


def mask_strings(expr: str) -> str:
    """`expr` with every string literal's body blanked, offsets preserved.

    Brackets and escapes inside a matcher value are the author's regex, not the
    expression's own structure.
    """
    return _STRING_RE.sub(
        lambda m: m.group(0)[0] + "x" * (len(m.group(0)) - 2) + m.group(0)[-1], expr
    )


def delimiter_problem(masked: str) -> str | None:
    stack: list[str] = []
    for offset, char in enumerate(masked):
        if char in "([{":
            stack.append(char)
        elif char in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[char]:
                return f"unbalanced {char!r} at offset {offset}"
            stack.pop()
    return f"unclosed {''.join(reversed(stack))!r}" if stack else None


def selector_problem(expr: str, masked: str) -> str | None:
    match = _SELECTOR_RE.search(masked)
    if not match:
        return "no stream selector `{...}` — a LogQL query must select a stream"
    body = expr[match.start() + 1 : match.end() - 1]
    if not _MATCHER_RE.search(body):
        return f"stream selector {{{body}}} carries no label matcher"
    head = masked[: match.start()]
    if not _HEAD_RE.match(head):
        return f"{head.strip()!r} precedes the stream selector"
    return None


def escape_problem(masked: str) -> str | None:
    if "\\" in masked:
        return "backslash outside a quoted matcher — regex escapes belong in the string"
    return None


def line_filter_problem(expr: str) -> str | None:
    """`|~` takes a Go double-quoted string, so `\\b` must be written `\\\\b`.

    The single-backslash form unquotes to a backspace and matches nothing.
    """
    for raw in _LINE_FILTER_RE.findall(expr):
        try:
            pattern = codecs.decode(raw, "unicode_escape")
        except (UnicodeDecodeError, ValueError) as exc:
            return f"line filter {raw!r} will not unquote: {exc}"
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"line filter {raw!r} is not a valid regex after unquoting: {exc}"
        if any(ord(char) < 0x20 for char in pattern):
            return (
                f"line filter {raw!r} unquotes to a control character — double the "
                "backslash in the YAML"
            )
    return None


def expr_problems(expr: str) -> list[str]:
    """Every structural fault in one LogQL expression."""
    text = " ".join(str(expr).split())
    masked = mask_strings(text)
    found = [delimiter_problem(masked)]
    if not found[0]:
        found += [selector_problem(text, masked), escape_problem(masked)]
    found.append(line_filter_problem(text))
    return [problem for problem in found if problem]


def ruler_rule_files() -> dict[str, list[dict]]:
    """{file name: its rules} for every ruler document in the Loki directory."""
    files: dict[str, list[dict]] = {}
    for path in sorted(LOKI_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict) or "groups" not in doc:
            continue
        files[reported(path).as_posix()] = [
            rule for group in doc["groups"] or [] for rule in group.get("rules") or []
        ]
    return files


def shape_problems(files: dict[str, list[dict]]) -> list[str]:
    found = []
    for source, rules in files.items():
        for index, rule in enumerate(rules):
            name = rule.get("alert") or f"<unnamed rule {index}>"
            for key in REQUIRED_KEYS:
                if not rule.get(key):
                    found.append(f"{source}: {name} has no {key}")
            if not (rule.get("labels") or {}).get("severity"):
                found.append(f"{source}: {name} has no labels.severity")
            if not (rule.get("annotations") or {}).get("runbook_url"):
                found.append(f"{source}: {name} has no annotations.runbook_url")
            if not rule.get("alert"):
                found.append(f"{source}: rule {index} has no alert name")
    return found


@pytest.fixture(scope="module")
def files() -> dict[str, list[dict]]:
    found = ruler_rule_files()
    assert found, (
        f"no `groups:` document under {reported(LOKI_DIR)} — the ruler files moved "
        "and this gate is examining nothing"
    )
    return found


@needs_corpus
def test_every_named_ruler_file_is_shipped_and_parses():
    """A file the kustomization generates a ConfigMap from but does not ship
    drops its alerts silently."""
    found, unreadable = loki_alert_rules()
    assert_all_parsed(unreadable, "Loki ruler alert")
    assert found, "the Loki ruler corpus holds no alerts — the gate would be vacuous"


@needs_corpus
def test_every_ruler_rule_is_actionable(files):
    problems = shape_problems(files)
    assert not problems, (
        "Loki ruler rules missing the fields an operator needs:\n  "
        + "\n  ".join(problems)
    )


@needs_corpus
def test_every_logql_expression_is_structurally_sound(files):
    problems = [
        f"{source}: {rule.get('alert', '<unnamed>')}: {problem}"
        for source, rules in files.items()
        for rule in rules
        for problem in expr_problems(rule.get("expr", ""))
    ]
    assert not problems, (
        "LogQL the ruler would reject, or would evaluate against nothing:\n  "
        + "\n  ".join(problems)
    )


def test_the_shipped_expressions_exercise_the_checker(files):
    """A checker that found no selector in any real rule would pass everything."""
    exprs = [rule.get("expr", "") for rules in files.values() for rule in rules]
    assert exprs, "no expression reached the checker"
    assert all(_SELECTOR_RE.search(mask_strings(expr)) for expr in exprs)


def test_a_malformed_selector_is_reported():
    """Mutation cases: each arm of the checker must go red on its own fault."""
    assert expr_problems('absent_over_time({job="journal"}[45m])') == []
    assert "unclosed" in expr_problems('absent_over_time({job="journal"}[45m]')[0]
    assert "unbalanced" in expr_problems('absent_over_time({job="journal"}[45m)')[0]
    assert "no label matcher" in expr_problems("absent_over_time({}[45m])")[0]
    assert "no stream selector" in expr_problems("absent_over_time(journal[45m])")[0]
    assert "precedes the stream selector" in expr_problems('up > {job="x"}')[0]


def test_a_regex_escape_outside_a_matcher_is_reported():
    """`\\b` written in the expression rather than inside the filter string is a
    LogQL parse error, not a regex."""
    assert expr_problems('{job="j"} |~ "\\\\bBUDGET\\\\b"') == []
    assert "backslash outside" in expr_problems('{job="j"} |~ \\bBUDGET')[0]


def test_a_single_backslash_line_filter_is_reported():
    """The filter parses but unquotes to a control character, so it matches
    nothing and the alert never fires."""
    problems = expr_problems(r'sum(count_over_time({job="j"} |~ "\bSTOP" [1h]))')
    assert problems and "control character" in problems[0]
