"""No site domain literal in the shell this repo runs.

The corpus is `.sh`/`.py` under scripts/ plus the Taskfile tree, whose inline
shell no other literal gate reads. Domains come from cluster-config.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
CLUSTER_CONFIG = REPO / "kubernetes" / "infrastructure" / "sources" / "cluster-config.yaml"
DOMAIN_KEYS = ("cluster_internal_domain", "cluster_external_domain")

# Files that cannot read cluster-config yet, each with the reason. Keys are
# paths relative to the repository root ("scripts/foo.sh"), so an entry waives
# one file and never a same-named sibling.
EXEMPT: dict[str, str] = {
    "Taskfile.yml": (
        "the forge host and the library clone URL, which are their own answers "
        "rather than cluster-config values"
    ),
}

needs_cluster_config = pytest.mark.skipif(
    not CLUSTER_CONFIG.is_file(), reason="no cluster-config.yaml to read the domains from"
)


def site_domains() -> list[str]:
    data = (yaml.safe_load(CLUSTER_CONFIG.read_text()) or {}).get("data") or {}
    return sorted({str(data[key]) for key in DOMAIN_KEYS if data.get(key)})


def domain_re(domains: list[str]) -> re.Pattern[str]:
    """One pattern matching any of the domains, or a sub-domain of them."""
    assert domains, "cluster-config names no site domain — this gate would match nothing"
    alternatives = "|".join(re.escape(domain) for domain in domains)
    return re.compile(rf"(?<![\w.-])(?:[\w-]+\.)*(?:{alternatives})(?![\w-])")


SCRIPT_SUFFIXES = {".sh", ".py"}


def targets(root: Path) -> list[Path]:
    """Every file the gate reads: scripts under scripts/, plus the Taskfile tree."""
    found = sorted(
        p
        for p in (root / "scripts").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in SCRIPT_SUFFIXES
    )
    found += sorted((root / "taskfiles").glob("*.yml"))
    if (root / "Taskfile.yml").is_file():
        found.append(root / "Taskfile.yml")
    return found


def offenders(root: Path, pattern: re.Pattern[str], exempt: set[str]) -> list[str]:
    """`path:line` for every domain literal in a non-exempt file under `root`.

    `exempt` holds paths relative to `root`, so a waiver names one file.
    """
    found = []
    for path in targets(root):
        rel = path.relative_to(root).as_posix()
        if rel in exempt:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                found.append(f"{rel}:{number}")
    return found


@needs_cluster_config
def test_the_whole_corpus_is_present_to_scan():
    """A half that stopped matching would narrow the gate to the other one
    without failing."""
    for suffix in sorted(SCRIPT_SUFFIXES):
        assert list(SCRIPTS.rglob(f"*{suffix}")), f"scripts/ holds no {suffix} scripts to scan"
    reached = {path.relative_to(REPO).as_posix() for path in targets(REPO)}
    assert "Taskfile.yml" in reached, "the root Taskfile is outside the scan"
    assert any(rel.startswith("taskfiles/") for rel in reached), (
        "the taskfiles/ tree is outside the scan"
    )


@needs_cluster_config
def test_no_unexempted_script_spells_a_site_domain():
    found = offenders(REPO, domain_re(site_domains()), set(EXEMPT))
    assert not found, (
        f"site domain literals in {found} — read the value out of cluster-config "
        "instead, or add the script's repo-relative path to EXEMPT with the "
        "reason it cannot be converted yet."
    )


@needs_cluster_config
def test_every_exemption_is_still_needed():
    """An exemption outliving its literal keeps the next one invisible."""
    pattern = domain_re(site_domains())
    shipped = {key: REPO / key for key in EXEMPT}
    stale = sorted(
        key
        for key, path in shipped.items()
        if path.is_file() and not pattern.search(path.read_text())
    )
    assert not stale, f"these scripts no longer carry a literal: {stale} — drop them"

    missing = sorted(key for key, path in shipped.items() if not path.is_file())
    assert not missing, (
        f"EXEMPT names paths no script resolves to: {missing} — keys are relative "
        "to the repository root, so a bare basename never matches"
    )


@needs_cluster_config
def test_every_exemption_names_a_scanned_file():
    """A key the collector never reaches waives nothing, so the literal it was
    written for goes unreported."""
    reached = {path.relative_to(REPO).as_posix() for path in targets(REPO)}
    stray = sorted(key for key in EXEMPT if key not in reached)
    assert not stray, (
        f"EXEMPT names paths outside the scan: {stray} — keys are relative to the "
        "repository root, so a bare basename never matches"
    )


def test_the_collector_reports_a_new_literal(tmp_path):
    """Mutation case: the gate must fail on the file it is meant to catch."""
    pattern = domain_re(["example.test"])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "clean.sh").write_text('curl "https://${DOMAIN}/health"\n')
    (tmp_path / "scripts" / "drifted.sh").write_text(
        'curl "https://git.example.test/health"\n'
    )
    assert offenders(tmp_path, pattern, set()) == ["scripts/drifted.sh:1"]
    assert offenders(tmp_path, pattern, {"scripts/drifted.sh"}) == []
    # A waiver names one path: a sibling of the same name stays in scope.
    (tmp_path / "scripts" / "lib").mkdir()
    (tmp_path / "scripts" / "lib" / "drifted.sh").write_text(
        'curl "https://git.example.test/x"\n'
    )
    assert offenders(tmp_path, pattern, {"scripts/drifted.sh"}) == [
        "scripts/lib/drifted.sh:1"
    ]


def test_the_collector_reaches_python_gates_and_subdirectories(tmp_path):
    """Mutation case for the recursive, two-suffix walk: a `.py` gate and a
    helper in a subdirectory are both in scope, and caches are not."""
    pattern = domain_re(["example.test"])
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gate.py").write_text('BASE = "https://api.example.test"\n')
    (scripts / "lib").mkdir()
    (scripts / "lib" / "helper.sh").write_text('printf "%s\\n" "${DOMAIN}"\n')
    (scripts / "lib" / "drifted.py").write_text('HOST = "git.example.test"\n')
    (scripts / "__pycache__").mkdir()
    (scripts / "__pycache__" / "gate.py").write_text('BASE = "https://api.example.test"\n')
    assert offenders(tmp_path, pattern, set()) == [
        "scripts/gate.py:1",
        "scripts/lib/drifted.py:1",
    ]
    assert offenders(tmp_path, pattern, {"scripts/gate.py", "scripts/lib/drifted.py"}) == []


def test_the_collector_reaches_the_taskfile_tree(tmp_path):
    """Mutation case for the widening: inline shell in the Taskfile tree is in
    scope, and the root Taskfile is reported under its own name."""
    pattern = domain_re(["example.test"])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "taskfiles").mkdir()
    (tmp_path / "taskfiles" / "clean.yml").write_text("  - echo \"${DOMAIN}\"\n")
    (tmp_path / "taskfiles" / "drifted.yml").write_text("  - echo home.example.test\n")
    (tmp_path / "Taskfile.yml").write_text("vars:\n  GIT_HOST: forge.example.test\n")
    assert offenders(tmp_path, pattern, set()) == [
        "taskfiles/drifted.yml:1",
        "Taskfile.yml:2",
    ]
    assert offenders(tmp_path, pattern, {"taskfiles/drifted.yml", "Taskfile.yml"}) == []
