"""Platform HelmRelease values whose failure is silent until the cluster runs them.

Each case reads the shipped manifest; the post-renderer case renders its patch
through kustomize, the engine Flux post-renders with.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import (
    REPO,
    assert_all_parsed,
    copier_answers,
    helm_release,
    k8s_documents,
    release_values,
    require_tool,
)

K8S = REPO / "kubernetes"
CONTROLLERS = K8S / "infrastructure" / "controllers"
OBSERVABILITY = K8S / "infrastructure" / "observability"
INVENTORY = REPO / "ansible" / "inventories" / "prod"
GROUP_VARS = INVENTORY / "group_vars"

CONNECT_RELEASE = CONTROLLERS / "onepassword-connect" / "release.yaml"
KPS_RELEASE = OBSERVABILITY / "kube-prometheus-stack" / "release.yaml"
METRICS_SERVER_RELEASE = CONTROLLERS / "metrics-server" / "release.yaml"
K3S_VARS = GROUP_VARS / "k3s.yml"

# Connect ships only on the 1Password secrets backend.
onepassword_only = pytest.mark.skipif(
    copier_answers()["secrets_backend"] != "onepassword",
    reason="secrets backend is not 1Password",
)


def chart_shaped_connect(namespace: str) -> dict:
    """The Deployment the Connect chart renders, reduced to what the patch
    targets. The chart writes metadata.namespace explicitly, so the patch
    carries it too or it matches nothing."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "onepassword-connect", "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": {"app": "onepassword-connect"}},
            "template": {
                "metadata": {"labels": {"app": "onepassword-connect"}},
                "spec": {"containers": [{"name": "connect-api", "image": "connect-api"}]},
            },
        },
    }


def _post_render(release: dict, base: dict, workdir: Path) -> str:
    """kustomize build of `base` with the release's post-renderer patches."""
    patches = []
    for renderer in (release.get("spec") or {}).get("postRenderers") or []:
        patches += (renderer.get("kustomize") or {}).get("patches") or []
    assert patches, "the release carries no kustomize post-renderer patch"
    (workdir / "chart.yaml").write_text(yaml.safe_dump(base))
    (workdir / "kustomization.yaml").write_text(
        yaml.safe_dump({"resources": ["chart.yaml"], "patches": patches})
    )
    built = subprocess.run(
        ["kustomize", "build", str(workdir)], capture_output=True, text=True, check=False
    )
    assert built.returncode == 0, f"the post-renderer patch does not apply:\n{built.stderr}"
    return built.stdout


@onepassword_only
def test_the_connect_deployment_mounts_no_service_account_token(tmp_path):
    """Connect never calls the Kubernetes API. The chart exposes no automount
    value, so only the post-renderer patch removes the projected token."""
    assert CONNECT_RELEASE.is_file(), "the 1Password backend is on but its release moved"
    require_tool("kustomize", "connect post-renderer")
    release = helm_release(CONNECT_RELEASE)
    spec = release.get("spec") or {}
    chart = chart_shaped_connect(spec.get("targetNamespace") or release["metadata"]["namespace"])
    rendered = yaml.safe_load(_post_render(release, chart, tmp_path))
    assert rendered["spec"]["template"]["spec"].get("automountServiceAccountToken") is False


def test_grafana_liveness_is_an_http_probe():
    """The grafana image is distroless, so an exec probe has nothing to run and
    the kubelet reports an unknown result on every period."""
    probe = (release_values(KPS_RELEASE).get("grafana") or {}).get("livenessProbe") or {}
    assert "exec" not in probe, "grafana livenessProbe execs a command the image lacks"
    http = probe.get("httpGet") or {}
    assert http.get("path") == "/api/health" and http.get("port") == 3000, probe


def metrics_server_halves(k3s_vars: dict, release_present: bool) -> list[str]:
    """The disabled AddOn and the Flux release are one unit."""
    disabled = "metrics-server" in (k3s_vars.get("k3s_disable") or [])
    if disabled and not release_present:
        return ["k3s_disable drops the AddOn but no Flux release replaces it"]
    if release_present and not disabled:
        return ["the Flux release and the k3s AddOn both claim the metrics.k8s.io APIService"]
    return []


