"""Failure-path tests for scripts/check-grafana-sidecar-init.py.

An init container that watches never exits and a long-running one that lists
crash-loops, so the gate must report either; helm is stubbed, no chart pull.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import REPO, SCRIPTS, load_script

GATE = SCRIPTS / "check-grafana-sidecar-init.py"
CHART_URL = "https://charts.example.test/prometheus-community"


@pytest.fixture(scope="module")
def gate():
    return load_script(GATE)


def _container(name: str, method: str | None) -> dict:
    env = [{"name": "RESOURCE", "value": "configmap"}]
    if method is not None:
        env.append({"name": "METHOD", "value": method})
    return {"name": name, "env": env}


def _deployment(
    init_method: str | None,
    container: str = "grafana-init-sc-datasources",
    dashboard_method: str | None = "WATCH",
) -> dict:
    """The Grafana Deployment as the chart renders it: one sidecar in each position."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "kube-prometheus-stack-grafana"},
        "spec": {"template": {"spec": {
            "initContainers": [_container(container, init_method)],
            "containers": [
                {"name": "grafana"},
                _container("grafana-sc-dashboard", dashboard_method),
            ],
        }}},
    }


class TestSidecarScan:
    def test_both_positions_are_scanned_with_their_own_required_mode(self, gate):
        assert gate.sidecars([_deployment("LIST")]) == [
            ("kube-prometheus-stack-grafana", "grafana-init-sc-datasources",
             "initContainers", "LIST"),
            ("kube-prometheus-stack-grafana", "grafana-sc-dashboard",
             "containers", "WATCH"),
        ]

    def test_a_container_without_the_sidecar_marker_is_not_scanned(self, gate):
        """Mirrors the chart: only `*-sc-*` containers are k8s-sidecar."""
        names = [name for _, name, _, _ in
                 gate.sidecars([_deployment("WATCH", container="init-chown-data")])]
        assert names == ["grafana-sc-dashboard"]


# A values block small enough to read, carrying the two knobs under test.
VALUES = {
    "grafana": {"sidecar": {
        "datasources": {"resource": "configmap", "initDatasources": True},
        "dashboards": {"resource": "configmap", "enabled": True},
    }},
}


def _fixture_repo(
    tmp_path: Path, watch_method: str | None, dashboard_method: str | None = None
) -> Path:
    """A tree with just the three files the gate reads."""
    values = yaml.safe_load(yaml.safe_dump(VALUES))
    if watch_method is not None:
        values["grafana"]["sidecar"]["datasources"]["watchMethod"] = watch_method
    if dashboard_method is not None:
        values["grafana"]["sidecar"]["dashboards"]["watchMethod"] = dashboard_method
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "kube-prometheus-stack", "namespace": "observability"},
        "spec": {
            "targetNamespace": "observability",
            "chart": {"spec": {"chart": "kube-prometheus-stack",
                               "version": "${helm_chart_versions_kube_prometheus_stack}",
                               "sourceRef": {"kind": "HelmRepository",
                                             "name": "prometheus-community",
                                             "namespace": "flux-system"}}},
            "values": values,
        },
    }
    manifest = tmp_path / "kubernetes/infrastructure/observability/kube-prometheus-stack"
    manifest.mkdir(parents=True)
    (manifest / "release.yaml").write_text(yaml.safe_dump(release, sort_keys=False))
    sources = tmp_path / "kubernetes/infrastructure/sources"
    sources.mkdir(parents=True)
    (sources / "versions-configmap.yaml").write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cluster-versions"},
        "data": {"helm_chart_versions_kube_prometheus_stack": "92.1.1",
                 "k3s_version": "v1.37.1+k3s1"},
    }))
    (sources / "prometheus-community.yaml").write_text(yaml.safe_dump({
        "apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "HelmRepository",
        "metadata": {"name": "prometheus-community", "namespace": "flux-system"},
        "spec": {"url": CHART_URL},
    }))
    (sources / "cluster-config.yaml").write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cluster-config"},
        "data": {"cluster_internal_domain": "example.test"},
    }))
    return tmp_path


