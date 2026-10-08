"""Exercise the two render-time guards in the UniFi networks template: the one
that refuses a LAN with no room for a DHCP pool, and the one that refuses an
address plan leaving no free pair of 10.0.x.0/24 ranges for the example VLANs.
"""

from __future__ import annotations

import ipaddress

import jinja2
import jinja2_ansible_filters
import pytest
import yaml

import render_cluster

REPO_ROOT = render_cluster.REPO_ROOT
TEMPLATE = (
    REPO_ROOT / "template" / "terraform" / "{% if use_unifi %}unifi{% endif %}"
    / "networks.tf.jinja"
)

DHCP_GUARD = "leaves no DHCP pool above the reserved address bands and the VIPs"
VLAN_GUARD = "leave no free pair of 10.0.x.0/24 ranges"


def _reserved_address_bands() -> list:
    """The computed answer copier derives from its own default, read from
    copier.yml so the fixture cannot drift from the question."""
    questions = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
    return yaml.safe_load(questions["reserved_address_bands"]["default"])


def _context(**overrides) -> dict:
    answers = yaml.safe_load(render_cluster.ANSWERS.read_text())
    answers.update(overrides)
    network = ipaddress.ip_network(answers["lan_cidr"])
    answers["lan_address_range"] = [
        int(network.network_address),
        int(network.broadcast_address),
    ]
    answers["reserved_address_bands"] = _reserved_address_bands()
    return answers


def _render(**overrides) -> str:
    """Render networks.tf.jinja alone under copier's envops and filter set."""
    env = jinja2.Environment(  # noqa: S701 - rendering our own template, no user input
        extensions=[jinja2_ansible_filters.AnsibleCoreFiltersExtension],
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.from_string(TEMPLATE.read_text()).render(**_context(**overrides))


# A LAN inside 10.0.0.0/16 claims every 10.0.x.0/24 the example VLANs could
# take, and the two k3s ranges are elsewhere.
NO_FREE_PAIR = {
    "lan_cidr": "10.0.0.0/16",
    "lan_prefix": "10.0.10",
    "lan_gateway": "10.0.10.1",
    "k3s_api_vip": "10.0.10.161",
    "metallb_public_vip": "10.0.10.100",
    "metallb_internal_vip": "10.0.10.101",
    "upstream_dns_servers": "10.0.10.21 10.0.10.22",
}


def test_fixture_answers_render_with_both_guards_silent():
    rendered = _render()
    assert "vlan        = 30" in rendered
    assert "vlan = 40" in rendered
    assert DHCP_GUARD not in rendered
    assert VLAN_GUARD not in rendered


def test_dhcp_guard_names_the_answer_at_fault():
    """A LAN too small to hold a pool above the reserved bands must fail the
    render rather than emit a backwards dhcp range."""
    with pytest.raises(jinja2.TemplateError) as excinfo:
        _render(lan_cidr="172.19.4.0/29")
    assert DHCP_GUARD in str(excinfo.value)
    assert "172.19.4.0/29" in str(excinfo.value)


def test_vlan_guard_names_the_answers_at_fault():
    """With no free 10.0.x.0/24 pair the guard must report what to write by
    hand, not a bare Jinja UndefinedError from picking out of an empty list."""
    with pytest.raises(jinja2.TemplateError) as excinfo:
        _render(**NO_FREE_PAIR)
    message = str(excinfo.value)
    assert VLAN_GUARD in message
    assert "10.0.0.0/16" in message
    assert "write the iot and guest networks in networks.tf by hand" in message


def test_the_guards_are_the_only_thing_standing_between_a_bad_plan_and_a_render():
    """Mutating the guard out of the template must break the two negative cases
    above, so neither can pass on unrelated grounds."""
    source = TEMPLATE.read_text()
    assert source.count("{%- if _free | length < 2 %}") == 1
    mutated = source.replace("{%- if _free | length < 2 %}", "{%- if false %}")
    env = jinja2.Environment(  # noqa: S701 - rendering our own template, no user input
        extensions=[jinja2_ansible_filters.AnsibleCoreFiltersExtension],
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    with pytest.raises(jinja2.TemplateError) as excinfo:
        env.from_string(mutated).render(**_context(**NO_FREE_PAIR))
    assert VLAN_GUARD not in str(excinfo.value)


def test_template_is_where_the_test_thinks_it_is():
    assert TEMPLATE.is_file(), f"{TEMPLATE} moved; this module tests nothing"
