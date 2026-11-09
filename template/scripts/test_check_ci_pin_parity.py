"""Unit tests for scripts/check-ci-pin-parity.sh.

Each test writes a pipeline file carrying the floor pins and checks the exit
code: 0 in agreement, 1 on drift, 2 on a broken derivation or unreadable file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "check-ci-pin-parity.sh"

CI = """\
variables:
  ANSIBLE_VERSION: "{variables_ansible}"

include:
  - project: lib
    inputs:
      ansible_version: "{input_ansible}"

cluster-verify:
  script:
    - |
      FLUX_VERSION="{flux_a}"
  after_script:
    - |
      FLUX_VERSION="{flux_b}"
{extra}"""


def _run(tmp_path: Path, **kwargs) -> subprocess.CompletedProcess:
    values = {
        "variables_ansible": "11.6.0",
        "input_ansible": "11.6.0",
        "flux_a": "2.7.6",
        "flux_b": "2.7.6",
        "extra": "",
    }
    values.update(kwargs)
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(CI.format(**values))
    return subprocess.run(
        ["bash", str(GATE), str(ci)], capture_output=True, text=True, check=False
    )


def test_pins_in_agreement_pass(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout


def test_an_include_input_that_drifted_from_variables_fails(tmp_path):
    """The mutation case: bump one half of a two-place pin and the gate reds."""
    result = _run(tmp_path, input_ansible="12.0.0")
    assert result.returncode == 1
    assert "DRIFT" in result.stdout


def test_two_script_copies_of_one_pin_must_agree(tmp_path):
    result = _run(tmp_path, flux_b="2.7.5")
    assert result.returncode == 1
    assert "FLUX_VERSION" in result.stdout


def test_a_pipeline_the_parser_no_longer_sees_fails(tmp_path):
    """A derivation that found none of the floor pins is a broken gate."""
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text("variables:\n  SOMETHING: \"1\"\n")
    result = subprocess.run(
        ["bash", str(GATE), str(ci)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "no longer sees it" in result.stdout


def test_a_missing_pipeline_file_is_an_inspection_failure(tmp_path):
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "absent.yml")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot read" in result.stderr


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        # An unparsed side is an operator error, not a policy finding.
        ({"input_ansible": ""}, 2),
        # Key material in two jobs is no pin, so differing values pass.
        ({"extra": 'job-c:\n  script:\n    - |\n      SSH_PUBLIC_KEY="a"\n'
                   'job-d:\n  script:\n    - |\n      SSH_PUBLIC_KEY="b"\n'}, 0),
    ],
    ids=["empty-include-input", "non-pin-assignment"],
)
def test_the_exit_code(tmp_path, overrides, code):
    result = _run(tmp_path, **overrides)
    assert result.returncode == code, result.stdout
