"""Invariants that keep site values out of the Kubernetes manifests.

Every domain, VIP and CIDR this cluster uses lives in ONE place — the
cluster-config ConfigMap — and reaches the manifests through Flux postBuild
substitution. These tests fail the moment a manifest hard-codes one of those
values instead, or references a placeholder no ConfigMap defines.

They need only PyYAML, so they run in the `python-tests` CI job without
kustomize; `flux-lint` covers the same ground on the POST-kustomize output.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S_ROOT = REPO_ROOT / "kubernetes"
SOURCES_DIR = K8S_ROOT / "infrastructure" / "sources"
CLUSTERS_DIR = K8S_ROOT / "clusters"
ANSWERS_FILE = REPO_ROOT / ".copier-answers.yml"

# The postBuild sources every stage after `sources` substitutes from.
SUBSTITUTE_SOURCES = ("cluster-versions", "cluster-config")

# Flux substitutes ${var}; $${var} is the escape that reaches the cluster as a
# literal ${var} (shell snippets embedded in manifests use it).
PLACEHOLDER_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")

pytestmark = pytest.mark.skipif(
    not K8S_ROOT.is_dir(), reason="no kubernetes/ tree in this repository"
)


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _code_lines(path: Path):
    """(lineno, line) for every line that is not a whole-line comment.

    kustomize drops comments before Flux ever sees the manifest, so a
    `${placeholder}` written in documentation is not a substitution.
    """
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield lineno, line


def _load_configmaps() -> dict[str, tuple[Path, dict[str, str]]]:
    """Map ConfigMap name -> (file, data) for the postBuild sources."""
    found: dict[str, tuple[Path, dict[str, str]]] = {}
    if not SOURCES_DIR.is_dir():
        return found
    for path in _yaml_files(SOURCES_DIR):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
                continue
            name = (doc.get("metadata") or {}).get("name")
            data = doc.get("data") or {}
            if name and data:
                found[name] = (path, {k: str(v) for k, v in data.items()})
    return found


CONFIGMAPS = _load_configmaps()
SUBSTITUTION_KEYS = {k for _, data in CONFIGMAPS.values() for k in data}


def test_cluster_config_configmap_exists():
    assert "cluster-config" in CONFIGMAPS, (
        "kubernetes/infrastructure/sources/ must define a `cluster-config` ConfigMap — "
        "it is the single source of the domains, VIPs and CIDRs the manifests substitute."
    )


def test_cluster_config_carries_the_expected_keys():
    _, data = CONFIGMAPS["cluster-config"]
    required = {
        "cluster_name",
        "cluster_internal_domain",
        "cluster_external_domain",
        "cluster_lan_cidr",
        "cluster_k3s_api_vip",
        "cluster_metallb_public_vip",
        "cluster_metallb_internal_vip",
    }
    assert required <= set(data), f"cluster-config is missing keys: {sorted(required - set(data))}"


def test_substitution_keys_are_unique_across_configmaps():
    """Flux merges substituteFrom sources in list order — a duplicate resolves
    to whichever source is listed last, which is invisible in review."""
    seen: dict[str, Path] = {}
    clashes = []
    for path, data in CONFIGMAPS.values():
        for key in data:
            if key in seen:
                clashes.append(f"{key} in {seen[key]} and {path}")
            seen[key] = path
    assert not clashes, "duplicate substitution keys: " + "; ".join(clashes)


def test_every_placeholder_resolves():
    unresolved: list[str] = []
    for path in _yaml_files(K8S_ROOT):
        for lineno, line in _code_lines(path):
            for name in PLACEHOLDER_RE.findall(line):
                if name not in SUBSTITUTION_KEYS:
                    rel = path.relative_to(REPO_ROOT)
                    unresolved.append(f"{rel}:{lineno} ${{{name}}}")
    assert not unresolved, (
        "placeholders with no ConfigMap key (envsubst and Flux both render these "
        "as EMPTY, so they fail silently in the cluster):\n  " + "\n  ".join(unresolved)
    )


def test_flux_kustomizations_carry_both_substitute_sources():
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    expected = set(SUBSTITUTE_SOURCES) & set(CONFIGMAPS)
    missing: list[str] = []
    for path in _yaml_files(CLUSTERS_DIR):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            spec = doc.get("spec") or {}
            if not spec.get("path"):
                continue
            refs = {
                entry.get("name")
                for entry in (spec.get("postBuild") or {}).get("substituteFrom") or []
                if isinstance(entry, dict)
            }
            absent = expected - refs
            if absent:
                name = (doc.get("metadata") or {}).get("name", "?")
                missing.append(f"{path.relative_to(REPO_ROOT)} ({name}): {sorted(absent)}")
    assert not missing, (
        "Flux Kustomizations that do not read every postBuild source — their "
        "manifests' placeholders would render empty:\n  " + "\n  ".join(missing)
    )


def test_no_site_addresses_are_hard_coded_in_manifests():
    """Domains, IPs and CIDRs belong in cluster-config, referenced as ${...}.

    Scoped to the values that came from the copier ANSWERS: a well-known
    constant that happens to live in the ConfigMap (the CGNAT range, the pod and
    service CIDRs) is legitimately spelled out in a NetworkPolicy.
    """
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap")
    if not ANSWERS_FILE.is_file():
        pytest.skip("no .copier-answers.yml to source the site values from")
    config_file, data = CONFIGMAPS["cluster-config"]
    answers = yaml.safe_load(ANSWERS_FILE.read_text()) or {}
    site_values = {
        str(value)
        for key, value in answers.items()
        if not key.startswith("_")
        and isinstance(value, str)
        and (IPV4_RE.match(value) or DOMAIN_RE.match(value))
    }
    literals = {
        value: key for key, value in data.items() if value in site_values
    }
    offenders: list[str] = []
    for path in _yaml_files(K8S_ROOT):
        if path == config_file:
            continue
        for lineno, line in _code_lines(path):
            for value, key in literals.items():
                if value in line:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno} {value!r} — use ${{{key}}}")
    assert not offenders, "site values hard-coded in manifests:\n  " + "\n  ".join(offenders)
