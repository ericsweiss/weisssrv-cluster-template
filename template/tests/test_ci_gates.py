"""The python-tests job must run on every file whose only gate is that suite.

Its `changes:` list is hand-kept: a vendored copy outside scripts/ missing from
it is edited in a pipeline the byte-identity gate never runs in.
"""

from __future__ import annotations

import re

import pytest
from conftest import REPO, load_script, vendored_consumer_paths

ci_yaml = load_script("ci_yaml.py")

CI_FILE = REPO / ".gitlab-ci.yml"
PYTHON_TESTS_TEMPLATE = "python-tests.yml"

needs_pipeline = pytest.mark.skipif(not CI_FILE.is_file(), reason="no .gitlab-ci.yml")


def python_tests_changes(doc: dict) -> list[str]:
    """The `changes:` globs of the python-tests include, as written."""
    includes = doc.get("include") or []
    for entry in includes if isinstance(includes, list) else [includes]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("file", "")).endswith(PYTHON_TESTS_TEMPLATE):
            return list((entry.get("inputs") or {}).get("changes") or [])
    return []


def _glob_re(pattern: str) -> re.Pattern[str]:
    """GitLab's `changes:` glob as a regex: `**` crosses directories, `*` does not."""
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            # `**/` matches zero or more directories, so the pattern also selects
            # a file sitting directly in the prefix directory.
            out += "(?:[^/]+/)*"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif char == "*":
            out += "[^/]*"
            index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(rf"^{out}$")


def unmatched(paths: list[str], globs: list[str]) -> list[str]:
    """Paths no glob in `globs` selects."""
    patterns = [_glob_re(glob) for glob in globs]
    return sorted({path for path in paths if not any(p.match(path) for p in patterns)})


@needs_pipeline
def test_every_vendored_copy_outside_scripts_runs_the_suite():
    changes = python_tests_changes(ci_yaml.load_ci(CI_FILE))
    assert changes, f"the {PYTHON_TESTS_TEMPLATE} include declares no changes: list"
    outside = [path for path in vendored_consumer_paths() if not path.startswith("scripts/")]
    assert outside, "the manifest registers nothing outside scripts/ — nothing to check"
    missing = unmatched(outside, changes)
    assert not missing, (
        f"the python-tests changes: list does not match {missing} — an edit to one "
        "skips tests/test_vendored_byte_identity.py, so the drift lands on main "
        "before any gate sees it. Add the path to that list."
    )


def test_the_glob_matcher_distinguishes_the_two_stars():
    """Mutation case: a dropped entry is reported, and `*` must not cross a
    directory boundary the way `**` does."""
    globs = ["ruff.toml", "scripts/**/*", "lint/*.yml"]
    assert unmatched(["ruff.toml", "scripts/a/b.py", "scripts/c.py", "lint/x.yml"], globs) == []
    assert unmatched(["ruff.toml"], ["scripts/**/*"]) == ["ruff.toml"]
    assert unmatched(["lint/sub/x.yml"], globs) == ["lint/sub/x.yml"]
