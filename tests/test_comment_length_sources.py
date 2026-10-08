"""The comment-length gate holds the line over `copier.yml` and `partials/`.

`task lint:comment-length` runs only inside a render, which contains neither
path. Each partial is staged as `<stem>.j2`, the suffix whose markers cover it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "template" / "scripts" / "check-comment-length.py"
SCANNED = re.compile(r"(\d+) file\(s\) scanned")


def _stage(destination: Path) -> list:
    """Copy this repository's unrendered sources under names the gate scans."""
    staged = [shutil.copy(REPO_ROOT / "copier.yml", destination / "copier.yml")]
    for partial in sorted((REPO_ROOT / "partials").glob("*.jinja")):
        staged.append(shutil.copy(partial, destination / f"{partial.stem}.j2"))
    return [Path(path) for path in staged]


def _run(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *(str(path) for path in paths)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_copier_yml_and_the_partials_are_within_the_limit(tmp_path):
    """The house rule in AGENTS.md names these two paths, and this is the gate
    behind that claim."""
    staged = _stage(tmp_path)
    result = _run(*staged)
    assert result.returncode == 0, result.stdout + result.stderr
    scanned = SCANNED.search(result.stdout)
    assert scanned, result.stdout
    assert int(scanned.group(1)) == len(staged) > 1, result.stdout


def test_an_over_long_block_in_a_partial_fails(tmp_path):
    """A gate that scanned nothing would pass just as quietly, so prove the
    staged names are ones it reads and rejects."""
    staged = _stage(tmp_path)
    partial = next(path for path in staged if path.suffix == ".j2")
    body = "\n".join(
        f"   line {number} of a block well over the limit" for number in range(5)
    )
    partial.write_text(f"{{#\n{body}\n#}}\n{partial.read_text()}", encoding="utf-8")
    result = _run(*staged)
    assert result.returncode == 1, result.stdout + result.stderr
    assert partial.name in result.stdout, result.stdout


def test_every_partial_is_staged():
    """A partial added with a suffix other than `.jinja` would otherwise drop
    out of the staging glob and go ungated."""
    present = {path.name for path in (REPO_ROOT / "partials").iterdir() if path.is_file()}
    assert present, "partials/ is empty"
    unstaged = {name for name in present if not name.endswith(".jinja")}
    assert not unstaged, f"partials the gate never sees: {sorted(unstaged)}"
