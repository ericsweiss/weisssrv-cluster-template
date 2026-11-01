"""group_vars/dns.yml rendered through Ansible's own templar against the real
inventory: every host reaches DNS, and the tailnet-dns override zone lists every
record that answers a direct IP."""
from __future__ import annotations

import ipaddress
import re

import pytest
import yaml
from conftest import REPO, ansible_templar, copier_answers, load_script, trust_as_template

GATE = "DNS records"
INVENTORY = REPO / "ansible/inventories/prod"
COREFILE = REPO / "kubernetes/apps/tailnet-dns/configmap.yaml"
PTR_RE = re.compile(
    r"^\|\|(\d+)\.(\d+)\.(\d+)\.(\d+)\.in-addr\.arpa\^\$dnsrewrite=NOERROR;PTR;(.+)\.$"
)
inventory_tree = load_script("inventory_tree.py")
gate_common = load_script("gate_common.py")


def _load(name: str) -> dict:
    return yaml.safe_load((INVENTORY / name).read_text()) or {}


def _render(names: list[str], inventory: dict | None = None) -> dict:
    inventory = inventory or inventory_tree.load_inventory(INVENTORY / "hosts.yml")
    index = inventory_tree.group_index(inventory)
    groups = {g: inventory_tree.resolve_hosts(g, index) for g in index}
    groups["all"] = inventory_tree.all_hosts(inventory)
    hostvars = {
        host: {"inventory_hostname": host, **merged}
        for host, merged in inventory_tree.host_vars(inventory).items()
    }
    all_vars = _load("group_vars/all.yml")
    variables = {
        "internal_domain": all_vars["internal_domain"],
        "external_domain": all_vars["external_domain"],
        "k3s_api_vip": all_vars["k3s_api_vip"],
        "metallb_public_vip": all_vars["metallb_public_vip"],
        "metallb_internal_vip": all_vars["metallb_internal_vip"],
        "groups": groups,
        "hostvars": hostvars,
        **trust_as_template(_load("group_vars/dns.yml"), GATE),
    }
    templar = ansible_templar(variables, GATE)
    return {n: templar.template(variables[n]) for n in names}


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _render(["adguard_home_rewrites", "adguard_home_user_rules", "_dns_addressed_hosts"])


def _ptrs(rules: list[str]) -> dict[str, str]:
    out = {}
    for rule in rules:
        m = PTR_RE.match(rule)
        if m:
            ip = ".".join(reversed(m.groups()[:4]))
            assert ip not in out, f"two PTR rules for {ip}"
            out[ip] = m.group(5)
    return out


def test_every_answer_is_an_address(rendered):
    for entry in rendered["adguard_home_rewrites"]:
        ipaddress.IPv4Address(entry["answer"])


def test_every_addressed_host_has_a_ptr(rendered):
    ptrs = _ptrs(rendered["adguard_home_user_rules"])
    assert rendered["_dns_addressed_hosts"], "the inventory addresses no host"
    for host in rendered["_dns_addressed_hosts"]:
        assert host["ansible_host"] in ptrs, host["inventory_hostname"]


def test_a_ptr_round_trips_unless_a_service_claims_the_name(rendered):
    forward = {}
    for entry in rendered["adguard_home_rewrites"]:
        forward.setdefault(entry["domain"], set()).add(entry["answer"])
    vip = _load("group_vars/all.yml")["metallb_internal_vip"]
    for ip, name in _ptrs(rendered["adguard_home_user_rules"]).items():
        assert ip in forward[name] or forward[name] == {vip}, name


def test_a_new_host_gets_both_records():
    inventory = inventory_tree.load_inventory(INVENTORY / "hosts.yml")
    inventory["all"].setdefault("hosts", {})["probe-01"] = {"ansible_host": "10.9.8.7"}
    out = _render(["adguard_home_rewrites", "adguard_home_user_rules"], inventory)
    domain = _load("group_vars/all.yml")["internal_domain"]
    assert {"domain": f"probe-01.{domain}", "answer": "10.9.8.7"} in out["adguard_home_rewrites"]
    assert _ptrs(out["adguard_home_user_rules"])["10.9.8.7"] == f"probe-01.{domain}"


# Direct-IP names left out of the tailnet-dns override zone, with the reason.
TAILNET_EXEMPT: dict[str, str] = {}
# Traefik routes kept in the override zone because a middleware on them rejects
# the pod source IP that the tailnet CNAME path arrives with.
TAILNET_STRICT_ROUTES: set[str] = set()


def tailnet_drift(answers: dict[str, set[str]], zone: set[str], vip: str) -> tuple[set, set]:
    """(direct-IP names missing from the zone, zone names with no direct-IP answer)."""
    direct = {name for name, ips in answers.items() if ips != {vip}} - TAILNET_EXEMPT.keys()
    return direct - zone, zone - direct - TAILNET_STRICT_ROUTES


@pytest.mark.parametrize(
    "answers, zone, exempt, strict, missing, extra",
    [
        ({"nas": {"10.0.0.5"}}, set(), {}, set(), {"nas"}, set()),
        ({"ml": {"10.0.0.6"}}, {"ml"}, {"ml": "LAN-only"}, set(), set(), {"ml"}),
        ({"grafana": {"VIP"}}, {"grafana", "traefik"}, {}, {"traefik"}, set(), {"grafana"}),
    ],
    ids=["direct-name-missing", "exempt-name-listed", "vip-only-name-listed"],
)
def test_tailnet_drift_reports_each_arm(monkeypatch, answers, zone, exempt, strict, missing, extra):
    monkeypatch.setitem(globals(), "TAILNET_EXEMPT", exempt)
    monkeypatch.setitem(globals(), "TAILNET_STRICT_ROUTES", strict)
    assert tailnet_drift(answers, zone, "VIP") == (missing, extra)


@pytest.mark.skipif(not copier_answers().get("vpn_tailscale"), reason="no tailnet")
def test_the_tailnet_override_zone_lists_every_direct_ip_name(rendered):
    """Tailnet clients CNAME every other internal name to the tailnet Traefik.
    An inventory host the zone does not list is left out: the starter zone names
    the NAS and the relay, and each further host is the cluster's choice."""
    config = gate_common.load_cluster_config(REPO)
    domain, vip = config["cluster_internal_domain"], config["cluster_metallb_internal_vip"]
    corefile = yaml.safe_load(COREFILE.read_text())["data"]["Corefile"]
    zones = [
        re.sub(r"\$\{(cluster_\w+)\}", lambda m: config[m.group(1)], line).split()[:-1]
        for line in corefile.splitlines()
        if line.rstrip().endswith("{") and not line.lstrip().startswith("#")
    ]
    zone = {n.removesuffix(f".{domain}") for n in max(zones, key=len) if n.endswith(f".{domain}")}
    hosts = {h["inventory_hostname"] for h in rendered["_dns_addressed_hosts"]}
    answers: dict[str, set[str]] = {}
    for entry in rendered["adguard_home_rewrites"]:
        label = entry["domain"].removesuffix(f".{domain}")
        if "." not in label and (label in zone or label not in hosts):
            answers.setdefault(label, set()).add(entry["answer"])
    assert any(ips != {vip} for ips in answers.values()), "no rewrite answers a direct IP"
    missing, extra = tailnet_drift(answers, zone, vip)
    assert not missing, f"missing from the {COREFILE.name} override zone: {sorted(missing)}"
    assert not extra, f"in the override zone without a direct-IP rewrite: {sorted(extra)}"
