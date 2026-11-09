"""scripts/flux-child-kustomizations.py over this cluster's own stages.

deploy-verify.sh dry-runs exactly these stages, so an empty list passes on
nothing. weisssrv-lib tests the script's own arms.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO, load_script

tool = load_script("flux-child-kustomizations.py")

CLUSTERS = REPO / "kubernetes" / "clusters"

needs_cluster = pytest.mark.skipif(
    not CLUSTERS.is_dir(), reason="this repository ships no kubernetes/clusters tree"
)


@needs_cluster
def test_this_cluster_yields_a_path_for_every_stage(capsys) -> None:
    """The shipped tree, not a fixture: the corpus the render jobs actually get."""
    directories = sorted(p for p in CLUSTERS.iterdir() if p.is_dir())
    assert len(directories) == 1, f"expected one cluster directory, got {directories}"
    assert tool.main(["--dir", str(directories[0]), "--paths"]) == 0
    paths = [line.split("\t")[1] for line in capsys.readouterr().out.splitlines()]
    assert paths, "deploy-verify.sh would dry-run nothing"
    stray = [p for p in paths if not p.startswith("./kubernetes/")]
    assert not stray, f"spec.path outside the manifest tree: {stray}"


FLUX_TASKFILE = REPO / "taskfiles" / "flux.yml"
ENUMERATOR = "--child-kustomizations scripts/flux-child-kustomizations.py"


def walks_dependson_order(task: dict) -> bool:
    """True when the task's flux-lint.sh call passes the enumerator."""
    cmds = [" ".join(c.split()) for c in task.get("cmds") or [] if isinstance(c, str)]
    return any("scripts/flux-lint.sh" in c and ENUMERATOR in c for c in cmds)


def test_flux_lint_renders_the_stages_in_dependson_order() -> None:
    """Without the flag flux-lint.sh walks the stages in directory-scan order."""
    tasks = yaml.safe_load(FLUX_TASKFILE.read_text(encoding="utf-8"))["tasks"]
    assert walks_dependson_order(tasks["lint"]), f"flux:lint never passes {ENUMERATOR}"


def test_a_flux_lint_call_without_the_enumerator_is_reported() -> None:
    """Mutation case: the collector, not just the shipped state."""
    call = "scripts/flux-lint.sh \\\n  --render-script scripts/flux-env.sh"
    assert not walks_dependson_order({"cmds": [call]})
    assert walks_dependson_order({"cmds": [f"{call} \\\n  {ENUMERATOR}"]})
