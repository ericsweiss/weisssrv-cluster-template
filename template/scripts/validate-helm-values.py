#!/usr/bin/env python3
"""Schema-validate the value-heavy Flux HelmReleases via `helm template`.

Renders each release's pinned chart with its substituted values and applies the
shared pod policies to the result. Contract: docs/SCRIPTS.md.
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
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# Load the allowlist and the violation scanner from check-hpa-vpa-invariant.py
# so the kustomize-side and helm-rendered-side checks cannot diverge. The
# hyphenated filename is not importable normally, hence the spec loader.
_HPA_SRC = Path(__file__).resolve().parent / "check-hpa-vpa-invariant.py"
if not _HPA_SRC.is_file():
    print(
        "ERROR: check-hpa-vpa-invariant.py must sit next to this script — "
        "vendor both (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2)
_hpa_spec = importlib.util.spec_from_file_location("check_hpa_vpa_invariant", str(_HPA_SRC))
_hpa = importlib.util.module_from_spec(_hpa_spec)
_hpa_spec.loader.exec_module(_hpa)

# Kubernetes version for capability-gated rendering, shared by `helm template`
# (--kube-version) and kubeconform. Derived from k3s_version by
# derive_kube_version(); this fallback applies only when that key is missing.
KUBE_VERSION_FALLBACK = "1.36.0"

# Charts gate on .Capabilities.APIVersions, commonly in the kind-qualified form
# (".../v1/ServiceMonitor"), so declare both forms - otherwise an offline
# `helm template` of a serviceMonitor-enabled release fails.
HELM_API_VERSIONS = [
    "monitoring.coreos.com/v1",
    "monitoring.coreos.com/v1/ServiceMonitor",
    "monitoring.coreos.com/v1/PodMonitor",
    "monitoring.coreos.com/v1/PrometheusRule",
]

DEFAULT_VERSIONS_CONFIGMAP = "kubernetes/infrastructure/sources/versions-configmap.yaml"
DEFAULT_RELEASES_FILE = "helm-values-releases.yaml"
DEFAULT_SOURCES_DIR = "kubernetes/infrastructure/sources"
# repo_name/repo_url are optional per-release OVERRIDES: the chart repo is
# normally resolved from the manifest's own sourceRef, the way the version is.
REQUIRED_RELEASE_KEYS = ("name", "manifest", "chart")
# Branch of datreeio/CRDs-catalog kubeconform reads schemas from.
DEFAULT_CRD_CATALOG_REF = os.environ.get("CRD_CATALOG_REF", "main")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Flux also accepts ${var:=default}, ${var:position} and ${var/a/b}. This gate
# resolves none of them, so it refuses rather than render what Flux would not.
UNSUPPORTED_PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*[:/][^}]*\}")
# Network-bound steps: a stalled chart repo must fail with a message, not hang
# the job until the CI timeout.
REPO_TIMEOUT_SECONDS = 120
RENDER_TIMEOUT_SECONDS = 300


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


def helm_repositories(sources_dir: str, unreadable: list | None = None) -> dict:
    """metadata.name -> spec.url for every HelmRepository under `sources_dir`.

    A file that will not parse is appended to `unreadable` rather than skipped:
    Flux would reject it, and a swallowed one misattributes the later failure.
    """
    repos: dict = {}
    if not os.path.isdir(sources_dir):
        return repos
    for root, _dirs, files in os.walk(sources_dir):
        for name in sorted(files):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path) as f:
                    docs = list(yaml.safe_load_all(f))
            except (OSError, yaml.YAMLError) as exc:
                if unreadable is not None:
                    unreadable.append((path, exc))
                continue
            for doc in docs:
                if not isinstance(doc, dict) or doc.get("kind") != "HelmRepository":
                    continue
                key = (doc.get("metadata") or {}).get("name")
                url = (doc.get("spec") or {}).get("url")
                if key and url:
                    # Trailing slash normalised, so a URL compares equal here
                    # and in check-helm-repo-parity.py.
                    repos[str(key)] = str(url).rstrip("/")
    return repos


def resolve_chart_repo(rel: dict, manifest_text: str, repos: dict):
    """(repo_name, repo_url) for one release, or a str naming why it failed.

    A per-release repo_name/repo_url pair wins; otherwise the manifest's
    `.spec.chart.spec.sourceRef.name` is matched against the sources dir.
    """
    if rel.get("repo_name") and rel.get("repo_url"):
        return rel["repo_name"], rel["repo_url"]
    try:
        hr = extract_helmrelease_from_text(manifest_text, rel["manifest"])
    except SystemExit as exc:
        return str(exc)
    source = (
        ((hr.get("spec") or {}).get("chart") or {}).get("spec") or {}
    ).get("sourceRef") or {}
    name = source.get("name")
    if not name:
        return "no .spec.chart.spec.sourceRef.name, and no repo_name/repo_url override"
    if name not in repos:
        return f"sourceRef {name!r} names no HelmRepository in the sources dir"
    return str(name), repos[str(name)]


def derive_kube_version(versions: dict) -> str:
    """Cluster Kubernetes version (X.Y.Z) for helm/kubeconform, from k3s_version.

    e.g. "v1.36.2+k3s1" -> "1.36.2". Falls back to KUBE_VERSION_FALLBACK when the
    key is absent or unparseable so a malformed pin can't break flux:lint.
    """
    m = re.match(r"v?(\d+\.\d+\.\d+)", str(versions.get("k3s_version", "")))
    return m.group(1) if m else KUBE_VERSION_FALLBACK


def unsupported_substitution(text: str) -> str | None:
    """The first postBuild substitution form this gate cannot evaluate, or None."""
    match = UNSUPPORTED_PLACEHOLDER_RE.search(text)
    return match.group(0) if match else None


def _run_tool(cmd: list[str], timeout: int, label: str, **kwargs):
    """Run a subprocess under a timeout. None (with a message) when it times out."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        print(f"ERROR [{label}]: {' '.join(cmd[:2])} timed out after {timeout}s")
        return None


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
    """Return the (first) HelmRelease document from manifest text.

    The text is already substituted, so PyYAML's mark names no file: the
    source path goes into the message or the failure identifies nothing.
    """
    try:
        docs = [d for d in yaml.safe_load_all(text)
                if isinstance(d, dict) and d.get("kind") == "HelmRelease"]
    except yaml.YAMLError as exc:
        raise SystemExit(f"ERROR: {source} is not parseable YAML: {exc}") from exc
    if not docs:
        raise SystemExit(f"ERROR: no HelmRelease found in {source}")
    return docs[0]


