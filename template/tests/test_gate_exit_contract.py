"""Every gate under scripts/ keeps the exit contract scripts/README.md states.

0 clean, 1 a finding, 2 the gate could not inspect its subject. Exiting 1 on a
missing dependency makes the corpus runner report an operator error as a finding.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conftest import REPO, SCRIPTS, vendored_consumer_paths

_DEPENDENCY_ERRORS = {"ImportError", "ModuleNotFoundError"}


def local_gates() -> list[Path]:
    """Python files under scripts/ that are not library copies, whose guards are
    fixed upstream."""
    vendored = set(vendored_consumer_paths(("vendored",)))
    return [
        path
        for path in sorted(SCRIPTS.rglob("*.py"))
        if path.relative_to(REPO).as_posix() not in vendored
    ]


def _catches_a_missing_dependency(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(name, ast.Name) and name.id in _DEPENDENCY_ERRORS for name in names)


def _is_string_exit(node: ast.AST) -> bool:
    """`sys.exit("...")`, `exit(f"...")` or `SystemExit("...")`: each exits 1."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = node.args[0]
    is_text = isinstance(first, ast.JoinedStr) or (
        isinstance(first, ast.Constant) and isinstance(first.value, str)
    )
    return name in {"exit", "SystemExit"} and is_text


def offenders(source: str) -> list[int]:
    """1-indexed lines where a dependency guard exits with a string, so rc 1."""
    found: set[int] = set()
    for handler in ast.walk(ast.parse(source)):
        if isinstance(handler, ast.ExceptHandler) and _catches_a_missing_dependency(handler):
            for statement in handler.body:
                found |= {n.lineno for n in ast.walk(statement) if _is_string_exit(n)}
    return sorted(found)


def test_every_local_gate_exits_2_on_a_missing_dependency():
    scripts = local_gates()
    assert scripts, "no local Python gate under scripts/ — this gate inspected nothing"
    bad = [
        f"{path.relative_to(REPO).as_posix()}:{line}"
        for path in scripts
        for line in offenders(path.read_text(encoding="utf-8"))
    ]
    assert not bad, (
        "dependency guard exits 1, which reads as a finding rather than an "
        "operator error:\n  " + "\n  ".join(bad) + "\n"
        'Use: print("ERROR: ...", file=sys.stderr) then '
        "raise SystemExit(2) from None."
    )


YAML_MODULES = {"yaml", "jinja2"}


def _imported_names(node) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {(node.module or "").split(".")[0]}
    return set()


def _is_executable_gate(tree) -> bool:
    """A `__main__` block, which the importable helper modules do not have."""
    return any(
        isinstance(node, ast.If) and "__name__" in ast.dump(node.test)
        for node in tree.body
    )


def unguarded_imports(source: str) -> list[int]:
    """1-indexed lines importing PyYAML or Jinja2 at module scope, bare.

    Only a runnable gate is reported: a helper module is imported by its caller,
    which carries the guard.
    """
    tree = ast.parse(source)
    if not _is_executable_gate(tree):
        return []
    return [
        node.lineno
        for node in tree.body
        if _imported_names(node) & YAML_MODULES
    ]


def test_every_local_gate_guards_its_yaml_import():
    """An unguarded import raises ImportError, which exits 1 through the
    traceback — the same misreporting a string-exit guard causes."""
    scripts = local_gates()
    assert scripts, "no local Python gate under scripts/ — this gate inspected nothing"
    bare = [
        f"{path.relative_to(REPO).as_posix()}:{line}"
        for path in scripts
        for line in unguarded_imports(path.read_text(encoding="utf-8"))
    ]
    assert not bare, (
        "PyYAML/Jinja2 imported at module scope with no guard, so a job whose "
        "image lacks it reports a broken install as a finding:\n  "
        + "\n  ".join(bare) + "\n"
        'Wrap it: try/except ImportError -> print("ERROR: ...", file=sys.stderr) '
        "then raise SystemExit(2) from None."
    )


def test_the_unguarded_import_collector_can_fail():
    """Mutation case: the collector, not only the shipped state."""
    main = 'if __name__ == "__main__":\n    pass\n'
    assert unguarded_imports("import sys\nimport yaml\n" + main) == [2]
    guarded = (
        "import sys\ntry:\n    import yaml\nexcept ImportError:\n"
        '    print("ERROR: PyYAML required", file=sys.stderr)\n'
        "    raise SystemExit(2) from None\n"
    )
    assert unguarded_imports(guarded + main) == []
    # A helper module carries no guard of its own; its caller does.
    assert unguarded_imports("import yaml\n") == []


def test_an_exit_outside_the_dependency_guard_is_not_reported():
    """Mutation case: an indented guard ends with its own block, so a string
    exit later in the function is not read as part of it."""
    source = (
        "def main():\n    try:\n        import yaml\n    except ImportError:\n"
        "        raise SystemExit(2) from None\n"
        '    sys.exit("usage: gate <file>")\n'
    )
    assert offenders(source) == []


def test_a_string_exit_guard_is_reported():
    """Mutation case: the collector, not only the shipped state."""
    bad = (
        "try:\n    import yaml\nexcept ImportError:  # pragma: no cover\n"
        '    sys.exit("PyYAML required")\n'
    )
    assert offenders(bad) == [4]
    raised = (
        "try:\n    import yaml\nexcept (OSError, ModuleNotFoundError):\n"
        '    raise SystemExit(f"{name} required")\n'
    )
    assert offenders(raised) == [4]
    good = (
        "try:\n    import yaml\nexcept ImportError:\n"
        '    print("ERROR: PyYAML required", file=sys.stderr)\n'
        "    raise SystemExit(2) from None\n"
    )
    assert not offenders(good)
