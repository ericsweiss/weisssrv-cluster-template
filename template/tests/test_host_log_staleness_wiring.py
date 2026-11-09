"""Site wiring for the generated host log-shipping rules. The vendored generator
is covered by weisssrv-lib's suite; this cluster owns the runbook it passes and
the placeholder file it ships."""
from __future__ import annotations

import re

from conftest import REPO, load_script

gen = load_script("generate-host-log-staleness.py")

# Every caller names the runbook the same way, so one regex reads them all.
_RUNBOOK_FLAG = re.compile(r'generate-host-log-staleness\.py[^\n]*?--runbook\s+"([^"]+)"')
_CALLERS = (".gitlab-ci.yml", "taskfiles/flux.yml", "taskfiles/lint.yml")


def _wired_runbooks() -> dict[str, set[str]]:
    found = {}
    for caller in _CALLERS:
        path = REPO / caller
        if path.is_file():
            found[caller] = set(_RUNBOOK_FLAG.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_caller_passes_one_runbook() -> None:
    wired = _wired_runbooks()
    assert wired.get(".gitlab-ci.yml"), "the pipeline never runs the generator with --runbook"
    assert wired.get("taskfiles/flux.yml"), "the sync task never runs the generator with --runbook"
    assert wired.get("taskfiles/lint.yml"), "lint:repo-sync never runs the generator with --runbook"
    runbooks = set().union(*wired.values())
    assert len(runbooks) == 1, f"the callers disagree on the runbook: {wired}"


def test_the_shipped_file_carries_the_cluster_wide_arm() -> None:
    """The cluster-wide arm ships with the callers' runbook, whose anchor
    test_runbook_coverage resolves; `task flux:sync-host-log-staleness` adds the
    per-host rules, so `--check` is red until that first run, not vacuous."""
    shipped = REPO / gen.OUTPUT
    assert shipped.is_file(), f"the repository ships no {gen.OUTPUT}"
    text = shipped.read_text(encoding="utf-8")
    assert "HostLogShippingStaleAll" in text
    (runbook,) = set().union(*_wired_runbooks().values())
    assert f"${{cluster_runbook_base_url}}/{runbook}" in text


def test_the_play_excludes_only_declared_groups(capsys) -> None:
    """The plays exclude the runtime ledger groups; the inventory declares them
    empty so the generator resolves every exclusion without a warning."""
    assert gen.role_hosts(REPO)
    assert "warning" not in capsys.readouterr().err
