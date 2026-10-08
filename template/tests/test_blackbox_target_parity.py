"""Every probe target an alert names must be a target the exporter probes.

A `probe_success{instance="..."}` selector against an unprobed target matches
nothing: an `absent()` arm fires forever and an exclusion mutes nothing.
"""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import OBSERVABILITY, alert_rules, assert_all_parsed

BLACKBOX = OBSERVABILITY / "exporters" / "blackbox-exporter.yaml"

# Targets an alert may name although the blackbox exporter does not probe them,
# each with the reason. Ships empty; a cluster probing from elsewhere adds here.
EXEMPT: dict[str, str] = {}

_PROBE_SELECTOR = re.compile(r"probe_\w*\s*\{([^}]*)\}")
_INSTANCE_MATCHER = re.compile(r'instance\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
# A matcher value that is one plain target rather than a real pattern. Backslash
# escapes are dropped first, so `1\.1\.1\.1` reads as the address it means.
_PLAIN_TARGET = re.compile(r"^[\w.:/${}-]+$")

needs_corpus = pytest.mark.skipif(
    not BLACKBOX.is_file(), reason="this repository ships no blackbox exporter"
)


def plain_target(value: str) -> str | None:
    """The one target a matcher value names, or None when it is a pattern."""
    plain = value.replace("\\", "")
    return plain if _PLAIN_TARGET.match(plain) else None


def named_targets(exprs: dict[str, str]) -> dict[str, set[str]]:
    """target -> the alerts whose probe selectors name it."""
    found: dict[str, set[str]] = {}
    for alert, expr in exprs.items():
        text = " ".join(str(expr).split())
        for body in _PROBE_SELECTOR.findall(text):
            for _operator, value in _INSTANCE_MATCHER.findall(body):
                target = plain_target(value)
                if target:
                    found.setdefault(target, set()).add(alert)
    return found


def probed_targets(release: dict) -> set[str]:
    """The `url` of every serviceMonitor target, which is the `instance` label
    Prometheus writes for that probe."""
    monitor = ((release.get("spec") or {}).get("values") or {}).get("serviceMonitor") or {}
    return {str(target["url"]) for target in monitor.get("targets") or [] if target.get("url")}


@pytest.fixture(scope="module")
def probed() -> set[str]:
    found = probed_targets(yaml.safe_load(BLACKBOX.read_text()) or {})
    assert found, (
        f"{BLACKBOX.name} declares no serviceMonitor targets — this gate is vacuous"
    )
    return found


@pytest.fixture(scope="module")
def named() -> dict[str, set[str]]:
    rules, unreadable = alert_rules()
    assert_all_parsed(unreadable, "probe alert")
    assert rules, "the corpus holds no alerts — the gate would be vacuous"
    found = named_targets({alert: rule.get("expr", "") for alert, rule in rules})
    assert found, "no alert names a probe target — the collector reads nothing"
    return found


@needs_corpus
def test_every_named_probe_target_is_probed(named, probed):
    """An alert selecting a target nothing probes reads as a permanent fault."""
    stray = sorted(
        f"{target} (named by {', '.join(sorted(alerts))})"
        for target, alerts in named.items()
        if target not in probed and target not in EXEMPT
    )
    assert not stray, (
        "alerts naming probe targets the blackbox exporter does not probe:\n  "
        + "\n  ".join(stray)
        + "\n\nAdd the target to serviceMonitor.targets in "
        f"{BLACKBOX.name}, or add it to EXEMPT with the prober that covers it."
    )


@needs_corpus
def test_every_exemption_is_still_named(named):
    """A stale exemption silently excuses the next unprobed target."""
    stale = sorted(target for target in EXEMPT if target not in named)
    assert not stale, f"EXEMPT names targets no alert selects: {stale} — drop them"


def test_the_collector_reads_exact_and_escaped_matchers():
    """Mutation case: both matcher spellings the rule tree uses are collected,
    a real pattern is not, and a non-probe metric is out of scope."""
    assert named_targets({"A": 'absent(probe_success{instance="https://auth.example"})'}) == {
        "https://auth.example": {"A"}
    }
    assert named_targets({"B": 'probe_success{instance!~"1\\.1\\.1\\.1"} == 0'}) == {
        "1.1.1.1": {"B"}
    }
    assert named_targets({"C": 'probe_success{instance=~"node-.*"} == 0'}) == {}
    assert named_targets({"D": 'up{instance="https://auth.example"} == 0'}) == {}


def test_the_target_collector_reads_the_release_shape():
    """Mutation case: a target that loses its url drops out, so the gate cannot
    certify a probe the exporter stopped making."""
    release = yaml.safe_load(
        "spec:\n"
        "  values:\n"
        "    serviceMonitor:\n"
        "      targets:\n"
        "        - name: sso\n"
        "          url: https://auth.example\n"
        "        - name: witness\n"
    )
    assert probed_targets(release) == {"https://auth.example"}
    assert probed_targets({}) == set()
