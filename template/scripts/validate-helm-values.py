#!/usr/bin/env python3
"""Schema-validate the value-heavy Flux HelmReleases via `helm template`.

`kustomize build | kubeconform` (task flux:lint) emits a HelmRelease verbatim as
a Flux CR and never renders its chart, so the chart-specific keys inside
`.spec.values` are NOT validated — the HelmRelease CRD treats `.spec.values` as a
free-form object. A typo in a values key (e.g. `prometheuss:` for `prometheus:`)
therefore slips past lint and silently no-ops in-cluster.

This closes that gap for the releases named in --releases by extracting each
release's `.spec.values`, substituting the `${...}` postBuild placeholders from
the cluster-versions ConfigMap, and running `helm template` against the pinned
chart version. That:
  - hard-fails on a typo'd key for charts that ship a values.schema.json
    (traefik does; helm validates values against it),
  - hard-fails on any values that produce an unrenderable template (all charts),
  - and (optionally) pipes the rendered output to kubeconform for structural
    validation of the produced resources.

It requires network access (it does `helm repo add`/`update` against the public
chart repos). Which releases to render is consumer data, read from a YAML/JSON
list (`--releases`, default `helm-values-releases.yaml` under the repo root):

    - name: traefik
      manifest: kubernetes/infrastructure/controllers/traefik/release.yaml
      chart: traefik
      repo_name: traefik
      repo_url: https://traefik.github.io/charts

The chart version comes from the manifest's `.spec.chart.spec.version` (a literal
or a "${configmap_key}" placeholder), so there is no version to keep in sync here.

Usage:
  validate-helm-values.py [--kubeconform] [--repo-root DIR] [--releases FILE]
                          [--versions-configmap PATH] [--policy-config PATH]

Exit code is non-zero if any release fails to template (or fails kubeconform
when --kubeconform is given).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

# Single source of truth for the no-CPU-limits policy: load the allowlist AND
# the violation scanner from check-hpa-vpa-invariant.py so the kustomize-side
# check (run by `task flux:lint`) and this helm-rendered-side check can never
# diverge. The sibling has a hyphenated filename (not importable normally) and a
# `__main__` guard, so loading it here does not run its main().
_HPA_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "check-hpa-vpa-invariant.py")
_hpa_spec = importlib.util.spec_from_file_location("check_hpa_vpa_invariant", _HPA_SRC)
_hpa = importlib.util.module_from_spec(_hpa_spec)
_hpa_spec.loader.exec_module(_hpa)

# Charts gate on .Capabilities.APIVersions, commonly in the kind-qualified form
# (".../v1/ServiceMonitor"), so declare both the group/version and the
# kind-qualified CRDs the cluster has — otherwise offline `helm template` of a
# serviceMonitor-enabled release fails.
# Kubernetes version for capability-gated rendering, shared by `helm template`
# (--kube-version) and kubeconform (-kubernetes-version) so version-gated chart
# templates render the way both tools see them. Derived at runtime from
# k3s_version in the cluster-versions ConfigMap (see derive_kube_version) so it
# tracks the live cluster; this fallback only applies if that key is missing.
KUBE_VERSION_FALLBACK = "1.36.0"

HELM_API_VERSIONS = [
    "monitoring.coreos.com/v1",
    "monitoring.coreos.com/v1/ServiceMonitor",
    "monitoring.coreos.com/v1/PodMonitor",
    "monitoring.coreos.com/v1/PrometheusRule",
]

DEFAULT_VERSIONS_CONFIGMAP = "kubernetes/infrastructure/sources/versions-configmap.yaml"
DEFAULT_RELEASES_FILE = "helm-values-releases.yaml"
REQUIRED_RELEASE_KEYS = ("name", "manifest", "chart", "repo_name", "repo_url")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_versions(repo_root: str, configmap: str = DEFAULT_VERSIONS_CONFIGMAP) -> dict:
    """Return the cluster-versions ConfigMap data map."""
    path = os.path.join(repo_root, configmap)
    with open(path) as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise SystemExit(
            f"ERROR: {configmap} must be a mapping, got {type(doc).__name__}"
        )
    data = doc.get("data", {}) or {}
    if not data:
        raise SystemExit(f"ERROR: no data keys in {configmap}")
    return {k: str(v) for k, v in data.items()}


def load_releases(path: str) -> list[dict]:
    """Return the consumer's release list, validating the required keys."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    if isinstance(doc, dict):
        doc = doc.get("releases")
    if not isinstance(doc, list) or not doc:
        raise SystemExit(f"ERROR: {path} must hold a non-empty list of releases")
    for rel in doc:
        if not isinstance(rel, dict):
            raise SystemExit(f"ERROR: {path}: release entry is not a mapping: {rel!r}")
        missing = [k for k in REQUIRED_RELEASE_KEYS if not rel.get(k)]
        if missing:
            raise SystemExit(f"ERROR: {path}: release {rel!r} is missing {missing}")
    return doc


