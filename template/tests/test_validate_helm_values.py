"""The post-render policy half of scripts/validate-helm-values.py.

`helm template` is monkeypatched out, so the CPU-limit arm that gates every app
added to this cluster is exercised here without the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import load_script

vhv = load_script("validate-helm-values.py")

HR_BODY = """apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: app, namespace: ns}
spec:
  chart: {spec: {version: 1.0.0}}
  values: {}
"""

RENDERED = """apiVersion: apps/v1
kind: Deployment
metadata: {name: app, namespace: ns}
spec:
  template:
    spec:
      containers:
        - name: c
          resources:
            limits: {%s}
"""


class _FakeSubprocess:
    """Stands in for the subprocess module validate_release renders through."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self._result = subprocess.CompletedProcess([], returncode, stdout, "render failed")

    def run(self, *_args, **_kwargs):
        return self._result


def _release(tmp_path: Path) -> dict:
    (tmp_path / "release.yaml").write_text(HR_BODY)
    return {
        "name": "app",
        "manifest": "release.yaml",
        "chart": "app",
        "repo_name": "app",
        "repo_url": "https://example.invalid/charts",
    }


def _render(monkeypatch, limits: str, returncode: int = 0) -> None:
    monkeypatch.setattr(vhv, "subprocess", _FakeSubprocess(RENDERED % limits, returncode))


def test_the_shared_cpu_limit_scanner_is_loaded():
    """The kustomize-side gate and this one must use one scanner and allowlist."""
    assert callable(vhv._hpa.cpu_limit_violations)
    assert isinstance(vhv._hpa.Policy().cpu_limit_allowlist, set)


def test_an_all_clean_render_passes(tmp_path, monkeypatch):
    _render(monkeypatch, "memory: 128Mi")
    assert vhv.validate_release(_release(tmp_path), {}, str(tmp_path), False, "1.30.0") is True


def test_a_chart_rendered_cpu_limit_fails(tmp_path, monkeypatch, capsys):
    """The policy's blind spot: the limit is a chart default, so it never appears
    in the HelmRelease values the kustomize-side gate scans."""
    _render(monkeypatch, "cpu: 500m")
    assert vhv.validate_release(_release(tmp_path), {}, str(tmp_path), False, "1.30.0") is False
    assert "chart-rendered pods set a CPU limit" in capsys.readouterr().out


def test_the_allowlist_suppresses_a_deliberate_limit(tmp_path, monkeypatch):
    _render(monkeypatch, "cpu: 500m")
    assert vhv.validate_release(
        _release(tmp_path), {}, str(tmp_path), False, "1.30.0",
        cpu_limit_allowlist={"ns/Deployment/app"},
    ) is True


def test_a_failed_render_fails_the_release(tmp_path, monkeypatch, capsys):
    _render(monkeypatch, "memory: 128Mi", returncode=1)
    assert vhv.validate_release(_release(tmp_path), {}, str(tmp_path), False, "1.30.0") is False
    assert "helm template failed" in capsys.readouterr().out


def test_an_unknown_configmap_placeholder_is_refused(tmp_path, monkeypatch, capsys):
    """Flux would substitute it; rendering with the literal validates an object
    the cluster never sees."""
    _render(monkeypatch, "memory: 128Mi")
    rel = _release(tmp_path)
    (tmp_path / "release.yaml").write_text(HR_BODY.replace("1.0.0", "${app_version}"))
    assert vhv.validate_release(rel, {}, str(tmp_path), False, "1.30.0") is False
    assert "unknown configmap key" in capsys.readouterr().out
