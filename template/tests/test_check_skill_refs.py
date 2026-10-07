"""Coverage for check-skill-refs.py.

The live skill resolves, so it proves nothing about failure: each arm runs
against a fixture repo, and the broken-pointer and vacuous-scan cases must FAIL.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-skill-refs.py"


def run(repo_root: Path, skill_dir: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT), "--repo-root", str(repo_root)]
    if skill_dir is not None:
        cmd += ["--skill-dir", str(skill_dir)]
    return subprocess.run(cmd, capture_output=True, text=True)


def make_tree(tmp_path: Path, skill_text: str, extra: tuple = ()) -> Path:
    """A miniature repo: one real docs file plus whatever the caller names."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "RUNBOOKS.md").write_text("# Runbooks\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check-lib-pins.py").write_text("#\n")
    for path in extra:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#\n")
    skill = tmp_path / ".claude" / "skills" / "cluster-development"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_text)
    return skill


def body(*paths: str) -> str:
    """A skill file citing each path, padded past the vacuity floor."""
    lines = [f"- see `{p}`" for p in paths]
    lines += [f"- see `docs/RUNBOOKS.md` ({i})" for i in range(40)]
    return "\n".join(lines) + "\n"


def test_the_repo_skill_resolves():
    assert run(REPO).returncode == 0


def test_a_resolving_citation_passes(tmp_path):
    skill = make_tree(tmp_path, body("scripts/check-lib-pins.py"))
    assert run(tmp_path, skill).returncode == 0


def test_a_renamed_file_fails(tmp_path):
    skill = make_tree(tmp_path, body("scripts/check-lib-pins-renamed.py"))
    result = run(tmp_path, skill)
    assert result.returncode == 1
    assert "check-lib-pins-renamed.py" in result.stderr


def test_a_references_cross_citation_resolves_against_the_skill_dir(tmp_path):
    skill = make_tree(tmp_path, body("references/debugging.md"))
    (skill / "references" / "debugging.md").write_text("# Debugging\n")
    assert run(tmp_path, skill).returncode == 0


def test_a_placeholder_path_is_not_checked(tmp_path):
    skill = make_tree(tmp_path, body("kubernetes/apps/<app>/release.yaml"))
    assert run(tmp_path, skill).returncode == 0


def test_a_bare_basename_is_not_checked(tmp_path):
    skill = make_tree(tmp_path, body("hosts.yml"))
    assert run(tmp_path, skill).returncode == 0


def test_a_library_doc_is_not_checked(tmp_path):
    skill = make_tree(tmp_path, body("docs/INCLUDE-CONTRACT.md"))
    assert run(tmp_path, skill).returncode == 0


def test_a_scan_that_matches_nothing_fails(tmp_path):
    skill = make_tree(tmp_path, "No backticked paths here at all.\n")
    result = run(tmp_path, skill)
    assert result.returncode == 2
    assert "not seeing" in result.stderr


def test_a_missing_skill_dir_fails(tmp_path):
    make_tree(tmp_path, body("scripts/check-lib-pins.py"))
    result = run(tmp_path, tmp_path / "nowhere")
    assert result.returncode == 2
