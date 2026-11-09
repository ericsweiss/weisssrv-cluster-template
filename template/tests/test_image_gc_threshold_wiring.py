"""Site wiring for the vendored check-image-gc-threshold.py.

weisssrv-lib's suite covers the gate's arms. This cluster owns the flags it
passes, the kubelet watermark in group_vars/k3s.yml and the alert that tracks it.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from conftest import REPO, load_script, wired_gate_argv

gate = load_script("check-image-gc-threshold.py")

SCRIPT = "check-image-gc-threshold.py"


def argv() -> list[str]:
    return wired_gate_argv(SCRIPT, "image-gc-threshold", "image-gc-threshold")


def test_the_live_tree_keeps_the_alert_above_the_watermark() -> None:
    assert gate.main([*argv(), "--repo-root", str(REPO)]) == 0


def test_a_moved_watermark_fails(tmp_path: Path) -> None:
    """Mutation case: the gate reads this cluster's watermark, not a constant."""
    flags = argv()
    rules = Path(flags[flags.index("--rules") + 1])
    for rel in (gate.K3S_VARS, gate.ALERTMANAGER, rules):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, tmp_path / rel)
    k3s = tmp_path / gate.K3S_VARS
    text, count = re.subn(
        r"image-gc-high-threshold=(\d+)",
        lambda m: f"image-gc-high-threshold={int(m.group(1)) - 10}",
        k3s.read_text(encoding="utf-8"),
    )
    assert count == 1
    k3s.write_text(text, encoding="utf-8")
    assert gate.main([*flags, "--repo-root", str(tmp_path)]) == 1
