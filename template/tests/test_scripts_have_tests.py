"""Every script shipped here is exercised by some suite, or exempt with a reason.

The walk covers scripts/, kubernetes/ and terraform/. Coverage means a `test_*.py`
uses the script, not that prose mentions it; vendored ones come from the manifest.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
MANIFEST = SCRIPTS / "vendored-manifest.yml"
# CronJob programs live beside their manifests and terraform helpers beside
# their roots, so an untested one outside scripts/ would be invisible here.
SCRIPT_ROOTS = (SCRIPTS, REPO / "kubernetes", REPO / "terraform")
SUITE_DIRS = (REPO / "tests", SCRIPTS)

_SCRIPT_SUFFIXES = {".py", ".sh"}
# Site data and fixtures that sit in scripts/ alongside the code. They are
# covered by the gates that read them, not by a script test, and some carry the
# executable bit from a checkout's umask.
_NON_SCRIPT_SUFFIXES = {".yml", ".yaml", ".json", ".conf", ".env", ".toml", ".md"}

# Operator-only scripts run by a human at a terminal, each with the reason no
# suite is worth writing. Keys are paths relative to the repository root
# ("scripts/foo.sh"), so an entry waives one file and never a same-named sibling.
EXEMPT: dict[str, str] = {}

# Suites whose string literals are exemption data, not usage. A script named
# only in one of these maps would otherwise read as covered by it.
EXEMPTION_SUITES = {"test_scripts_have_tests.py", "test_script_literals.py"}


def _rendered_name(path: Path) -> str:
    """The basename a render ships: copier strips the `.jinja` suffix."""
    return path.name.removesuffix(".jinja")


def _rendered_path(path: Path) -> str:
    """The repo-relative path a render ships, the form an EXEMPT key takes."""
    return path.relative_to(REPO).with_name(_rendered_name(path)).as_posix()


def _scripts(root: Path = SCRIPTS) -> list[Path]:
    """Every script shipped under scripts/, recursively.

    Collected by executable bit as well as `.py`/`.sh` suffix, since sourced
    copies carry mode 644 and shebang scripts often carry no suffix.
    """
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.name.startswith("test_")
        and Path(_rendered_name(p)).suffix not in _NON_SCRIPT_SUFFIXES
        and (Path(_rendered_name(p)).suffix in _SCRIPT_SUFFIXES or os.access(p, os.X_OK))
    )


def _all_scripts() -> list[Path]:
    """Every script under the roots this gate claims to cover."""
    return sorted(p for root in SCRIPT_ROOTS if root.is_dir() for p in _scripts(root))


@pytest.fixture(scope="module")
def upstream_covered() -> set[str]:
    """Filenames the manifest registers as byte-identical library copies.

    Read from the manifest itself, not the library gate, so this never needs a
    weisssrv-lib checkout: without one the whole gate would skip.
    """
    manifest = yaml.safe_load(MANIFEST.read_text()) or {}
    names = {
        Path(entry["consumer"] if isinstance(entry, dict) else entry).name
        for entry in manifest.get("vendored") or []
    }
    assert names, (
        f"{MANIFEST.name} registers no vendored copies — every library copy would "
        "read as a local script"
    )
    return names


def _code_strings(source: str) -> list[str]:
    """Every string literal in `source` that is not a docstring.

    Prose mentions therefore do not read as coverage; an argv literal does.
    """
    tree = ast.parse(source)
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, holders) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.fixture(scope="module")
def suite_bodies() -> dict[str, list[str]]:
    # This file is excluded from its own inputs: every EXEMPT reason below is a
    # string literal, so a script would cover itself by being exempted.
    return {
        p.name: _code_strings(p.read_text())
        for directory in SUITE_DIRS
        if directory.is_dir()
        for p in directory.glob("test_*.py")
        if p.name != Path(__file__).name
    }


def _named_by_a_suite(name: str, suite_bodies: dict[str, list[str]]) -> bool:
    return any(
        name in s
        for suite, strings in suite_bodies.items()
        if suite not in EXEMPTION_SUITES
        for s in strings
    )


def test_the_exemption_suites_are_still_shipped():
    """A renamed suite would drop out of the filter silently and let its
    exemption entries count as coverage again."""
    missing = sorted(
        name
        for name in EXEMPTION_SUITES
        if not any((directory / name).is_file() for directory in SUITE_DIRS if directory.is_dir())
    )
    assert not missing, f"EXEMPTION_SUITES names suites that no longer exist: {missing}"


def test_an_exemption_entry_is_not_coverage():
    """Mutation case: a script named only in another suite's exemption map must
    still read as untested."""
    assert not _named_by_a_suite("orphan.sh", {"test_script_literals.py": ["orphan.sh"]})
    assert _named_by_a_suite("orphan.sh", {"test_orphan.py": ["scripts/orphan.sh"]})


def test_every_local_script_is_named_by_a_suite(upstream_covered, suite_bodies):
    uncovered = []
    for script in _all_scripts():
        name = _rendered_name(script)
        if name in upstream_covered or _rendered_path(script) in EXEMPT:
            continue
        if not _named_by_a_suite(name, suite_bodies):
            uncovered.append(_rendered_path(script))
    assert not uncovered, (
        "local scripts no test suite uses: "
        + ", ".join(sorted(uncovered))
        + "\n\nAdd a tests/test_*.py that exercises it (naming the file in code, "
        "not only in a comment), or add its repo-relative path to EXEMPT here "
        "with the reason a suite is not worth writing."
    )


def test_a_comment_only_mention_does_not_count_as_coverage():
    """Mutation proof for the rule above: prose must not satisfy the gate."""
    prose = '"""deploy-verify.sh is great."""\n# and so is collect-state.sh\n'
    assert not any("deploy-verify.sh" in s for s in _code_strings(prose))
    code = 'subprocess.run(["bash", "scripts/deploy-verify.sh"])\n'
    assert any("deploy-verify.sh" in s for s in _code_strings(code))


def test_every_exemption_still_names_a_script(upstream_covered):
    present = {_rendered_path(p) for p in _all_scripts()}
    stale = sorted(set(EXEMPT) - present)
    assert not stale, (
        f"EXEMPT names paths no script resolves to: {stale} — keys are relative "
        "to the repository root, so a bare basename never matches"
    )
    upstream = sorted(k for k in EXEMPT if Path(k).name in upstream_covered)
    assert not upstream, (
        f"these are exempt here but vendored from the library: {upstream} — the "
        "manifest already accounts for them, drop the EXEMPT entries"
    )


def test_no_exemption_already_has_a_suite(suite_bodies):
    """A suite that already exercises an exempt script makes the exemption stale:
    it keeps the script out of the coverage set while the test exists."""
    covered = sorted(
        key for key in EXEMPT if _named_by_a_suite(Path(key).name, suite_bodies)
    )
    assert not covered, (
        f"exempt here yet already exercised by a suite: {covered} — drop the "
        "EXEMPT entries so the scripts are held to the coverage check."
    )


def test_an_exemption_with_a_real_suite_is_reported():
    """Mutation case for the rule above: the detection must fire on a suite that
    names the script, and only on one that does."""
    bodies = {"test_demo.py": ["scripts/demo.sh", "--dry-run"]}
    assert _named_by_a_suite("demo.sh", bodies)
    assert not _named_by_a_suite("other.sh", bodies)


def test_every_exemption_carries_a_reason():
    for name, reason in EXEMPT.items():
        assert len(reason.split()) >= 10, f"{name} needs a real reason, not {reason!r}"


def test_every_exemption_key_is_a_repo_relative_path():
    """A bare basename would waive every same-named file under the walked roots."""
    bare = sorted(key for key in EXEMPT if "/" not in key)
    assert not bare, (
        f"EXEMPT keys must be repo-relative paths, not basenames: {bare}"
    )


def test_a_basename_exemption_does_not_resolve(monkeypatch, upstream_covered):
    """Mutation case: the stale-entry check must reject the old key shape."""
    script = _all_scripts()[0]
    monkeypatch.setitem(EXEMPT, _rendered_name(script), "x " * 10)
    with pytest.raises(AssertionError, match="no script resolves to"):
        test_every_exemption_still_names_a_script(upstream_covered)


def test_a_path_exemption_resolves(monkeypatch, upstream_covered):
    script = next(
        p for p in _all_scripts() if _rendered_name(p) not in upstream_covered
    )
    monkeypatch.setitem(EXEMPT, _rendered_path(script), "x " * 10)
    test_every_exemption_still_names_a_script(upstream_covered)


def test_the_gate_sees_a_realistic_number_of_scripts():
    """A collection rule that stopped matching would exempt everything, and a
    root that stops resolving would quietly narrow the claim."""
    assert len(_scripts()) > 20, "the script walk resolved almost nothing"
    assert len(_all_scripts()) > len(_scripts()), (
        "the kubernetes/ and terraform/ roots contributed nothing — a shipped "
        "in-cluster program would go uncovered"
    )


def test_the_collector_sees_the_shapes_an_extension_filter_missed(tmp_path):
    """The collector is recursive and not extension-keyed: extensionless
    executables, subdirectory helpers and `.jinja`-named scripts must be seen."""
    (tmp_path / "gate.py").write_text("#!/usr/bin/env python3\n")
    (tmp_path / "test_gate.py").write_text("")
    (tmp_path / "config.yaml").write_text("---\n")
    (tmp_path / "values.yaml.jinja").write_text("---\n")
    (tmp_path / "state.sh.jinja").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "gate.pyc").write_text("")
    shebang = tmp_path / "reap"
    shebang.write_text("#!/usr/bin/env bash\n")
    shebang.chmod(0o755)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.sh").write_text("")
    app = tmp_path / "kubernetes" / "apps" / "demo"
    app.mkdir(parents=True)
    (app / "cronjob.yaml").write_text("---\n")
    (app / "demo.py").write_text("#!/usr/bin/env python3\n")
    (app / "demo.py").chmod(0o755)

    assert [_rendered_name(p) for p in _scripts(tmp_path)] == [
        "gate.py",
        "demo.py",
        "helper.sh",
        "reap",
        "state.sh",
    ]


def _basename_collisions(scripts: list[Path]) -> list[str]:
    """Scripts that would ship under one rendered basename."""
    seen: dict[str, Path] = {}
    collisions = []
    for script in scripts:
        name = _rendered_name(script)
        if name in seen:
            collisions.append(f"{name}: {seen[name]} and {script}")
        seen[name] = script
    return collisions


def test_no_two_scripts_share_a_rendered_basename():
    """Coverage and EXEMPT are keyed on the rendered basename, so a second script
    with the same one would ship untested on the first script's suite."""
    collisions = _basename_collisions(
        [p.relative_to(REPO) for p in _all_scripts()]
    )
    assert not collisions, (
        "scripts sharing a basename across the walked roots:\n  "
        + "\n  ".join(collisions)
        + "\n\nRename one: a suite naming the basename reads as covering both."
    )


def test_a_shared_basename_is_reported(tmp_path):
    """Mutation case for the rule above: the detection must fire, and only on a
    real collision."""
    for relpath in ("scripts/reap.py", "kubernetes/apps/demo/reap.py", "scripts/other.py"):
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    scripts = sorted(tmp_path.rglob("*.py"))
    collisions = _basename_collisions(scripts)
    assert len(collisions) == 1 and "reap.py" in collisions[0]
    assert _basename_collisions([p for p in scripts if p.name != "reap.py"]) == []
