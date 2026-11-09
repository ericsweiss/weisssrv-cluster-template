"""validate-helm-values.py's post-render policy, driven with stubbed helm.

These pin the arms the library suite does not: the release identity helm
renders with, a failed render, a bare-name allowlist entry and an unknown key.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from conftest import load_script

vhv = load_script("validate-helm-values.py")

VERSIONS = {"demo_version": "1.2.3", "k3s_version": "v1.36.2+k3s1"}

MANIFEST = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: demo
  namespace: demo-ns
spec:
  chart:
    spec:
      chart: demo
      version: "${demo_version}"
  values:
    replicaCount: 1
"""

REL = {"name": "demo", "manifest": "release.yaml",
       "repo_name": "demo-repo", "chart": "demo"}


def _workload(cpu_limit: str | None = None) -> str:
    container: dict = {"name": "app", "image": "demo:1"}
    if cpu_limit is not None:
        container["resources"] = {"limits": {"cpu": cpu_limit}}
    return yaml.safe_dump({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "demo", "namespace": "demo-ns"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    })


def _validate(monkeypatch, repo: Path, rendered: str, *, allowlist=None,
              manifest: str = MANIFEST, helm_rc: int = 0):
    """Run validate_release with `helm template` stubbed out. Returns (ok, cmd)."""
    (repo / "release.yaml").write_text(manifest)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, helm_rc, stdout=rendered, stderr="boom")

    monkeypatch.setattr(vhv.subprocess, "run", fake_run)
    ok = vhv.validate_release(REL, VERSIONS, str(repo), False, "1.36.2", allowlist)
    return ok, (calls[0] if calls else [])


def test_helm_is_given_the_helmrelease_name_namespace_and_version(monkeypatch, tmp_path):
    """Charts that key off .Release.Name/.Release.Namespace must render the
    same object Flux applies."""
    _, cmd = _validate(monkeypatch, tmp_path, _workload())
    assert cmd[:3] == ["helm", "template", "demo"]
    assert cmd[cmd.index("--namespace") + 1] == "demo-ns"
    assert cmd[cmd.index("--version") + 1] == "1.2.3"


def test_targetnamespace_wins_over_the_metadata_namespace(monkeypatch, tmp_path):
    manifest = MANIFEST.replace(
        "spec:\n  chart:", "spec:\n  targetNamespace: elsewhere\n  chart:")
    _, cmd = _validate(monkeypatch, tmp_path, _workload(), manifest=manifest)
    assert cmd[cmd.index("--namespace") + 1] == "elsewhere"


def test_a_failed_render_fails(monkeypatch, tmp_path):
    ok, _ = _validate(monkeypatch, tmp_path, "", helm_rc=1)
    assert not ok


def test_a_bare_name_allowlist_entry_does_not_waive(monkeypatch, tmp_path):
    """A bare name would waive the same workload in every namespace."""
    ok, _ = _validate(monkeypatch, tmp_path, _workload("500m"), allowlist={"demo"})
    assert not ok


def test_an_unknown_configmap_key_fails_before_helm_runs(monkeypatch, tmp_path):
    """Flux would substitute it; rendering with the literal validates an object
    the cluster never sees."""
    manifest = MANIFEST.replace("${demo_version}", "${not_a_key}")
    ok, cmd = _validate(monkeypatch, tmp_path, _workload(), manifest=manifest)
    assert not ok
    assert cmd == []
