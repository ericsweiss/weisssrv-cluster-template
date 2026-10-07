"""Coverage for check-cluster-literals.py.

The live tree is in step, so it proves nothing about failure. Each arm runs
against a fixture repository, and every mutation case must FAIL.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script

gate = load_script("check-cluster-literals.py")

# A sample value per cluster-config key the gate can check. Filtered to the keys
# this render's gate actually lists, so a seam that drops one keeps the fixture.
SAMPLES = {
    "cluster_internal_domain": "example.lan",
    "cluster_external_domain": "example.test",
    "cluster_node_label_domain": "example.lan",
    "cluster_lan_cidr": "10.9.0.0/24",
    "cluster_pod_cidr": "10.244.0.0/16",
    "cluster_service_cidr": "10.245.0.0/16",
    "cluster_tailnet_cidr": "100.64.0.0/10",
    "cluster_metallb_public_vip": "10.9.0.100",
    "cluster_metallb_internal_vip": "10.9.0.101",
    "cluster_api_vip": "10.9.0.161",
    "cluster_k3s_api_vip": "10.9.0.161",
    "cluster_timezone": "Atlantic/Reykjavik",
    "cluster_upstream_dns_servers": "10.9.0.150 10.9.0.160",
}


def config_values() -> dict[str, str]:
    return {key: SAMPLES[key] for key in sorted(gate.REQUIRED_KEYS)}


def stage(name: str, path: str) -> str:
    """One Flux Kustomization: the gate derives its scan set from these."""
    return textwrap.dedent(
        f"""\
        apiVersion: kustomize.toolkit.fluxcd.io/v1
        kind: Kustomization
        metadata:
          name: {name}
        spec:
          path: ./{path}
          postBuild:
            substituteFrom:
              - kind: ConfigMap
                name: cluster-config
        """
    )


def write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def write_config(repo: Path, values: dict[str, str]) -> None:
    write(
        repo,
        gate.CLUSTER_CONFIG,
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "cluster-config"},
                "data": values,
            }
        ),
    )


def write_inventory(repo: Path, values: dict[str, str]) -> None:
    """The mirror side, built from the gate's own mirror maps."""
    docs: dict[str, dict] = {gate.ANSIBLE_ALL: {}, gate.ANSIBLE_K3S: {}}
    for key, (path, var) in {**gate.INVENTORY_MIRRORS, **gate.SECONDARY_MIRRORS}.items():
        docs.setdefault(path, {})[var] = values[key]
    if gate.DNS_SERVERS_KEY:
        docs[gate.ANSIBLE_ALL]["dns_servers"] = values[gate.DNS_SERVERS_KEY].split()
    for path, doc in docs.items():
        write(repo, path, yaml.safe_dump(doc))


def edit_inventory(repo: Path, path: str, **changes) -> None:
    doc = yaml.safe_load((repo / path).read_text())
    doc.update(changes)
    write(repo, path, yaml.safe_dump(doc))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    values = config_values()
    write_config(tmp_path, values)
    write_inventory(tmp_path, values)
    (tmp_path / "kubernetes/apps/demo").mkdir(parents=True)
    (tmp_path / "kubernetes/infrastructure/observability").mkdir(parents=True)
    for extra in gate.EXTRA_TREES:
        (tmp_path / extra).mkdir(parents=True, exist_ok=True)
    write(tmp_path, f"{gate.CLUSTER_DIR}/apps.yaml", stage("apps", "kubernetes/apps"))
    write(
        tmp_path,
        f"{gate.CLUSTER_DIR}/infrastructure-observability.yaml",
        stage("infrastructure-observability", "kubernetes/infrastructure/observability"),
    )
    return tmp_path


def run(repo: Path) -> list[str]:
    config = gate.load_config(repo)
    trees = gate.substituted_trees(repo)
    return gate.check_literals(repo, config, trees) + gate.check_inventory(repo, config)


def test_every_key_the_gate_checks_has_a_fixture_value() -> None:
    missing = sorted(gate.REQUIRED_KEYS - set(SAMPLES))
    assert not missing, f"SAMPLES has no value for {missing}, so the fixture is smaller"


