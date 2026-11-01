"""Site cases for check-guest-endpoint-parity.py; weisssrv-lib owns its behaviour
suite. They prove the gate reads this tree's inventory, endpoints and exports.
"""
from __future__ import annotations

from conftest import REPO, load_script

gate = load_script("check-guest-endpoint-parity.py")

HOSTS_YML = f"{gate.DEFAULT_INVENTORY}/hosts.yml"


def test_the_live_tree_is_clean() -> None:
    problems, checked = gate.check(REPO)
    assert problems == []
    assert checked, "the live tree compared no LAN address"


def test_the_parser_sees_the_inventory_guests_and_k3s_groups() -> None:
    """Every scoped k3s group resolves to addresses, so the export arm compares
    whole groups rather than skipping a group it could not read."""
    known, groups = gate.inventory(REPO, HOSTS_YML, gate.DEFAULT_SCOPED_GROUPS)
    assert known, f"{HOSTS_YML} yielded no ansible_host"
    missing = [name for name in gate.DEFAULT_SCOPED_GROUPS if not groups.get(name)]
    assert not missing, f"scoped groups with no address in {HOSTS_YML}: {missing}"


def test_the_live_tree_declares_hand_written_endpoints() -> None:
    """The endpoint arm is only a gate while a manifest names a guest address."""
    config = gate.gate_common.load_cluster_config(REPO)
    found, skipped = gate.endpoint_addresses(REPO, gate.DEFAULT_MANIFEST_TREE, config)
    assert skipped == []
    assert found, f"no EndpointSlice/Endpoints address under {gate.DEFAULT_MANIFEST_TREE}/"


def test_the_live_tree_declares_nfs_exports() -> None:
    """The export arm is only a gate while it has a subject to read."""
    found, skipped = gate.nfs_exports(REPO, gate.DEFAULT_INVENTORY)
    assert skipped == []
    assert found, f"no {gate.EXPORTS_KEY} found under {gate.DEFAULT_INVENTORY}/"
