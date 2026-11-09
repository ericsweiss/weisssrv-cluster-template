"""Site wiring for the vendored check-skill-refs.py.

weisssrv-lib's suite covers the gate's arms. This cluster owns the flags it
passes and the skill whose cited paths must resolve in every render.
"""
from __future__ import annotations

from conftest import REPO, load_script, wired_gate_argv

gate = load_script("check-skill-refs.py")


def test_every_path_the_skill_cites_resolves() -> None:
    argv = wired_gate_argv("check-skill-refs.py", "skill-refs", "skill-refs")
    assert gate.main([*argv, "--repo-root", str(REPO)]) == 0
