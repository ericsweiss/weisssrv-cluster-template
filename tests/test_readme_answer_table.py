"""README.md § The answers is where an operator picks answers, and nothing held it
to copier.yml: a question could go unlisted, or keep a Default cell the question's
expression no longer produces.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml
from jinja2 import meta

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
CONFIG = yaml.safe_load((REPO_ROOT / "copier.yml").read_text(encoding="utf-8"))
QUESTIONS = {
    name: question
    for name, question in CONFIG.items()
    if not name.startswith("_") and isinstance(question, dict) and "type" in question
}
# `when: false` entries are shared machinery: never prompted, never recorded, so
# the operator-facing table does not list them.
ASKED = {name: q for name, q in QUESTIONS.items() if q.get("when") is not False}

HEADING = "## The answers"
# A cell deferring to the schema rather than restating a value, for the two
# answers whose defaults are bumped as a pair.
DEFERRAL = "in `copier.yml`"

_BACKTICKED = re.compile(r"`([^`]+)`")
_ENV = jinja2.Environment()


def _cells(line: str) -> list[str]:
    r"""Row cells. `\|` is an escaped pipe inside a cell, not a separator."""
    parts = re.split(r"(?<!\\)\|", line.strip())
    return [cell.strip().replace("\\|", "|") for cell in parts[1:-1]]


def answer_rows(readme: str) -> list[tuple[list[str], str]]:
    """(question names, Default cell) per row of the answer table."""
    rows: list[tuple[list[str], str]] = []
    in_section = False
    for line in readme.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == HEADING
            continue
        if not in_section or not line.startswith("| `"):
            continue
        cells = _cells(line)
        if len(cells) < 2:
            continue
        rows.append((_BACKTICKED.findall(cells[0]), cells[1]))
    return rows


def _expected(question: dict) -> tuple[str, str]:
    """(rule, what the Default cell must carry) for one question."""
    if "default" not in question:
        return "no-default", "—"
    default = question["default"]
    if isinstance(default, str) and "{{" in default:
        refs = sorted(meta.find_undeclared_variables(_ENV.parse(default)))
        return "derived", ", ".join(refs)
    return "literal", str(default)


def _satisfied(rule: str, want: str, part: str) -> bool:
    if DEFERRAL in part:
        return True
    if rule == "no-default":
        return "—" in part or "asked" in part.lower()
    if rule == "derived":
        return any(ref and ref in part for ref in want.split(", "))
    return want.lower() in part.lower()


def default_cell_problems(
    rows: list[tuple[list[str], str]], questions: dict
) -> list[str]:
    """One message per question whose Default cell disagrees with copier.yml."""
    problems = []
    for names, cell in rows:
        parts = [p.strip() for p in cell.split(" / ")]
        if len(parts) != len(names):
            # One cell covering every answer the row names.
            parts = [cell] * len(names)
        for name, part in zip(names, parts):
            question = questions.get(name)
            if question is None:
                problems.append(f"{name}: named in the table, not a question in copier.yml")
                continue
            rule, want = _expected(question)
            if not _satisfied(rule, want, part):
                problems.append(
                    f"{name}: Default cell {part!r} does not carry its {rule} default "
                    f"({want!r})"
                )
    return problems


def missing_questions(rows: list[tuple[list[str], str]], asked: dict) -> list[str]:
    listed = {name for names, _ in rows for name in names}
    return sorted(set(asked) - listed)


def test_the_answer_table_names_every_asked_question():
    rows = answer_rows(README)
    assert rows, f"no rows found under README.md {HEADING} — this gate examined nothing"
    missing = missing_questions(rows, ASKED)
    assert not missing, f"README.md {HEADING} omits: " + ", ".join(missing)


def test_answer_table_default_cells_agree_with_copier_yml():
    rows = answer_rows(README)
    assert rows, f"no rows found under README.md {HEADING} — this gate examined nothing"
    problems = default_cell_problems(rows, QUESTIONS)
    assert not problems, (
        f"README.md {HEADING} states defaults copier.yml does not:\n  "
        + "\n  ".join(problems)
    )


def test_a_stale_or_unknown_default_cell_is_caught():
    """Mutation case: a literal that moved, a derivation naming the wrong answer,
    a default invented for an answer that has none, and a row for no question."""
    questions = {
        "timezone": {"type": "str", "default": "UTC"},
        "lan_gateway": {"type": "str", "default": "{{ lan_prefix }}.1"},
        "cluster_name": {"type": "str"},
    }
    rows = [
        (["timezone"], "`Europe/Berlin`"),
        (["lan_gateway"], "`<lan_cidr>.1`"),
        (["cluster_name"], "`mycluster`"),
        (["cluster_label"], "—"),
    ]
    problems = default_cell_problems(rows, questions)
    assert len(problems) == 4, problems
    assert "timezone" in problems[0] and "'UTC'" in problems[0]
    assert "lan_gateway" in problems[1] and "derived" in problems[1]
    assert "cluster_name" in problems[2] and "no-default" in problems[2]
    assert "not a question in copier.yml" in problems[3]


def test_an_unlisted_question_is_caught():
    """Mutation case: a question the table never names."""
    rows = [(["timezone"], "`UTC`")]
    assert missing_questions(rows, {"timezone": {}, "gpu": {}}) == ["gpu"]


def test_a_grouped_row_is_checked_per_answer():
    """A row naming several answers pairs its `/`-separated cell with them in order,
    so one wrong half still reports."""
    questions = {
        "k3s_pod_cidr": {"type": "str", "default": "10.42.0.0/16"},
        "k3s_service_cidr": {"type": "str", "default": "10.43.0.0/16"},
    }
    rows = [(["k3s_pod_cidr", "k3s_service_cidr"], "`10.42.0.0/16` / `10.44.0.0/16`")]
    problems = default_cell_problems(rows, questions)
    assert len(problems) == 1 and "k3s_service_cidr" in problems[0]
