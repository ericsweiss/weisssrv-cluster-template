"""The upstream-rule mirror gate must fail when a copy falls behind its chart.

Covers both subjects: the in-tree replacements for the alerts the chart's
`defaultRules.disabled` list turns off, and the supplementary promtool mirrors.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO, load_script

GATE = REPO / "scripts" / "check-upstream-rule-mirror.py"

gate = load_script(GATE)

VARS = 'helm_chart_versions:\n  kube_prometheus_stack: "91.3.0"\n'
RELEASE_HEAD = (
    "apiVersion: helm.toolkit.fluxcd.io/v2\n"
    "kind: HelmRelease\n"
    "spec:\n"
    "  values:\n"
    "    defaultRules:\n"
    "      disabled:\n"
    "        AlertmanagerClusterFailedToSendAlerts: true\n"
)


def _release(marker: str | None, alert: str = "AlertmanagerClusterFailedToSendAlerts") -> str:
    """The chart release with its in-tree replacement rule, marked or not."""
    header = f"              # {marker}\n" if marker else ""
    return (
        RELEASE_HEAD + "    additionalPrometheusRulesMap:\n"
        "      platform-monitoring:\n"
        "        groups:\n"
        "          - name: platform.monitoring\n"
        "            rules:\n"
        f"{header}"
        f"              - alert: {alert}\n"
        "                expr: vector(1) > 0\n"
    )


def _tree(
    root: Path,
    vars_text: str = VARS,
    release_text: str | None = None,
    mirror_text: str | None = None,
) -> Path:
    (root / "ansible/inventories/prod/group_vars").mkdir(parents=True)
    (root / "ansible/inventories/prod/group_vars/all.yml").write_text(vars_text)
    (root / gate.MIRROR_DIR).mkdir(parents=True)
    if mirror_text is not None:
        (root / gate.MIRROR_DIR / "upstream-etcd.rules.yaml").write_text(mirror_text)
    release = root / gate.CHART_RELEASE
    release.parent.mkdir(parents=True)
    release.write_text(
        release_text
        if release_text is not None
        else _release("Taken from kube-prometheus-stack 91.3.0")
    )
    return root


def test_an_in_tree_copy_at_the_pinned_version_passes(tmp_path):
    assert gate.check(_tree(tmp_path)) == []


def test_an_in_tree_copy_with_no_marker_fails(tmp_path):
    """Mutation case: the local copy of a disabled upstream alert, unmarked."""
    problems = gate.check(_tree(tmp_path, release_text=_release(None)))
    assert problems and "AlertmanagerClusterFailedToSendAlerts" in problems[0]
    assert "no `# Taken from" in problems[0]


def test_a_chart_bump_leaving_the_copy_behind_fails(tmp_path):
    """Mutation case: the pin moves, the marker does not."""
    root = _tree(tmp_path, vars_text='helm_chart_versions:\n  kube_prometheus_stack: "92.0.0"\n')
    problems = gate.check(root)
    assert problems and "re-take the expr" in problems[0]


def test_an_unknown_chart_name_fails_rather_than_passing_silently(tmp_path):
    root = _tree(tmp_path, release_text=_release("Taken from some-other-chart 1.0.0"))
    problems = gate.check(root)
    assert problems and "CHART_KEYS" in problems[0]


def test_a_rule_the_chart_still_ships_needs_no_marker(tmp_path):
    """Only the alerts `defaultRules.disabled` turns off are mirrors."""
    release = _release("Taken from kube-prometheus-stack 91.3.0") + (
        "              - alert: DiskUsageWarning\n                expr: vector(1) > 0\n"
    )
    assert gate.check(_tree(tmp_path, release_text=release)) == []


def test_a_disabled_alert_with_no_replacement_anywhere_fails(tmp_path):
    """Mutation case: deleting the mirrored rule while the chart still disables
    it drops the alert, so every required name must be accounted for."""
    problems = gate.check(_tree(tmp_path, release_text=RELEASE_HEAD))
    assert len(problems) == 1
    assert "AlertmanagerClusterFailedToSendAlerts" in problems[0]
    assert "NOT_MIRRORED" in problems[0]
    assert gate.RULE_TREE in problems[0]


def test_an_exempt_disabled_alert_needs_no_replacement(tmp_path, monkeypatch):
    """The NOT_MIRRORED escape hatch still covers a deliberate non-mirror."""
    monkeypatch.setitem(gate.NOT_MIRRORED, "KubeletDown", "replaced by a node-level probe")
    release = _release("Taken from kube-prometheus-stack 91.3.0").replace(
        "        AlertmanagerClusterFailedToSendAlerts: true\n",
        "        AlertmanagerClusterFailedToSendAlerts: true\n        KubeletDown: true\n",
    )
    assert gate.check(_tree(tmp_path, release_text=release)) == []


def test_a_supplementary_mirror_with_no_taken_from_line_fails(tmp_path):
    problems = gate.check(_tree(tmp_path, mirror_text="---\ngroups: []\n"))
    assert problems and "Taken from" in problems[0]


def test_a_supplementary_mirror_at_the_pin_passes(tmp_path):
    mirror = "---\n# Taken from kube-prometheus-stack 91.3.0\ngroups: []\n"
    assert gate.check(_tree(tmp_path, mirror_text=mirror)) == []


def test_a_release_that_disables_nothing_is_a_could_not_inspect_error(tmp_path):
    root = _tree(tmp_path, release_text="spec:\n  values: {}\n")
    with pytest.raises(gate.OperatorError) as excinfo:
        gate.check(root)
    assert "vacuously" in str(excinfo.value)


def test_a_missing_inventory_is_a_could_not_inspect_error(tmp_path):
    with pytest.raises(gate.OperatorError) as excinfo:
        gate.check(tmp_path)
    assert "could not be read" in str(excinfo.value)


def test_an_operator_error_exits_2_not_1(tmp_path):
    """Mutation case: exit 1 is a drifted mirror, so a broken input must not
    share it — the reader would go looking for a stale expr that is not there."""
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_an_unparseable_release_exits_2(tmp_path):
    root = _tree(tmp_path, release_text="spec: [unclosed\n")
    assert gate.main(["--repo-root", str(root)]) == 2


def test_a_yml_rule_file_is_checked_too(tmp_path):
    """Mutation case for the glob: kustomize renders either suffix, so an
    unmarked copy in a `.yml` file must not be invisible to the walk."""
    root = _tree(tmp_path)
    (root / gate.RULE_TREE / "rules" / "extra.yml").parent.mkdir(
        parents=True, exist_ok=True
    )
    (root / gate.RULE_TREE / "rules" / "extra.yml").write_text(
        "spec:\n  groups:\n    - name: extra\n      rules:\n"
        "        - alert: AlertmanagerClusterFailedToSendAlerts\n"
        "          expr: vector(1) > 0\n",
        encoding="utf-8",
    )
    assert any("no `# Taken from" in p for p in gate.check(root))


def test_the_live_repo_is_in_step():
    run = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True, cwd=REPO)
    assert run.returncode == 0, run.stdout + run.stderr
