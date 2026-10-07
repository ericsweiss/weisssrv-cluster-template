"""Tool pins this repository repeats as literals are held equal to their variable.

An `include:`'s `inputs:` cannot read `variables:`, so a few pins are spelled
twice; a stale literal installs a different tool than the rest of the pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from render_cluster import load_ci

REPO = Path(__file__).resolve().parent.parent
CI_FILE = REPO / ".gitlab-ci.yml"
TEMPLATE_CI = REPO / "template" / ".gitlab-ci.yml.jinja"

# pip requirement literal -> the `variables:` entry that is its single source.
VARIABLE_FOR = {
    "copier": "COPIER_VERSION",
    "pyyaml": "PYYAML_VERSION",
    "yamllint": "YAMLLINT_VERSION",
    "ansible-core": "ANSIBLE_CORE_VERSION",
}

_LITERAL_PIN = re.compile(r"(?P<package>[A-Za-z][A-Za-z0-9._-]*)==(?P<version>\d[^\"'\s\\]*)")


def pipeline_variables(path: Path) -> dict[str, str]:
    return {k: str(v) for k, v in (load_ci(path).get("variables") or {}).items()}


def literal_pins(text: str) -> list[tuple[str, str]]:
    """(package, version) for every `name==version` literal, `$VAR` forms aside."""
    return [(m["package"], m["version"]) for m in _LITERAL_PIN.finditer(text)]


def drifted(text: str, variables: dict[str, str]) -> list[str]:
    """Literal pins that disagree with the variable that is meant to own them."""
    return [
        f"{package}=={version} but {VARIABLE_FOR[package]} is {variables[VARIABLE_FOR[package]]}"
        for package, version in literal_pins(text)
        if package in VARIABLE_FOR
        and VARIABLE_FOR[package] in variables
        and variables[VARIABLE_FOR[package]] != version
    ]


def test_the_single_source_variables_exist() -> None:
    """A renamed variable would make the parity checks below vacuous."""
    variables = pipeline_variables(CI_FILE)
    missing = sorted(name for name in VARIABLE_FOR.values() if name not in variables)
    assert not missing, f".gitlab-ci.yml declares no {missing}"


def test_the_scan_finds_the_repeated_pins() -> None:
    """Both pipelines carry at least one literal; a changed spelling is drift."""
    for path in (CI_FILE, TEMPLATE_CI):
        pins = [p for p, _v in literal_pins(path.read_text(encoding="utf-8"))]
        assert [p for p in pins if p in VARIABLE_FOR], (
            f"{path.name} spells no `<package>==<version>` literal this gate "
            "knows — either the pins moved to variables, or the shape changed."
        )


def test_literal_pip_pins_match_their_variable() -> None:
    variables = pipeline_variables(CI_FILE)
    stale = drifted(CI_FILE.read_text(encoding="utf-8"), variables)
    assert not stale, ".gitlab-ci.yml literal pins disagree with variables:\n  " + "\n  ".join(
        stale
    )


def test_the_generated_pipeline_pins_the_same_tools() -> None:
    """The template's own pipeline installs the tools a render is tested with, so
    a generated cluster is not validated against a different ansible-core."""
    variables = pipeline_variables(CI_FILE)
    stale = drifted(TEMPLATE_CI.read_text(encoding="utf-8"), variables)
    assert not stale, (
        "template/.gitlab-ci.yml.jinja literal pins disagree with this "
        "repository's variables:\n  " + "\n  ".join(stale)
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('pip_packages: "copier==9.0.0"', ["copier==9.0.0 but COPIER_VERSION is 9.17.0"]),
        ('pip_packages: "copier==9.17.0"', []),
        ('pip install "copier==${COPIER_VERSION}"', []),
    ],
    ids=["drifted", "matching", "variable-form"],
)
def test_a_drifted_literal_pin_is_reported(text, expected) -> None:
    """Mutation case: the detection fires on a stale literal, and only on one."""
    assert drifted(text, {"COPIER_VERSION": "9.17.0"}) == expected
