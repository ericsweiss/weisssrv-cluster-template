"""Unit tests for scripts/check-integration-matrix-coverage.py.

Each test builds a throwaway CI file and integration-test tree, points the gate
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


def test_this_repository_passes_on_the_real_matrix():
    """The invocation `task lint` and check-integration-matrix make: this
    repository's own included job file and integration tree are the subject, and
    a fixture cannot catch a rename in either of them."""
    res = _run(["--ci-file", ".gitlab/ci/integration-jobs.yml"])
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"


def test_every_scenario_in_the_matrix_passes(tmp_path):
    args = _tree(tmp_path, ["dns-stack", "mail-stack"], ["dns-stack", "mail-stack"])
    res = _run(args)
    assert res.returncode == 0, res.stderr


def test_a_scenario_missing_from_the_matrix_fails(tmp_path):
    # The mutation this gate exists for: a directory that never runs reads as
    # green because nothing executed it.
    args = _tree(tmp_path, ["dns-stack", "mail-stack"], ["dns-stack"])
    res = _run(args)
    assert res.returncode == 1
    assert "mail-stack" in res.stderr


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


def test_a_missing_job_is_an_inspection_failure(tmp_path):
    args = _tree(tmp_path, ["dns-stack"], ["dns-stack"])
    res = _run(_with(args, "--integration-job", "not-a-job"))
    assert res.returncode == 2


def test_a_missing_integration_directory_is_an_inspection_failure(tmp_path):
    args = _tree(tmp_path, ["dns-stack"], ["dns-stack"])
    assert _run(_with(args, "--integration-dir", str(tmp_path / "absent"))).returncode == 2


def test_a_matrix_entry_naming_no_directory_fails(tmp_path):
    # The other direction: a renamed or deleted suite still listed in the matrix
    # fails inside the privileged DinD job this lint runs before.
    args = _tree(tmp_path, ["dns-stack"], ["dns-stack", "gone-stack"])
    res = _run(args)
    assert res.returncode == 1
    assert "gone-stack" in res.stderr


def test_an_explicitly_empty_integration_dir_disables_the_half(tmp_path):
    # The opt-out for a cluster with no integration suites: without it the
    # vacuity guard below turns "no suites at all" into an inspection failure.
    args = _tree(tmp_path, [], [])
    res = _run(_with(args, "--integration-dir", ""))
    assert res.returncode == 0, res.stderr
    assert "No integration suite declared" in res.stdout


def test_no_suite_and_no_matrix_entry_is_an_inspection_failure(tmp_path):
    # Both comparisons are empty here, so without the vacuity guard the gate
    # reports "covers all 0 test dir(s)" and exits 0.
    args = _tree(tmp_path, [], [])
    Path(args[args.index("--integration-dir") + 1]).mkdir(parents=True, exist_ok=True)
    res = _run(args)
    assert res.returncode == 2
    assert "inspected nothing" in res.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
