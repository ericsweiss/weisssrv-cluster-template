"""Every container excluded from the memory-ceiling rule needs a replacement band.

PageCacheWorkloadRSSNearLimit covers on RSS what ContainerMemoryNearLimit
excludes; an exclusion with no band leaves only an OOMKill as a memory signal.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script, reported

WORKING_SET = "ContainerMemoryNearLimit"
RSS_BAND = "PageCacheWorkloadRSSNearLimit"
# The workload whose death blinds every other alert, so it must never sit in an
# exclusion without a band of its own.
MUST_BE_BANDED = "prometheus"

_CONTAINER_RE = re.compile(r'container=~"([^"]+)"')


def _expressions() -> dict[str, str]:
    """{alertname: expr} for the extracted kube-prometheus-stack rules."""
    extract = load_script("extract-prometheus-config.py")
    release = REPO / extract.DEFAULT_RELEASE
    with tempfile.TemporaryDirectory() as scratch:
        rules_file = Path(scratch) / "rules.yaml"
        assert extract.extract_rules(rules_file, release) == 0, (
            f"{reported(release)}: no rule groups extracted"
        )
        doc = yaml.safe_load(rules_file.read_text()) or {}
    return {
        rule["alert"]: rule.get("expr", "")
        for group in doc.get("groups") or []
        for rule in group.get("rules") or []
        if rule.get("alert")
    }


def containers(expr: str) -> set[str]:
    """Container names in every `container=~"a|b"` alternation of an expression."""
    return {
        name
        for match in _CONTAINER_RE.finditer(expr)
        for name in match.group(1).split("|")
        if name
    }


@pytest.fixture(scope="module")
def expressions() -> dict[str, str]:
    found = _expressions()
    for alert in (WORKING_SET, RSS_BAND):
        assert alert in found, f"{alert} is not in the rule corpus — the gate would be vacuous"
    return found


def test_every_excluded_container_has_a_replacement_band(expressions):
    excluded = containers(expressions[WORKING_SET])
    assert excluded, f"{WORKING_SET} excludes nothing — the gate would be vacuous"
    missing = sorted(excluded - containers(expressions[RSS_BAND]))
    assert not missing, (
        f"containers excluded from {WORKING_SET} with no band in {RSS_BAND}: {missing} — "
        "their only memory signal is an OOMKill. Add them to the RSS rule's selector."
    )


def test_prometheus_is_banded(expressions):
    """Prometheus dying is what blinds every other alert, so it is never exempt."""
    assert MUST_BE_BANDED in containers(expressions[RSS_BAND]), (
        f"{MUST_BE_BANDED} has no memory band: it is the one workload whose death "
        "stops every other alert from being evaluated"
    )


def test_dropping_a_container_from_the_band_fails(expressions):
    """Mutation case: a shortened RSS selector must red the coverage assertion."""
    excluded = containers(expressions[WORKING_SET])
    dropped = sorted(excluded)[0]
    assert sorted(excluded - (containers(expressions[RSS_BAND]) - {dropped})) == [dropped]
