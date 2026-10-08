"""Every gate under scripts/ keeps the exit contract scripts/README.md states.

0 clean, 1 a finding, 2 the gate could not inspect its subject. Exiting 1 on a
missing dependency makes the corpus runner report an operator error as a finding.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
MANIFEST = SCRIPTS / "vendored-manifest.yml"

_GUARD = re.compile(r"^except[^:]*\b(?:ImportError|ModuleNotFoundError)\b[^:]*:")
_STRING_EXIT = re.compile(r"\bsys\.exit\(\s*[\"']")


def vendored_names() -> set[str]:
    """Basenames of the library copies. Their guards are fixed upstream."""
    entries = (yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}).get(
        "vendored"
    ) or []
    names = set()
    for entry in entries:
        path = entry if isinstance(entry, str) else (entry or {}).get("consumer", "")
        names.add(Path(str(path)).name)
    return names


def offenders(source: str) -> list[int]:
    """1-indexed lines where a dependency guard exits with a string, so rc 1."""
    lines = source.splitlines()
    found: list[int] = []
    for index, line in enumerate(lines):
        if not _GUARD.match(line.strip()):
            continue
        for offset in range(index + 1, len(lines)):
            body = lines[offset]
            if body.strip() and not body.startswith((" ", "\t")):
                break
            if _STRING_EXIT.search(body):
                found.append(offset + 1)
    return found


def test_every_local_gate_exits_2_on_a_missing_dependency():
    vendored = vendored_names()
    scripts = [p for p in sorted(SCRIPTS.rglob("*.py")) if p.name not in vendored]
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
    vendored = vendored_names()
    scripts = [p for p in sorted(SCRIPTS.rglob("*.py")) if p.name not in vendored]
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


def test_a_string_exit_guard_is_reported():
    """Mutation case: the collector, not only the shipped state."""
    bad = (
        "try:\n    import yaml\nexcept ImportError:  # pragma: no cover\n"
        '    sys.exit("PyYAML required")\n'
    )
    assert offenders(bad) == [4]
    good = (
        "try:\n    import yaml\nexcept ImportError:\n"
        '    print("ERROR: PyYAML required", file=sys.stderr)\n'
        "    raise SystemExit(2) from None\n"
    )
    assert not offenders(good)
