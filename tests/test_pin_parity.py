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


# The community package's major N ships ansible-core 2.(N + 7).
_CORE_MINOR_OFFSET = 7
TEMPLATE_REQUIREMENTS = REPO / "template" / "requirements.txt"


def core_line_for(ansible_pin: str) -> str:
    """The ansible-core `major.minor` the community `ansible` pin resolves to."""
    major = int(ansible_pin.split(".")[0])
    return f"2.{major + _CORE_MINOR_OFFSET}"


def test_the_core_pin_tracks_the_renders_ansible_pin() -> None:
    """ANSIBLE_CORE_VERSION installs the interpreter the render's nested pytest
    drives, so it must be the core line a generated cluster deploys with."""
    text = TEMPLATE_REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(r"^ansible==([^\s#]+)", text, re.MULTILINE)
    assert match, "template/requirements.txt no longer pins `ansible==`"
    expected = core_line_for(match.group(1))
    core = pipeline_variables(CI_FILE)["ANSIBLE_CORE_VERSION"]
    assert core.startswith(f"{expected}."), (
        f"template/requirements.txt pins ansible=={match.group(1)}, which needs "
        f"ansible-core {expected}.x, but ANSIBLE_CORE_VERSION is {core}"
    )


@pytest.mark.parametrize(
    ("ansible_pin", "expected"),
    [("14.4.0", "2.21"), ("11.6.0", "2.18")],
    ids=["current", "older-line"],
)
def test_the_core_line_derivation_is_exercised(ansible_pin, expected) -> None:
    """Mutation case: a stale core line is only caught if the mapping holds."""
    assert core_line_for(ansible_pin) == expected


# The template's kubectl pin and the k3s release it talks to.
TEMPLATE_GROUP_VARS = REPO / "template" / "ansible" / "inventories" / "prod" / "group_vars" / "all.yml.jinja"
_KUBECTL_INPUT = re.compile(r'^\s*kubectl_version:\s*"v?(\d+)\.(\d+)\.', re.MULTILINE)
_K3S_PIN = re.compile(r'^k3s_version:\s*"v?(\d+)\.(\d+)\.', re.MULTILINE)


def skew(ci_text: str, group_vars_text: str) -> list[str]:
    """kubectl pins outside Kubernetes' supported +/-1 minor skew of k3s."""
    pins = sorted({(int(m[1]), int(m[2])) for m in _KUBECTL_INPUT.finditer(ci_text)})
    k3s = _K3S_PIN.search(group_vars_text)
    assert pins, "the template's pipeline spells no kubectl_version: input"
    assert k3s, "the template's group_vars/all.yml.jinja pins no k3s_version"
    major, minor = int(k3s[1]), int(k3s[2])
    return [
        f"kubectl v{pin[0]}.{pin[1]} against k3s v{major}.{minor}"
        for pin in pins
        if pin[0] != major or abs(pin[1] - minor) > 1
    ]


def test_the_kubectl_pin_tracks_the_k3s_release() -> None:
    """kubectl is pinned in the pipeline and k3s in group_vars, so only a reader
    pairs them. Outside one minor, kubectl refuses to talk to the apiserver."""
    stale = skew(
        TEMPLATE_CI.read_text(encoding="utf-8"),
        TEMPLATE_GROUP_VARS.read_text(encoding="utf-8"),
    )
    assert not stale, (
        "the template's kubectl pin is outside the supported skew:\n  "
        + "\n  ".join(stale)
        + "\nBump kubectl_version and its sha256 in template/.gitlab-ci.yml.jinja."
    )


@pytest.mark.parametrize(
    ("kubectl", "expected"),
    [("v1.37.1", []), ("v1.36.2", []), ("v1.35.9", ["kubectl v1.35 against k3s v1.37"])],
    ids=["equal", "one-minor-back", "two-minors-back"],
)
def test_a_kubectl_pin_outside_the_skew_is_reported(kubectl, expected) -> None:
    """Mutation case: the skew check fires, and only outside one minor."""
    assert skew(f'      kubectl_version: "{kubectl}"\n', 'k3s_version: "v1.37.1+k3s1"\n') == expected
