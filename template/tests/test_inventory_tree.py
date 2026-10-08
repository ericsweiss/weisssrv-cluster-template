"""Coverage for inventory_tree.py, the shared inventory resolver.

Every gate and generator that reads hosts.yml goes through it, so a wrong group
expansion or a dropped hostvar is a wrong answer in all of them at once.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import load_script

tree = load_script("inventory_tree.py")

INVENTORY = yaml.safe_load("""
all:
  hosts:
    bastion:
      ansible_host: 192.0.2.1
  children:
    proxmox:
      hosts:
        pve-a:
          ansible_host: 192.0.2.2
        pve-b:
          ansible_host: 192.0.2.3
    dns:
      hosts:
        dns-01:
          ansible_host: 192.0.2.4
    base_managed:
      children:
        proxmox:
        dns:
    empty_parent:
      children:
        empty_child:
          hosts: {}
""")


class TestGroupIndex:
    def test_every_group_with_content_is_indexed(self):
        index = tree.group_index(INVENTORY)
        assert index["proxmox"]["hosts"] == ["pve-a", "pve-b"]
        assert index["base_managed"]["children"] == ["proxmox", "dns"]
        assert index["all"]["hosts"] == ["bastion"]

    def test_an_empty_hosts_mapping_is_still_a_group(self):
        """`hosts: {}` is a real empty group, not an absent one."""
        assert "empty_child" in tree.group_index(INVENTORY)

    def test_a_bare_reference_does_not_shadow_the_definition(self):
        """base_managed names `proxmox:` with no body; the occurrence carrying
        the hosts must survive."""
        assert tree.group_index(INVENTORY)["proxmox"]["hosts"] == ["pve-a", "pve-b"]

    def test_a_group_defined_twice_merges(self):
        inventory = {
            "all": {
                "children": {
                    "a": {"hosts": {"h1": None}},
                    "b": {"children": {"a": {"hosts": {"h2": None}}}},
                }
            }
        }
        assert tree.group_index(inventory)["a"]["hosts"] == ["h1", "h2"]


class TestResolveHosts:
    def test_a_group_of_groups_expands_in_inventory_order(self):
        index = tree.group_index(INVENTORY)
        assert tree.resolve_hosts("base_managed", index) == ["pve-a", "pve-b", "dns-01"]

    def test_a_leaf_group_resolves_to_its_own_hosts(self):
        assert tree.resolve_hosts("dns", tree.group_index(INVENTORY)) == ["dns-01"]

    def test_an_undeclared_group_resolves_to_nothing(self):
        assert tree.resolve_hosts("nope", tree.group_index(INVENTORY)) == []

    def test_an_empty_parent_resolves_to_nothing(self):
        assert tree.resolve_hosts("empty_parent", tree.group_index(INVENTORY)) == []

    def test_a_host_in_two_groups_appears_once(self):
        inventory = {
            "all": {
                "children": {
                    "a": {"hosts": {"shared": None}},
                    "b": {"hosts": {"shared": None}},
                    "both": {"children": {"a": None, "b": None}},
                }
            }
        }
        assert tree.resolve_hosts("both", tree.group_index(inventory)) == ["shared"]

    def test_a_cycle_terminates_and_still_yields_the_hosts(self):
        with pytest.raises(tree.InventoryCycle, match="a -> b -> a"):
            tree.resolve_hosts("a", tree.group_index(CYCLE), strict=True)
        assert tree.resolve_hosts("a", tree.group_index(CYCLE)) == ["only"]


CYCLE = {
    "all": {
        "children": {
            "a": {"children": {"b": None}},
            "b": {"children": {"a": None}, "hosts": {"only": None}},
        }
    }
}


class TestDeclaredGroups:
    def test_a_null_bodied_placeholder_counts_as_declared(self):
        inventory = {"all": {"children": {"ghost": None}}}
        assert "ghost" in tree.declared_groups(inventory)
        assert "ghost" not in tree.group_index(inventory)

    def test_all_is_always_declared(self):
        assert "all" in tree.declared_groups({})


class TestHostVars:
    def test_vars_merge_across_the_groups_that_list_a_host(self):
        inventory = {
            "all": {
                "children": {
                    "a": {"hosts": {"h": {"ansible_host": "192.0.2.9"}}},
                    "b": {"hosts": {"h": {"role": "edge"}}},
                }
            }
        }
        assert tree.host_vars(inventory)["h"] == {"ansible_host": "192.0.2.9", "role": "edge"}

    def test_a_null_bodied_host_does_not_clobber_its_vars(self):
        inventory = {
            "all": {
                "children": {
                    "a": {"hosts": {"h": {"ansible_host": "192.0.2.9"}}},
                    "b": {"hosts": {"h": None}},
                }
            }
        }
        assert tree.host_vars(inventory)["h"]["ansible_host"] == "192.0.2.9"

    def test_all_hosts_includes_a_host_declared_directly_under_all(self):
        assert "bastion" in tree.all_hosts(INVENTORY)


class TestAddresses:
    def test_an_address_maps_back_to_its_host(self):
        assert tree.hosts_by_address(INVENTORY)["192.0.2.2"] == "pve-a"

    def test_a_host_without_an_address_is_omitted(self):
        inventory = {"all": {"children": {"a": {"hosts": {"h": {}}}}}}
        assert tree.addresses_by_host(inventory) == {}
        assert tree.hosts_by_address(inventory) == {}

    def test_the_first_host_to_claim_an_address_keeps_it(self):
        inventory = {
            "all": {
                "children": {
                    "a": {"hosts": {"first": {"ansible_host": "192.0.2.9"}}},
                    "b": {"hosts": {"second": {"ansible_host": "192.0.2.9"}}},
                }
            }
        }
        assert tree.hosts_by_address(inventory)["192.0.2.9"] == "first"


class TestLoadInventory:
    def test_it_parses_a_hosts_yml(self, tmp_path):
        path = tmp_path / "hosts.yml"
        path.write_text(yaml.safe_dump(INVENTORY))
        assert tree.load_inventory(path)["all"]["children"]["dns"]["hosts"]

    def test_an_empty_file_is_an_empty_mapping(self, tmp_path):
        path = tmp_path / "hosts.yml"
        path.write_text("")
        assert tree.load_inventory(path) == {}