def _stub_helm(tmp_path: Path) -> Path:
    """A `helm` that renders both sidecar METHODs straight from the values.

    Stands in for the chart's own template, so these cases exercise the gate
    rather than the network.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "helm"
    stub.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import sys, yaml
        argv = sys.argv[1:]
        values = yaml.safe_load(open(argv[argv.index("-f") + 1]).read()) or {}
        sidecar = values["grafana"]["sidecar"]

        def env(block):
            # WATCH is the chart's own default for an unset watchMethod.
            return [{"name": "RESOURCE", "value": block["resource"]},
                    {"name": "METHOD", "value": block.get("watchMethod", "WATCH")}]

        print(yaml.safe_dump({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "kube-prometheus-stack-grafana"},
            "spec": {"template": {"spec": {
                "initContainers": [{"name": "grafana-init-sc-datasources",
                                    "env": env(sidecar["datasources"])}],
                "containers": [
                    {"name": "grafana"},
                    {"name": "grafana-sc-dashboard",
                     "env": env(sidecar["dashboards"])},
                ],
            }}},
        }))
        '''))
    stub.chmod(0o755)
    return bin_dir


def _stub_shell_helm(tmp_path: Path, body: str) -> Path:
    """A `helm` that runs one shell body, for the failure paths below."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "helm"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return bin_dir


def _run(root: Path, bin_dir: Path, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(root)],
        capture_output=True, text=True, check=False, cwd=REPO, env=env,
    )


# An init sidecar that watches never exits; a long-running one that lists
# crash-loops. An unset watchMethod renders the chart's default, WATCH.
@pytest.mark.parametrize(
    ("init_method", "dashboard_method", "rc", "fragments"),
    [
        pytest.param(None, None, 1, ("init container grafana-init-sc-datasources",
                                     "METHOD=WATCH, not LIST"), id="init-unset"),
        pytest.param("WATCH", None, 1, ("METHOD=WATCH, not LIST",), id="init-watch"),
        pytest.param("LIST", None, 0, (), id="init-list-passes"),
        pytest.param("LIST", "LIST", 1, ("long-running container grafana-sc-dashboard",
                                         "METHOD=LIST, not WATCH"), id="long-running-list"),
        pytest.param("LIST", "WATCH", 0, (), id="long-running-watch-passes"),
    ],
)
def test_each_position_requires_its_mode(tmp_path, init_method, dashboard_method, rc,
                                         fragments):
    result = _run(_fixture_repo(tmp_path / "tree", init_method, dashboard_method),
                  _stub_helm(tmp_path))
    assert result.returncode == rc, result.stdout + result.stderr
    for fragment in fragments:
        assert fragment in result.stdout


def test_a_missing_manifest_is_exit_2_not_a_finding(tmp_path):
    """The could-not-inspect arm: 1 would read as a policy violation."""
    (tmp_path / "tree").mkdir()
    result = _run(tmp_path / "tree", _stub_helm(tmp_path))
    assert result.returncode == 2, result.stdout + result.stderr


def test_a_render_with_no_sidecar_at_all_is_exit_2_not_a_pass(tmp_path):
    """A gate that inspects nothing is not a gate: a renamed container is an
    operator error, not silent coverage loss."""
    bin_dir = _stub_shell_helm(
        tmp_path,
        "echo 'apiVersion: v1'\necho 'kind: ConfigMap'\necho 'metadata:'\n"
        "echo '  name: nothing'",
    )
    result = _run(_fixture_repo(tmp_path / "tree", "LIST"), bin_dir)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "checked nothing" in result.stderr


@pytest.mark.parametrize(
    ("missing", "rendered", "method"),
    [
        ("containers", "initContainers", "LIST"),
        ("initContainers", "containers", "WATCH"),
    ],
)
def test_a_render_missing_one_position_is_exit_2_not_a_pass(
    tmp_path, missing, rendered, method
):
    """Half the subject is still coverage lost in silence: the values enable a
    sidecar in both positions, and the one that renders is in the right mode."""
    bin_dir = _stub_shell_helm(tmp_path, textwrap.dedent(f"""\
        cat <<'EOF'
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: kube-prometheus-stack-grafana
        spec:
          template:
            spec:
              {rendered}:
                - name: grafana-sc-only
                  env:
                    - name: METHOD
                      value: {method}
        EOF"""))
    result = _run(_fixture_repo(tmp_path / "tree", "LIST"), bin_dir)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "checked nothing" in result.stderr
    assert missing in result.stderr