def derive_kube_version(versions: dict) -> str:
    """Cluster Kubernetes version (X.Y.Z) for helm/kubeconform, from k3s_version.

    e.g. "v1.36.2+k3s1" -> "1.36.2". Falls back to KUBE_VERSION_FALLBACK when the
    key is absent or unparseable so a malformed pin can't break flux:lint.
    """
    m = re.match(r"v?(\d+\.\d+\.\d+)", str(versions.get("k3s_version", "")))
    return m.group(1) if m else KUBE_VERSION_FALLBACK


def substitute(text: str, versions: dict) -> tuple[str, list[str]]:
    """Replace ${var} placeholders from versions; return (text, missing keys)."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in versions:
            missing.append(key)
            return m.group(0)
        return versions[key]

    return PLACEHOLDER_RE.sub(repl, text), sorted(set(missing))


def extract_helmrelease_from_text(text: str, source: str) -> dict:
    """Return the (first) HelmRelease document from manifest text."""
    docs = [d for d in yaml.safe_load_all(text)
            if isinstance(d, dict) and d.get("kind") == "HelmRelease"]
    if not docs:
        raise SystemExit(f"ERROR: no HelmRelease found in {source}")
    return docs[0]


def extract_helmrelease(manifest_path: str) -> dict:
    """Return the (first) HelmRelease document from a manifest file."""
    with open(manifest_path) as f:
        return extract_helmrelease_from_text(f.read(), manifest_path)


def validate_release(rel: dict, versions: dict, repo_root: str, run_kubeconform: bool,
                     kube_version: str, cpu_limit_allowlist: set | None = None) -> bool:
    """Template one release; return True on success."""
    manifest = os.path.join(repo_root, rel["manifest"])
    # Substitute ${placeholders} in the RAW manifest text first — exactly like
    # Flux's postBuild.substituteFrom — so a quoted placeholder keeps its YAML
    # type after substitution (e.g. "${redis_version}" stays a string, not a
    # number parsed from the bare value), matching the object Flux applies.
    with open(manifest) as f:
        rendered_manifest, missing = substitute(f.read(), versions)
    if missing:
        print(f"ERROR [{rel['name']}]: manifest references unknown configmap key(s): {missing}")
        return False
    hr = extract_helmrelease_from_text(rendered_manifest, manifest)
    spec = hr.get("spec", {})

    version = str(spec.get("chart", {}).get("spec", {}).get("version", ""))
    if not version:
        print(f"ERROR [{rel['name']}]: could not determine chart version")
        return False

    # Values are already resolved (with their original YAML types preserved)
    # from the text substitution above; just dump .spec.values for helm.
    values = spec.get("values", {})
    values_yaml = yaml.safe_dump(values, default_flow_style=False, sort_keys=False)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as vf:
        vf.write(values_yaml)
        values_file = vf.name

    try:
        # Render with the HelmRelease's actual release identity so charts that
        # key off .Release.Name/.Release.Namespace validate the same output Flux
        # produces (falls back to the registry name / "default").
        meta = hr.get("metadata", {})
        release_name = spec.get("releaseName") or meta.get("name") or rel["name"]
        namespace = spec.get("targetNamespace") or meta.get("namespace") or "default"
        cmd = [
            "helm", "template", release_name,
            f"{rel['repo_name']}/{rel['chart']}",
            "--version", version,
            "--namespace", namespace,
            "--kube-version", kube_version,
            "-f", values_file,
            "--skip-tests",
        ]
        for api in HELM_API_VERSIONS:
            cmd += ["--api-versions", api]
        print(f"=== helm template {rel['name']} ({rel['chart']}@{version}) ===")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"ERROR [{rel['name']}]: helm template failed:")
            # helm can emit useful render diagnostics on stdout too, not just stderr.
            if proc.stdout.strip():
                print("stdout:", proc.stdout.strip())
            print("stderr:", proc.stderr.strip())
            return False

        # No-CPU-limits policy on the CHART-RENDERED pods: check-hpa-vpa-
        # invariant.py scans only the HelmRelease `.spec.values`, so a chart
        # default would slip past it. Same scanner and allowlist, so the two
        # checks cannot disagree.
        rendered_docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
        cpu_viol = _hpa.cpu_limit_violations(rendered_docs, cpu_limit_allowlist)
        if cpu_viol:
            print(
                f"ERROR [{rel['name']}]: chart-rendered pods set a CPU limit "
                "(a compressible resource; CFS throttling distorts latency and "
                "CPU-based HPAs). To intentionally permit one, add its "
                "'namespace/Kind/name' key to cpu_limit_allowlist in the "
                "--policy-config. Offenders:"
            )
            print("\n".join(cpu_viol))
            return False

        if run_kubeconform:
            kc = subprocess.run(
                [
                    "kubeconform", "-strict", "-ignore-missing-schemas",
                    "-kubernetes-version", kube_version,
                    "-schema-location", "default",
                    "-schema-location",
                    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
                    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                    "-summary",
                ],
                input=proc.stdout, capture_output=True, text=True,
            )
            print(kc.stdout.strip())
            if kc.returncode != 0:
                print(f"ERROR [{rel['name']}]: kubeconform failed:")
                print(kc.stderr.strip())
                return False
        return True
    finally:
        os.unlink(values_file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kubeconform", action="store_true",
        help="also pipe rendered output through kubeconform",
    )
    parser.add_argument(
        "--repo-root", default=".",
        help="repo root (default: cwd)",
    )
    parser.add_argument(
        "--releases", default=None,
        help=f"release list (default: <repo-root>/{DEFAULT_RELEASES_FILE})",
    )
    parser.add_argument(
        "--versions-configmap", default=DEFAULT_VERSIONS_CONFIGMAP,
        help="cluster-versions ConfigMap, relative to --repo-root",
    )
    parser.add_argument(
        "--policy-config", default=None,
        help="autoscaling policy file supplying the shared cpu_limit_allowlist",
    )
    args = parser.parse_args()

    if shutil.which("helm") is None:
        print("ERROR: helm not found on PATH")
        return 1
    if args.kubeconform and shutil.which("kubeconform") is None:
        print("ERROR: kubeconform not found on PATH (--kubeconform given)")
        return 1

    releases = load_releases(
        args.releases or os.path.join(args.repo_root, DEFAULT_RELEASES_FILE)
    )
    policy = _hpa.load_policy(args.policy_config) if args.policy_config else _hpa.Policy()
    versions = load_versions(args.repo_root, args.versions_configmap)
    kube_version = derive_kube_version(versions)

    # Add/refresh the chart repos once (network).
    for rel in releases:
        add = subprocess.run(
            # --force-update keeps repeated local runs idempotent (a plain
            # `repo add` errors when the repo already exists) and refreshes a
            # changed URL.
            ["helm", "repo", "add", rel["repo_name"], rel["repo_url"], "--force-update"],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            print(f"ERROR: failed to add/update Helm repo {rel['repo_name']}:")
            print(add.stderr.strip())
            return 1
    upd = subprocess.run(["helm", "repo", "update"], capture_output=True, text=True)
    if upd.returncode != 0:
        print("ERROR: helm repo update failed:")
        print(upd.stderr.strip())
        return 1

    failed = 0
    for rel in releases:
        if not validate_release(rel, versions, args.repo_root, args.kubeconform, kube_version,
                                policy.cpu_limit_allowlist):
            failed += 1
    if failed:
        print(f"\n{failed} release(s) failed helm-values validation")
        return 1
    print(f"\nAll {len(releases)} HelmRelease(s) validated via helm template")
    return 0


if __name__ == "__main__":
    sys.exit(main())
