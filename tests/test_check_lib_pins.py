"""Tests for scripts/check-lib-pins.py, the library pin gate.

Canonical suite: a consumer vendors this file with the script (see
scripts/README.md) and adds only its own smoke test.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "scripts" / "check-lib-pins.py"

_spec = importlib.util.spec_from_file_location("check_lib_pins", _SCRIPT)
assert _spec and _spec.loader
clp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clp)

MARKER = "$[[ inputs.lib_ref ]]"


def _ci(
    declared: str = "v0.4.0",
    refs: tuple[str, ...] = (MARKER, MARKER),
    variable: str = MARKER,
) -> str:
    """The header layout: a `spec:` document, then two lib includes reading it."""
    return textwrap.dedent(
        f"""\
        spec:
          inputs:
            lib_ref:
              default: {declared}
        ---
        variables:
          WEISSSRV_LIB_REF: {variable}
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


def _literal_ci(
    declared: str = "v0.4.0", refs: tuple[str, ...] = ("v0.4.0", "v0.4.0")
) -> str:
    """The layout --fix converts from: a literal tag repeated on every pin."""
    return textwrap.dedent(
        f"""\
        ---
        # A consumer pipeline with no `spec:` header.
        variables:
          WEISSSRV_LIB_REF: "{declared}"
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


# ---------------------------------------------------------------------------
# The declared ref lives in the header document.


def test_the_declared_ref_is_the_header_default(tmp_path: Path) -> None:
    assert clp.declared_ref(_ci("v0.4.0")) == "v0.4.0"


def test_a_file_without_a_header_declares_nothing(tmp_path: Path) -> None:
    assert clp.declared_ref(_literal_ci()) is None


def test_the_header_is_not_mistaken_for_the_pipeline_body(tmp_path: Path) -> None:
    """parse_ci must read the jobs document, not the `spec:` one."""
    doc = clp.parse_ci(_ci())
    assert "include" in doc and "spec" not in doc


def test_a_clean_header_layout_passes(tmp_path: Path) -> None:
    assert clp.check(_write(tmp_path, _ci())) == []


def test_a_quoted_interpolation_is_the_same_pin(tmp_path: Path) -> None:
    """Quoting is invisible after the parse, so both spellings are the pin."""
    content = _ci(refs=(f'"{MARKER}"', MARKER), variable=f'"{MARKER}"')
    assert clp.check(_write(tmp_path, content)) == []


def test_fix_normalizes_a_quoted_pin_to_the_bare_interpolation(
    tmp_path: Path,
) -> None:
    """One spelling, so a bump's diff is the declared ref and nothing else."""
    p = _write(tmp_path, _ci(refs=(f'"{MARKER}"', MARKER), variable=f'"{MARKER}"'))
    assert clp.fix(p) == 2
    assert f"ref: {MARKER}\n" in p.read_text()
    assert clp.fix(p) == 0


# ---------------------------------------------------------------------------
# What the contract refuses.


def test_a_literal_include_ref_fails(tmp_path: Path) -> None:
    problems = clp.check(_write(tmp_path, _ci(refs=(MARKER, "v0.4.0"))))
    # The drifted entry carries TWO files, and both must be named — a caller
    # who sees only the entry cannot tell which jobs came from the literal.
    assert len(problems) == 2
    assert any("yaml-lint" in p for p in problems)
    assert any("shellcheck" in p for p in problems)
    assert all("inputs.lib_ref" in p for p in problems)


def test_a_literal_include_ref_fails_even_at_the_declared_tag(tmp_path: Path) -> None:
    """Equality with the declared tag is not the contract; the input is."""
    problems = clp.check(_write(tmp_path, _ci("v0.4.0", ("v0.4.0", "v0.4.0"))))
    assert problems and all("$[[ inputs.lib_ref ]]" in p for p in problems)


def test_a_literal_variable_fails(tmp_path: Path) -> None:
    problems = clp.check(_write(tmp_path, _ci(variable="v0.4.0")))
    assert len(problems) == 1
    assert "WEISSSRV_LIB_REF" in problems[0]


def test_an_absent_variable_fails(tmp_path: Path) -> None:
    content = _ci().replace("variables:\n  WEISSSRV_LIB_REF: %s\n" % MARKER, "")
    problems = clp.check(_write(tmp_path, content))
    assert len(problems) == 1
    assert "WEISSSRV_LIB_REF is None" in problems[0]


def test_a_missing_header_is_reported(tmp_path: Path) -> None:
    problems = clp.check(_write(tmp_path, _literal_ci()))
    assert len(problems) == 1
    assert "spec.inputs.lib_ref.default is not set" in problems[0]


def test_a_branch_default_is_rejected(tmp_path: Path) -> None:
    # Every pin reads the input, so a pure consistency check would pass this. A
    # branch is forbidden by the include contract regardless.
    problems = clp.check(_write(tmp_path, _ci("main")))
    assert any("not a release tag" in p for p in problems)


def test_a_default_with_a_trailing_newline_is_rejected(tmp_path: Path) -> None:
    """Python's `$` matches before a trailing newline; `fullmatch` does not."""
    content = _ci('"v0.5.1\\n"')
    assert any("not a release tag" in p for p in clp.check(_write(tmp_path, content)))


def test_no_lib_includes_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    # An empty set must not read as "consistent": if the includes are ever
    # restructured out from under the gate it has to say so.
    content = _ci().replace("eric/weisssrv-lib", "eric/somewhere-else")
    problems = clp.check(_write(tmp_path, content))
    assert len(problems) == 1
    assert "no `eric/weisssrv-lib` include entries found" in problems[0]


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


def test_project_and_ref_var_are_overridable(tmp_path: Path) -> None:
    """A consumer pinning a fork, or naming its variable differently."""
    content = textwrap.dedent(
        f"""\
        spec:
          inputs:
            lib_ref:
              default: v1.2.3
        ---
        variables:
          MY_LIB_REF: {MARKER}
        include:
          - project: acme/my-lib
            ref: {MARKER}
            file: /ci/lint/yaml-lint.yml
          - project: eric/weisssrv-lib
            ref: not-a-tag
            file: /ci/lint/shellcheck.yml
        """
    )
    p = _write(tmp_path, content)
    # Scoped to the fork + its own variable: clean, and the OTHER project's
    # literal ref is none of this invocation's business.
    assert clp.check(p, project="acme/my-lib", ref_var="MY_LIB_REF") == []
    assert clp.check(p) != []


# ---------------------------------------------------------------------------
# --fix converts the literal layout, and stays put once converted.


def test_fix_converts_a_literal_file_and_is_idempotent(tmp_path: Path) -> None:
    p = _write(tmp_path, _literal_ci())
    assert clp.fix(p) > 0
    assert clp.check(p) == []
    assert clp.declared_ref(p.read_text()) == "v0.4.0"
    assert clp.fix(p) == 0  # nothing left to change


def test_fix_keeps_the_leading_document_marker_as_the_separator(
    tmp_path: Path,
) -> None:
    """A second `---` would open an empty document between header and body."""
    p = _write(tmp_path, _literal_ci())
    clp.fix(p)
    text = p.read_text()
    assert text.startswith("spec:\n")
    assert text.count("---\n") == 1
    assert "# A consumer pipeline with no `spec:` header." in text


def test_fix_adds_the_separator_when_the_file_has_none(tmp_path: Path) -> None:
    content = _literal_ci().replace("---\n", "", 1)
    p = _write(tmp_path, content)
    clp.fix(p)
    assert clp.check(p) == []
    assert p.read_text().startswith("spec:\n")


def test_fix_moves_only_the_default_once_the_header_exists(tmp_path: Path) -> None:
    """A converted file's include refs already read the input; only the tag moves."""
    p = _write(tmp_path, _ci("v0.4.0"))
    assert clp.fix(p, want="v0.5.0") == 1
    assert clp.declared_ref(p.read_text()) == "v0.5.0"
    assert clp.check(p) == []


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
    assert refs["eric/weisssrv-lib"] == MARKER
    # The other project's pin is not ours to move.
    assert refs["eric/some-other-project"] == "v0.3.2"
    assert clp.check(p) == []


def test_fix_does_not_rewrite_a_project_whose_path_merely_ends_with_ours(
    tmp_path: Path,
) -> None:
    """--fix edits exactly what check() verifies, never a suffix match."""
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
    clp.fix(p)
    refs = {
        i["project"]: i["ref"]
        for i in clp.load_ci(p)["include"]
        if isinstance(i, dict) and i.get("project")
    }
    assert refs["eric/weisssrv-lib"] == MARKER
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
    clp.fix(p)
    assert clp.check(p) == []


def test_fix_refuses_a_branch_source_without_touching_the_file(
    tmp_path: Path,
) -> None:
    """The ref is validated before anything is written.

    The rewrite cannot repair a branch, so propagating it to every entry and
    only then reporting would leave the CI file worse than it found it.
    """
    p = _write(tmp_path, _literal_ci("main"))
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "must be a release tag" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_refuses_a_file_declaring_no_ref_at_all(tmp_path: Path) -> None:
    """With no header and no variable there is nothing to convert from."""
    content = textwrap.dedent(
        """\
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "nothing to sync" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_refuses_a_header_that_declares_no_lib_ref_input(tmp_path: Path) -> None:
    """Inventing an input inside someone else's header is not a line rewrite."""
    content = textwrap.dedent(
        f"""\
        spec:
          inputs:
            env:
              default: prod
        ---
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref: {MARKER}
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "declares no `lib_ref:` input" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_ignores_project_ref_pairs_outside_the_include_block(
    tmp_path: Path,
) -> None:
    """--fix touches only `include:`, the one thing check() reads."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
        mirror-job:
          variables:
            project: eric/weisssrv-lib
            ref: v0.1.1
          script:
            - echo "$ref"
        """
    )
    p = _write(tmp_path, content)
    clp.fix(p)
    doc = clp.load_ci(p)
    assert doc["include"][0]["ref"] == MARKER
    assert doc["mirror-job"]["variables"]["ref"] == "v0.1.1"
    assert clp.check(p) == []


def test_fix_rewrites_an_empty_ref_without_corrupting_the_line(
    tmp_path: Path,
) -> None:
    """`line.replace("", want, 1)` would insert at column 0 and mangle the file."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref:
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    clp.fix(p)
    assert f"    ref: {MARKER}\n" in p.read_text()
    assert clp.check(p) == []


def test_fix_preserves_a_trailing_comment_on_the_ref_line(tmp_path: Path) -> None:
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2  # one of the lint templates
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    clp.fix(p)
    assert f"ref: {MARKER}  # one of the lint templates" in p.read_text()


def test_fix_leaves_a_nested_inputs_ref_alone(tmp_path: Path) -> None:
    """`inputs:` can carry its own `ref` input; only the entry's pin is ours."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/thing.yml
            inputs:
              ref: some-user-value
        """
    )
    p = _write(tmp_path, content)
    clp.fix(p)
    doc = clp.load_ci(p)
    assert doc["include"][0]["ref"] == MARKER
    assert doc["include"][0]["inputs"]["ref"] == "some-user-value"


def test_fix_refuses_a_block_scalar_ref_and_leaves_the_file_alone(
    tmp_path: Path,
) -> None:
    """A `ref: >-` body survives a first-line rewrite and breaks the document.

    fix() re-parses its own output and refuses anything that did not land as the
    exact intended string, leaving the file untouched.
    """
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.0"
        include:
          - project: eric/weisssrv-lib
            ref: >-
              v0.3.2
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "did not land cleanly" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_refuses_a_flow_style_entry_it_cannot_rewrite(tmp_path: Path) -> None:
    """The line rewriter only understands block style; say so, do not return 0."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        include:
          - {project: eric/weisssrv-lib, ref: v0.3.2, file: /ci/lint/yaml-lint.yml}
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "did not land cleanly" in str(excinfo.value)
    assert p.read_text() == before


# ---------------------------------------------------------------------------
# The header `default:` every other surface is rewritten from.


def _flow_header_ci(header: str) -> str:
    """A clean pipeline whose only unusual shape is its header `default:`."""
    return header + textwrap.dedent(
        f"""\
        ---
        variables:
          WEISSSRV_LIB_REF: {MARKER}
        include:
          - project: eric/weisssrv-lib
            ref: {MARKER}
            file: /ci/lint/yaml-lint.yml
        """
    )


@pytest.mark.parametrize(
    "header",
    [
        "spec:\n  inputs:\n    lib_ref: {default: v0.4.0}\n",
        "spec: {inputs: {lib_ref: {default: v0.4.0}}}\n",
        'spec:\n  inputs:\n    lib_ref: {default: "v0.4.0", type: string}\n',
        "spec:\n  inputs:\n    lib_ref: {default: }\n",
    ],
    ids=["flow-input", "flow-spec", "flow-with-a-sibling-key", "flow-empty-default"],
)
def test_bump_rewrites_a_single_line_flow_header(tmp_path, capsys, header) -> None:
    """A flow `default:` written on one line is a pure column splice.

    Bounded by the value node's own marks, so a sibling key on the same line
    survives it.
    """
    ci = _write(tmp_path, _flow_header_ci(header))
    assert clp.main(["--ci-file", str(ci), "--bump", "v0.6.0"]) == 0
    assert clp.declared_ref(ci.read_text()) == "v0.6.0"
    assert clp.check(ci) == []
    assert "v0.6.0" in capsys.readouterr().out
    if "type: string" in header:
        assert "type: string" in ci.read_text()


@pytest.mark.parametrize(
    ("header", "line"),
    [
        ("spec:\n  inputs:\n    lib_ref: {default:\n      v0.4.0}\n", 3),
        ("spec:\n  inputs:\n    lib_ref:\n      default: >-\n        v0.4.0\n", 4),
    ],
    ids=["flow-across-lines", "folded-scalar"],
)
def test_bump_refuses_a_header_default_it_cannot_reach(
    tmp_path, capsys, header, line
) -> None:
    """Reporting a clean 0 at the OLD tag is the failure this forecloses.

    Every other pin already reads the input, so nothing else needs rewriting
    and the bump has no drift left to betray it.
    """
    ci = _write(tmp_path, _flow_header_ci(header))
    before = ci.read_text()
    assert clp.main(["--ci-file", str(ci), "--bump", "v0.6.0"]) == 2
    err = capsys.readouterr().err
    assert f"{ci}:{line}" in err and "flow mapping spanning lines" in err
    assert ci.read_text() == before
    assert clp.declared_ref(ci.read_text()) == "v0.4.0"


def test_fix_refuses_an_entry_with_no_ref_to_rewrite(tmp_path: Path) -> None:
    """`--fix` cannot ADD a missing pin; reporting 0 would hide that."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        include:
          - project: eric/weisssrv-lib
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "did not land cleanly" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_ignores_a_ref_reached_through_an_alias_outside_include(
    tmp_path: Path,
) -> None:
    """An alias resolves to its anchor, whose marks may be anywhere in the file.

    Rewriting there would edit shared configuration and still re-parse cleanly,
    so targets are bounded to the include block's own span.
    """
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.1"
        .shared: &shared
          project: eric/weisssrv-lib
          ref: v0.3.2
          file: /ci/lint/yaml-lint.yml
        include:
          - *shared
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "anchor or alias" in str(excinfo.value)
    assert p.read_text() == before  # the anchor is untouched


def test_fix_refuses_an_alias_anchored_inside_the_include_block(
    tmp_path: Path,
) -> None:
    """An alias anchored inside `include:` passes the bounds check.

    It still points the rewrite at another entry's nested config, so the whole
    block is refused rather than edited.
    """
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        include:
          - project: eric/weisssrv-lib
            file: /ci/a.yml
            inputs:
              shared: &shared
                ref: v0.0.1
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/b.yml
            inputs:
              reused: *shared
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "anchor or alias" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_refuses_an_anchor_defined_inside_the_include_block(
    tmp_path: Path,
) -> None:
    """An anchor defined here can be referenced from outside the block, so the
    block is refused even though no alias appears within it.
    """
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        include:
          - &entry
            project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
        elsewhere:
          copy: *entry
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    assert "anchor or alias" in str(excinfo.value)
    assert p.read_text() == before


def test_fix_still_works_with_an_alias_outside_the_include_block(
    tmp_path: Path,
) -> None:
    """An alias elsewhere in the file must not disable --fix."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        .base: &base
          image: python:3.13
        some-job:
          <<: *base
          script: ["true"]
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/lint/yaml-lint.yml
        """
    )
    p = _write(tmp_path, content)
    assert clp.fix(p) > 0
    assert clp.check(p) == []


def test_fix_refuses_duplicate_top_level_include_keys(tmp_path: Path) -> None:
    """check() reads the LAST duplicate; rewriting the first would disagree."""
    content = textwrap.dedent(
        """\
        variables:
          WEISSSRV_LIB_REF: "v0.5.2"
        include:
          - project: eric/weisssrv-lib
            ref: v0.1.1
            file: /ci/ignored.yml
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: /ci/live.yml
        """
    )
    p = _write(tmp_path, content)
    before = p.read_text()
    with pytest.raises(SystemExit) as excinfo:
        clp.fix(p)
    # The message matters: a different SystemExit would let this pass.
    assert "multiple top-level `include:`" in str(excinfo.value)
    assert p.read_text() == before


def test_check_names_an_entry_that_has_no_file_key(tmp_path: Path) -> None:
    """Drift on a file-less entry must not be reported against a bare None."""
    content = _ci(refs=(MARKER, MARKER)).replace(
        "    file: /ci/lint/docs-link-check.yml\n", ""
    ).replace(f"    ref: {MARKER}\n", "    ref: v0.3.2\n", 1)
    problems = clp.check(_write(tmp_path, content))
    assert problems and "<entry with no file:>" in problems[0]


def test_check_reports_drift_on_an_entry_with_an_empty_file_list(
    tmp_path: Path,
) -> None:
    """`file: []` must not collapse the report loop and hide a literal pin."""
    content = textwrap.dedent(
        """\
        spec:
          inputs:
            lib_ref:
              default: v0.5.2
        ---
        variables:
          WEISSSRV_LIB_REF: $[[ inputs.lib_ref ]]
        include:
          - project: eric/weisssrv-lib
            ref: v0.3.2
            file: []
        """
    )
    problems = clp.check(_write(tmp_path, content))
    assert problems, "a literal pin passed the gate because file: was empty"
    assert "empty file: list" in problems[0]


# ---------------------------------------------------------------------------
# CLI surface: --print, --bump, and the operator-error exits.


class TestCliErrors:
    """Operator errors exit 2 with one line, matching the sibling checkers.

    CI must tell drifted pins (1) from an unreadable file (2).
    """

    def test_missing_file_exits_two_without_traceback(self, tmp_path, capsys):
        rc = clp.main(["--ci-file", str(tmp_path / "nope.yml")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "could not read" in err
        assert "Traceback" not in err

    def test_directory_argument_exits_two(self, tmp_path, capsys):
        rc = clp.main(["--ci-file", str(tmp_path)])
        assert rc == 2
        assert "could not read" in capsys.readouterr().err

    def test_malformed_yaml_exits_two(self, tmp_path, capsys):
        p = tmp_path / ".gitlab-ci.yml"
        p.write_text("include: [\n  - project: unclosed\n")
        rc = clp.main(["--ci-file", str(p)])
        assert rc == 2
        assert "not valid YAML" in capsys.readouterr().err

    def test_unknown_flag_is_rejected(self):
        with pytest.raises(SystemExit):
            clp.main(["--nope"])

    def test_help_exits_zero(self):
        with pytest.raises(SystemExit) as e:
            clp.main(["--help"])
        assert e.value.code == 0


def test_print_writes_the_declared_ref_and_nothing_else(tmp_path, capsys) -> None:
    """A Taskfile reads this into an image tag, so stdout is the bare tag."""
    ci = _write(tmp_path, _ci("v0.7.1"))
    assert clp.main(["--ci-file", str(ci), "--print"]) == 0
    out = capsys.readouterr()
    assert out.out == "v0.7.1\n"
    assert out.err == ""


def test_print_refuses_a_file_declaring_no_tag(tmp_path, capsys) -> None:
    """Exporting a garbage image tag is worse than failing the command."""
    ci = _write(tmp_path, _literal_ci())
    assert clp.main(["--ci-file", str(ci), "--print"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "not a release tag" in out.err


def test_bump_moves_every_pin_from_one_command(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci("v0.4.0"))
    _write_requirements(tmp_path, _requirements("v0.4.0"))
    assert clp.main(["--ci-file", str(ci), "--bump", "v0.5.0"]) == 0
    assert clp.declared_ref(ci.read_text()) == "v0.5.0"
    assert clp.check(ci) == []
    assert clp.check_requirements(ci, "v0.5.0") == []
    assert "rewrote" in capsys.readouterr().out


def test_bump_rejects_a_branch(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci("v0.4.0"))
    before = ci.read_text()
    assert clp.main(["--ci-file", str(ci), "--bump", "main"]) == 2
    assert "release tag" in capsys.readouterr().err
    assert ci.read_text() == before


def test_fix_from_the_cli_converts_and_reports(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _literal_ci("v0.4.0", ("v0.4.0", "v0.3.2")))
    _write_requirements(tmp_path, _requirements("v0.3.2"))
    assert clp.main(["--ci-file", str(ci), "--fix"]) == 0
    assert clp.check(ci) == []
    assert clp.check_requirements(ci, "v0.4.0") == []
    assert "rewrote" in capsys.readouterr().out


def test_a_clean_tree_reports_what_it_inspected(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci())
    assert clp.main(["--ci-file", str(ci)]) == 0
    assert "3 template(s) and 0 site(s) pinned at v0.4.0" in capsys.readouterr().out


def test_non_mapping_document_is_an_operator_error(tmp_path, capsys) -> None:
    """Valid YAML of the wrong shape must exit 2, not crash on doc.get()."""
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text("- just\n- a\n- list\n")
    rc = clp.main(["--ci-file", str(p)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid YAML" in err
    assert "Traceback" not in err


def test_non_mapping_variables_is_an_operator_error(tmp_path, capsys) -> None:
    """A non-mapping `variables:` is an operator error (exit 2), not a crash."""
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text(
        "variables: invalid\n"
        "include:\n"
        "  - project: eric/weisssrv-lib\n"
        "    ref: v0.3.2\n"
        "    file: /ci/a.yml\n"
    )
    rc = clp.main(["--ci-file", str(p)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid YAML" in err
    assert "Traceback" not in err


def test_a_file_removed_after_the_guard_starts_still_exits_two(
    tmp_path, capsys
) -> None:
    """The handler wraps the real read, not a preflight."""
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text("include: []\n")
    p.unlink()
    rc = clp.main(["--ci-file", str(p)])
    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_a_scalar_include_is_reported_not_crashed(tmp_path, capsys) -> None:
    """A scalar `include:` is reported, not iterated."""
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text('variables:\n  WEISSSRV_LIB_REF: "v0.5.2"\ninclude: 5\n')
    rc = clp.main(["--ci-file", str(p)])
    assert rc == 1
    assert "no `eric/weisssrv-lib` include entries found" in capsys.readouterr().err


def test_a_string_include_is_reported_not_iterated_character_wise(
    tmp_path,
) -> None:
    """One local file is a valid `include:`; it simply pins no project."""
    p = tmp_path / ".gitlab-ci.yml"
    p.write_text('variables:\n  WEISSSRV_LIB_REF: "v0.5.2"\ninclude: local.yml\n')
    problems = clp.check(p)
    assert len(problems) == 1
    assert "no `eric/weisssrv-lib` include entries found" in problems[0]


# ---------------------------------------------------------------------------
# Ansible collection pin (sibling ansible/requirements.yml) — synced from the
# SAME single source as the include refs.


_LIB_URL = "git+https://git.ericsweiss.com/eric/weisssrv-lib.git#/ansible_collections/weisssrv/infra"


def _requirements(version: str | None = "v0.4.0", url: str = _LIB_URL) -> str:
    """A requirements.yml installing the library collection plus an unrelated
    Galaxy collection. version=None omits the library's `version:` (a floating
    pin); the unrelated collection always carries its own version constraint."""
    lines = [
        "---",
        "collections:",
        f"  - name: {url}",
        "    type: git",
    ]
    if version is not None:
        lines.append(f"    version: {version}")
    lines += [
        "  - name: ansible.posix",
        '    version: ">=2.1.0,<3.0.0"',
        "",
    ]
    return "\n".join(lines)


def _write_requirements(tmp_path: Path, content: str) -> None:
    req = tmp_path / "ansible" / "requirements.yml"
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text(content)


def test_requirements_matching_the_source_passes(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(tmp_path, _requirements("v0.4.0"))
    assert clp.check_requirements(ci, "v0.4.0") == []


def test_requirements_drift_is_reported(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(tmp_path, _requirements("v0.3.2"))
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "v0.3.2" in problems[0] and "v0.4.0" in problems[0]


def test_requirements_drift_reds_the_gate_from_the_cli(tmp_path, capsys) -> None:
    """The declared ref reaches the collection half, not just the includes."""
    ci = _write(tmp_path, _ci("v0.4.0"))
    _write_requirements(tmp_path, _requirements("v0.3.2"))
    assert clp.main(["--ci-file", str(ci)]) == 1
    assert "collection pins version 'v0.3.2'" in capsys.readouterr().err


def test_no_requirements_file_is_a_noop(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())  # no ansible/requirements.yml written
    assert clp.check_requirements(ci, "v0.4.0") == []
    assert clp.fix_requirements(ci, "v0.4.0") == 0


def test_requirements_installing_the_lib_without_a_version_is_reported(
    tmp_path: Path,
) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(tmp_path, _requirements(None))
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "without a version" in problems[0]


def test_fix_requirements_syncs_only_the_lib_collection_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(tmp_path, _requirements("v0.3.2"))
    assert clp.fix_requirements(ci, "v0.4.0") == 1
    assert clp.check_requirements(ci, "v0.4.0") == []
    text = (tmp_path / "ansible" / "requirements.yml").read_text()
    assert "version: v0.4.0" in text            # library synced
    assert 'version: ">=2.1.0,<3.0.0"' in text  # unrelated collection untouched
    assert clp.fix_requirements(ci, "v0.4.0") == 0  # nothing left to change


def test_requirements_matches_on_the_repository_name(tmp_path: Path) -> None:
    """The include project path resolves instance-locally, so it need not be a
    substring of the clone URL: an instance-local mirror is still gated."""
    ci = _write(tmp_path, _ci())
    _write_requirements(
        tmp_path,
        _requirements("v0.3.2", url=_LIB_URL.replace("eric/", "mirrors/")),
    )
    problems = clp.check_requirements(ci, "v0.4.0", project="eric/weisssrv-lib")
    assert len(problems) == 1
    assert "v0.3.2" in problems[0]


def test_a_git_collection_matching_nothing_is_reported(tmp_path: Path) -> None:
    """The gate never passes by failing to find its subject."""
    ci = _write(tmp_path, _ci())
    _write_requirements(
        tmp_path,
        _requirements("v0.3.2", url="git+https://example.com/other/tooling.git"),
    )
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "is not being checked" in problems[0]


def test_requirements_without_any_git_collection_is_a_noop(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(
        tmp_path,
        "---\ncollections:\n  - name: ansible.posix\n    version: \">=2.1.0\"\n",
    )
    assert clp.check_requirements(ci, "v0.4.0") == []


def test_requirements_project_override_scopes_the_fork(tmp_path: Path) -> None:
    """--project reaches the requirements half too, not just the includes."""
    ci = _write(tmp_path, _ci())
    fork = "git+https://git.example.com/acme/my-lib.git#/ansible_collections/acme/infra"
    _write_requirements(tmp_path, _requirements("v1.0.0", url=fork))

    problems = clp.check_requirements(ci, "v1.2.3", project="acme/my-lib")
    assert len(problems) == 1
    assert "v1.0.0" in problems[0]

    assert clp.fix_requirements(ci, "v1.2.3", project="acme/my-lib") == 1
    text = (tmp_path / "ansible" / "requirements.yml").read_text()
    assert "version: v1.2.3" in text
    assert 'version: ">=2.1.0,<3.0.0"' in text  # unrelated collection untouched
    assert clp.check_requirements(ci, "v1.2.3", project="acme/my-lib") == []


def test_source_key_is_matched_like_name(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_requirements(
        tmp_path,
        "---\ncollections:\n"
        "  - name: weisssrv.infra\n"
        "    source: git+https://git.ericsweiss.com/eric/weisssrv-lib.git\n"
        "    type: git\n"
        "    version: v0.3.2\n",
    )
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "v0.3.2" in problems[0]


def test_a_fork_whose_url_contains_the_project_path_is_not_the_library(
    tmp_path: Path,
) -> None:
    """A substring test counted a fork or a path-prefixed mirror as the library,
    so its unrelated pin passed the gate instead of being unchecked."""
    ci = _write(tmp_path, _ci())
    fork = (
        "git+https://git.example.com/mirrors/eric/weisssrv-lib-fork.git"
        "#/ansible_collections/weisssrv/infra"
    )
    _write_requirements(tmp_path, _requirements("v0.1.0", url=fork))
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "is not being checked" in problems[0]


def test_the_same_repository_on_another_host_is_still_the_library(
    tmp_path: Path,
) -> None:
    """The host-independent match the repo-name comparison exists for."""
    ci = _write(tmp_path, _ci())
    mirror = (
        "git+https://mirror.example.com/eric/weisssrv-lib.git"
        "#/ansible_collections/weisssrv/infra"
    )
    _write_requirements(tmp_path, _requirements("v0.3.2", url=mirror))
    problems = clp.check_requirements(ci, "v0.4.0")
    assert len(problems) == 1
    assert "v0.3.2" in problems[0]
    assert clp.check_requirements(ci, "v0.3.2") == []


# ---------------------------------------------------------------------------
# The per-repo site list: every OTHER file that repeats the tag.


_SITES = textwrap.dedent(
    """\
    ---
    sites:
      - path: terraform/*/main.tf
        pattern: 'weisssrv-lib\\.git//terraform/modules/[a-z-]+\\?ref=(v\\d+\\.\\d+\\.\\d+)'
    """
)


def _write_sites(tmp_path: Path, content: str = _SITES) -> Path:
    path = tmp_path / "scripts" / "lib-ref-sites.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _write_module(tmp_path: Path, root: str, ref: str, module: str = "cloudflare-zone"):
    path = tmp_path / "terraform" / root / "main.tf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'module "x" {\n  source = "git::https://git.ericsweiss.com/eric/'
        f'weisssrv-lib.git//terraform/modules/{module}?ref={ref}"\n}}\n'
    )
    return path


def test_print_needs_no_pyyaml(tmp_path: Path) -> None:
    """A Taskfile var reads --print from a shell whose python may lack PyYAML."""
    ci = _write(tmp_path, _ci())
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path / "no-yaml")}
    (tmp_path / "no-yaml" / "yaml").mkdir(parents=True)
    (tmp_path / "no-yaml" / "yaml" / "__init__.py").write_text("raise ImportError('stub')\n")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--print", "--ci-file", str(ci)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == clp.declared_ref(ci.read_text())
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--ci-file", str(ci)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2 and "PyYAML required" in proc.stderr


def test_no_site_list_is_a_noop(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    assert clp.load_sites(ci) == []
    assert clp.check_sites(ci, "v0.4.0") == []
    assert clp.fix_sites(ci, "v0.4.0") == 0


def test_a_matching_site_passes(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.4.0")
    assert clp.check_sites(ci, "v0.4.0") == []


def test_site_drift_is_reported_with_its_line(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.4.0")
    _write_module(tmp_path, "tailscale", "v0.3.2", module="tailscale-acl")
    problems = clp.check_sites(ci, "v0.4.0")
    assert len(problems) == 1
    assert "tailscale/main.tf:2" in problems[0]
    assert "v0.3.2" in problems[0]


def test_site_drift_reds_the_gate_from_the_cli(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.3.2")
    assert clp.main(["--ci-file", str(ci)]) == 1
    assert "pins 'v0.3.2'" in capsys.readouterr().err


def test_a_site_that_matches_nothing_is_reported(tmp_path: Path) -> None:
    """A stale glob or pattern would otherwise pass by inspecting nothing."""
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    problems = clp.check_sites(ci, "v0.4.0")
    assert len(problems) == 1
    assert "matched no ref" in problems[0]


def test_fix_syncs_every_site(tmp_path: Path) -> None:
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.3.2")
    _write_module(tmp_path, "tailscale", "v0.1.0", module="tailscale-acl")
    assert clp.fix_sites(ci, "v0.4.0") == 2
    assert clp.check_sites(ci, "v0.4.0") == []
    assert clp.fix_sites(ci, "v0.4.0") == 0


def test_print_reads_only_the_requested_input_stanza(tmp_path: Path) -> None:
    """A header with several inputs must not bind lib_ref to a sibling's default."""
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(textwrap.dedent("""\
        spec:
          inputs:
            k8s_version:
              default: 1.36.0
            lib_ref:
              description: the library release
              default: v0.4.0
            other_ref:
              default: v9.9.9
        ---
        variables: {}
        """))
    for want, expect in (("lib_ref", "v0.4.0"), ("other_ref", "v9.9.9")):
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--print", "--ci-file", str(ci), "--input", want],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expect
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--print", "--ci-file", str(ci), "--input", "k8s_version"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def _print(ci: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--print", "--ci-file", str(ci)],
        capture_output=True, text=True,
    )


def test_print_reads_only_the_leading_document(tmp_path: Path) -> None:
    """GitLab reads `spec:` from the first document alone, so a later document
    must not lend the header an inputs block, whatever the separator carries."""
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(textwrap.dedent("""\
        --- # the header
        spec:
          description: no inputs here
        --- # the pipeline
        spec:
          inputs:
            lib_ref:
              default: v9.9.9
        """))
    proc = _print(ci)
    assert proc.returncode == 2, proc.stdout
    assert proc.stdout == ""
    ci.write_text(textwrap.dedent("""\
        spec:
          inputs:
            lib_ref:
              default: v0.4.0
        ---\t
        variables: {}
        """))
    proc = _print(ci)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "v0.4.0\n"


def test_print_reads_the_header_behind_a_preamble(tmp_path: Path) -> None:
    """Comments, blank lines and a directive may precede the first `---`;
    the document that follows is still the header."""
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(textwrap.dedent("""\
        # the pipeline

        %YAML 1.2
        --- # header
        spec:
          inputs:
            lib_ref:
              default: v0.4.0
        ---
        variables: {}
        """))
    proc = _print(ci)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "v0.4.0\n"


def test_print_never_borrows_inputs_from_a_later_root_key(tmp_path: Path) -> None:
    """Once the document dedents out of `spec:`, a two-space `inputs:` under
    another root key is not the header's."""
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(textwrap.dedent("""\
        spec:
          description: no inputs here
        other:
          inputs:
            lib_ref:
              default: v9.9.9
        ---
        variables: {}
        """))
    proc = _print(ci)
    assert proc.returncode == 2, proc.stdout
    assert proc.stdout == ""


def test_print_accepts_an_indented_preamble_comment(tmp_path: Path) -> None:
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text("  # indented comment\n---\nspec:\n  inputs:\n    lib_ref:\n      default: v0.4.0\n---\nvariables: {}\n")
    proc = _print(ci)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "v0.4.0\n"


def test_print_fails_fast_on_a_long_header_without_inputs(tmp_path: Path) -> None:
    """Each header line must match the scan one way only, or a header with
    no inputs block backtracks exponentially instead of exiting 2."""
    import time
    ci = tmp_path / ".gitlab-ci.yml"
    body = "".join(f"  key{i}: v{i}\n  # note {i}\n\n" for i in range(400))
    ci.write_text(f"spec:\n{body}---\nvariables: {{}}\n")
    started = time.monotonic()
    proc = _print(ci)
    assert time.monotonic() - started < 5
    assert proc.returncode == 2
    assert proc.stdout == ""


def test_print_refuses_a_default_glued_to_a_comment(tmp_path: Path) -> None:
    """`v0.4.0#x` is one scalar to YAML, so it is no release tag; only a `#`
    after separation whitespace is a comment."""
    ci = tmp_path / ".gitlab-ci.yml"
    for default, expect in (
        ("v0.4.0#x", 2), ("v0.4.0 # x", 0), ("\"v0.4.0\"  # x", 0), ("\"v0.4.0", 2), ("'v0.4.0\"", 2)
    ):
        ci.write_text(f"spec:\n  inputs:\n    lib_ref:\n      default: {default}\n---\nvariables: {{}}\n")
        proc = _print(ci)
        assert proc.returncode == expect, (default, proc.stdout, proc.stderr)
        assert proc.stdout == ("v0.4.0\n" if expect == 0 else "")


def test_a_pipeline_with_no_project_include_is_refused_before_any_write(tmp_path: Path) -> None:
    """With the variable drifted the plan has a rewrite to make, and the absent
    include must still refuse it during planning, not after the write."""
    text = _ci(variable="v0.1.0").replace("  - project: eric/weisssrv-lib\n", "  - project: eric/other\n")
    ci = _write(tmp_path, text)
    _write_sites(tmp_path)
    site = _write_module(tmp_path, "cloudflare", "v0.3.2")
    before = (ci.read_text(), site.read_text())
    with pytest.raises((clp.OperatorError, SystemExit), match="include"):
        clp.plan_fix(ci, "v0.4.0")
    assert (ci.read_text(), site.read_text()) == before


def test_a_site_whose_context_embeds_the_tag_is_refused_at_planning(tmp_path: Path) -> None:
    """The pattern matches `modules/x?ref=v0.3.2` only while the tag is v0.3.2,
    so after the rewrite it matches nothing and the check would red post-write."""
    ci = _write(tmp_path, _ci())
    _write_sites(
        tmp_path,
        "sites:\n  - path: terraform/*/main.tf\n"
        "    pattern: 'ref=(v0\\.3\\.2)'\n",
    )
    site = _write_module(tmp_path, "cloudflare", "v0.3.2")
    before = site.read_text()
    with pytest.raises(clp.OperatorError, match="fails to match its own rewrite"):
        clp.plan_fix_sites(ci, "v0.4.0")
    assert site.read_text() == before


def test_fix_refuses_a_pattern_whose_group_did_not_capture(tmp_path: Path) -> None:
    """An optional group matches with bounds of -1; slicing on it would corrupt."""
    ci = _write(tmp_path, _ci())
    _write_sites(
        tmp_path,
        "sites:\n  - path: terraform/*/main.tf\n"
        "    pattern: 'weisssrv-lib\\.git//terraform/modules/[a-z-]+\\?ref=(v\\d+\\.\\d+\\.\\d+)?'\n",
    )
    path = _write_module(tmp_path, "cloudflare", "v0.3.2")
    path.write_text(path.read_text().replace("?ref=v0.3.2", "?ref="))
    before = path.read_text()
    with pytest.raises(clp.OperatorError, match="without a captured ref"):
        clp.fix_sites(ci, "v0.4.0")
    assert path.read_text() == before


def test_fix_leaves_an_unrelated_version_in_the_same_file_alone(
    tmp_path: Path,
) -> None:
    """The pattern's capture group bounds the rewrite, not the whole match."""
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    path = _write_module(tmp_path, "cloudflare", "v0.3.2")
    path.write_text(path.read_text() + 'provider "x" { version = "v9.9.9" }\n')
    clp.fix_sites(ci, "v0.4.0")
    assert "v9.9.9" in path.read_text()
    assert clp.check_sites(ci, "v0.4.0") == []


def test_bump_moves_the_sites_too(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci("v0.4.0"))
    _write_requirements(tmp_path, _requirements("v0.4.0"))
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.4.0")
    assert clp.main(["--ci-file", str(ci), "--bump", "v0.6.0"]) == 0
    assert clp.check_sites(ci, "v0.6.0") == []
    assert "?ref=v0.6.0" in (tmp_path / "terraform/cloudflare/main.tf").read_text()


def test_a_site_list_counted_in_the_success_line(tmp_path, capsys) -> None:
    ci = _write(tmp_path, _ci())
    _write_sites(tmp_path)
    _write_module(tmp_path, "cloudflare", "v0.4.0")
    assert clp.main(["--ci-file", str(ci)]) == 0
    assert "1 site(s) pinned at v0.4.0" in capsys.readouterr().out


class TestSiteFilesStayInTheRepo:
    """A site glob reads and rewrites files in the repository, and nothing else.

    `--fix` writes to every match, so a link out of the tree is refused before
    it is opened rather than followed.
    """

    def _outside(self, tmp_path: Path) -> Path:
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        target = outside / "main.tf"
        target.write_text(
            'module "x" {\n  source = "git::https://git.ericsweiss.com/eric/'
            'weisssrv-lib.git//terraform/modules/cloudflare-zone?ref=v0.1.0"\n}\n'
        )
        return target

    def test_fix_refuses_a_symlinked_match_and_leaves_the_target_alone(
        self, tmp_path, capsys
    ) -> None:
        target = self._outside(tmp_path)
        ci = _write(tmp_path, _ci())
        _write_sites(tmp_path)
        link = tmp_path / "terraform" / "cloudflare" / "main.tf"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        assert clp.main(["--ci-file", str(ci), "--fix"]) == 2
        err = capsys.readouterr().err
        assert "matched the symlink" in err and str(link) in err
        assert "?ref=v0.1.0" in target.read_text()

    def test_check_refuses_a_symlinked_match_too(self, tmp_path, capsys) -> None:
        target = self._outside(tmp_path)
        ci = _write(tmp_path, _ci())
        _write_sites(tmp_path)
        link = tmp_path / "terraform" / "cloudflare" / "main.tf"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        assert clp.main(["--ci-file", str(ci)]) == 2
        assert "matched the symlink" in capsys.readouterr().err

    def test_a_match_a_linked_directory_carries_outside_is_refused(
        self, tmp_path, capsys
    ) -> None:
        """The leaf is a real file: only resolving the whole path finds the escape."""
        target = self._outside(tmp_path)
        ci = _write(tmp_path, _ci())
        _write_sites(tmp_path)
        (tmp_path / "terraform").mkdir(parents=True, exist_ok=True)
        (tmp_path / "terraform" / "cloudflare").symlink_to(
            target.parent, target_is_directory=True
        )
        assert clp.main(["--ci-file", str(ci), "--fix"]) == 2
        err = capsys.readouterr().err
        assert "resolves to" in err and str(target.parent) in err
        assert "?ref=v0.1.0" in target.read_text()

    def test_a_real_file_in_the_repo_still_passes(self, tmp_path) -> None:
        ci = _write(tmp_path, _ci())
        _write_sites(tmp_path)
        _write_module(tmp_path, "cloudflare", "v0.3.2")
        assert clp.fix_sites(ci, "v0.4.0") == 1
        assert clp.check_sites(ci, "v0.4.0") == []


class TestSiteListSchema:
    """A malformed site list is an operator error (2), never a silent no-op."""

    def _run(self, tmp_path, body, capsys):
        ci = _write(tmp_path, _ci())
        _write_sites(tmp_path, body)
        rc = clp.main(["--ci-file", str(ci)])
        return rc, capsys.readouterr().err

    def test_a_non_mapping_document_exits_two(self, tmp_path, capsys):
        rc, err = self._run(tmp_path, "- a\n- b\n", capsys)
        assert rc == 2 and "mapping with `sites:`" in err

    def test_a_non_list_sites_key_exits_two(self, tmp_path, capsys):
        rc, err = self._run(tmp_path, "---\nsites: nope\n", capsys)
        assert rc == 2 and "must be a list" in err

    def test_an_entry_without_a_pattern_exits_two(self, tmp_path, capsys):
        rc, err = self._run(tmp_path, "---\nsites:\n  - path: a/*.tf\n", capsys)
        assert rc == 2 and "`pattern:` must be a non-empty regex" in err

    def test_an_invalid_regex_exits_two(self, tmp_path, capsys):
        rc, err = self._run(
            tmp_path, "---\nsites:\n  - path: a/*.tf\n    pattern: '(unclosed'\n", capsys
        )
        assert rc == 2 and "not a valid regex" in err

    def test_a_pattern_without_exactly_one_group_exits_two(self, tmp_path, capsys):
        rc, err = self._run(
            tmp_path, "---\nsites:\n  - path: a/*.tf\n    pattern: 'ref=v1'\n", capsys
        )
        assert rc == 2 and "exactly one capture group" in err

    def test_a_pattern_with_two_groups_exits_two(self, tmp_path, capsys):
        rc, err = self._run(
            tmp_path,
            "---\nsites:\n  - path: a/*.tf\n    pattern: '(a)(b)'\n",
            capsys,
        )
        assert rc == 2 and "exactly one capture group" in err

    def test_a_glob_escaping_the_repo_exits_two(self, tmp_path, capsys):
        rc, err = self._run(
            tmp_path,
            "---\nsites:\n  - path: ../elsewhere/*.tf\n    pattern: 'ref=(v1)'\n",
            capsys,
        )
        assert rc == 2 and "escapes it" in err

    def test_an_empty_document_is_a_noop(self, tmp_path, capsys):
        rc, _ = self._run(tmp_path, "", capsys)
        assert rc == 0


class TestFixIsPlannedBeforeItWrites:
    """Every surface is validated and computed before the first byte is written.

    A site refusing must leave the pipeline file, requirements.yml and every
    earlier site exactly as they were.
    """

    _TERRAFORM = (
        r"'weisssrv-lib\.git//terraform/modules/[a-z-]+\?ref=(v\d+\.\d+\.\d+)(?=\")'"
    )
    _CLI = r"'weisssrv-lib\.git@(v\d+\.\d+\.\d+)(?=#subdirectory=cli)'"

    def _tree(self, tmp_path: Path, second: str) -> tuple[Path, Path]:
        """A literal-layout repo drifted on every surface, so a working --fix
        would rewrite all of them."""
        ci = _write(tmp_path, _literal_ci())
        _write_requirements(tmp_path, _requirements("v0.3.2"))
        _write_sites(
            tmp_path,
            "sites:\n"
            f"  - path: terraform/*/main.tf\n    pattern: {self._TERRAFORM}\n"
            f"  - path: docs/*.md\n    pattern: {second}\n",
        )
        return ci, _write_module(tmp_path, "cloudflare", "v0.3.2")

    def _snapshot(self, *paths: Path) -> dict[Path, str]:
        return {path: path.read_text() for path in paths}

    def _unchanged(self, before: dict[Path, str]) -> None:
        for path, text in before.items():
            assert path.read_text() == text, f"{path} was rewritten"

    def _install_doc(self, directory: Path, ref: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        doc = directory / "install.md"
        doc.write_text(
            "pipx install 'git+https://git.ericsweiss.com/eric/weisssrv-lib.git@"
            f"{ref}#subdirectory=cli'\n"
        )
        return doc

    def test_a_later_sites_bad_pattern_leaves_every_surface_untouched(
        self, tmp_path, capsys
    ) -> None:
        ci, module = self._tree(tmp_path, "'(unclosed'")
        before = self._snapshot(ci, module, tmp_path / "ansible" / "requirements.yml")
        assert clp.main(["--ci-file", str(ci), "--fix"]) == 2
        assert "not a valid regex" in capsys.readouterr().err
        self._unchanged(before)

    def test_a_later_site_matching_nothing_refuses_before_any_write(
        self, tmp_path, capsys
    ) -> None:
        """--fix cannot repair a stale glob, so it must not half-apply around it."""
        ci, module = self._tree(tmp_path, self._CLI)
        before = self._snapshot(ci, module, tmp_path / "ansible" / "requirements.yml")
        assert clp.main(["--ci-file", str(ci), "--fix"]) == 2
        assert "matched no ref" in capsys.readouterr().err
        self._unchanged(before)

    def test_a_later_symlinked_site_leaves_the_earlier_one_alone(
        self, tmp_path, capsys
    ) -> None:
        ci, module = self._tree(tmp_path, self._CLI)
        outside = self._install_doc(tmp_path.parent / "outside", "v0.1.0")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "install.md").symlink_to(outside)
        before = self._snapshot(
            ci, module, tmp_path / "ansible" / "requirements.yml", outside
        )
        assert clp.main(["--ci-file", str(ci), "--fix"]) == 2
        assert "matched the symlink" in capsys.readouterr().err
        self._unchanged(before)

    def test_a_site_glob_naming_the_pipeline_file_is_refused(
        self, tmp_path, capsys
    ) -> None:
        """Two surfaces rewriting one file would discard one of the two plans."""
        ci = _write(tmp_path, _literal_ci())
        _write_sites(
            tmp_path,
            "sites:\n  - path: .gitlab-ci.yml\n"
            "    pattern: 'WEISSSRV_LIB_REF: \"(v\\d+\\.\\d+\\.\\d+)\"'\n",
        )
        before = self._snapshot(ci)
        assert clp.main(["--ci-file", str(ci), "--bump", "v0.6.0"]) == 2
        assert "rewritten by two surfaces" in capsys.readouterr().err
        self._unchanged(before)

    def test_a_clean_plan_still_writes_every_surface(self, tmp_path) -> None:
        """The preflight must not cost the working path."""
        ci, module = self._tree(tmp_path, self._CLI)
        doc = self._install_doc(tmp_path / "docs", "v0.1.0")
        assert clp.main(["--ci-file", str(ci), "--bump", "v0.6.0"]) == 0
        assert clp.declared_ref(ci.read_text()) == "v0.6.0"
        assert "?ref=v0.6.0" in module.read_text()
        assert "@v0.6.0#subdirectory=cli" in doc.read_text()
        req = (tmp_path / "ansible" / "requirements.yml").read_text()
        assert "version: v0.6.0" in req
