"""Coverage for check-guest-endpoint-parity.py.

The live tree agrees, so it proves nothing about failure: each arm runs against
a fixture repo, and every drift case must FAIL.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import REPO, load_script

gate = load_script("check-guest-endpoint-parity.py")

NAS_YML = f"{gate.GROUP_VARS}/{gate.NAS_GROUP}.yml"


HOSTS = textwrap.dedent(
    """\
    all:
      children:
        guests:
          hosts:
            plex:
              ansible_host: 192.0.2.152
            gitlab:
              ansible_host: 192.0.2.153
    """
)

CONFIG = textwrap.dedent(
    """\
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: cluster-config
    data:
      cluster_lan_cidr: "192.0.2.0/24"
      cluster_lan_gateway: "192.0.2.1"
    """
)

SLICE = textwrap.dedent(
    """\
    apiVersion: discovery.k8s.io/v1
    kind: EndpointSlice
    metadata:
      name: plex
    endpoints:
      - addresses:
          - 192.0.2.152
    ---
    apiVersion: discovery.k8s.io/v1
    kind: EndpointSlice
    metadata:
      name: router
    endpoints:
      - addresses:
          - 192.0.2.1
    """
)


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, gate.HOSTS_YML, HOSTS)
    write(tmp_path, gate.CLUSTER_CONFIG, CONFIG)
    write(tmp_path, "kubernetes/apps/vm-ingress/services.yaml", SLICE)
    write(tmp_path, NAS_YML, "nas_storage_exports: []\n")
    return tmp_path


def test_addresses_that_match_the_inventory_pass(repo: Path) -> None:
    assert gate.check(repo) == []
    assert gate.main(["--repo-root", str(repo)]) == 0


def test_a_renumbered_guest_fails(repo: Path) -> None:
    write(
        repo,
        "kubernetes/apps/vm-ingress/services.yaml",
        SLICE.replace("192.0.2.152", "192.0.2.159"),
    )
    problems = gate.check(repo)
    assert any("192.0.2.159" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_the_gateway_needs_no_inventory_entry(repo: Path) -> None:
    assert not any("192.0.2.1" in p for p in gate.check(repo))


def test_an_off_lan_address_is_out_of_scope(repo: Path) -> None:
    write(
        repo,
        "kubernetes/apps/vm-ingress/services.yaml",
        SLICE.replace("192.0.2.152", "192.168.1.9"),
    )
    with pytest.raises(gate.Vacuous):
        gate.check(repo)


def test_the_legacy_endpoints_kind_is_read_too(repo: Path) -> None:
    write(
        repo,
        "kubernetes/apps/legacy/endpoints.yaml",
        textwrap.dedent(
            """\
            apiVersion: v1
            kind: Endpoints
            metadata:
              name: legacy
            subsets:
              - addresses:
                  - ip: 192.0.2.199
            """
        ),
    )
    assert any("192.0.2.199" in p for p in gate.check(repo))


def test_no_endpoints_at_all_is_vacuous(repo: Path) -> None:
    (repo / "kubernetes/apps/vm-ingress/services.yaml").unlink()
    with pytest.raises(gate.Vacuous):
        gate.check(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_a_missing_lan_cidr_is_vacuous(repo: Path) -> None:
    write(repo, gate.CLUSTER_CONFIG, CONFIG.replace('  cluster_lan_cidr: "192.0.2.0/24"\n', ""))
    with pytest.raises(gate.Vacuous):
        gate.check(repo)


def test_a_cluster_with_no_gateway_key_loses_only_that_allowance(repo: Path) -> None:
    """The gateway is an allowance, not a requirement: without it the gate is
    stricter, never vacuous."""
    write(repo, gate.CLUSTER_CONFIG, CONFIG.replace('  cluster_lan_gateway: "192.0.2.1"\n', ""))
    assert any("192.0.2.1" in problem for problem in gate.check(repo))


def test_the_live_tree_is_clean() -> None:
    assert gate.check(REPO) == []


def test_a_cluster_config_placeholder_resolves_before_the_ip_parse():
    """Identity values are spelled as placeholders, so the gate must substitute."""
    config = {"cluster_lan_gateway": "192.0.2.1"}
    assert gate.substitute("${cluster_lan_gateway}", config) == "192.0.2.1"


def test_an_unknown_placeholder_is_left_alone_so_a_typo_still_fails():
    assert gate.substitute("${cluster_lan_gatway}", {"cluster_lan_gateway": "192.0.2.1"}) == (
        "${cluster_lan_gatway}"
    )


def test_a_whole_list_roster_placeholder_is_resolved(repo: Path) -> None:
    """A slice may spell its endpoints as one `${cluster_*}` key holding a JSON
    list; the roster behind it is what drifts from the inventory."""
    write(
        repo,
        "kubernetes/apps/vm-ingress/roster.yaml",
        "apiVersion: discovery.k8s.io/v1\n"
        "kind: EndpointSlice\n"
        "metadata:\n"
        "  name: roster\n"
        "addressType: IPv4\n"
        "endpoints: ${cluster_roster_addresses}\n",
    )
    write(
        repo,
        gate.CLUSTER_CONFIG,
        CONFIG + '  cluster_roster_addresses: \'[{"addresses": ["192.0.2.240"]}]\'\n',
    )
    assert any("192.0.2.240" in problem for problem in gate.check(repo))


def test_an_unparseable_manifest_is_reported_not_dropped(repo: Path) -> None:
    """A file that will not parse takes its endpoints out of the comparison, so
    the gate has to name it rather than report agreement."""
    write(repo, "kubernetes/apps/demo/broken.yaml", "kind: [unclosed\n")
    problems = gate.check(repo)
    assert any("broken.yaml unparseable" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_unresolved_roster_placeholder_is_reported_not_dropped(repo: Path) -> None:
    """A roster key cluster-config does not define would otherwise remove every
    address behind it from the comparison and still report agreement."""
    write(
        repo,
        "kubernetes/apps/vm-ingress/roster.yaml",
        "apiVersion: discovery.k8s.io/v1\n"
        "kind: EndpointSlice\n"
        "metadata:\n"
        "  name: roster\n"
        "addressType: IPv4\n"
        "endpoints: ${cluster_roster_addresses}\n",
    )
    problems = gate.check(repo)
    assert any("cluster_roster_addresses" in p and "does not define" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


NAS_EXPORTS = textwrap.dedent(
    """\
    nas_storage_exports:
      - path: /export/appdata
        clients:
          - spec: "192.0.2.152/32"
            options: "rw,sync"
          - spec: "192.0.2.0/24"
            options: "ro,sync"
    """
)


def test_an_export_allowing_a_real_host_passes(repo: Path) -> None:
    write(repo, NAS_YML, NAS_EXPORTS)
    assert gate.check(repo) == []


def test_an_export_allowing_a_renumbered_host_fails(repo: Path) -> None:
    write(repo, NAS_YML, NAS_EXPORTS.replace("192.0.2.152/32", "192.0.2.159/32"))
    problems = gate.check(repo)
    assert any("192.0.2.159/32" in p and "/export/appdata" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_whole_subnet_export_spec_is_out_of_scope(repo: Path) -> None:
    """Only per-host /32s name an inventory address; a CIDR allow is a policy."""
    write(repo, NAS_YML, NAS_EXPORTS.replace("192.0.2.152/32", "192.0.2.0/25"))
    assert gate.check(repo) == []


def test_an_unparseable_nas_group_vars_is_reported_not_dropped(repo: Path) -> None:
    write(repo, NAS_YML, "nas_storage_exports: [unclosed\n")
    problems = gate.check(repo)
    assert any("nas.yml unparseable" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_yaml_extension_group_vars_is_read(repo: Path) -> None:
    (repo / NAS_YML).unlink()
    write(repo, f"{gate.GROUP_VARS}/{gate.NAS_GROUP}.yaml",
          NAS_EXPORTS.replace("192.0.2.152/32", "192.0.2.159/32"))
    assert any("192.0.2.159/32" in p for p in gate.check(repo))
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_group_vars_directory_is_read(repo: Path) -> None:
    """Ansible accepts group_vars/<group>/ — a gate reading only <group>.yml
    would see no exports here and pass silently."""
    (repo / NAS_YML).unlink()
    write(repo, f"{gate.GROUP_VARS}/{gate.NAS_GROUP}/storage.yml",
          NAS_EXPORTS.replace("192.0.2.152/32", "192.0.2.159/32"))
    problems = gate.check(repo)
    assert any("192.0.2.159/32" in p and "nas/storage.yml" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_no_nas_group_vars_at_all_exits_2(repo: Path) -> None:
    (repo / NAS_YML).unlink()
    with pytest.raises(gate.Vacuous):
        gate.check(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_a_lan_cidr_with_host_bits_is_tolerated(repo: Path) -> None:
    """Every other gate parses the CIDR with strict=False, so an answer like
    192.0.2.5/24 has to scope the same way here, not traceback."""
    write(repo, gate.CLUSTER_CONFIG, CONFIG.replace("192.0.2.0/24", "192.0.2.5/24"))
    assert gate.check(repo) == []
    assert gate.main(["--repo-root", str(repo)]) == 0


def test_an_unparseable_lan_cidr_exits_2_naming_cluster_config(repo: Path, capsys) -> None:
    write(repo, gate.CLUSTER_CONFIG, CONFIG.replace("192.0.2.0/24", "192.0.2.0/33"))
    with pytest.raises(gate.Vacuous):
        gate.check(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert "cluster-config" in capsys.readouterr().err


def test_an_unparseable_cluster_config_exits_2_naming_the_file(repo: Path, capsys) -> None:
    """A parse error must name cluster-config.yaml, not "<unicode string>"."""
    write(repo, gate.CLUSTER_CONFIG, "data: [unclosed\n")
    with pytest.raises(gate.Vacuous):
        gate.check(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert "cluster-config.yaml" in capsys.readouterr().err


def test_a_cluster_config_carrying_no_data_map_exits_2(repo: Path, capsys) -> None:
    write(repo, gate.CLUSTER_CONFIG, "- not: a mapping\n")
    with pytest.raises(gate.Vacuous):
        gate.check(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert "no data keys" in capsys.readouterr().err


K3S_HOSTS = textwrap.dedent(
    """\
    all:
      children:
        k3s_servers:
          hosts:
            srv-01:
              ansible_host: 192.0.2.221
            srv-02:
              ansible_host: 192.0.2.222
        k3s_agents:
          hosts:
            agt-01:
              ansible_host: 192.0.2.231
            agt-02:
              ansible_host: 192.0.2.232
        k3s:
          children:
            k3s_servers:
            k3s_agents:
        guests:
          hosts:
            plex:
              ansible_host: 192.0.2.152
    """
)

K3S_EXPORTS = textwrap.dedent(
    """\
    nas_storage_exports:
      - path: /export
        clients:
          - spec: "192.0.2.0/24"
            options: "ro,sync"
      - path: /export/appdata
        clients:
          - spec: "192.0.2.221/32"
            options: "rw,sync"
          - spec: "192.0.2.222/32"
            options: "rw,sync"
          - spec: "192.0.2.231/32"
            options: "rw,sync"
          - spec: "192.0.2.232/32"
            options: "rw,sync"
      - path: /export/k3s-etcd
        clients:
          - spec: "192.0.2.221/32"
            options: "rw,sync"
          - spec: "192.0.2.222/32"
            options: "rw,sync"
    """
)


@pytest.fixture
def k3s_repo(repo: Path) -> Path:
    write(repo, gate.HOSTS_YML, K3S_HOSTS)
    write(repo, NAS_YML, K3S_EXPORTS)
    return repo


def test_a_group_admitted_in_full_passes(k3s_repo: Path) -> None:
    """A servers-only export is a scope, not a gap: only a partial group fails."""
    assert gate.check(k3s_repo) == []
    assert gate.main(["--repo-root", str(k3s_repo)]) == 0


def test_an_export_missing_one_agent_fails(k3s_repo: Path) -> None:
    write(
        k3s_repo,
        NAS_YML,
        K3S_EXPORTS.replace('      - spec: "192.0.2.232/32"\n        options: "rw,sync"\n', "", 1),
    )
    problems = gate.check(k3s_repo)
    assert any(
        "/export/appdata" in p and "k3s_agents" in p and "192.0.2.232" in p for p in problems
    )
    assert gate.main(["--repo-root", str(k3s_repo)]) == 1


def test_a_group_whose_members_are_all_absent_is_not_a_partial(k3s_repo: Path) -> None:
    """/export/k3s-etcd names no agent at all, which is its design, not drift."""
    assert not any("k3s_agents" in p for p in gate.check(k3s_repo))


def test_an_export_mixing_a_cidr_with_32s_is_not_group_checked(k3s_repo: Path) -> None:
    """A CIDR client already admits the whole group, so the /32s beside it are an
    addition rather than an exhaustive list."""
    without_agent = K3S_EXPORTS.replace(
        '      - spec: "192.0.2.232/32"\n        options: "rw,sync"\n', "", 1
    )
    write(
        k3s_repo,
        NAS_YML,
        without_agent.replace(
            '  - path: /export/appdata\n    clients:\n',
            '  - path: /export/appdata\n    clients:\n'
            '      - spec: "192.0.2.0/24"\n        options: "ro,sync"\n',
            1,
        ),
    )
    assert not any("k3s_agents" in p for p in gate.check(k3s_repo))


# --- Multi-VLAN scope --------------------------------------------------------
# A site that separates management from storage or a DMZ names one
# cluster-config key per network; the seam is ANY-match membership.

STORAGE_SLICE = textwrap.dedent(
    """\
    apiVersion: discovery.k8s.io/v1
    kind: EndpointSlice
    metadata:
      name: storage
    endpoints:
      - addresses:
          - 198.51.100.40
    """
)


def test_a_second_network_is_out_of_scope_until_its_key_is_named(repo: Path) -> None:
    write(repo, gate.CLUSTER_CONFIG, CONFIG + '  cluster_storage_cidr: "198.51.100.0/24"\n')
    write(repo, "kubernetes/apps/vm-ingress/storage.yaml", STORAGE_SLICE)
    assert gate.check(repo) == []
    problems = gate.check(repo, ["cluster_lan_cidr", "cluster_storage_cidr"])
    assert any("198.51.100.40" in p for p in problems)
    assert gate.main([
        "--repo-root", str(repo),
        "--lan-cidr-key", "cluster_lan_cidr",
        "--lan-cidr-key", "cluster_storage_cidr",
    ]) == 1


def test_an_extra_cidr_literal_widens_the_scope_too(repo: Path) -> None:
    """A network the cluster-config does not declare still gets a scope."""
    write(repo, "kubernetes/apps/vm-ingress/storage.yaml", STORAGE_SLICE)
    problems = gate.check(repo, None, ["198.51.100.0/24"])
    assert any("198.51.100.40" in p for p in problems)


def test_a_named_key_the_config_does_not_declare_is_vacuous(repo: Path) -> None:
    with pytest.raises(gate.Vacuous):
        gate.check(repo, ["cluster_storage_cidr"])


def test_a_non_utf8_hosts_yml_exits_2(repo: Path, capsys) -> None:
    """A file the gate cannot decode is an operator error, never agreement."""
    (repo / gate.HOSTS_YML).write_bytes(b"all:\n  hosts:\n    \xff\xfe:\n")
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert "inspected nothing" in capsys.readouterr().err


def test_a_non_utf8_manifest_is_reported_not_dropped(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/broken.yaml", "")
    (repo / "kubernetes/apps/demo/broken.yaml").write_bytes(b"kind: \xff\xfe\n")
    assert any("broken.yaml unparseable" in p for p in gate.check(repo))
