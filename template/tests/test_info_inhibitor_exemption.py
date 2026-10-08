"""Drift guard for the InfoInhibitor exemption in the Alertmanager config.

`equal: [namespace]` treats absent as equal, so an info alert aggregating the
namespace label away is muted unless the `alertname!~` alternation names it.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO, assert_all_parsed, k8s_documents, load_script, reported

OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
APPS = REPO / "kubernetes" / "apps"
ALERTMANAGER_YML = OBSERVABILITY / "kube-prometheus-stack" / "alertmanager-config.yaml"

# Info alerts deliverable under `equal: [namespace]` because a warning or
# critical always fires in the same namespace beside them. Every other info
# alert belongs in the InfoInhibitor alternation.
NAMESPACE_CARRYING: set[str] = set()

_MATCHER = re.compile(r'alertname!~"([^"]+)"')

needs_corpus = pytest.mark.skipif(
    not ALERTMANAGER_YML.is_file(), reason="no Alertmanager config in this repository"
)


def _release_rules() -> tuple[list[dict], list[str]]:
    """The rules inside the kube-prometheus-stack HelmRelease's values.

    The release path is absolute: the extractor's default is relative to the
    working directory, so a caller running from elsewhere would extract nothing.
    """
    extract = load_script("extract-prometheus-config.py")
    release = REPO / extract.DEFAULT_RELEASE
    with tempfile.TemporaryDirectory() as scratch:
        rules_file = Path(scratch) / "rules.yaml"
        if extract.extract_rules(rules_file, release) != 0:
            return [], [f"{reported(release)}: no rule groups extracted"]
        doc = yaml.safe_load(rules_file.read_text()) or {}
    return [rule for group in doc.get("groups") or [] for rule in group.get("rules") or []], []


def _prometheusrule_rules() -> tuple[list[dict], list[str]]:
    """The rules any PrometheusRule CR declares, platform or app."""
    found: list[dict] = []
    unreadable: list[str] = []
    for tree in (OBSERVABILITY, APPS):
        if not tree.is_dir():
            continue
        docs, failed = k8s_documents(tree)
        unreadable += failed
        for _, doc in docs:
            if doc.get("kind") != "PrometheusRule":
                continue
            for group in (doc.get("spec") or {}).get("groups") or []:
                found += group.get("rules") or []
    return found, unreadable


def _loki_rules() -> tuple[list[dict], list[str]]:
    """The LogQL rules the Loki ruler evaluates. They reach the same
    Alertmanager, so the alternation has to cover them too."""
    found: list[dict] = []
    unreadable: list[str] = []
    loki = OBSERVABILITY / "loki"
    kustomization = loki / "kustomization.yaml"
    if not kustomization.is_file():
        return found, unreadable
    try:
        config = yaml.safe_load(kustomization.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return found, [f"{reported(kustomization)}: {exc}"]
    for entry in config.get("configMapGenerator") or []:
        for name in entry.get("files") or []:
            path = loki / name.rsplit("=", 1)[-1]
            if not path.is_file():
                unreadable.append(
                    f"{reported(path)}: named by {reported(kustomization)} but not shipped, "
                    "so its alerts went unchecked"
                )
                continue
            try:
                doc = yaml.safe_load(path.read_text()) or {}
            except (OSError, yaml.YAMLError) as exc:
                unreadable.append(f"{reported(path)}: {exc}")
                continue
            for group in doc.get("groups") or []:
                found += group.get("rules") or []
    return found, unreadable


def info_alertnames(rules: list[dict]) -> set[str]:
    return {
        rule["alert"]
        for rule in rules
        if rule.get("alert") and (rule.get("labels") or {}).get("severity") == "info"
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
    rules: list[dict] = []
    unreadable: list[str] = []
    for walker in (_release_rules, _prometheusrule_rules, _loki_rules):
        walked, failed = walker()
        rules += walked
        unreadable += failed
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


def test_a_loki_shaped_info_alert_reaches_the_coverage_check():
    """Mutation case: a bare `groups:` rule file must reach the gate's corpus."""
    doc = yaml.safe_load(
        "groups:\n"
        "  - name: logging\n"
        "    rules:\n"
        "      - alert: SyslogQuiet\n"
        "        labels:\n"
        "          severity: info\n"
    )
    rules = [rule for group in doc["groups"] for rule in group["rules"]]
    assert info_alertnames(rules) == {"SyslogQuiet"}
