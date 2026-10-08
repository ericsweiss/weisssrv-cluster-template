"""Tests that check-role-default-flips.py catches a boolean default that flips unadopted."""
from __future__ import annotations

import pytest
from conftest import load_script

gate = load_script("check-role-default-flips.py")


def test_a_flipped_bool_is_reported() -> None:
    flips = gate.flipped_bools(
        {"zfs_encryption": {"zfs_encryption_autostart": True}},
        {"zfs_encryption": {"zfs_encryption_autostart": False}},
    )
    assert flips == [("zfs_encryption", "zfs_encryption_autostart", True, False)]


def test_a_non_bool_change_is_not_a_flip() -> None:
    """Only booleans flip silently: a version bump is visible in the diff."""
    assert gate.flipped_bools({"qol": {"qol_omz_commit": "a"}}, {"qol": {"qol_omz_commit": "b"}}) == []


def test_a_new_key_is_not_a_flip() -> None:
    assert gate.flipped_bools({"qol": {}}, {"qol": {"qol_new_toggle": True}}) == []


def test_a_role_the_old_ref_lacks_is_not_a_flip() -> None:
    assert gate.flipped_bools({}, {"brand_new": {"brand_new_enabled": True}}) == []


def test_an_undeclared_flip_is_unadopted() -> None:
    flips = [("nic_tuning", "nic_tuning_bond_asa_guard", False, True)]
    assert gate.unadopted(flips, {"other_var"}, set())


def test_a_declared_flip_is_adopted() -> None:
    flips = [("nic_tuning", "nic_tuning_bond_asa_guard", False, True)]
    assert gate.unadopted(flips, {"nic_tuning_bond_asa_guard"}, set()) == []


def test_an_allowlisted_flip_is_adopted() -> None:
    flips = [("nic_tuning", "nic_tuning_bond_asa_guard", False, True)]
    assert gate.unadopted(flips, set(), {"nic_tuning_bond_asa_guard"}) == []


def test_an_empty_inventory_is_vacuous(tmp_path) -> None:
    with pytest.raises(gate.Vacuous):
        gate.inventory_keys(tmp_path)


def test_a_checkout_without_the_collection_is_vacuous(tmp_path) -> None:
    with pytest.raises(gate.Vacuous):
        gate.lib_root(str(tmp_path))


def test_a_missing_lib_exits_two(tmp_path) -> None:
    assert gate.main(["--from", "v0.0.1", "--to", "v0.0.2", "--lib", str(tmp_path)]) == 2


def test_the_real_inventory_declares_variables() -> None:
    assert gate.inventory_keys(gate.INVENTORY)
