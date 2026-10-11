"""The memory-ceiling rule judges containers on memory the kernel cannot reclaim.

Page cache in a working set reads as a container at its ceiling, so RSS is the
metric, a working-set arm is pinned, and an exclusion needs a replacement band.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script, reported

CEILING = "ContainerMemoryNearLimit"
RSS_METRIC = "container_memory_rss"
WORKING_SET_METRIC = "container_memory_working_set_bytes"

_CONTAINER_RE = re.compile(r'container=~?"([^"]+)"')
_UNLESS_RE = re.compile(r"\bunless\b(?P<arm>.*)", re.S)
# A `metric{...}` selector, matched non-greedily so one arm's braces end it.
_SELECTOR_RE = re.compile(r"(?P<metric>[a-z_][a-z0-9_]*)\{(?P<matchers>[^{}]*)\}")


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
    """Container names in every `container=` matcher, alternations expanded."""
    return {
        name
        for match in _CONTAINER_RE.finditer(expr)
        for name in match.group(1).split("|")
        if name
    }


def unpinned_working_set_arms(expr: str) -> list[str]:
    """Working-set selectors that do not pin both a namespace and a container.

    An unpinned one judges every container on its working set, which is the
    page cache the rule exists to stop reading.
    """
    return [
        match.group(0)
        for match in _SELECTOR_RE.finditer(" ".join(expr.split()))
        if match.group("metric") == WORKING_SET_METRIC
        and not ("namespace=" in match.group("matchers") and "container=" in match.group("matchers"))
    ]


def unbanded(expressions: dict[str, str]) -> list[str]:
    """Containers the ceiling rule excludes that no other alert's expr names."""
    arm = _UNLESS_RE.search(expressions[CEILING])
    excluded = containers(arm.group("arm")) if arm else set()
    banded = {
        name
        for alert, expr in expressions.items()
        if alert != CEILING
        for name in containers(expr)
    }
    return sorted(excluded - banded)


@pytest.fixture(scope="module")
def expressions() -> dict[str, str]:
    found = _expressions()
    assert CEILING in found, f"{CEILING} is not in the rule corpus — the gate would be vacuous"
    return found


def test_the_ceiling_rule_reads_rss(expressions):
    """A working-set ratio pages on reclaimable cache, so RSS is the metric."""
    assert RSS_METRIC in expressions[CEILING], (
        f"{CEILING} does not read {RSS_METRIC}: a working-set ratio counts page "
        "cache, which sits at the limit for as long as the container is busy"
    )


def test_every_working_set_arm_names_its_container(expressions):
    """The working-set exception is one named container, never a whole tier."""
    unpinned = unpinned_working_set_arms(expressions[CEILING])
    assert not unpinned, (
        f"{CEILING} holds working-set selectors with no namespace and container "
        f"pin: {unpinned} — they judge every container on its page cache"
    )


def test_an_unpinned_working_set_arm_fails(expressions):
    """Mutation case: widening the arm to a whole namespace must red the gate."""
    widened = expressions[CEILING].replace('container="postgresql"', 'container!=""')
    assert unpinned_working_set_arms(widened), (
        "the pin gate accepts a selector with no container matcher, so it would "
        "not catch a working-set arm widened to a whole namespace"
    )


def test_no_excluded_container_is_left_without_a_band(expressions):
    """An exclusion with no band leaves an OOMKill as the only memory signal."""
    missing = unbanded(expressions)
    assert not missing, (
        f"containers excluded from {CEILING} that no other alert bands: {missing} "
        "— give each one its own rule, or drop the exclusion"
    )


def test_an_exclusion_without_a_band_fails(expressions):
    """Mutation case: an invented exclusion no other alert covers must report."""
    invented = expressions[CEILING] + ' unless on (namespace, container) x{container=~"nothing"}'
    assert unbanded({**expressions, CEILING: invented}) == ["nothing"]
