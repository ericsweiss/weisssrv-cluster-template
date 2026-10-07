"""Every address an alert pins must be one the cluster actually serves.

An exact `instance="10.0.0.5"` matcher is the one site literal no other gate
reads: after a renumber it matches nothing and the alert reads healthy forever.
"""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import REPO, alert_rules, assert_all_parsed, load_script

INVENTORY = REPO / "ansible" / "inventories" / "prod" / "hosts.yml"
CLUSTER_CONFIG = REPO / "kubernetes" / "infrastructure" / "sources" / "cluster-config.yaml"

# Addresses an alert pins that no inventory host and no cluster-config value
# serves: an upstream resolver, a gateway peer, network gear the cluster probes
# but Ansible never manages. Add your own gateway and switch probes here.
NON_INVENTORY_TARGETS: dict[str, str] = {
    "1.1.1.1": (
        "the outside-in WAN witness the blackbox exporter probes; a public "
        "resolver, not a host this cluster serves"
    ),
}

_EXACT = re.compile(r'instance="([^"]+)"')
_EXCLUDED = re.compile(r'instance!~"([^"]+)"')
_QUAD = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

needs_inventory = pytest.mark.skipif(
    not INVENTORY.is_file() or not CLUSTER_CONFIG.is_file(),
    reason="no inventory or cluster-config to cross-check against",
)


def alternatives(pattern: str) -> set[str]:
    """Every string a `(a|b)`-style regex alternation can match."""
    group = re.search(r"\(([^()]*)\)", pattern)
    if not group:
        return set(pattern.split("|"))
    head, tail = pattern[: group.start()], pattern[group.end() :]
    found: set[str] = set()
    for choice in group.group(1).split("|"):
        found |= alternatives(head + choice + tail)
    return found


def unescape(value: str) -> str:
    """A YAML scalar spells PromQL's `\\.` as `\\\\.`, so both forms arrive here."""
    return value.replace("\\\\", "\\").replace("\\.", ".")


def pinned_addresses(exprs: dict[str, str]) -> dict[str, set[str]]:
    """address -> the alerts pinning it, over exact and excluded dotted quads.

    An `instance!~` exclusion pins the same addresses: a renumber leaves it
    excluding something nothing serves, so the suppressor arm never fires.
    """
    found: dict[str, set[str]] = {}
    for alert, expr in exprs.items():
        text = " ".join(str(expr).split())
        literals = list(_EXACT.findall(text))
        for excluded in _EXCLUDED.findall(text):
            literals += [unescape(member) for member in alternatives(excluded)]
        for literal in literals:
            host = literal.rsplit(":", 1)[0]
            if _QUAD.match(host):
                found.setdefault(host, set()).add(alert)
    return found


@pytest.fixture(scope="module")
def served() -> set[str]:
    """Every address the inventory or cluster-config says this cluster answers on."""
    tree = load_script("inventory_tree.py")
    found = set(tree.addresses_by_host(tree.load_inventory(INVENTORY)).values())
    assert found, (
        f"{INVENTORY.name} declares no ansible_host — the gate would accept anything"
    )
    config = (yaml.safe_load(CLUSTER_CONFIG.read_text()) or {}).get("data") or {}
    return found | {str(value) for value in config.values() if _QUAD.match(str(value))}


@pytest.fixture(scope="module")
def pinned() -> dict[str, set[str]]:
    """A repository pinning no address at all is the shipped state, so an empty
    result is a pass here; the collector is held up by its own cases below."""
    rules, unreadable = alert_rules()
    assert_all_parsed(unreadable, "address-pinning alert")
    assert rules, "the corpus holds no alerts — the gate would be vacuous"
    return pinned_addresses({alert: rule.get("expr", "") for alert, rule in rules})


@needs_inventory
def test_every_pinned_address_is_served(pinned, served):
    stray = sorted(
        f"{address} (pinned by {', '.join(sorted(alerts))})"
        for address, alerts in pinned.items()
        if address not in served and address not in NON_INVENTORY_TARGETS
    )
    assert not stray, (
        "alerts pinning an address no ansible_host and no cluster-config value "
        "serves:\n  " + "\n  ".join(stray) + "\n\nRenumber the rule with the "
        "inventory, or add the address to NON_INVENTORY_TARGETS with the reason "
        "it has no inventory source."
    )


@needs_inventory
def test_every_non_inventory_target_is_still_pinned(pinned):
    """The allowlist cannot outlive the rules that needed it."""
    stale = sorted(address for address in NON_INVENTORY_TARGETS if address not in pinned)
    assert not stale, f"no alert pins these addresses any more: {stale} — drop them"


def test_the_collector_expands_an_exclusion_alternation():
    """Mutation case: a quad left behind in an `instance!~` list is reported,
    and the port is dropped so a host and its exporter read as one address."""
    found = pinned_addresses(
        {"Bogus": 'probe_success{instance!~"10\\\\.9\\\\.9\\\\.9|1\\\\.1\\\\.1\\\\.1"}'}
    )
    assert found == {"10.9.9.9": {"Bogus"}, "1.1.1.1": {"Bogus"}}
    assert pinned_addresses({"Host": 'up{instance="10.0.0.5:9101"} == 0'}) == {
        "10.0.0.5": {"Host"}
    }


def test_a_hostname_instance_is_not_read_as_an_address():
    """Mutation case: only dotted quads are inventory addresses — a URL or a
    hostname target belongs to the blackbox parity gate."""
    assert pinned_addresses({"A": 'probe_success{instance="https://auth.example"}'}) == {}
    assert pinned_addresses({"B": 'up{instance="nas-01.example:9101"} == 0'}) == {}
