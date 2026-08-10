"""Tests for scripts/check-lib-pins.py (the library pin gate).

Exercises the drift, branch-ref and missing-include cases against synthetic CI
files, plus the --project / --ref-var overrides a consumer needs when it pins a
fork or names its variable differently.

Plus a smoke check that this repository's REAL .gitlab-ci.yml passes — the one
that fires when someone bumps the library pin, since `changes: ["**/*"]` puts
every file in the python-tests trigger.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "scripts" / "check-lib-pins.py"

_spec = importlib.util.spec_from_file_location("check_lib_pins", _SCRIPT)
assert _spec and _spec.loader
clp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clp)


def _ci(ref_var: str = "v0.4.0", refs: tuple[str, ...] = ("v0.4.0", "v0.4.0")) -> str:
    """A minimal .gitlab-ci.yml with two lib includes, the 2nd a `file:` list."""
    return textwrap.dedent(
        f"""\
        variables:
          WEISSSRV_LIB_REF: "{ref_var}"
        include:
          - project: eric/weisssrv-lib
            ref: {refs[0]}
            file: /ci/lint/docs-link-check.yml
          - project: eric/weisssrv-lib
            ref: {refs[1]}
            file:
              - /ci/lint/yaml-lint.yml
              - /ci/lint/shellcheck.yml
          - local: .gitlab/ci/molecule-jobs.gitlab-ci.yml
        """
    )


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text(content)
    return p


def test_consistent_pins_pass(tmp_path: Path) -> None:
    assert clp.check(_write(tmp_path, _ci())) == []


def test_drifted_entry_is_reported_per_file(tmp_path: Path) -> None:
    problems = clp.check(_write(tmp_path, _ci(refs=("v0.4.0", "v0.3.2"))))
    # The drifted entry carries TWO files, and both must be named — a caller
    # who sees only the entry cannot tell which jobs came from the stale tag.
    assert len(problems) == 2
    assert any("yaml-lint" in p for p in problems)
    assert any("shellcheck" in p for p in problems)
    assert all("v0.3.2" in p for p in problems)


def test_branch_ref_is_rejected_even_when_all_entries_agree(tmp_path: Path) -> None:
    # Every entry agrees, so a pure equality check would pass this. A branch is
    # forbidden by the include contract regardless of consistency.
    problems = clp.check(_write(tmp_path, _ci("main", ("main", "main"))))
    assert any("not a release tag" in p for p in problems)


def test_missing_variable_is_reported(tmp_path: Path) -> None:
    content = _ci().replace('  WEISSSRV_LIB_REF: "v0.4.0"\n', "")
    problems = clp.check(_write(tmp_path, content))
    assert any("WEISSSRV_LIB_REF" in p for p in problems)


def test_no_lib_includes_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    # An empty set must not read as "consistent": if the includes are ever
    # restructured out from under the gate it has to say so.
    content = _ci().replace("eric/weisssrv-lib", "eric/somewhere-else")
    assert clp.check(_write(tmp_path, content)) != []


def test_reference_tag_does_not_break_parsing(tmp_path: Path) -> None:
    # .gitlab-ci.yml uses GitLab's !reference tag freely, which plain
    # safe_load rejects outright.
    content = _ci() + textwrap.dedent(
        """\
        some-job:
          script:
            - !reference [.some-anchor, script]
        """
    )
    assert clp.check(_write(tmp_path, content)) == []


def test_fix_rewrites_drift_and_is_idempotent(tmp_path: Path) -> None:
    p = _write(tmp_path, _ci(refs=("v0.4.0", "v0.3.2")))
    assert clp.fix(p) == 1
    assert clp.check(p) == []
    assert clp.fix(p) == 0  # nothing left to change


def test_fix_leaves_non_lib_includes_alone(tmp_path: Path) -> None:
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.4.0"
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
          - project: eric/some-other-project
            ref: v0.3.2
            file: /ci/thing.yml
        """
    )
    p = _write(tmp_path, content)
    clp.fix(p)
    refs = {
        i["project"]: i["ref"]
        for i in clp.load_ci(p)["include"]
        if isinstance(i, dict) and i.get("project")
    }
    assert refs["eric/weisssrv-lib"] == "v0.4.0"
    # The other project's pin is not ours to move.
    assert refs["eric/some-other-project"] == "v0.3.2"
    assert clp.check(p) == []


def test_project_and_ref_var_are_overridable(tmp_path: Path) -> None:
    """A consumer pinning a fork, or naming its variable differently."""
    content = textwrap.dedent(
        """\
        variables:
          MY_LIB_REF: "v1.2.3"
        include:
          - project: acme/my-lib
            ref: v1.2.3
            file: /ci/lint/yaml-lint.yml
          - project: eric/weisssrv-lib
            ref: not-a-tag
            file: /ci/lint/shellcheck.yml
        """
    )
    p = _write(tmp_path, content)
    # Scoped to the fork + its own variable: clean, and the OTHER project's
    # non-tag ref is none of this invocation's business.
    assert clp.check(p, project="acme/my-lib", ref_var="MY_LIB_REF") == []
    # Defaults would look at eric/weisssrv-lib, which has no MY_LIB_REF.
    assert clp.check(p) != []


def test_fix_does_not_rewrite_a_project_whose_path_merely_ends_with_ours(
    tmp_path: Path,
) -> None:
    """`--fix` must edit exactly what check() verifies, not a suffix match.

    `acme/eric/weisssrv-lib` ends with the library's path but is a different
    project. check() compares the whole value, so it never reports that entry —
    a fix() that rewrote it would be editing a pin nothing verifies.
    """
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: acme/eric/weisssrv-lib
            ref: v9.9.9
            file: /ci/lint/yaml-lint.yml
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/shellcheck.yml
        """
    )
    p = _write(tmp_path, content)
    assert clp.fix(p) == 1  # only ours
    refs = {
        i["project"]: i["ref"]
        for i in clp.load_ci(p)["include"]
        if isinstance(i, dict) and i.get("project")
    }
    assert refs["eric/weisssrv-lib"] == "v0.5.0"
    assert refs["acme/eric/weisssrv-lib"] == "v9.9.9"
    assert clp.check(p) == []


def test_fix_matches_a_quoted_or_commented_project_value(tmp_path: Path) -> None:
    """`project:` is YAML, so quotes and trailing comments are not part of it."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: "eric/weisssrv-lib"
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
          - project: eric/weisssrv-lib  # the shared CI library
            ref: v0.3.2
            file: /ci/lint/shellcheck.yml
        """
    )
    p = _write(tmp_path, content)
    assert clp.fix(p) == 2
    assert clp.check(p) == []


def test_fix_reports_failure_when_the_result_still_violates(
    tmp_path: Path, capsys
) -> None:
    """--fix must not exit 0 on a file it cannot actually repair.

    A branch ref in the single source is not something the rewrite can fix: it
    happily makes every entry agree with `main` and would otherwise report
    success for a file that still violates the contract.
    """
    p = _write(tmp_path, _ci("main", ("v0.4.0", "v0.4.0")))
    rc = clp.main(["--ci-file", str(p), "--fix"])
    assert rc == 1
    assert "FAILED after rewrite" in capsys.readouterr().err


def test_real_repo_ci_file_passes() -> None:
    """The live gate: fails the moment a real include drifts from the source."""
    assert clp.check(REPO / ".gitlab-ci.yml") == []