def release_vpas(manifest_path: str, namespace: str, versions: dict) -> list[dict]:
    """VerticalPodAutoscalers declared beside a release manifest.

    The kustomize-side gate skips a VPA whose target is chart-rendered; here the
    rendered pod spec is in hand, so the memory-cap rule can run on it.
    """
    out: list[dict] = []
    directory = os.path.dirname(os.path.abspath(manifest_path))
    if not os.path.isdir(directory):
        return out
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(directory, entry)) as f:
                text, _missing = substitute(f.read(), versions)
            docs = list(yaml.safe_load_all(text))
        except (OSError, yaml.YAMLError):
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "VerticalPodAutoscaler":
                continue
            _stamp_namespace(doc, namespace)
            out.append(doc)
    return out


def _stamp_namespace(doc: dict, namespace: str) -> None:
    """Apply the namespace the object is installed into, as the apiserver does."""
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        doc["metadata"] = meta
    meta.setdefault("namespace", namespace)


def validate_release(rel: dict, versions: dict, repo_root: str, run_kubeconform: bool,
                     kube_version: str, cpu_limit_allowlist: set | None = None,
                     vpa_cap_allowlist: set | None = None,
                     crd_catalog_ref: str = DEFAULT_CRD_CATALOG_REF) -> bool:
    """Template one release; return True on success."""
    manifest = os.path.join(repo_root, rel["manifest"])
    # Substitute ${placeholders} in the raw manifest text first, as Flux's
    # postBuild.substituteFrom does, so a quoted placeholder keeps its YAML
    # type after substitution.
    with open(manifest) as f:
        manifest_text = f.read()
    unsupported = unsupported_substitution(manifest_text)
    if unsupported:
        print(
            f"ERROR [{rel['name']}]: manifest uses a postBuild substitution form this "
            f"gate cannot evaluate ({unsupported}) — only ${{VAR}} is handled, so the "
            f"rendered object would differ from what Flux installs"
        )
        return False
    rendered_manifest, missing = substitute(manifest_text, versions)
    if missing:
        print(f"ERROR [{rel['name']}]: manifest references unknown configmap key(s): {missing}")
        return False
    hr = extract_helmrelease_from_text(rendered_manifest, manifest)
    spec = hr.get("spec", {})

    if spec.get("valuesFrom"):
        print(
            f"ERROR [{rel['name']}]: .spec.valuesFrom is not rendered by this gate — "
            f"the templated values would differ from what Flux installs"
        )
        return False

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
        proc = _run_tool(cmd, RENDER_TIMEOUT_SECONDS, rel["name"])
        if proc is None:
            return False
        if proc.returncode != 0:
            print(f"ERROR [{rel['name']}]: helm template failed:")
            # helm can emit useful render diagnostics on stdout too, not just stderr.
            if proc.stdout.strip():
                print("stdout:", proc.stdout.strip())
            print("stderr:", proc.stderr.strip())
            return False

        # No-CPU-limits policy on the chart-rendered pods. The sibling gate
        # scans only the HelmRelease `.spec.values`, so a chart default would
        # slip past it. Same scanner and allowlist.
        rendered_docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
        for doc in rendered_docs:
            _stamp_namespace(doc, namespace)
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

        # VPA memory caps against the CHART-RENDERED limits: the kustomize-side
        # gate has no limit to compare against for a chart-rendered workload.
        cap_viol = _hpa.vpa_cap_violations(
            rendered_docs + release_vpas(manifest, namespace, versions), vpa_cap_allowlist
        )
        if cap_viol:
            print(
                f"ERROR [{rel['name']}]: a VPA caps memory at or above the "
                "chart-rendered container limit. To carry one while it is re-derived, "
                "add its 'namespace/VerticalPodAutoscaler/name' key to "
                "vpa_cap_allowlist in the --policy-config. Offenders:"
            )
            print("\n".join(cap_viol))
            return False

        if run_kubeconform:
            kc = _run_tool(
                [
                    "kubeconform", "-strict", "-ignore-missing-schemas",
                    "-kubernetes-version", kube_version,
                    "-schema-location", "default",
                    "-schema-location",
                    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/"
                    f"{crd_catalog_ref}/"
                    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                    "-summary",
                ],
                RENDER_TIMEOUT_SECONDS, rel["name"], input=proc.stdout,
            )
            if kc is None:
                return False
            print(kc.stdout.strip())
            if kc.returncode != 0:
                print(f"ERROR [{rel['name']}]: kubeconform failed:")
                print(kc.stderr.strip())
                return False
        return True
    finally:
        os.unlink(values_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
    parser.add_argument(
        "--sources-dir", default=DEFAULT_SOURCES_DIR,
        help="HelmRepository dir the chart repo is resolved from, relative to --repo-root",
    )
    parser.add_argument(
        "--crd-catalog-ref", default=DEFAULT_CRD_CATALOG_REF,
        help="datreeio/CRDs-catalog ref kubeconform reads schemas from",
    )
    args = parser.parse_args(argv)

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

    unreadable: list = []
    repos = helm_repositories(
        os.path.join(args.repo_root, args.sources_dir), unreadable
    )
    if unreadable:
        for path, exc in unreadable:
            print(f"ERROR: {path} could not be read as YAML: {exc}")
        return 1
    for rel in releases:
        manifest = os.path.join(args.repo_root, rel["manifest"])
        try:
            with open(manifest) as f:
                text, _missing = substitute(f.read(), versions)
        except OSError as exc:
            print(f"ERROR [{rel['name']}]: cannot read {manifest}: {exc}")
            return 1
        resolved = resolve_chart_repo(rel, text, repos)
        if isinstance(resolved, str):
            print(
                f"ERROR [{rel['name']}]: cannot resolve the chart repo: {resolved}. "
                f"Add the HelmRepository under {args.sources_dir}, or set "
                f"repo_name/repo_url on the release entry."
            )
            return 1
        rel["repo_name"], rel["repo_url"] = resolved

    # Add/refresh the chart repos once (network).
    for rel in releases:
        add = _run_tool(
            # --force-update keeps repeated local runs idempotent (a plain
            # `repo add` errors when the repo already exists) and refreshes a
            # changed URL.
            ["helm", "repo", "add", rel["repo_name"], rel["repo_url"], "--force-update"],
            REPO_TIMEOUT_SECONDS, rel["name"],
        )
        if add is None:
            return 1
        if add.returncode != 0:
            print(f"ERROR: failed to add/update Helm repo {rel['repo_name']}:")
            print(add.stderr.strip())
            return 1
    upd = _run_tool(["helm", "repo", "update"], REPO_TIMEOUT_SECONDS, "helm repo update")
    if upd is None:
        return 1
    if upd.returncode != 0:
        print("ERROR: helm repo update failed:")
        print(upd.stderr.strip())
        return 1

    failed = 0
    for rel in releases:
        if not validate_release(rel, versions, args.repo_root, args.kubeconform, kube_version,
                                policy.cpu_limit_allowlist, policy.vpa_cap_allowlist,
                                args.crd_catalog_ref):
            failed += 1
    if failed:
        print(f"\n{failed} release(s) failed helm-values validation")
        return 1
    print(f"\nAll {len(releases)} HelmRelease(s) validated via helm template")
    return 0


if __name__ == "__main__":
    sys.exit(main())
