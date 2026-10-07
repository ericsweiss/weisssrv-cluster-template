"""The toolchain pins that exist in two places must stay one pin.

requirements.txt is what a local checkout installs and .gitlab-ci.yml is what the
pipeline installs; a drift means a local gate run unlike the one deciding merge.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
CI_FILE = REPO / ".gitlab-ci.yml"


class _CILoader(yaml.SafeLoader):
    """SafeLoader tolerating GitLab's `!reference` tags, subclassed so the
    constructor is not registered on the global SafeLoader."""


_CILoader.add_constructor(
    "!reference", lambda loader, node: loader.construct_sequence(node)
)


def pinned(package: str) -> str:
    """The exact version requirements.txt pins for a package."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(package)}==([^\s#]+)", text, re.MULTILINE)
    assert match, f"requirements.txt no longer pins `{package}==`"
    return match.group(1)


def ci_variables() -> dict:
    doc = yaml.load(CI_FILE.read_text(encoding="utf-8"), Loader=_CILoader) or {}
    return doc.get("variables") or {}


def test_the_ansible_pin_matches_the_ci_variable():
    """Several CI jobs pip-install `variables.ANSIBLE_VERSION` directly."""
    ci_pin = ci_variables().get("ANSIBLE_VERSION")
    assert ci_pin, "variables.ANSIBLE_VERSION is where the CI jobs read the pin"
    local = pinned("ansible")
    assert local == str(ci_pin), (
        f"requirements.txt pins ansible=={local} but .gitlab-ci.yml's "
        f"ANSIBLE_VERSION is {ci_pin!r}. They are one pin — bump both."
    )


def test_the_ruff_pin_matches_the_python_lint_include_input():
    """A drift here means a rule the local run does not see."""
    inputs = re.findall(
        r'^\s*ruff_version:\s*"([^"]+)"', CI_FILE.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert inputs, "the python-lint include no longer passes ruff_version"
    local = pinned("ruff")
    off = sorted({value for value in inputs if value != local})
    assert not off, (
        f"requirements.txt pins ruff=={local} but python-lint's ruff_version is "
        f"{off}. They are one pin — bump both."
    )


def test_the_pyyaml_pin_matches_the_ci_variable():
    """The corpus gates import PyYAML; CI installs `variables.PYYAML_VERSION`."""
    ci_pin = ci_variables().get("PYYAML_VERSION")
    assert ci_pin, "variables.PYYAML_VERSION is where the CI jobs read the pin"
    local = pinned("pyyaml")
    assert local == str(ci_pin), (
        f"requirements.txt pins pyyaml=={local} but .gitlab-ci.yml's "
        f"PYYAML_VERSION is {ci_pin!r}. They are one pin — bump both."
    )
