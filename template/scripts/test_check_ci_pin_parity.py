"""Unit tests for scripts/check-ci-pin-parity.sh.

Each test writes a pipeline file carrying the floor pins the gate asserts on and
checks the exit code: 0 in agreement, 1 on drift or a broken derivation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
"""


def _run(tmp_path: Path, **kwargs) -> subprocess.CompletedProcess:
    values = {
        "variables_ansible": "11.6.0",
        "input_ansible": "11.6.0",
        "flux_a": "2.7.6",
        "flux_b": "2.7.6",
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
    assert result.returncode == 1
    assert "no longer sees it" in result.stdout
