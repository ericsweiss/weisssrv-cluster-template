"""The doc-link and lib-pin gates pass over this repository's own tree.

Both are exercised inside a render, which says nothing about the Markdown and
the include refs here. Behavioural and mutation cases live with each gate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from render_cluster import load_ci

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )


def test_this_repos_own_doc_links_resolve():
    """The pipeline self-applies this gate; `pytest tests` is where a contributor
    sees a renamed doc before pushing."""
    result = _run("scripts/check-doc-links.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_this_repos_own_lib_pins_match_the_single_source():
    """No job runs this gate over this repository. The include refs are literals
    GitLab will not interpolate, so a bump without --fix leaves a stale pin."""
    project = load_ci(REPO_ROOT / ".gitlab-ci.yml")["variables"]["LIB_PROJECT"]
    result = _run("scripts/check-lib-pins.py", "--project", project)
    assert result.returncode == 0, result.stdout + result.stderr