def test_metrics_server_is_replaced_not_doubled():
    assert K3S_VARS.is_file(), "group_vars/k3s.yml moved; re-point this suite"
    k3s_vars = yaml.safe_load(K3S_VARS.read_text()) or {}
    problems = metrics_server_halves(k3s_vars, METRICS_SERVER_RELEASE.is_file())
    assert not problems, problems


def test_one_half_of_the_metrics_server_unit_is_reported():
    assert metrics_server_halves({"k3s_disable": ["metrics-server"]}, False)
    assert metrics_server_halves({"k3s_disable": ["traefik"]}, True)
    assert metrics_server_halves({"k3s_disable": ["metrics-server"]}, True) == []


# API groups whose CRDs exist before the controllers stage applies: Flux's own,
# installed by bootstrap, and monitoring.coreos.com from the crds stage.
EARLIER_STAGE_GROUPS = {
    "helm.toolkit.fluxcd.io",
    "kustomize.toolkit.fluxcd.io",
    "source.toolkit.fluxcd.io",
    "notification.toolkit.fluxcd.io",
    "monitoring.coreos.com",
}


# Built-in API groups the apiserver serves without a CRD. A `*.k8s.io` suffix
# alone is no proof: gateway.networking.k8s.io is a chart-installed CRD.
BUILTIN_GROUPS = {
    "", "apps", "batch", "policy", "autoscaling",
    "admissionregistration.k8s.io", "apiextensions.k8s.io", "apiregistration.k8s.io",
    "certificates.k8s.io", "coordination.k8s.io", "discovery.k8s.io", "events.k8s.io",
    "flowcontrol.apiserver.k8s.io", "networking.k8s.io", "node.k8s.io",
    "rbac.authorization.k8s.io", "scheduling.k8s.io", "storage.k8s.io",
    "kustomize.config.k8s.io",
}


def _group(api_version: str) -> str:
    return api_version.rsplit("/", 1)[0] if "/" in api_version else ""


def chart_defined_crs(docs) -> list[str]:
    """Documents whose kind a same-stage HelmRelease defines. A dry-run of the
    stage fails on them before any release has installed its CRDs."""
    found = []
    for path, doc in docs:
        group = _group(str(doc.get("apiVersion", "")))
        if group in BUILTIN_GROUPS or group in EARLIER_STAGE_GROUPS:
            continue
        found.append(f"{path}: {doc.get('kind')} ({group})")
    return found


def test_the_controllers_stage_declares_no_chart_defined_cr():
    """CRs belong to the configs stage, which runs after the releases here."""
    assert CONTROLLERS.is_dir(), "infrastructure/controllers/ moved; re-point this suite"
    found, unreadable = k8s_documents(CONTROLLERS)
    assert_all_parsed(unreadable, "chart-defined CR")
    docs = [(path.relative_to(REPO).as_posix(), doc) for path, doc in found]
    assert docs, "no manifest under controllers/ — this gate examined nothing"
    offenders = chart_defined_crs(docs)
    assert not offenders, "move these CRs to configs/:\n  " + "\n  ".join(offenders)


def test_a_chart_defined_cr_in_the_controllers_stage_is_reported():
    docs = [
        ("a.yaml", {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret"}),
        ("b.yaml", {"apiVersion": "apps/v1", "kind": "Deployment"}),
        ("c.yaml", {"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease"}),
        ("d.yaml", {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget"}),
        ("e.yaml", {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute"}),
        ("f.yaml", {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy"}),
        ("g.yaml", {"apiVersion": "v1", "kind": "ConfigMap"}),
    ]
    assert chart_defined_crs(docs) == [
        "a.yaml: ExternalSecret (external-secrets.io)",
        "e.yaml: HTTPRoute (gateway.networking.k8s.io)",
    ]