def test_a_clean_tree_passes(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/route.yaml", """\
        apiVersion: traefik.io/v1alpha1
        kind: IngressRoute
        spec:
          routes:
            - match: Host(`app.${cluster_internal_domain}`)
        """)
    assert run(repo) == []
    assert gate.main(["--repo-root", str(repo)]) == 0


def test_a_domain_literal_is_reported(repo: Path) -> None:
    """Mutation case: the hard-coded identity this gate exists to catch."""
    write(repo, "kubernetes/apps/demo/route.yaml", """\
        apiVersion: traefik.io/v1alpha1
        kind: IngressRoute
        spec:
          routes:
            - match: Host(`app.example.lan`)
        """)
    assert any("example.lan" in v for v in run(repo))
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_yaml_comment_is_not_content(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/cm.yaml", """\
        # app.example.lan is the internal name.
        apiVersion: v1
        kind: ConfigMap
        data: {}
        """)
    assert run(repo) == []


def test_a_comment_inside_a_block_scalar_is_not_content(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/corefile.yaml", """\
        apiVersion: v1
        kind: ConfigMap
        data:
          Corefile: |
            # Split-DNS scopes queries to example.lan.
            . {
                forward . ${cluster_internal_domain}
            }
        """)
    assert run(repo) == []


def test_an_escaped_regex_spelling_is_exempt(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/runner.yaml", """\
        apiVersion: v1
        kind: ConfigMap
        data:
          config: |
            node_selector_overwrite_allowed = "^example\\\\.lan/cpu=(modern|legacy)$"
        """)
    assert run(repo) == []


def test_an_address_literal_is_reported(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/svc.yaml", """\
        apiVersion: v1
        kind: Service
        metadata:
          annotations:
            metallb.io/loadBalancerIPs: 10.9.0.101
        """)
    assert any("10.9.0.101" in v for v in run(repo))


def test_an_address_literal_is_exempt_inside_a_networkpolicy(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/networkpolicy.yaml", """\
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        spec:
          egress:
            - to:
                - ipBlock: {cidr: 10.9.0.101/32}
        """)
    assert run(repo) == []


def test_an_address_literal_is_exempt_in_the_rules_tree(repo: Path) -> None:
    write(repo, f"{gate.RULES_TREE}/infrastructure.yaml", """\
        apiVersion: monitoring.coreos.com/v1
        kind: PrometheusRule
        spec:
          groups:
            - name: vip
              rules:
                - expr: absent(x{ip="10.9.0.101"})
        """)
    assert run(repo) == []


def test_cluster_config_itself_is_not_scanned(repo: Path) -> None:
    """It declares the values, so it is the one manifest spelling them literally."""
    assert gate.CLUSTER_CONFIG.startswith("kubernetes/infrastructure/sources/")
    write(
        repo,
        f"{gate.CLUSTER_DIR}/infrastructure-sources.yaml",
        stage("infrastructure-sources", "kubernetes/infrastructure/sources"),
    )
    assert run(repo) == []


def test_a_per_node_address_is_not_reported(repo: Path) -> None:
    """Per-guest addresses are inventory data and stay literal."""
    write(repo, "kubernetes/apps/demo/endpointslice.yaml", """\
        apiVersion: discovery.k8s.io/v1
        kind: EndpointSlice
        endpoints:
          - addresses: ["10.9.0.57"]
        """)
    assert run(repo) == []


def test_inventory_drift_is_reported(repo: Path) -> None:
    edit_inventory(repo, gate.ANSIBLE_ALL, internal_domain="moved.lan")
    assert any("cluster_internal_domain" in v for v in run(repo))


def test_a_metallb_vip_drift_is_reported(repo: Path) -> None:
    edit_inventory(repo, gate.ANSIBLE_ALL, metallb_internal_vip="10.9.0.109")
    assert any("cluster_metallb_internal_vip" in v for v in run(repo))


def test_a_mismatched_timezone_mirror_is_reported(repo: Path) -> None:
    edit_inventory(repo, gate.ANSIBLE_ALL, timezone="UTC")
    assert any("cluster_timezone" in v for v in run(repo))


@pytest.mark.skipif(not gate.DNS_SERVERS_KEY, reason="no resolver key in cluster-config")
def test_a_resolver_list_drift_is_reported(repo: Path) -> None:
    edit_inventory(repo, gate.ANSIBLE_ALL, dns_servers=["10.9.0.150", "10.9.0.161"])
    assert any(gate.DNS_SERVERS_KEY in v for v in run(repo))


@pytest.mark.skipif(not gate.DNS_SERVERS_KEY, reason="no resolver key in cluster-config")
def test_a_missing_resolver_mirror_is_a_violation_not_a_skip(repo: Path) -> None:
    doc = yaml.safe_load((repo / gate.ANSIBLE_ALL).read_text())
    del doc["dns_servers"]
    write(repo, gate.ANSIBLE_ALL, yaml.safe_dump(doc))
    assert any("dns_servers not found" in v for v in run(repo))


def test_a_missing_inventory_variable_is_a_violation_not_a_skip(repo: Path) -> None:
    doc = yaml.safe_load((repo / gate.ANSIBLE_ALL).read_text())
    del doc["internal_domain"]
    write(repo, gate.ANSIBLE_ALL, yaml.safe_dump(doc))
    assert any("internal_domain not found" in v for v in run(repo))


def test_a_new_vip_key_is_scanned_without_editing_the_gate(repo: Path) -> None:
    write_config(repo, {**config_values(), "cluster_extra_vip": "10.9.0.98"})
    write(repo, "kubernetes/apps/demo/svc.yaml", """\
        apiVersion: v1
        kind: Service
        metadata:
          annotations:
            metallb.io/loadBalancerIPs: 10.9.0.98
        """)
    assert any("10.9.0.98" in v and "cluster_extra_vip" in v for v in run(repo))


def test_a_new_vip_key_with_no_mirror_arm_is_reported(repo: Path) -> None:
    write_config(repo, {**config_values(), "cluster_extra_vip": "10.9.0.98"})
    assert any(
        "cluster_extra_vip" in v and "mirrored by no arm" in v
        for v in gate.check_inventory(repo, gate.load_config(repo))
    )


def test_a_substituted_tree_is_derived_not_hand_listed(repo: Path) -> None:
    """A new stage brings its tree into the scan without editing the gate."""
    write(
        repo,
        f"{gate.CLUSTER_DIR}/infrastructure-metrics-server.yaml",
        stage("infrastructure-metrics-server", "kubernetes/infrastructure/metrics-server"),
    )
    write(repo, "kubernetes/infrastructure/metrics-server/release.yaml", """\
        apiVersion: v1
        kind: ConfigMap
        data:
          host: metrics.example.lan
        """)
    assert "kubernetes/infrastructure/metrics-server" in gate.substituted_trees(repo)
    assert any("metrics-server/release.yaml" in v for v in run(repo))


def test_a_nested_stage_path_does_not_double_the_scan(repo: Path) -> None:
    write(
        repo,
        f"{gate.CLUSTER_DIR}/infrastructure-nested.yaml",
        stage("infrastructure-nested", "kubernetes/apps/demo"),
    )
    trees = gate.substituted_trees(repo)
    assert "kubernetes/apps" in trees and "kubernetes/apps/demo" not in trees


def test_a_stage_path_absent_from_disk_is_vacuous(repo: Path) -> None:
    write(
        repo,
        f"{gate.CLUSTER_DIR}/infrastructure-gone.yaml",
        stage("infrastructure-gone", "kubernetes/infrastructure/gone"),
    )
    with pytest.raises(gate.Vacuous):
        gate.substituted_trees(repo)


def test_no_substituting_stage_at_all_is_vacuous(repo: Path) -> None:
    for path in (repo / gate.CLUSTER_DIR).glob("*.yaml"):
        path.unlink()
    with pytest.raises(gate.Vacuous):
        gate.substituted_trees(repo)


def test_a_missing_cluster_config_is_vacuous_not_a_traceback(repo: Path) -> None:
    (repo / gate.CLUSTER_CONFIG).unlink()
    with pytest.raises(gate.Vacuous):
        gate.load_config(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_an_empty_cluster_config_data_block_is_vacuous(repo: Path) -> None:
    write(repo, gate.CLUSTER_CONFIG, "data: {}\n")
    with pytest.raises(gate.Vacuous):
        gate.load_config(repo)


@pytest.mark.parametrize("key", ["cluster_internal_domain", "cluster_metallb_public_vip"])
def test_a_disappeared_key_is_vacuous_not_a_quiet_pass(repo: Path, key: str) -> None:
    """Each arm skips an absent key, so the whole gate would shrink in silence."""
    config = gate.load_config(repo)
    del config[key]
    with pytest.raises(gate.Vacuous) as excinfo:
        gate.require_keys(config)
    assert key in str(excinfo.value)


def test_the_success_count_reflects_the_checks_that_ran(repo: Path) -> None:
    config = gate.load_config(repo)
    full = gate.mirror_check_count(config)
    assert full == (
        len(gate.INVENTORY_MIRRORS)
        + len(gate.SECONDARY_MIRRORS)
        + (1 if gate.DNS_SERVERS_KEY else 0)
    )
    del config["cluster_metallb_public_vip"]
    assert gate.mirror_check_count(config) == full - 1


def test_a_dashboard_json_literal_is_reported(repo: Path) -> None:
    """Dashboard JSON renders into a substituted manifest, so it is scanned too."""
    write(repo, "kubernetes/infrastructure/observability/dashboards/x.json", """\
        {"panels": [{"expr": "probe_success{instance=\\"https://git.example.lan\\"}"}]}
        """)
    assert any("x.json" in v for v in run(repo))


def test_a_hash_inside_dashboard_json_is_still_scanned(repo: Path) -> None:
    """JSON has no comment syntax: a `#` there is data, not a comment."""
    write(repo, "kubernetes/infrastructure/observability/dashboards/y.json", """\
        {"panels": [{"title": "load # host app.example.lan"}]}
        """)
    assert any("y.json" in v for v in run(repo))


def test_a_generator_source_comment_is_not_content(repo: Path) -> None:
    write(repo, "kubernetes/infrastructure/observability/gen.py", """\
        # The zone is example.test in production.
        ZONE = "${cluster_external_domain}"
        """)
    assert run(repo) == []


def test_a_trailing_comment_is_not_content(repo: Path) -> None:
    write(repo, "kubernetes/infrastructure/observability/gen.py", """\
        ZONE = "${cluster_external_domain}"  # resolves to example.test
        """)
    assert run(repo) == []


def test_markdown_beside_a_manifest_is_not_scanned(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/README.md", "Reachable at app.example.lan.\n")
    assert run(repo) == []


def test_a_hard_coded_timezone_is_a_violation() -> None:
    """cluster_timezone has no netpol/rules exemption: nothing parses a TZ pre-Flux."""
    hits = gate.scan_text("apps/x/deployment.yaml", 'TZ: "Atlantic/Reykjavik"', [],
                          {"Atlantic/Reykjavik": "cluster_timezone"})
    assert hits and "cluster_timezone" in hits[0]


def test_the_timezone_placeholder_is_not_a_violation() -> None:
    assert gate.scan_text("apps/x/deployment.yaml", 'TZ: "${cluster_timezone}"', [],
                          {"Atlantic/Reykjavik": "cluster_timezone"}) == []


def test_the_longest_matching_domain_is_the_one_reported() -> None:
    """A domain spelled inside a longer one must not double-report."""
    hits = gate.scan_text(
        "apps/x/route.yaml", "host: app.lan.example.test",
        ["example.test", "lan.example.test"], {},
    )
    assert hits == [
        "apps/x/route.yaml: literal 'lan.example.test' — use the cluster-config placeholder"
    ]


def test_the_longest_matching_address_is_the_one_reported() -> None:
    """Same for an address spelled inside a CIDR: the CIDR owns the hit."""
    hits = gate.scan_text(
        "apps/x/policy.yaml", "cidr: 10.9.0.0/24", [],
        {"10.9.0.0/24": "cluster_lan_cidr", "10.9.0.0": "cluster_lan_network"},
    )
    assert hits == ["apps/x/policy.yaml: literal '10.9.0.0/24' — use ${cluster_lan_cidr}"]


@pytest.mark.parametrize(
    "target", ["cluster_config", "ansible_all", "cluster_dir_stage", "scanned_manifest"]
)
def test_an_unparseable_gate_input_is_vacuous_not_a_traceback(repo: Path, target: str) -> None:
    """A manifest the gate cannot parse is exit 2, not a literal-drift finding:
    conflating the two sends the reader hunting a hard-coded domain."""
    rel = {
        "cluster_config": gate.CLUSTER_CONFIG,
        "ansible_all": gate.ANSIBLE_ALL,
        "cluster_dir_stage": f"{gate.CLUSTER_DIR}/apps.yaml",
        "scanned_manifest": "kubernetes/apps/demo/release.yaml",
    }[target]
    write(repo, rel, "a: [1,\n  b: {\n")
    with pytest.raises(gate.Vacuous):
        run(repo)
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_the_live_tree_is_placeholder_only() -> None:
    assert gate.main(["--repo-root", str(REPO)]) == 0
