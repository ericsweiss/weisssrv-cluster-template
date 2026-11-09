"""Unit tests for scripts/check-integration-matrix-coverage.py.

Each case builds a throwaway CI file and integration-test tree, points the gate
at them through its flags, and asserts the exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "check-integration-matrix-coverage.py"

CI_TEMPLATE = """---
integration-tests:
  parallel:
    matrix:
      - TEST:
{entries}
"""

# The rules block the real job carries: a GitLab `!reference` tag the gate's
# loader has to tolerate.
REFERENCED_RULES = """  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes: !reference [.paths-integration, changes]
"""


def _tree(tmp_path: Path, scenarios: list[str], in_matrix: list[str],
          referenced: bool = False) -> list[str]:
    """A scenario directory per name, and a CI file listing `in_matrix`."""
    it_dir = tmp_path / "integration-tests"
    it_dir.mkdir(parents=True)
    for name in scenarios:
        (it_dir / name / "molecule" / "default").mkdir(parents=True)
        (it_dir / name / "molecule" / "default" / "molecule.yml").write_text("---\n")
    entries = "\n".join(f"          - {name}" for name in in_matrix) or "          []"
    ci = tmp_path / "integration-jobs.yml"
    ci.write_text(CI_TEMPLATE.format(entries=entries)
                  + (REFERENCED_RULES if referenced else ""))
    return [
        "--ci-file", str(ci),
        "--integration-dir", str(it_dir),
        "--integration-job", "integration-tests",
    ]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        capture_output=True,
        text=True,
        cwd=GATE.parent.parent,
    )


def _with(args: list[str], flag: str, value: str) -> list[str]:
    """`args` with `flag`'s value replaced."""
    out = list(args)
    out[out.index(flag) + 1] = value
    return out


def test_the_default_invocation_passes_on_this_repository():
    """The invocation `task lint` and CI make: this repository's own job file
    and integration tree, where a rename in either surfaces."""
    res = _run([])
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"


@pytest.mark.parametrize(
    ("scenarios", "in_matrix", "override", "code", "named"),
    [
        (["dns-stack", "mail-stack"], ["dns-stack", "mail-stack"], None, 0, None),
        # A directory that never runs reads as green because nothing ran it.
        (["dns-stack", "mail-stack"], ["dns-stack"], None, 1, "mail-stack"),
        # A deleted suite still listed fails inside the privileged DinD job.
        (["dns-stack"], ["dns-stack", "gone-stack"], None, 1, "gone-stack"),
        (["dns-stack"], ["dns-stack"], ("--integration-job", "not-a-job"), 2, None),
        (["dns-stack"], ["dns-stack"], ("--integration-dir", "/nonexistent"), 2, None),
        # Both sides empty would report "covers all 0 test dir(s)" and exit 0.
        ([], [], None, 2, "inspected nothing"),
    ],
)
def test_the_exit_code(tmp_path, scenarios, in_matrix, override, code, named):
    args = _tree(tmp_path, scenarios, in_matrix)
    if override:
        args = _with(args, *override)
    res = _run(args)
    assert res.returncode == code, res.stdout + res.stderr
    if named:
        assert named in res.stdout + res.stderr


def test_a_referenced_rules_block_does_not_break_the_matrix_read(tmp_path):
    args = _tree(tmp_path, ["dns-stack", "mail-stack"], ["dns-stack", "mail-stack"],
                referenced=True)
    res = _run(args)
    assert res.returncode == 0, res.stderr
    args = _tree(tmp_path / "mutated", ["dns-stack", "mail-stack"], ["dns-stack"],
                referenced=True)
    res = _run(args)
    assert res.returncode == 1
    assert "mail-stack" in res.stderr


def test_a_directory_without_a_scenario_is_not_required(tmp_path):
    args = _tree(tmp_path, ["dns-stack"], ["dns-stack"])
    (Path(args[args.index("--integration-dir") + 1]) / "_shared").mkdir()
    assert _run(args).returncode == 0