class TestCannotInspect:
    """Every read, render and parse failure is exit 2 with one ERROR line.

    Exit 1 means a watching init container, so a broken environment reaching it
    reads as a policy violation; a traceback is not a gate message either.
    """

    @staticmethod
    def _assert_clean_exit_2(result):
        assert result.returncode == 2, result.stdout + result.stderr
        assert "Traceback" not in result.stderr, result.stderr
        assert result.stderr.strip().startswith("ERROR:"), result.stderr
        assert len(result.stderr.strip().splitlines()) == 1, result.stderr

    def test_a_non_yaml_render_is_not_a_finding(self, tmp_path):
        bin_dir = _stub_shell_helm(tmp_path, "echo 'spec: [unclosed'")
        self._assert_clean_exit_2(_run(_fixture_repo(tmp_path / "tree", "LIST"), bin_dir))

    def test_a_failing_helm_is_not_a_finding(self, tmp_path):
        bin_dir = _stub_shell_helm(tmp_path, "echo 'Error: chart not found' >&2\nexit 1")
        self._assert_clean_exit_2(_run(_fixture_repo(tmp_path / "tree", "LIST"), bin_dir))

    # The reused validator exits 1 by itself, so its failures need wrapping.
    @pytest.mark.parametrize(
        ("relpath", "content"),
        [
            pytest.param("kubernetes/infrastructure/sources/versions-configmap.yaml",
                         "data: [unclosed\n", id="versions-malformed"),
            pytest.param("kubernetes/infrastructure/sources/versions-configmap.yaml",
                         "- a list, not a ConfigMap\n", id="versions-not-a-mapping"),
            pytest.param("kubernetes/infrastructure/sources/cluster-config.yaml",
                         "data: [unclosed\n", id="cluster-config-malformed"),
            pytest.param("kubernetes/infrastructure/observability/kube-prometheus-stack/"
                         "release.yaml", "---\nkind: ConfigMap\nmetadata:\n  name: nope\n",
                         id="manifest-not-a-helmrelease"),
        ],
    )
    def test_a_corrupt_input_file_is_not_a_finding(self, tmp_path, relpath, content):
        root = _fixture_repo(tmp_path / "tree", "LIST")
        (root / relpath).write_text(content)
        self._assert_clean_exit_2(_run(root, _stub_helm(tmp_path)))


def test_the_real_release_pins_list_for_every_init_sidecar():
    """The values-level invariant, with no chart pull: an init sidecar the real
    HelmRelease enables must carry watchMethod: LIST."""
    text = (REPO / "kubernetes/infrastructure/observability/kube-prometheus-stack"
            / "release.yaml").read_text(encoding="utf-8")
    sidecar = yaml.safe_load(text)["spec"]["values"]["grafana"]["sidecar"]
    enabled = {
        kind: block
        for kind, block in sidecar.items()
        if isinstance(block, dict) and block.get(f"init{kind.capitalize()}") is True
    }
    assert enabled, "no grafana sidecar runs in init mode — drop this assertion with the knob"
    wrong = {kind: block.get("watchMethod") for kind, block in enabled.items()
             if block.get("watchMethod") != "LIST"}
    assert not wrong, (
        "a grafana sidecar runs in init mode without watchMethod: LIST, so its init "
        f"container watches forever and the pod never leaves PodInitializing: {wrong}"
    )


def test_the_render_uses_the_url_of_the_named_helmrepository(tmp_path):
    bin_dir = _stub_shell_helm(tmp_path, f'echo "$@" > {tmp_path}/argv; exit 1')
    _run(_fixture_repo(tmp_path / "tree", "LIST"), bin_dir)
    argv = (tmp_path / "argv").read_text().split()
    assert argv[argv.index("--repo") + 1] == CHART_URL


def test_a_missing_helmrepository_is_exit_2_not_a_finding(tmp_path):
    root = _fixture_repo(tmp_path / "tree", "LIST")
    (root / "kubernetes/infrastructure/sources/prometheus-community.yaml").unlink()
    result = _run(root, _stub_helm(tmp_path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no HelmRepository 'prometheus-community'" in result.stderr


# `task flux:lint` and the CI flux-lint job's extra_validation must both run the
# gate; both carry the helm it needs to render the chart.
_CALLERS = ("taskfiles/flux.yml", ".gitlab-ci.yml")


@pytest.mark.parametrize("caller", _CALLERS)
def test_the_gate_is_wired_into_both_callers(caller):
    """A gate nothing calls enforces nothing."""
    path = REPO / caller
    assert path.is_file(), f"{caller} is missing"
    assert "scripts/check-grafana-sidecar-init.py" in path.read_text(encoding="utf-8"), (
        f"{caller} does not run scripts/check-grafana-sidecar-init.py, so the "
        "chart-rendered sidecar mode is ungated there"
    )
