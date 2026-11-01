"""Drift guard for the InfoInhibitor exemption in the Alertmanager config.

`equal: [namespace]` treats absent as equal, so an info alert aggregating the
namespace label away is muted unless the `alertname!~` alternation names it.
"""

from __future__ import annotations

import re

import pytest
from conftest import REPO, alert_rules, assert_all_parsed, loki_alert_rules

OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
ALERTMANAGER_YML = OBSERVABILITY / "kube-prometheus-stack" / "alertmanager-config.yaml"

# Info alerts deliverable under `equal: [namespace]` because a warning or
# critical always fires in the same namespace beside them. Every other info
# alert belongs in the InfoInhibitor alternation.
NAMESPACE_CARRYING: set[str] = set()

_MATCHER = re.compile(r'alertname!~"([^"]+)"')

needs_corpus = pytest.mark.skipif(
    not ALERTMANAGER_YML.is_file(), reason="no Alertmanager config in this repository"
)


def info_alertnames(rules: list[tuple[str, dict]]) -> set[str]:
    return {
        name
        for name, rule in rules
        if name and (rule.get("labels") or {}).get("severity") == "info"
    }


def exempted_alertnames(text: str) -> set[str]:
    """Alternation members of the InfoInhibitor rule's `alertname!~` matcher.

    alertmanager.yaml is a Go-templated block scalar, so the inhibit rules are
    read as text rather than parsed.
    """
    starts = [m.start() for m in re.finditer(r"- source_matchers:", text)]
    blocks = [
        text[a:b]
        for a, b in zip(starts, starts[1:] + [len(text)])
        if 'alertname="InfoInhibitor"' in text[a:b]
    ]
    assert len(blocks) == 1, f"expected one InfoInhibitor inhibit rule, found {len(blocks)}"
    found = _MATCHER.search(blocks[0])
    assert found, "the InfoInhibitor rule has no alertname!~ exemption matcher"
    return set(found.group(1).split("|"))


@pytest.fixture(scope="module")
def info_alerts() -> set[str]:
    rules, unreadable = alert_rules()
    assert_all_parsed(unreadable, "info alert")
    names = info_alertnames(rules)
    assert names, "no severity: info alerts found — the gate would be vacuous"
    return names


@pytest.fixture(scope="module")
def exempted() -> set[str]:
    return exempted_alertnames(ALERTMANAGER_YML.read_text(encoding="utf-8"))


@needs_corpus
def test_every_exempted_name_is_a_live_info_alert(info_alerts, exempted):
    """A renamed alert leaves an inert matcher that re-mutes it cluster-wide."""
    stale = sorted(exempted - info_alerts)
    assert not stale, (
        f"InfoInhibitor exempts alertnames no severity: info rule defines: {stale} — "
        "the matcher is inert and those alerts are muted again"
    )


@needs_corpus
def test_every_info_alert_is_exempted_or_carries_a_namespace(info_alerts, exempted):
    """A new namespace-less info alert is muted the moment any namespace fires."""
    unclassified = sorted(info_alerts - exempted - NAMESPACE_CARRYING)
    assert not unclassified, (
        "severity: info alerts neither exempted from InfoInhibitor nor declared "
        f"namespace-carrying: {unclassified} — add the name to the alertname!~ "
        "alternation in alertmanager-config.yaml, or to NAMESPACE_CARRYING in this "
        "file if a warning or critical always fires in the same namespace"
    )


@needs_corpus
def test_namespace_carrying_list_names_live_alerts(info_alerts):
    """A stale entry silently excuses the next namespace-less info alert."""
    stale = sorted(NAMESPACE_CARRYING - info_alerts)
    assert not stale, f"NAMESPACE_CARRYING names alerts that are no longer info: {stale}"


@needs_corpus
def test_dropping_a_member_from_the_alternation_fails(info_alerts, exempted):
    """Mutation case: a shortened alternation must red the coverage assertion."""
    dropped = sorted(exempted)[0]
    mutated = exempted - {dropped}
    assert sorted(info_alerts - mutated - NAMESPACE_CARRYING) == [dropped]


def test_a_loki_shaped_info_alert_reaches_the_coverage_check(tmp_path):
    """Mutation case: a bare `groups:` rule file a Loki configMapGenerator ships,
    in the `key=path` form, reaches the gate's corpus through the walker."""
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "logging.yaml").write_text(
        "groups:\n"
        "  - name: logging\n"
        "    rules:\n"
        "      - alert: SyslogQuiet\n"
        "        labels:\n"
        "          severity: info\n"
    )
    (tmp_path / "kustomization.yaml").write_text(
        "configMapGenerator:\n"
        "  - name: loki-rules\n"
        "    files:\n"
        "      - logging.yaml=rules/logging.yaml\n"
    )
    rules, unreadable = loki_alert_rules(loki=tmp_path)
    assert unreadable == []
    assert info_alertnames(rules) == {"SyslogQuiet"}
