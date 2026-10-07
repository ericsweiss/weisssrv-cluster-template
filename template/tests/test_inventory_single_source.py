"""The LAN gateway is declared once and referenced everywhere else.

`lan_gateway` in group_vars/all.yml is the only copy. A rule that spells the
address out again survives a renumber, because no other gate reads it.
"""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import REPO, reported

GROUP_VARS = REPO / "ansible" / "inventories" / "prod" / "group_vars"
ALL_YML = GROUP_VARS / "all.yml"
SOURCE_KEY = "lan_gateway"
# The keys whose declared value IS the gateway: the declaration itself and the
# two guest-provisioning mirrors, which reference it rather than restating it.
MIRROR_KEYS = ("lan_gateway", "proxmox_lxc_gateway", "proxmox_vm_cloudinit_gateway")

needs_group_vars = pytest.mark.skipif(
    not ALL_YML.is_file(), reason="no ansible group_vars in this repository"
)


def gateway() -> str:
    value = (yaml.safe_load(ALL_YML.read_text()) or {}).get(SOURCE_KEY)
    assert value, f"{reported(ALL_YML)} declares no {SOURCE_KEY} — nothing to hold in step"
    return str(value)


def address_re(address: str) -> re.Pattern[str]:
    """The address as a bare literal: a CIDR or a longer address never matches."""
    return re.compile(rf"(?<![\d.]){re.escape(address)}(?![\d./])")


def key_of(line: str) -> str:
    key, sep, _ = line.partition(":")
    return key.strip().lstrip("- ") if sep else ""


def restatements(root, address: str, mirrors=MIRROR_KEYS) -> list[str]:
    """`path:line` for every bare gateway literal outside the mirrored keys."""
    pattern = address_re(address)
    found = []
    for path in sorted(root.glob("*.yml")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if not pattern.search(code) or key_of(code) in mirrors:
                continue
            found.append(f"{path.name}:{number}")
    return found


@needs_group_vars
def test_the_gateway_is_declared_where_the_gate_reads_it():
    """A moved declaration would leave the scan with nothing to compare."""
    assert address_re(gateway()).search(ALL_YML.read_text()), (
        f"{SOURCE_KEY} resolves to a value that does not appear literally in "
        f"{reported(ALL_YML)} — the gate would accept any restatement"
    )


@needs_group_vars
def test_no_group_vars_file_restates_the_gateway():
    found = restatements(GROUP_VARS, gateway())
    assert not found, (
        f"the gateway address is spelled out again in {found} — reference "
        f"`{{{{ {SOURCE_KEY} }}}}` instead, so a renumber moves one line."
    )


def test_a_hand_written_gateway_in_a_firewall_rule_is_reported(tmp_path):
    """Mutation case: the security-group form the fix was written for."""
    (tmp_path / "all.yml").write_text(
        f"{SOURCE_KEY}: 10.9.0.1\n"
        'proxmox_lxc_gateway: "{{ lan_gateway }}"\n'
        "proxmox_firewall_security_groups:\n"
        "  - name: sg-syslog-vip\n"
        '    rules:\n'
        '      - "IN ACCEPT -source 10.9.0.1 -dest 10.9.0.162 -p udp -dport 514"\n'
    )
    assert restatements(tmp_path, "10.9.0.1") == ["all.yml:6"]


def test_a_longer_address_and_a_cidr_are_not_the_gateway(tmp_path):
    """Mutation case: `10.9.0.1` must not match `10.9.0.160` or `10.9.0.1/24`,
    and a commented-out line is not shipped configuration."""
    (tmp_path / "k3s.yml").write_text(
        "k3s_api_vip: 10.9.0.160\n"
        "lan_cidr: 10.9.0.1/24\n"
        "# old gateway was 10.9.0.1\n"
    )
    assert restatements(tmp_path, "10.9.0.1") == []
